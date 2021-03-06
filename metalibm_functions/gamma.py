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
#
# created:            Mar   11th, 2016
# last-modified:      Mar    3rd, 2019
#
# description: meta-implementation of gamma function
###############################################################################

import sollya
import bigfloat

from sollya import (
    Interval, ceil, floor, round, inf, sup,
    dirtyinfnorm, guessdegree)

from metalibm_core.core.ml_function import DefaultArgTemplate
from metalibm_core.core.ml_operations import *
from metalibm_core.core.ml_formats import *
from metalibm_core.core.polynomials import Polynomial, PolynomialSchemeEvaluator
from metalibm_core.core.ml_table import ML_NewTable, generic_mantissa_msb_index_fct
from metalibm_core.core.precisions import ML_Faithful
from metalibm_core.core.special_values import (
        FP_QNaN, FP_MinusInfty, FP_PlusInfty, FP_PlusZero
)
from metalibm_core.core.indexing import SubFPIndexing
from metalibm_core.core.approximation import (
    search_bound_threshold, generic_poly_split,
    search_bound_threshold_mirror,
)
from metalibm_core.core.simple_scalar_function import ScalarUnaryFunction

from metalibm_core.code_generation.generic_processor import GenericProcessor

from metalibm_core.utility.ml_template import ML_NewArgTemplate
from metalibm_core.utility.debug_utils import debug_multi


# static constant for numerical value 2
S2 = sollya.SollyaObject(2)


class ML_Gamma(ScalarUnaryFunction):
    """ Meta implementation of the error-function """
    function_name = "gamma"
    def __init__(self, args):
        super().__init__(args)

    @staticmethod
    def get_default_args(**kw):
        """ Return a structure containing the arguments for ML_Gamma,
            builtin from a default argument mapping overloaded with @p kw """
        default_args_erf = {
                "output_file": "gamma.c",
                "function_name": "gamma",
                "precision": ML_Binary32,
                "accuracy": ML_Faithful,
                "target": GenericProcessor.get_target_instance(),
                "passes": [
                    ("start:instantiate_abstract_prec"),
                    ("start:instantiate_prec"),
                    ("start:basic_legalization"),
                    ("start:expand_multi_precision")],
        }
        default_args_erf.update(kw)
        return DefaultArgTemplate(**default_args_erf)

    def generate_scalar_scheme(self, vx):
        # approximation the gamma function
        abs_vx = Abs(vx, precision=self.precision)

        FCT_LIMIT = 1.0

        omega_value = self.precision.get_omega()

        def sollya_wrap_bigfloat_fct(bfct):
            """ wrap bigfloat's function <bfct> such that is can be used
                on SollyaObject inputs and returns SollyaObject results """
            def fct(x):
                return sollya.SollyaObject(bfct(SollyaObject(x).bigfloat()))
            return fct
        sollya_gamma = sollya_wrap_bigfloat_fct(bigfloat.gamma)
        sollya_digamma = sollya_wrap_bigfloat_fct(bigfloat.digamma)
        # first derivative of gamma is digamma * gamma
        bigfloat_gamma_d0 = lambda x: bigfloat.gamma(x) * bigfloat.digamma(x)
        sollya_gamma_d0 = sollya_wrap_bigfloat_fct(bigfloat_gamma_d0)

        # approximating trigamma with straightforward derivatives formulae of digamma
        U = 2**-64
        bigfloat_trigamma = lambda x: ((bigfloat.digamma(x * (1 + U)) - bigfloat.digamma(x)) / (x*U))
        sollya_trigamma = sollya_wrap_bigfloat_fct(bigfloat_trigamma)

        bigfloat_gamma_d1 = lambda x: (bigfloat_trigamma(x) * bigfloat.gamma(x) + bigfloat_gamma_d0(x) * bigfloat.digamma(x))
        sollya_gamma_d1 = sollya_wrap_bigfloat_fct(bigfloat_gamma_d1)

        def sollya_gamma_fct(x, diff_order, prec):
            """ wrapper to use bigfloat implementation of exponential
                rather than sollya's implementation directly.
                This wrapper implements sollya's function API.

                :param x: numerical input value (may be an Interval)
                :param diff_order: differential order
                :param prec: numerical precision expected (min)
            """
            fct = None
            if diff_order == 0:
                fct = sollya_gamma
            elif diff_order == 1:
                fct = sollya_gamma_d0
            elif diff_order == 2:
                fct = sollya_gamma_d1
            else:
                raise NotImplementedError
            with bigfloat.precision(prec):
                if x.is_range():
                    lo = sollya.inf(x)
                    hi = sollya.sup(x)
                    return sollya.Interval(
                        fct(lo),
                        fct(hi)
                    )
                else:
                    return fct(x)

        # search the lower x such that gamma(x) >= omega
        omega_upper_limit = search_bound_threshold(sollya_gamma, omega_value, 2, 1000.0, self.precision)
        Log.report(Log.Debug, "gamma(x) = {} limit is {}", omega_value, omega_upper_limit)

        # evaluate gamma(<min-normal-value>)
        lower_x_bound = self.precision.get_min_normal_value()
        value_min = sollya_gamma(lower_x_bound)
        Log.report(Log.Debug, "gamma({}) = {}(log2={})", lower_x_bound, value_min, int(sollya.log2(value_min))) 

        # evaluate gamma(<min-subnormal-value>)
        lower_x_bound = self.precision.get_min_subnormal_value()
        value_min = sollya_gamma(lower_x_bound)
        Log.report(Log.Debug, "gamma({}) = {}(log2={})", lower_x_bound, value_min, int(sollya.log2(value_min))) 

        # Gamma is defined such that gamma(x+1) = x * gamma(x)
        #
        # we approximate gamma over [1, 2]
        # y in [1, 2]
        # gamma(y) = (y-1) * gamma(y-1)
        # gamma(y-1) = gamma(y) / (y-1)
        Log.report(Log.Info, "building mathematical polynomial")
        approx_interval = Interval(1, 1.01)
        approx_fct = sollya.function(sollya_gamma_fct)
        poly_degree = int(sup(guessdegree(approx_fct, approx_interval, S2**-(self.precision.get_field_size()+5)))) + 1
        Log.report(Log.Debug, "approximation's poly degree over [1, 2] is {}", poly_degree)

        global_poly_object = Polynomial.build_from_approximation(approx_fct, poly_degree, [self.precision]*(poly_degree+1), approx_interval, sollya.relative)
        Log.report(Log.Debug, "inform is {}", dirtyinfnorm(approx_fct - global_poly_object.get_sollya_object(), approx_interval))

        poly_object = global_poly_object.sub_poly(start_index=1, offset=1)

        ext_precision = {
            ML_Binary32: ML_SingleSingle,
            ML_Binary64: ML_DoubleDouble,
        }[self.precision]

        pre_poly = PolynomialSchemeEvaluator.generate_horner_scheme(
            poly_object, abs_vx, unified_precision=self.precision)

        result = FMA(pre_poly, abs_vx, abs_vx)
        result.set_attributes(tag="result", debug=debug_multi)


        eps_target = S2**-(self.precision.get_field_size() +5)
        def offset_div_function(fct):
            return lambda offset: fct(sollya.x + offset)

        # empiral numbers
        field_size = {
            ML_Binary32: 6,
            ML_Binary64: 8
        }[self.precision]

        near_indexing = SubFPIndexing(eps_exp, 0, 6, self.precision)
        near_approx = generic_poly_split(offset_div_function(sollya.erf), near_indexing, eps_target, self.precision, abs_vx)
        near_approx.set_attributes(tag="near_approx", debug=debug_multi)

        def offset_function(fct):
            return lambda offset: fct(sollya.x + offset)
        medium_indexing = SubFPIndexing(1, one_limit_exp, 7, self.precision)

        medium_approx = generic_poly_split(offset_function(sollya.erf), medium_indexing, eps_target, self.precision, abs_vx)
        medium_approx.set_attributes(tag="medium_approx", debug=debug_multi)

        # approximation for positive values
        scheme = ConditionBlock(
            abs_vx < eps,
            Return(result),
            ConditionBlock(
                abs_vx < near_indexing.get_max_bound(),
                Return(near_approx),
                ConditionBlock(
                    abs_vx < medium_indexing.get_max_bound(),
                    Return(medium_approx),
                    Return(Constant(1.0, precision=self.precision))
                )
            )
        )
        return scheme

    def numeric_emulate(self, input_value):
        return bigfloat.gamma(sollya.SollyaObject(input_value).bigfloat())

    standard_test_cases = [
        (1.0, None),
        (sollya.parse("0x1.13b2c6p-2"), None),
    ]



if __name__ == "__main__":
    # auto-test
    arg_template = ML_NewArgTemplate(default_arg=ML_Gamma.get_default_args())
    args = arg_template.arg_extraction()

    ml_gamma = ML_Gamma(args)
    ml_gamma.gen_implementation()
