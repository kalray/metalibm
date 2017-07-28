# -*- coding: utf-8 -*-

import sys

import sollya

from sollya import (
        S2, Interval, ceil, floor, round, inf, sup, pi, log, exp, cos, sin,
        guessdegree, dirtyinfnorm
)

from metalibm_core.core.ml_function import ML_Function, ML_FunctionBasis, DefaultArgTemplate

from metalibm_core.core.attributes import ML_Debug
from metalibm_core.core.ml_operations import *
from metalibm_core.core.ml_formats import *
from metalibm_core.core.precisions import ML_Faithful, ML_CorrectlyRounded
from metalibm_core.code_generation.generic_processor import GenericProcessor
from metalibm_core.core.polynomials import *
from metalibm_core.core.ml_table import ML_NewTable
from metalibm_core.core.ml_complex_formats import ML_Mpfr_t
from metalibm_core.code_generation.generator_utility import FunctionOperator, FO_Result, FO_Arg
from metalibm_core.core.payne_hanek import generate_payne_hanek

from metalibm_core.utility.ml_template import ML_NewArgTemplate, ArgDefault
from metalibm_core.utility.log_report  import Log
from metalibm_core.utility.debug_utils import *
from metalibm_core.utility.num_utils   import ulp
from metalibm_core.utility.gappa_utils import is_gappa_installed

# disabling sollya's rounding warning
sollya.roundingwarnings = sollya.off
sollya.verbosity = 0
sollya.showmessagenumbers = sollya.on

## Implementation of sine or cosine sharing a common
#  approximation scheme
class ML_SinCos(ML_Function("ml_cos")):
  """ Implementation of cosinus function """
  def __init__(self,
             arg_template = DefaultArgTemplate,
               precision = ML_Binary32,
               accuracy  = ML_CorrectlyRounded,
               libm_compliant = True,
               debug_flag = False,
               fuse_fma = True,
               fast_path_extract = True,
               target = GenericProcessor(),
               output_file = "ml_sincos.c",
               function_name = "ml_sincos",
               sin_output = True):
    # initializing I/O precision
    precision = ArgDefault.select_value([arg_template.precision, precision])
    io_precisions = [precision] * 2

    # initializing base class
    ML_FunctionBasis.__init__(self,
      base_name = "sincos",
      function_name = function_name,
      output_file = output_file,

      io_precisions = io_precisions,
      abs_accuracy = None,
      libm_compliant = libm_compliant,

      processor = target,
      fuse_fma = fuse_fma,
      fast_path_extract = fast_path_extract,

      debug_flag = debug_flag,
      arg_template = arg_template
    )
    self.precision = precision
    self.sin_output = sin_output



  def generate_emulate(self, result_ternary, result, mpfr_x, mpfr_rnd):
    """ generate the emulation code for ML_SinCos functions
        mpfr_x is a mpfr_t variable which should have the right precision
        mpfr_rnd is the rounding mode
    """
    emulate_func_name = "mpfr_cos" if self.sin_output else "mpfr_sin"
    emulate_func_op = FunctionOperator(emulate_func_name, arg_map = {0: FO_Arg(0), 1: FO_Arg(1), 2: FO_Arg(2)}, require_header = ["mpfr.h"])
    emulate_func   = FunctionObject(emulate_func_name, [ML_Mpfr_t, ML_Mpfr_t, ML_Int32], ML_Int32, emulate_func_op)
    mpfr_call = Statement(ReferenceAssign(result_ternary, emulate_func(result, mpfr_x, mpfr_rnd)))

    return mpfr_call


  def generate_scheme(self):
    # declaring CodeFunction and retrieving input variable
    vx = self.implementation.add_input_variable("x", self.precision)

    Log.report(Log.Info, "generating implementation scheme")
    if self.debug_flag:
        Log.report(Log.Info, "debug has been enabled")

    # local overloading of RaiseReturn operation
    def SincosRaiseReturn(*args, **kwords):
        kwords["arg_value"] = vx
        kwords["function_name"] = self.function_name
        return RaiseReturn(*args, **kwords)

    sollya_precision = self.precision.get_sollya_object()
    hi_precision = self.precision.get_field_size() - 8
    cw_hi_precision = self.precision.get_field_size() - 4

    ext_precision = {
      ML_Binary32: ML_Binary64,
      ML_Binary64: ML_Binary64
    }[self.precision]
    
    int_precision = {
      ML_Binary32 : ML_Int32,
      ML_Binary64 : ML_Int64
    }[self.precision]
    
    if self.precision is ML_Binary32:
      ph_bound = S2**10
    else:
      ph_bound = S2**33
    
    test_ph_bound = Comparison(vx, ph_bound, specifier = Comparison.GreaterOrEqual, precision = ML_Bool)
    
    # argument reduction
    # m
    frac_pi_index = {ML_Binary32: 12, ML_Binary64: 14}[self.precision]

    # 2^m / pi
    frac_pi     = round(S2**frac_pi_index / pi, hi_precision, sollya.RN)
    frac_pi_lo = round(S2**frac_pi_index / pi - frac_pi, sollya_precision, sollya.RN)
    # pi / 2^m, high part
    inv_frac_pi = round(pi / S2**frac_pi_index, cw_hi_precision, sollya.RN)
    # pi / 2^m, low part
    inv_frac_pi_lo = round(pi / S2**frac_pi_index - inv_frac_pi, sollya_precision, sollya.RN)

    # computing k
    vx.set_attributes(tag = "vx", debug = debug_multi);

    vx_pi = Addition(
      Multiplication(
        vx,
        Constant(frac_pi, precision = self.precision),
        precision = self.precision),
      Multiplication(
        vx,
        Constant(frac_pi_lo, precision = self.precision),
        precision = self.precision),
      precision = self.precision,
      tag = "vx_pi",
      debug = debug_multi)

    k = NearestInteger(vx_pi, precision = int_precision, tag = "k", debug = debug_multi)
    # k in floating-point precision
    fk = Conversion(k, precision = self.precision, tag = "fk", debug = debug_multi)

    inv_frac_pi_cst    = Constant(inv_frac_pi, tag = "inv_frac_pi", precision = self.precision, debug = debug_multi)
    inv_frac_pi_lo_cst = Constant(inv_frac_pi_lo, tag = "inv_frac_pi_lo", precision = self.precision, debug = debug_multi)

    # Cody-Waite reduction
    red_coeff1 = Multiplication(fk, inv_frac_pi_cst, precision = self.precision, exact = True)
    red_coeff2 = Multiplication(Negation(fk, precision = self.precision), inv_frac_pi_lo_cst, precision = self.precision, exact = True)
    
    # Should be exact / Sterbenz' Lemma
    pre_sub_mul = Subtraction(vx, red_coeff1, precision = self.precision, exact = True)
    
    # Fast2Sum 
    s = Addition(pre_sub_mul, red_coeff2, precision = self.precision, unbreakable = True, tag = "s", debug = debug_multi)
    z = Subtraction(s, pre_sub_mul, precision = self.precision, unbreakable = True, tag = "z", debug = debug_multi)
    t = Subtraction(red_coeff2, z, precision = self.precision, unbreakable = True, tag = "t", debug = debug_multi)
    
    red_vx_std = Addition(s, t, precision = self.precision)
    red_vx_std.set_attributes(tag = "red_vx_std", debug = debug_multi)
    
    # To compute sine we offset x by 3pi/2
    # which means add 3  * S2^(frac_pi_index-1) to k
    if self.sin_output:
      Log.report(Log.Info, "Computing Sin")
      offset_k = Addition(
        k,
        Constant(3 * S2**(frac_pi_index - 1), precision = int_precision),
        precision = int_precision,
        tag = "offset_k"
      )
    else:
      Log.report(Log.Info, "Computing Cos")
      offset_k = k

    modk       = Variable("modk", precision = int_precision, var_type = Variable.Local)
    red_vx     = Variable("red_vx", precision = self.precision, var_type = Variable.Local)

    # Faster modulo using bitwise logic
    modk_std = BitLogicAnd(offset_k, 2**(frac_pi_index+1)-1, precision = int_precision, tag = "modk", debug = debug_multi)

    approx_interval = Interval(-pi/(S2**(frac_pi_index+1)), pi / S2**(frac_pi_index+1))

    red_vx.set_interval(approx_interval)
    red_vx.set_attributes(debug = debug_multi)

    Log.report(Log.Info, "approx interval: %s\n" % approx_interval)

    Log.report(Log.Info, "building tabulated approximation for sin and cos")

    error_function = lambda p, f, ai, mod, t: dirtyinfnorm(f - p, ai)

    # polynomial_scheme_builder = PolynomialSchemeEvaluator.generate_estrin_scheme
    polynomial_scheme_builder = PolynomialSchemeEvaluator.generate_horner_scheme

    table_index_size = frac_pi_index+1
    cos_table = ML_NewTable(dimensions = [2**table_index_size, 1], storage_precision = self.precision, tag = self.uniquify_name("cos_table"))

    for i in xrange(2**(frac_pi_index+1)):
      local_x = i*pi/S2**frac_pi_index
      cos_local = round(cos(local_x), self.precision.get_sollya_object(), sollya.RN)
      cos_table[i][0] = cos_local


    sin_index = Modulo(modk + 2**(frac_pi_index-1), 2**(frac_pi_index+1), precision = int_precision, tag = "sin_index")#, debug = debug_multi)
    tabulated_cos = TableLoad(cos_table, modk, 0, precision = self.precision, tag = "tab_cos", debug = debug_multi)
    tabulated_sin = -TableLoad(cos_table, sin_index , 0, precision = self.precision, tag = "tab_sin", debug = debug_multi)

    poly_degree_cos   = sup(guessdegree(cos(sollya.x), approx_interval, S2**-self.precision.get_precision()) + 2) 
    poly_degree_sin   = sup(guessdegree(sin(sollya.x)/sollya.x, approx_interval, S2**-self.precision.get_precision()) + 2) 
    
    poly_degree_cos_list = range(0, poly_degree_cos + 1)
    poly_degree_sin_list = range(0, poly_degree_sin + 3)

    # cosine polynomial: limiting first and second coefficient precision to 1-bit
    poly_cos_prec_list = [self.precision] * len(poly_degree_cos_list)
    # sine polynomial: limiting first coefficient precision to 1-bit
    poly_sin_prec_list = [self.precision] * len(poly_degree_sin_list)

    error_function = lambda p, f, ai, mod, t: dirtyinfnorm(f - p, ai)
    Log.report(Log.Info, "building mathematical polynomials for sin and cos")
    # Polynomial approximations
    Log.report(Log.Info, "cos")
    poly_object_cos, poly_error_cos = Polynomial.build_from_approximation_with_error(cos(sollya.x), poly_degree_cos_list, poly_cos_prec_list, approx_interval, sollya.absolute, error_function = error_function)
    Log.report(Log.Info, "sin")
    poly_object_sin, poly_error_sin = Polynomial.build_from_approximation_with_error(sin(sollya.x), poly_degree_sin_list, poly_sin_prec_list, approx_interval, sollya.absolute, error_function = error_function)

    Log.report(Log.Info, "poly error cos: {} / {:d}".format(poly_error_cos, int(sollya.log2(poly_error_cos))))
    Log.report(Log.Info, "poly error sin: {0} / {1:d}".format(poly_error_sin, int(sollya.log2(poly_error_sin))))
    Log.report(Log.Info, "poly cos : %s" % poly_object_cos)
    Log.report(Log.Info, "poly sin : %s" % poly_object_sin)

    # Polynomial evaluation scheme
    poly_cos = polynomial_scheme_builder(poly_object_cos.sub_poly(start_index = 2), red_vx, unified_precision = self.precision)
    poly_sin = polynomial_scheme_builder(poly_object_sin.sub_poly(start_index = 2), red_vx, unified_precision = self.precision)
    poly_cos.set_attributes(tag = "poly_cos", debug = debug_multi)
    poly_sin.set_attributes(tag = "poly_sin", debug = debug_multi, unbreakable = True)
    
    # TwoProductFMA
    mul_cos_x = tabulated_cos* poly_cos
    mul_cos_y = FusedMultiplyAdd(tabulated_cos, poly_cos, -mul_cos_x, precision = self.precision) 
    
    mul_sin_x = tabulated_sin*poly_sin
    mul_sin_y = FusedMultiplyAdd(tabulated_sin, poly_sin, -mul_sin_x, precision = self.precision)

    mul_coeff_sin_hi = tabulated_sin*red_vx
    mul_coeff_sin_lo = FusedMultiplyAdd(tabulated_sin, red_vx, -mul_coeff_sin_hi)
    
    mul_cos = Addition(mul_cos_x, mul_cos_y, precision = self.precision, tag = "mul_cos", debug = debug_multi)
    mul_sin = Negation(Addition(mul_sin_x, mul_sin_y, precision = self.precision), precision = self.precision, tag = "mul_sin", debug = debug_multi)
    mul_coeff_sin = Negation(Addition(mul_coeff_sin_hi, mul_coeff_sin_lo, precision = self.precision), precision = self.precision, tag = "mul_coeff_sin", debug = debug_multi)


    mul_cos_x.set_attributes(tag = "mul_cos_x", precision = self.precision)#, debug = debug_multi)
    mul_cos_y.set_attributes(tag = "mul_cos_y", precision = self.precision)#, debug = debug_multi)
    mul_sin_x.set_attributes(tag = "mul_sin_x", precision = self.precision)#, debug = debug_multi)
    mul_sin_y.set_attributes(tag = "mul_sin_y", precision = self.precision)#, debug = debug_multi)
    
    cos_eval_d_1 = (((mul_cos + mul_sin) +  mul_coeff_sin) + tabulated_cos)

    cos_eval_d_1.set_attributes(tag = "cos_eval_d_1", precision = self.precision, debug = debug_multi)

    result_1 = Statement(
       Return(cos_eval_d_1)
    )
    
    #######################################################################
    #                    LARGE ARGUMENT MANAGEMENT                        #
    #                 (lar: Large Argument Reduction)                     #
    #######################################################################
    # payne and hanek argument reduction for large arguments
    ph_k = frac_pi_index
    ph_frac_pi     = round(S2**ph_k / pi, 1200, sollya.RN)
    ph_inv_frac_pi = pi / S2**ph_k 
    
    ph_statement, ph_acc, ph_acc_int = generate_payne_hanek(vx, ph_frac_pi, self.precision, n = 100, k = ph_k)

    # assigning Large Argument Reduction reduced variable
    lar_vx = Variable("lar_vx", precision = self.precision, var_type = Variable.Local)
    
    lar_red_vx = Addition(
      Multiplication(
        lar_vx,
        inv_frac_pi,
        precision = self.precision),
      Multiplication(
        lar_vx,
        inv_frac_pi_lo,
        precision = self.precision),
      precision = self.precision,
      tag = "lar_red_vx",
      debug = debug_multi)

    C32 = Constant(2**(ph_k+1), precision = int_precision, tag = "C32")
    ph_acc_int_red = Conversion(
      Select(ph_acc_int < Constant(0, precision = int_precision), C32 + ph_acc_int  , ph_acc_int, precision = int_precision, tag = "ph_acc_int_red"),
      precision = int_precision,
      tag = "ph_acc_int_red",
      debug = debug_multi
    )

    if self.sin_output:
      lar_offset_k = Addition(
        ph_acc_int_red,
        Constant(3 * S2**(frac_pi_index - 1), precision = int_precision),
        precision = int_precision,
        tag = "lar_offset_k"
      )
    else:
      lar_offset_k = ph_acc_int_red

    lar_modk = BitLogicAnd(lar_offset_k, 2**(frac_pi_index+1) - 1, precision = int_precision, tag = "lar_modk", debug = debug_multi )

    lar_statement = Statement(
        ph_statement,
        ReferenceAssign(lar_vx, ph_acc, debug = debug_multi),
        ReferenceAssign(red_vx, lar_red_vx, debug = debug_multi),
        ReferenceAssign(modk, lar_modk),
        prevent_optimization = True
      )
        
    test_NaN_or_Inf = Test(vx, specifier = Test.IsInfOrNaN, likely = False, tag = "NaN_or_Inf", debug = debug_multi)
    return_NaN_or_Inf = Statement(Return(FP_QNaN(self.precision)))
    scheme = ConditionBlock(test_NaN_or_Inf,
        return_NaN_or_Inf,
        Statement(
            modk,
            red_vx,
            ConditionBlock(
              test_ph_bound,
              lar_statement,
              Statement(
                ReferenceAssign(modk, modk_std),
                ReferenceAssign(red_vx, red_vx_std),
              )
            ),
            result_1
          )
        )
        

    return scheme

  def numeric_emulate(self, input_value):
    if self.sin_output:
      return sin(input_value)
    else:
      return cos(input_value)

  # standard_test_cases =[[sollya.parse(x)] for x in  ["0x1.5d6fb52272d9bp+34", "0x1.2f6c2d822e4bdp+34", "0x1.c3cb885323b3ep+11"]]
  standard_test_cases =[[sollya.parse(x)] for x in  ["0x1.313962p+32", "0x1.499f5cp+32", "0x1.927712p+40",
   "0x1.ba30bap+40", "0x1.7e886ap+40", "0x1.e57acp+10", "0x1.5615fcp+10",]]



if __name__ == "__main__":
  # auto-test
  arg_template = ML_NewArgTemplate(default_function_name = "new_sincos", default_output_file = "new_sincos.c" )
  # argument extraction
  arg_template.get_parser().add_argument("--sin", dest = "sin_output", default = False, const = True, action = "store_const", help = "select sine output (default is cosine)")
  args = arg_template.arg_extraction()
  ml_sincos = ML_SinCos(args, sin_output = args.sin_output)
  ml_sincos.gen_implementation()
