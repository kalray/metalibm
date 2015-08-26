# -*- coding: utf-8 -*-

# Dynamic implementation of Payne and Hanek argument reduction
# created:        Augest 24th, 2015
# last modified:  August 24th, 2015


from pythonsollya import *

from metalibm_core.core.ml_function import ML_Function, ML_FunctionBasis

from metalibm_core.core.attributes import ML_Debug
from metalibm_core.core.ml_operations import *
from metalibm_core.core.ml_formats import *
from metalibm_core.code_generation.generic_processor import GenericProcessor
from metalibm_core.core.polynomials import *
from metalibm_core.core.ml_table import ML_Table
from metalibm_core.core.ml_complex_formats import ML_Mpfr_t
from metalibm_core.code_generation.generator_utility import FunctionOperator, FO_Result, FO_Arg

from metalibm_core.utility.ml_template import ML_ArgTemplate
from metalibm_core.utility.log_report  import Log
from metalibm_core.utility.debug_utils import *
from metalibm_core.utility.num_utils   import ulp
from metalibm_core.utility.gappa_utils import is_gappa_installed

def get_remainder(vx, precision, k, debug = None, tag = ""):
  """ get in floating-point format <precision>
      the integer part of vx with the k least
      significant bits zeroed """
  int_precision = {
    ML_Binary64: ML_Int64,
    ML_Binary32: ML_Int32
  }[precision]
  result  = Conversion(
                BitLogicAnd(
                    NearestInteger(
                      vx, precision = int_precision), 
                    Constant(~(2**(k+1)-1), precision = int_precision),
                    tag = tag, 
                    debug = debug
                  ), 
                precision = precision
              )
  return result

def generate_payne_hanek(vx, frac_pi, precision, chunk_num, n = 100, k = 4, ):
  """ generate payne and hanek argument reduction for frac_pi * variable """
  cst_msb = ceil(log2(abs(frac_pi)))
  cst_exp_range = cst_msb - precision.get_emin_subnormal() + 1
  chunk_size = 24 # precision.get_field_size() / 2 - 2
  chunk_number = int(ceil((cst_exp_range + chunk_size - 1) / chunk_size)) 
  scaling_factor = S2**-(chunk_size/2)

  chunk_size_cst = Constant(chunk_size, precision = ML_Int32)
  cst_msb_node   = Constant(cst_msb, precision = ML_Int32)

  p = precision.get_field_size()

  debug_precision = {ML_Binary32: debug_ftox, ML_Binary64: debug_lftolx}[precision]

  print "cst_msb: ", cst_msb
  print "p:       ", p

  # saving sollya's global precision
  old_global_prec = get_prec()
  prec(cst_exp_range + 100)

  print "sollya precision: ", get_prec()

  cst_table = ML_Table(dimensions = [chunk_number, 1], storage_precision = precision, tag = "PH_cst_table")
  scale_table =  ML_Table(dimensions = [chunk_number, 1], storage_precision = precision, tag = "PH_scale_table")
  tmp_cst = frac_pi
  
  for i in xrange(chunk_number):
    local_cst = round(tmp_cst, chunk_size, RZ)
    local_scale = (scaling_factor**i)
    # storing scaled constant chunks
    cst_table[i][0] = local_cst / (local_scale**2)
    scale_table[i][0] = local_scale
    tmp_cst = tmp_cst - local_cst

  vx_exp = ExponentExtraction(vx)
  msb_exp = -vx_exp + p - 1 + k
  msb_exp.set_attributes(tag = "msb_exp", debug = debugd)

  msb_index = Select(cst_msb_node < msb_exp, 0, (cst_msb_node - msb_exp) / chunk_size_cst)
  msb_index.set_attributes(tag = "msb_index", debug = debugd)

  lsb_exp = -vx_exp + p - 1 -n
  lsb_exp.set_attributes(tag = "lsb_exp", debug = debugd)

  lsb_index = (cst_msb_node - lsb_exp) / chunk_size_cst
  lsb_index.set_attributes(tag = "lsb_index", debug = debugd)

  vx_hi = TypeCast(BitLogicAnd(TypeCast(vx, precision = ML_Int64), Constant(~(2**chunk_size-1), precision = ML_Int64)), precision = precision) 
  vx_hi.set_attributes(tag = "vx_hi", debug = debug_precision)

  vx_lo = vx - vx_hi
  vx_lo.set_attributes(tag = "vx_lo", debug = debug_precision)

  vi = Variable("i", precision = ML_Int32, var_type = Variable.Local)

  half_scaling = Constant(S2**(-chunk_size/2), precision = precision)

  cst_load_0 = TableLoad(cst_table, msb_index, 0, tag = "cst_load_0", debug = debug_precision)
  sca_load_0 = TableLoad(scale_table, msb_index, 0, tag = "sca_load_0", debug = debug_precision)
  acc_expr_0 = (vx_hi * sca_load_0) * (cst_load_0 * sca_load_0)
  acc_expr_0.set_attributes(tag = "acc_expr_0", debug = debug_precision)
  acc_mod_0 = get_remainder(acc_expr_0, precision, k, debuglld, tag = "acc_mod_0")

  i1 = Constant(1, precision = ML_Int32)
  pre_exclude_0 = ((cst_msb_node - (msb_index + i1) * chunk_size + i1) + (vx_exp + Constant(- chunk_size + 1, precision = ML_Int32)))
  pre_exclude_0.set_attributes(tag = "pre_exclude_0", debug = debugd)
  exclude_0 = pre_exclude_0 > k
  exclude_0.set_attributes(tag = "exclude_0", debug = debugd)
  acc_0 = Select(exclude_0, 0, acc_expr_0 - acc_mod_0)
  acc_0.set_attributes(tag = "acc_0", debug = debug_precision)

  acc_expr_1 = (vx_lo * sca_load_0) * (cst_load_0 * sca_load_0)
  acc_expr_1.set_attributes(tag = "acc_expr_1", debug = debug_precision)
  acc_mod_1 = get_remainder(acc_expr_1, precision, k, debuglld, tag = "acc_mod_1")

  acc_1 = acc_expr_1 - acc_mod_1
  acc_1.set_attributes(tag = "acc1", debug = debug_precision)

  acc = Variable("acc", precision = precision, var_type = Variable.Local)
  init_loop = Statement(
    ReferenceAssign(vi, msb_index+1),
    ReferenceAssign(acc, acc_0 + acc_1), #   ReferenceAssign(acc, Constant(0, precision = precision)),
  )
  
  cst_load = TableLoad(cst_table, vi, 0, tag = "cst_load", debug = debug_precision)
  sca_load = TableLoad(scale_table, vi, 0, tag = "sca_load", debug = debug_precision)
  acc_expr = acc + (vx * sca_load) * (cst_load * sca_load)
  acc_expr.set_attributes(tag = "acc_expr", debug = debug_precision)
  acc_modulo = get_remainder(acc_expr, precision, k, debuglld, tag = "acc_modulo")

  red_loop = Loop(init_loop,
      vi <= lsb_index,
       Statement(
          ReferenceAssign(acc, 
            (acc_expr - acc_modulo).modify_attributes(tag = "diff", debug = debug_precision),
          ),
          ReferenceAssign(vi, vi + 1)
        )
      )
  result = Statement(lsb_index, msb_index, red_loop) 

  # restoring sollya's global precision
  prec(old_global_prec)

  return result, acc



    

