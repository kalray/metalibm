# -*- coding: utf-8 -*-

###############################################################################
# This file is part of metalibm (https://github.com/kalray/metalibm)
###############################################################################
# MIT License
#
# Copyright (c) 2018 Kalray
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
###############################################################################
# last-modified:    Oct 5th, 2018
#
# Description:      Meta-implementation of floating-point division
###############################################################################
import sollya

from sollya import Interval, sup

from metalibm_core.core.ml_operations import (
    Variable,
    Abs, Equal,
    Min, Max, Comparison,
    Constant,
    Test,
    NotEqual,
    ConditionBlock, Statement, Return,
    ExponentInsertion, ExponentExtraction,
    LogicalOr, Select,
    LogicalAnd, LogicalNot,
    MantissaExtraction, ExponentExtraction,
    Loop,
    Modulo, TypeCast,
    ReferenceAssign, Dereference,
)
from metalibm_core.core.attributes import Attributes
from metalibm_core.core.ml_complex_formats import ML_Pointer_Format
from metalibm_core.core.ml_formats import (
    ML_Binary32, ML_Binary64, ML_SingleSingle, ML_DoubleDouble,
    ML_Int32,
    ML_UInt64, ML_Int64,
    ML_Bool, ML_Exact,
)
from metalibm_core.core.special_values import (
    FP_QNaN, FP_MinusInfty, FP_PlusInfty,
    FP_MinusZero, FP_PlusZero,
    is_nan, is_zero, is_infty,
)
from metalibm_core.core.precisions import ML_CorrectlyRounded
from metalibm_core.core.ml_function import ML_FunctionBasis

from metalibm_core.core.meta_interval import MetaInterval, MetaIntervalList

from metalibm_core.code_generation.generic_processor import GenericProcessor
from metalibm_core.code_generation.code_constant import C_Code
from metalibm_core.code_generation.code_object import  GappaCodeObject
from metalibm_core.code_generation.gappa_code_generator import GappaCodeGenerator

from metalibm_core.utility.gappa_utils import execute_gappa_script_extract
from metalibm_core.utility.ml_template import ML_NewArgTemplate, DefaultArgTemplate
from metalibm_core.utility.debug_utils import debug_multi
from metalibm_core.utility.log_report import Log


S2 = sollya.SollyaObject(2)


class RemquoMode: pass
class QUOTIENT_MODE(RemquoMode): pass
class REMAINDER_MODE(RemquoMode): pass
class FULL_MODE(RemquoMode): pass

def remquo_mode_parser(s):
    """ converts a mode string into a Remquo mode enum.
    
        :arg s: remquo mode str descriptor
        :type s: str 
        :return Remquo class enum
    """
    return {
        "quotient": QUOTIENT_MODE,
        "full": FULL_MODE,
        "remainder": REMAINDER_MODE
    }[s]

class MetaRemQuo(ML_FunctionBasis):
    function_name = "ml_remquo"
    arity = 2

    def __init__(self, args=DefaultArgTemplate):
        # initializing class specific arguments (required by ML_FunctionBasis init)
        self.mode = remquo_mode_parser(args.mode)
        self.quotient_size = args.quotient_size
        # initializing base class
        ML_FunctionBasis.__init__(self, args=args)

    @staticmethod
    def get_default_args(**args):
        """ Generate a default argument structure set specifically for
            the Hyperbolic Cosine """
        default_div_args = {
            "precision": ML_Binary64,
            "accuracy": ML_CorrectlyRounded,
            "target": GenericProcessor.get_target_instance(),
            "output_file": "my_remquo.c",
            "function_name": "my_remquo",
            "input_intervals": [None, None],
            "auto_test_range": DefaultArgTemplate.auto_test_range * 2,
            "bench_test_range": DefaultArgTemplate.bench_test_range * 2,
            "language": C_Code,
            "mode": "remainder",
            "quotient_size": 7,
            "passes": ["typing:basic_legalization", "beforecodegen:expand_multi_precision"],
        }
        default_div_args.update(args)
        return DefaultArgTemplate(**default_div_args)

    def get_output_precision(self):
        if self.mode is QUOTIENT_MODE:
            return self.precision.get_integer_format()
        else:
            return self.precision

    def generate_scheme(self):
        int_precision = self.precision.get_integer_format()
        # We wish to compute vx / vy
        vx = self.implementation.add_input_variable("x", self.precision, interval=self.input_intervals[0])
        vy = self.implementation.add_input_variable("y", self.precision, interval=self.input_intervals[1])
        if self.mode is FULL_MODE:
            quo = self.implementation.add_input_variable("quo", ML_Pointer_Format(int_precision))

        i = Variable("i", precision=int_precision, var_type=Variable.Local)
        q = Variable("q", precision=int_precision, var_type=Variable.Local)

        CI = lambda v: Constant(v, precision=int_precision)
        CF = lambda v: Constant(v, precision=self.precision)

        vx_subnormal = Test(vx, specifier=Test.IsSubnormal, tag="vx_subnormal")
        vy_subnormal = Test(vy, specifier=Test.IsSubnormal, tag="vy_subnormal")

        DELTA_EXP = self.precision.get_mantissa_size()
        scale_factor = Constant(2.0**DELTA_EXP, precision=self.precision)
        inv_scale_factor = Constant(2.0**-DELTA_EXP, precision=self.precision)

        normalized_vx = Select(vx_subnormal, vx * scale_factor, vx, tag="scaled_vx")
        normalized_vy = Select(vy_subnormal, vy * scale_factor, vy, tag="scaled_vy")

        real_ex = ExponentExtraction(vx, tag="real_ex", precision=int_precision)
        real_ey = ExponentExtraction(vy, tag="real_ey", precision=int_precision)

        # if real_e<x/y> is +1023 then it may Overflow in -real_ex for ExponentInsertion
        # which only supports downto -1022 before falling into subnormal numbers (which are
        # not supported by ExponentInsertion)
        real_ex_h0 = real_ex / 2
        real_ex_h1 = real_ex - real_ex_h0

        real_ey_h0 = real_ey / 2
        real_ey_h1 = real_ey - real_ey_h0

        EI = lambda v: ExponentInsertion(v, precision=self.precision)

        mx = Abs((vx * EI(-real_ex_h0)) * EI(-real_ex_h1), tag="mx")
        my = Abs((vy * EI(-real_ey_h0)) * EI(-real_ey_h1), tag="pre_my")

        # scale_ey is used to regain the unscaling of mx in the first loop
        # if real_ey >= real_ex, the first loop is never executed
        # so a different scaling is required
        mx_unscaling = Select(real_ey < real_ex, real_ey, real_ex)
        ey_half0 = (mx_unscaling) / 2
        ey_half1 = (mx_unscaling) - ey_half0

        scale_ey_half0 = ExponentInsertion(ey_half0, precision=self.precision, tag="scale_ey_half0")
        scale_ey_half1 = ExponentInsertion(ey_half1, precision=self.precision, tag="scale_ey_half1")

        # if only vy is subnormal we want to normalize it
        #normal_cond = LogicalAnd(vy_subnormal, LogicalNot(vx_subnormal))
        normal_cond = vy_subnormal #LogicalAnd(vy_subnormal, LogicalNot(vx_subnormal))
        my = Select(normal_cond, Abs(MantissaExtraction(vy * scale_factor)), my, tag="my")


        # vx / vy = vx * 2^-ex * 2^(ex-ey) / (vy * 2^-ey)
        # vx % vy

        post_mx = Variable("post_mx", precision=self.precision, var_type=Variable.Local)

        # scaling for half comparison
        VY_SCALING = Select(vy_subnormal, 1.0, 0.5, precision=self.precision)
        VX_SCALING = Select(vy_subnormal, 2.0, 1.0, precision=self.precision)

        def LogicalXor(a, b):
            return LogicalOr(LogicalAnd(a, LogicalNot(b)), LogicalAnd(LogicalNot(a), b))

        rem_sign = Select(vx < 0, CF(-1), CF(1), precision=self.precision, tag="rem_sign")
        quo_sign = Select(LogicalXor(vx <0, vy < 0), CI(-1), CI(1), precision=int_precision, tag="quo_sign")

        loop_watchdog = Variable("loop_watchdog", precision=ML_Int32, var_type=Variable.Local)

        loop = Statement(
            real_ex, real_ey, mx, my, loop_watchdog,
            ReferenceAssign(loop_watchdog, 5000),
            ReferenceAssign(q, CI(0)),
            Loop(
                ReferenceAssign(i, CI(0)), i < (real_ex - real_ey),
                Statement(
                    ReferenceAssign(i, i+CI(1)),
                    ReferenceAssign(q, ((q << 1) + Select(mx >= my, CI(1), CI(0))).modify_attributes(tag="step1_q")),
                    ReferenceAssign(mx, (CF(2) * (mx - Select(mx >= my, my, CF(0)))).modify_attributes(tag="step1_mx")),
                    # loop watchdog
                    ReferenceAssign(loop_watchdog, loop_watchdog - 1),
                    ConditionBlock(loop_watchdog < 0, Return(-1)),
                ),
            ),
            # unscaling remainder
            ReferenceAssign(mx, ((mx * scale_ey_half0) * scale_ey_half1).modify_attributes(tag="scaled_rem")),
            ReferenceAssign(my, ((my * scale_ey_half0) * scale_ey_half1).modify_attributes(tag="scaled_rem_my")),
            Loop(
                Statement(), (my > Abs(vy)),
                Statement(
                    ReferenceAssign(q, ((q << 1) + Select(mx >= Abs(my), CI(1), CI(0))).modify_attributes(tag="step2_q")),
                    ReferenceAssign(mx, (mx - Select(mx >= Abs(my), Abs(my), CF(0))).modify_attributes(tag="step2_mx")),
                    ReferenceAssign(my, (my * 0.5).modify_attributes(tag="step2_my")),
                    # loop watchdog
                    ReferenceAssign(loop_watchdog, loop_watchdog - 1),
                    ConditionBlock(loop_watchdog < 0, Return(-1)),
                ),
            ),
            ReferenceAssign(q, q << 1),
            Loop(
                ReferenceAssign(i, CI(0)), mx > Abs(vy),
                Statement(
                    ReferenceAssign(q, (q + Select(mx > Abs(vy), CI(1), CI(0))).modify_attributes(tag="step3_q")),
                    ReferenceAssign(mx, (mx - Select(mx > Abs(vy), Abs(vy), CF(0))).modify_attributes(tag="step3_mx")),
                    # loop watchdog
                    ReferenceAssign(loop_watchdog, loop_watchdog - 1),
                    ConditionBlock(loop_watchdog < 0, Return(-1)),
                ),
            ),
            ReferenceAssign(q, q + Select(mx >= Abs(vy), CI(1), CI(0))),
            ReferenceAssign(mx, (mx - Select(mx >= Abs(vy), Abs(vy), CF(0))).modify_attributes(tag="pre_half_mx")),
            ConditionBlock(
                # actual comparison is mx > | abs(vy * 0.5) | to avoid rounding effect when
                # vy is subnormal we mulitply both side by 2.0**60
                ((mx * VX_SCALING) > Abs(vy * VY_SCALING)).modify_attributes(tag="half_test"),
                Statement(
                    ReferenceAssign(q, q + CI(1)),
                    ReferenceAssign(mx, (mx - Abs(vy)))
                )
            ),
            ConditionBlock(
                # if the remainder is exactly half the dividend
                # we need to make sure the quotient is even
                LogicalAnd(
                    Equal(mx * VX_SCALING, Abs(vy * VY_SCALING)),
                    Equal(Modulo(q, CI(2)), CI(1)),
                ),
                Statement(
                    ReferenceAssign(q, q + CI(1)),
                    ReferenceAssign(mx, (mx - Abs(vy)))
                )
            ),
            ReferenceAssign(mx, rem_sign * mx),
            ReferenceAssign(q,
                Modulo(TypeCast(q, precision=self.precision.get_unsigned_integer_format()), Constant(2**self.quotient_size, precision=self.precision.get_unsigned_integer_format()), tag="mod_q")
            ),
            ReferenceAssign(q, quo_sign * q),
        )

        # NOTES: Warning QuotientReturn must always preceeds RemainderReturn
        if self.mode is QUOTIENT_MODE:
            #
            QuotientReturn = Return
            RemainderReturn = lambda _: Statement()
        elif self.mode is REMAINDER_MODE:
            QuotientReturn = lambda _: Statement()
            RemainderReturn = Return
        elif self.mode is FULL_MODE:
            QuotientReturn = lambda v: ReferenceAssign(Dereference(quo, precision=int_precision), v) 
            RemainderReturn = Return
        else:
            raise NotImplemented

        # quotient invalid value
        QUO_INVALID_VALUE = 0

        mod_scheme = Statement(
            # x or y is NaN, a NaN is returned
            ConditionBlock(
                LogicalOr(Test(vx, specifier=Test.IsNaN), Test(vy, specifier=Test.IsNaN)),
                Statement(
                    QuotientReturn(QUO_INVALID_VALUE),
                    RemainderReturn(FP_QNaN(self.precision))
                ),
            ),
            #
            ConditionBlock(
                Test(vy, specifier=Test.IsZero),
                Statement(
                    QuotientReturn(QUO_INVALID_VALUE),
                    RemainderReturn(FP_QNaN(self.precision))
                ),
            ),
            ConditionBlock(
                Test(vx, specifier=Test.IsZero),
                Statement(
                    QuotientReturn(0),
                    RemainderReturn(vx)
                ),
            ),
            ConditionBlock(
                Test(vx, specifier=Test.IsInfty),
                Statement(
                    QuotientReturn(QUO_INVALID_VALUE),
                    RemainderReturn(FP_QNaN(self.precision))
                )
            ),
            ConditionBlock(
                Test(vy, specifier=Test.IsInfty),
                Statement(
                    QuotientReturn(0),
                    RemainderReturn(vx),
                )
            ),
            ConditionBlock(
                Abs(vx) < Abs(vy * 0.5),
                Statement(
                    QuotientReturn(0),
                    RemainderReturn(vx),
                )
            ),
            ConditionBlock(
                Equal(vx, vy),
                Statement(
                    QuotientReturn(1),
                    # 0 with the same sign as x
                    RemainderReturn(vx - vx),
                ),
            ),
            ConditionBlock(
                Equal(vx, -vy),
                Statement(
                    # quotient is -1
                    QuotientReturn(-1),
                    # 0 with the same sign as x
                    RemainderReturn(vx - vx),
                ),
            ),
            loop,
            QuotientReturn(q),
            RemainderReturn(mx),
        )

        quo_scheme = Statement(
            # x or y is NaN, a NaN is returned
            ConditionBlock(
                LogicalOr(Test(vx, specifier=Test.IsNaN), Test(vy, specifier=Test.IsNaN)),
                Return(QUO_INVALID_VALUE),
            ),
            #
            ConditionBlock(
                Test(vy, specifier=Test.IsZero),
                Return(QUO_INVALID_VALUE),
            ),
            ConditionBlock(
                Test(vx, specifier=Test.IsZero),
                Return(0),
            ),
            ConditionBlock(
                Test(vx, specifier=Test.IsInfty),
                Return(QUO_INVALID_VALUE),
            ),
            ConditionBlock(
                Test(vy, specifier=Test.IsInfty),
                Return(QUO_INVALID_VALUE),
            ),
            ConditionBlock(
                Abs(vx) < Abs(vy * 0.5),
                Return(0),
            ),
            ConditionBlock(
                Equal(vx, vy),
                Return(1),
            ),
            ConditionBlock(
                Equal(vx, -vy),
                Return(-1),
            ),
            loop,
            Return(q),

        )

        return mod_scheme


    def numeric_emulate(self, vx, vy):
        """ Numeric emulation of exponential """
        if self.mode is QUOTIENT_MODE:
            if is_nan(vx) or is_nan(vy) or is_zero(vy) or is_infty(vx):
                # invalid value specified by OpenCL-C
                return 0
            if is_infty(vy) or is_zero(vx):
                # valid value
                return 0
        else:
            if is_nan(vx) or is_nan(vy) or is_zero(vy):
                return FP_QNaN(self.precision)
            elif is_zero(vx):
                return vx
            elif is_infty(vx):
                return FP_QNaN(self.precision)
            elif is_infty(vy):
                return vx
        # factorizing canonical cases (including correctionà
        # between quotient_mode and remainder mode
        pre_mod = sollya.euclidian_mod(vx, vy)
        pre_quo = int(sollya.euclidian_div(vx, vy))
        if abs(pre_mod) > abs(vy * 0.5):
            if (pre_mod < 0 and vy < 0) or (pre_mod > 0 and vy > 0):
                # same sign
                pre_mod -= vy
                pre_quo += 1
            else:
                # opposite sign
                pre_mod += vy
                pre_quo -= 1
        if self.mode is QUOTIENT_MODE:
            quo_mod = abs(pre_quo) % 2**self.quotient_size
            if vx / vy < 0:
                return -quo_mod
            else:
                return quo_mod

        else:
            return pre_mod


    @property
    def standard_test_cases(self):
        fp64_list = [
            # random test
            (sollya.parse("0x1.e906cc97d7cc1p+743"), sollya.parse("0x0.000001b84ba98p-1022")),
            (sollya.parse("0x1.9c4110b0dea4fp+279"), sollya.parse("0x0.000ccf2945bd8p-1022")),
            # OpenCL CTS error
            # infinite loop
            # ERROR: remquoD: {-inf, 77} ulp error at {0x0.eaffffffffb86p-1022, -0x0.0000000000202p-1022} ({ 0x000eaffffffffb86, 0x8000000000000202}): *{0x0.00000000000eap-1022, -78} ({ 0x00000000000000ea, 0xffffffb2}) vs. {-0x1.0000000000000p+0, -1} ({ 0xbff0000000000000, 0xffffffff})
            (sollya.parse("0x0.eaffffffffb86p-1022"), sollya.parse("-0x0.0000000000202p-1022"), sollya.parse("0x0.00000000000eap-1022") if self.mode is REMAINDER_MODE else -78),
            # ERROR: remquoD: {0.000000, 92} ulp error at {-0x1.b9000000003e0p-982, -0x0.0000000000232p-1022} ({ 0x829b9000000003e0, 0x8000000000000232}): *{-0x0.0000000000000p+0, 0} ({ 0x8000000000000000, 0x00000000}) vs. {-0x0.0000000000000p+0, 92} ({ 0x8000000000000000, 0x0000005c})
            (sollya.parse("-0x1.b9000000003e0p-982"), sollya.parse("-0x0.0000000000232p-1022"), FP_MinusZero(self.precision) if self.mode is REMAINDER_MODE else 0),

            # ERROR: remquoD: {-26458647810801664.000000, 1} ulp error at {-0x1.be000000005dfp+977, 0x1.78000000006f1p+975} ({ 0xfd0be000000005df, 0x7ce78000000006f1}): *{0x1.8000000002ce4p+973, -5} ({ 0x7cc8000000002ce4, 0xfffffffb}) vs. {-0x1.17ffffffffbb8p+975, -4} ({ 0xfce17ffffffffbb8, 0xfffffffc})
            (sollya.parse("-0x1.be000000005dfp+977"), sollya.parse("0x1.78000000006f1p+975"), sollya.parse("0x1.8000000002ce4p+973") if self.mode is REMAINDER_MODE else -5),
            # ERROR: remquoD: {-171.000000, -1} ulp error at {-0x1.02ffffffffc2bp+489, -0x0.00000000000abp-1022} ({ 0xde802ffffffffc2b, 0x80000000000000ab}): *{0x0.0000000000055p-1022, 127} ({ 0x0000000000000055, 0x0000007f}) vs. {-0x0.0000000000056p-1022, 254} ({ 0x8000000000000056, 0x000000fe})
            (sollya.parse("0x1.02ffffffffc2bp+489"), sollya.parse("0x0.00000000000abp-1022")),
            (sollya.parse("-0x1.02ffffffffc2bp+489"), sollya.parse("-0x0.00000000000abp-1022"), sollya.parse("0x0.0000000000055p-1022") if self.mode is REMAINDER_MODE else 127),
            # ERROR: remquoD: {nan, 0} ulp error at {-0x1.69000000001e1p+749, -inf} ({ 0xeec69000000001e1, 0xfff0000000000000}): *{-0x1.69000000001e1p+749, 0} ({ 0xeec69000000001e1, 0x00000000}) vs. {nan, 0} ({ 0x7ff8000000000000, 0x00000000})
            (sollya.parse("-0x1.69000000001e1p+749"), FP_MinusInfty(self.precision), sollya.parse("-0x1.69000000001e1p+749") if self.mode is REMAINDER_MODE else 0),

            # ERROR: remquo: {0.000000, -126} ulp error at {0x1.921456p+70, -0x1.921456p+70} ({0x62c90a2b, 0xe2c90a2b}): *{0x0p+0, -1} ({0x00000000, 0xffffffff}) vs. {0x0p+0, 1} ({0x00000000, 0x00000001})
            (sollya.parse("0x1.921456p+70"), sollya.parse("-0x1.921456p+70"), 0 if self.mode is REMAINDER_MODE else -1),
            # ERROR: remquoD: {10731233487093760.000000, 0} ulp error at {0x1.30fffffffff7ep-597, -0x1.13ffffffffed0p-596} ({ 0x1aa30fffffffff7e, 0x9ab13ffffffffed0}): *{-0x1.edffffffffc44p-598, -1} ({ 0x9a9edffffffffc44, 0xffffffff}) vs. {0x1.d000000000ae0p-600, -1} ({ 0x1a7d000000000ae0, 0xffffffff})
            (sollya.parse("0x1.30fffffffff7ep-597"), sollya.parse("-0x1.13ffffffffed0p-596"), -1 if self.mode is QUOTIENT_MODE else sollya.parse("-0x1.edffffffffc44p-598")),
            #  {-0x1.9bffffffffd38p+361, 0x0.00000000000a5p-1022} ({ 0xd689bffffffffd38, 0x00000000000000a5}): *{-0x0.000000000000fp-1022, -93} ({ 0x800000000000000f, 0xffffffa3}) vs. {0x0.000000000000fp-1022, -1171354717} ({ 0x000000000000000f, 0xba2e8ba3})
            (sollya.parse("-0x1.9bffffffffd38p+361"), sollya.parse("0x0.00000000000a5p-1022")),
            (sollya.parse("0x0.ac0f94b9da13p-1022"), sollya.parse("-0x1.1e4580d7eb2e7p-1022")),
            (sollya.parse("0x1.fffffffffffffp+1023"), sollya.parse("-0x1.fffffffffffffp+1023")),
            (sollya.parse("0x0.a9f466178b1fcp-1022"), sollya.parse("0x0.b22f552dc829ap-1022")),
            (sollya.parse("0x1.4af8b07942537p-430"), sollya.parse("-0x0.f72be041645b7p-1022")),
            #result is 0x0.0000000000505p-1022 vs expected0x0.0000000000a3cp-1022
            #(sollya.parse("0x1.9f9f4e9a29fcfp-421"), sollya.parse("0x0.0000000001b59p-1022"), sollya.parse("0x0.0000000000a3cp-1022")),
            (sollya.parse("0x1.9906165fb3e61p+62"), sollya.parse("0x1.9906165fb3e61p+60")),
            (sollya.parse("0x1.9906165fb3e61p+62"), sollya.parse("0x0.0000000005e7dp-1022")),
            (sollya.parse("0x1.77f00143ba3f4p+943"), sollya.parse("0x0.000000000001p-1022")),
            (sollya.parse("0x0.000000000001p-1022"), sollya.parse("0x0.000000000001p-1022")),
            (sollya.parse("0x0.000000000348bp-1022"), sollya.parse("0x0.000000000001p-1022")),
            (sollya.parse("0x1.bcf3955c3b244p-130"), sollya.parse("0x1.77aef33890951p-1003")),
            (sollya.parse("0x1.8de59bd84c51ep-866"), sollya.parse("0x1.045aa9bf14fb1p-774")),
            (sollya.parse("0x1.9f9f4e9a29fcfp-421"), sollya.parse("0x0.0000000001b59p-1022")),
            (sollya.parse("0x1.2e1c59b43a459p+953"), sollya.parse("0x0.0000001cf5319p-1022")),
            (sollya.parse("-0x1.86c83abe0854ep+268"), FP_MinusInfty(self.precision)),
            # bad sign of remainder
            (sollya.parse("0x1.d3fb9968850a5p-960"), sollya.parse("-0x0.23c1ed19c45fp-1022")),
            # bad sign of zero
            (FP_MinusZero(self.precision), sollya.parse("0x1.85200a9235193p-450")),
            # bad remainder
            (sollya.parse("0x1.fffffffffffffp+1023"), sollya.parse("0x1.1f31bcd002a7ap-803")),
            # bad sign
            (sollya.parse("-0x1.4607d0c9fc1a7p-878"), sollya.parse("-0x1.9b666b840b1bp-1023")),
        ]
        return fp64_list if self.precision.get_bit_size() >= 64 else []



if __name__ == "__main__":
    # auto-test
    arg_template = ML_NewArgTemplate(
        default_arg=MetaRemQuo.get_default_args()
    )
    arg_template.get_parser().add_argument(
         "--quotient-size", dest="quotient_size", default=3, type=int,
        action="store", help="number of bit to return for the quotient")
    arg_template.get_parser().add_argument(
         "--mode", dest="mode", default="quotient", choices=['quotient', 'remainder', 'full'],
        action="store", help="number of bit to return for the quotient")

    ARGS = arg_template.arg_extraction()

    ml_remquo = MetaRemQuo(ARGS)
    ml_remquo.gen_implementation()
