# Copyright (c) 2022 Intel Corporation
#
# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================


"""Tests for fused_batch_norm related functionality in tensorflow.ops.nn."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from intel_extension_for_tensorflow.python.test_func import test_util
from intel_extension_for_tensorflow.python.test_func import test

import tensorflow as tf
import numpy as np
import os

from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import gradient_checker
from tensorflow.python.ops import gradients_impl
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_grad
from tensorflow.python.ops import nn_impl
from tensorflow.python.ops import nn_ops

# Test plain format.
os.environ['ITEX_ENABLE_ONEDNN_LAYOUT_OPT']="0"

class BatchNormalizationTest(test.TestCase):

  def _batch_norm(self, x, mean, var, offset, scale, epsilon):
    # We compute the batch norm manually in this function because
    # nn_impl.batch_normalization does not support float16 yet.
    # TODO(reedwm): Add float16 support to nn_impl.batch_normalization.
    inv = math_ops.rsqrt(var + epsilon) * scale
    y = math_ops.cast(x, scale.dtype) * inv + (offset - mean * inv)
    return math_ops.cast(y, x.dtype)

  def _inference_ref(self, x, scale, offset, mean, var, epsilon, data_format):
    if data_format not in ['NHWC', 'NCHW', 'NDHWC', 'NCDHW']:
      raise ValueError('data_format must be NCHW or NHWC for 4D tensors or'
                       'NCDHW or NDHWC for 5D tensors, got %s.' % data_format)
    if data_format == 'NCHW':
      x = array_ops.transpose(x, [0, 2, 3, 1])
    elif data_format == 'NCDHW':
      x = array_ops.transpose(x, [0, 2, 3, 4, 1])
    y = self._batch_norm(x, mean, var, offset, scale, epsilon)
    if data_format == 'NCHW':
      y = array_ops.transpose(y, [0, 3, 1, 2])
    elif data_format == 'NCDHW':
      y = array_ops.transpose(y, [0, 4, 1, 2, 3])
    return self.evaluate(y)

  def _test_inference(self,
                      x_shape,
                      x_dtype,
                      scale_shape,
                      scale_dtype,
                      use_gpu=True,
                      exponential_avg_factor=1.0,
                      data_format='NHWC'):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    mean_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    var_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu) as sess:
      x = constant_op.constant(x_val, name='x')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      mean = constant_op.constant(mean_val, name='mean')
      var = constant_op.constant(var_val, name='variance')
      epsilon = 0.001
      y, _, _ = array_ops.identity_n(nn_impl.fused_batch_norm(
          x,
          scale,
          offset,
          mean=mean,
          variance=var,
          epsilon=epsilon,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=False))
      y_val = self.evaluate(y)
      y_ref = self._inference_ref(x, scale, offset, mean, var, epsilon,
                                  data_format)
    # An atol value of 1e-3 is too small for float16's, because some adjacent
    # float16 values that y_val can take are greater than 1e-3 apart, e.g.
    # 2.16602 and 2.16797.
    atol = 2e-3 if x_dtype == np.float16 else 1e-3
    self.assertAllClose(y_ref, y_val, atol=atol)

  def _running_mean(self, old_mean, new_val, factor):
    if factor == 1.0:
      return new_val
    else:
      return (1.0 - factor) * old_mean + factor * new_val

  def _training_ref(self, x, scale, offset, old_mean, old_var,
                    exponential_avg_factor, epsilon, data_format):
    if data_format not in ['NHWC', 'NCHW', 'NDHWC', 'NCDHW']:
      raise ValueError('data_format must be NCHW or NHWC for 4D tensors or'
                       'NCDHW or NDHWC for 5D tensors, got %s.' % data_format)
    use_4d_tensor = (x.shape.ndims == 4)
    if data_format == 'NCHW':
      x = array_ops.transpose(x, [0, 2, 3, 1])
    elif data_format == 'NCDHW':
      x = array_ops.transpose(x, [0, 2, 3, 4, 1])

    mean_axis = [0, 1, 2] if use_4d_tensor else [0, 1, 2, 3]
    batch_mean, batch_var = nn_impl.moments(
        math_ops.cast(x, scale.dtype), mean_axis, keep_dims=False)

    y = self._batch_norm(x, batch_mean, batch_var, offset, scale, epsilon)
    if data_format == 'NCHW':
      y = array_ops.transpose(y, [0, 3, 1, 2])
    elif data_format == 'NCDHW':
      y = array_ops.transpose(y, [0, 4, 1, 2, 3])

    # This is for Bessel's correction. tf.nn.moments uses n, instead of n-1, as
    # the denominator in the formula to calculate variance, while
    # tf.compat.v1.nn.fused_batch_norm has Bessel's correction built in.
    sample_size = math_ops.cast(
        array_ops.size(x) / array_ops.size(scale), scale.dtype)
    batch_var_corrected = batch_var * sample_size / (
        math_ops.maximum(sample_size - 1.0, 1.0))

    mean = self._running_mean(old_mean, batch_mean, exponential_avg_factor)
    var = self._running_mean(old_var, batch_var_corrected,
                             exponential_avg_factor)
    return self.evaluate(y), self.evaluate(mean), self.evaluate(var)

  def _test_training(self,
                     x_shape,
                     x_dtype,
                     scale_shape,
                     scale_dtype,
                     use_gpu=True,
                     exponential_avg_factor=1.0,
                     data_format='NHWC'):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    if exponential_avg_factor == 1.0:
      old_mean_val = None
      old_var_val = None
    else:
      old_mean_val = np.random.random_sample(scale_shape).astype(scale_dtype)
      old_var_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu) as sess:
      x = constant_op.constant(x_val, name='x')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      epsilon = 0.001
      y, mean, var = array_ops.identity_n(nn_impl.fused_batch_norm(
          x,
          scale,
          offset,
          mean=old_mean_val,
          variance=old_var_val,
          epsilon=epsilon,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=True))
      y_val, mean_val, var_val = self.evaluate([y, mean, var])
      y_ref, mean_ref, var_ref = self._training_ref(x, scale, offset,
                                                    old_mean_val, old_var_val,
                                                    exponential_avg_factor,
                                                    epsilon, data_format)
    y_atol = 2e-3 if x_dtype == np.float16 else 1e-3
    self.assertAllClose(y_ref, y_val, atol=y_atol)
    self.assertAllClose(mean_ref, mean_val, atol=1e-3)
    self.assertAllClose(var_ref, var_val, atol=1e-3)

  def _compute_gradient_error_float16(self, x, x32, x_shape, y, y32, y_shape):
    """Computes the gradient error for float16 inputs and/or outputs.

    This returns the same value as gradient_checker.compute_gradient_error. The
    difference is that gradient_checker.compute_gradient_error does not
    numerically compute the gradients in a numerically stable way for float16
    tensors. To fix this, this function requires float32 versions of x and y to
    numerically compute the gradients, to compare with the float16 symbolically
    computed gradients.

    Args:
      x: The input tensor.
      x32: A float32 version of x.
      x_shape: The shape of x.
      y: The output tensor.
      y32: A float32 version of y. Must be calculated based on x32, not x.
      y_shape: The shape of y.

    Returns:
      The maximum error in between the two Jacobians, as in
      gradient_checker.compute_gradient_error.
    """
    x_init_val = np.random.random_sample(x_shape).astype(np.float16)
    x32_init_val = x_init_val.astype(np.float32)

    # TODO(reedwm): Do not perform the unnecessary computations in
    # compute_gradient, since they double the computation time of this function.
    theoretical_grad, _ = gradient_checker.compute_gradient(
        x, x_shape, y, y_shape, delta=1e-3, x_init_value=x_init_val)
    _, numerical_grad = gradient_checker.compute_gradient(
        x32, x_shape, y32, y_shape, delta=1e-3, x_init_value=x32_init_val)

    # If grad is empty, no error.
    if theoretical_grad.size == 0 and numerical_grad.size == 0:
      return 0
    return np.fabs(theoretical_grad - numerical_grad).max()

  def _test_gradient(self,
                     x_shape,
                     x_dtype,
                     scale_shape,
                     scale_dtype,
                     use_gpu=True,
                     exponential_avg_factor=1.0,
                     data_format='NHWC',
                     is_training=True):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu):
      x = constant_op.constant(x_val, name='x')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      if is_training and exponential_avg_factor == 1.0:
        pop_mean = None
        pop_var = None
      else:
        pop_mean = np.random.random_sample(scale_shape).astype(scale_dtype)
        pop_var = np.random.random_sample(scale_shape).astype(scale_dtype)
      y, _, _ = array_ops.identity_n(nn_impl.fused_batch_norm(
          x,
          scale,
          offset,
          mean=pop_mean,
          variance=pop_var,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=is_training))
      if x_dtype != np.float16:
        err_x = gradient_checker.compute_gradient_error(x, x_shape, y, x_shape)
        err_scale = gradient_checker.compute_gradient_error(
            scale, scale_shape, y, x_shape)
        err_offset = gradient_checker.compute_gradient_error(
            offset, scale_shape, y, x_shape)
      else:
        x32 = constant_op.constant(x_val, name='x32', dtype=dtypes.float32)
        y32, _, _ = array_ops.identity_n(nn_impl.fused_batch_norm(
            x32,
            scale,
            offset,
            mean=pop_mean,
            variance=pop_var,
            data_format=data_format,
            exponential_avg_factor=exponential_avg_factor,
            is_training=is_training))
        err_x = self._compute_gradient_error_float16(x, x32, x_shape, y, y32,
                                                     x_shape)
        err_scale = self._compute_gradient_error_float16(
            scale, scale, scale_shape, y, y32, x_shape)
        err_offset = self._compute_gradient_error_float16(
            offset, offset, scale_shape, y, y32, x_shape)

    x_err_tolerance = 2e-3 if x_dtype == np.float16 else 1e-3
    scale_err_tolerance = 1e-3
    self.assertLess(err_x, x_err_tolerance)
    self.assertLess(err_scale, scale_err_tolerance)
    self.assertLess(err_offset, scale_err_tolerance)

  def _test_grad_grad(self,
                      x_shape,
                      x_dtype,
                      scale_shape,
                      scale_dtype,
                      use_gpu=True,
                      exponential_avg_factor=1.0,
                      data_format='NHWC',
                      is_training=True,
                      err_tolerance=1e-3):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    grad_y_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu) as sess:
      x = constant_op.constant(x_val, name='x')
      grad_y = constant_op.constant(grad_y_val, name='grad_y')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      if is_training and exponential_avg_factor == 1.0:
        pop_mean = None
        pop_var = None
      else:
        pop_mean = np.random.random_sample(scale_shape).astype(scale_dtype)
        pop_var = np.random.random_sample(scale_shape).astype(scale_dtype)
      y, _, _ = nn_impl.fused_batch_norm(
          x,
          scale,
          offset,
          mean=pop_mean,
          variance=pop_var,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=is_training)
      grad_x, grad_scale, grad_offset = gradients_impl.gradients(
          y, [x, scale, offset], grad_y)

      if is_training:
        epsilon = y.op.get_attr('epsilon')
        data_format = y.op.get_attr('data_format')
        grad_vals = self.evaluate([grad_x, grad_scale, grad_offset])
        grad_internal = nn_grad._BatchNormGrad(grad_y, x, scale, pop_mean,
                                               pop_var, epsilon, data_format)
        grad_internal_vals = self.evaluate(list(grad_internal))
        for grad_val, grad_internal_val in zip(grad_vals, grad_internal_vals):
          self.assertAllClose(grad_val, grad_internal_val, atol=err_tolerance)

      if x_dtype != np.float16:
        err_grad_grad_y_1 = gradient_checker.compute_gradient_error(
            grad_y, x_shape, grad_x, x_shape)
        err_grad_grad_y_2 = gradient_checker.compute_gradient_error(
            grad_y, x_shape, grad_scale, scale_shape)
        err_grad_grad_y_3 = gradient_checker.compute_gradient_error(
            grad_y, x_shape, grad_offset, scale_shape)
        # In freeze mode, grad_x is not a function of x.
        if is_training:
          err_grad_x_1 = gradient_checker.compute_gradient_error(
              x, x_shape, grad_x, x_shape)
        err_grad_x_2 = gradient_checker.compute_gradient_error(
            x, x_shape, grad_scale, scale_shape)

        err_grad_scale = gradient_checker.compute_gradient_error(
            scale, scale_shape, grad_x, x_shape)
      else:
        x32 = constant_op.constant(x_val, dtype=dtypes.float32, name='x32')
        grad_y32 = constant_op.constant(
            grad_y_val, dtype=dtypes.float32, name='grad_y32')
        y32, _, _ = array_ops.identity_n(nn_impl.fused_batch_norm(
            x32,
            scale,
            offset,
            mean=pop_mean,
            variance=pop_var,
            exponential_avg_factor=exponential_avg_factor,
            data_format=data_format,
            is_training=is_training))
        grad_x32, grad_scale32, grad_offset32 = gradients_impl.gradients(
            y32, [x32, scale, offset], grad_y32)
        err_grad_grad_y_1 = self._compute_gradient_error_float16(
            grad_y, grad_y32, x_shape, grad_x, grad_x32, x_shape)
        err_grad_grad_y_2 = self._compute_gradient_error_float16(
            grad_y, grad_y32, x_shape, grad_scale, grad_scale32, scale_shape)
        err_grad_grad_y_3 = self._compute_gradient_error_float16(
            grad_y, grad_y32, x_shape, grad_offset, grad_offset32, scale_shape)
        # In freeze mode, grad_x is not a function of x.
        if is_training:
          err_grad_x_1 = self._compute_gradient_error_float16(
              x, x32, x_shape, grad_x, grad_x32, x_shape)
        err_grad_x_2 = self._compute_gradient_error_float16(
            x, x32, x_shape, grad_scale, grad_scale32, scale_shape)

        err_grad_scale = self._compute_gradient_error_float16(
            scale, scale, scale_shape, grad_x, grad_x32, x_shape)

    self.assertLess(err_grad_grad_y_1, err_tolerance)
    self.assertLess(err_grad_grad_y_2, err_tolerance)
    self.assertLess(err_grad_grad_y_3, err_tolerance)
    if is_training:
      self.assertLess(err_grad_x_1, err_tolerance)
    self.assertLess(err_grad_x_2, err_tolerance)
    self.assertLess(err_grad_scale, err_tolerance)

  def _runtests(self, x_shape, is_training, gradient_test=False,
                cpu_only=False):
    if len(x_shape) == 4:
      data_format_list = ['NHWC', 'NCHW']
    else:
      data_format_list = ['NCDHW', 'NDHWC']
    use_gpu_vals = [False]
    if test.is_gpu_available(cuda_only=True) and not cpu_only:
      use_gpu_vals += [True]
    factors = [1.0, 0.6]
    for dtype in [np.float32]:
      for use_gpu in use_gpu_vals:
        for data_format in data_format_list:
          if data_format == 'NHWC' or data_format == 'NDHWC':
            scale_shape = x_shape[-1:]
          else:
            scale_shape = x_shape[1:2]
          for exponential_avg_factor in factors:
            if gradient_test:
              self._test_gradient(
                  x_shape,
                  dtype,
                  scale_shape,
                  np.float32,
                  use_gpu=use_gpu,
                  data_format=data_format,
                  is_training=is_training,
                  exponential_avg_factor=exponential_avg_factor)
            else:
              if is_training:
                self._test_training(
                    x_shape,
                    dtype,
                    scale_shape,
                    np.float32,
                    use_gpu=use_gpu,
                    data_format=data_format,
                    exponential_avg_factor=exponential_avg_factor)
              else:
                self._test_inference(
                    x_shape,
                    dtype,
                    scale_shape,
                    np.float32,
                    use_gpu=use_gpu,
                    data_format=data_format,
                    exponential_avg_factor=exponential_avg_factor)

  def _test_raw_ops_fusedbatchnorm_inference(self,
                                             x_shape,
                                             x_dtype,
                                             scale_shape,
                                             scale_dtype,
                                             use_gpu=True,
                                             exponential_avg_factor=1.0,
                                             data_format='NHWC'):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    mean_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    var_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu) as sess:
      x = constant_op.constant(x_val, name='x')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      mean = constant_op.constant(mean_val, name='mean')
      var = constant_op.constant(var_val, name='variance')
      epsilon = 0.001
      y, _, _, _, _ = array_ops.identity_n(tf.raw_ops.FusedBatchNorm(
          x = x,
          scale = scale,
          offset = offset,
          mean=mean,
          variance=var,
          epsilon=epsilon,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=False))
      yv2, _, _, _, _ = array_ops.identity_n(tf.raw_ops.FusedBatchNormV2(
          x = x,
          scale = scale,
          offset = offset,
          mean=mean,
          variance=var,
          epsilon=epsilon,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=False))
      y_val = self.evaluate(y)
      yv2_val = self.evaluate(yv2)
      y_ref = self._inference_ref(x, scale, offset, mean, var, epsilon,
                                  data_format)
    # An atol value of 1e-3 is too small for float16's, because some adjacent
    # float16 values that y_val can take are greater than 1e-3 apart, e.g.
    # 2.16602 and 2.16797.
    atol = 2e-3 if x_dtype == np.float16 else 1e-3
    self.assertAllClose(y_ref, y_val, atol=atol)
    self.assertAllClose(y_ref, yv2_val, atol=atol)

  def _test_raw_ops_fusedbatchnorm_training(self,
                                            x_shape,
                                            x_dtype,
                                            scale_shape,
                                            scale_dtype,
                                            use_gpu=True,
                                            exponential_avg_factor=1.0,
                                            data_format='NHWC'):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    old_mean_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    old_var_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu) as sess:
      x = constant_op.constant(x_val, name='x')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      epsilon = 0.001
      y, mean, var, _, _ = array_ops.identity_n(tf.raw_ops.FusedBatchNorm(
          x = x,
          scale = scale,
          offset = offset,
          mean = old_mean_val,
          variance = old_var_val,
          epsilon=epsilon,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=True))
      yv2, meanv2, varv2, _, _ = array_ops.identity_n(tf.raw_ops.FusedBatchNormV2(
          x = x,
          scale = scale,
          offset = offset,
          mean = old_mean_val,
          variance = old_var_val,
          epsilon=epsilon,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=True))
      y_val, mean_val, var_val = self.evaluate([y, mean, var])
      yv2_val, meanv2_val, varv2_val = self.evaluate([yv2, meanv2, varv2])
      y_ref, mean_ref, var_ref = self._training_ref(x, scale, offset,
                                                    old_mean_val, old_var_val,
                                                    exponential_avg_factor,
                                                    epsilon, data_format)
    y_atol = 2e-3 if x_dtype == np.float16 else 1e-3
    self.assertAllClose(y_ref, y_val, atol=y_atol)
    self.assertAllClose(mean_ref, mean_val, atol=1e-3)
    self.assertAllClose(var_ref, var_val, atol=1e-3)

    self.assertAllClose(y_ref, yv2_val, atol=y_atol)
    self.assertAllClose(mean_ref, meanv2_val, atol=1e-3)
    self.assertAllClose(var_ref, varv2_val, atol=1e-3)

  def _test_raw_ops_fusedbatchnorm_gradient(self,
                                            x_shape,
                                            x_dtype,
                                            scale_shape,
                                            scale_dtype,
                                            use_gpu=True,
                                            exponential_avg_factor=1.0,
                                            data_format='NHWC',
                                            is_training=True):
    np.random.seed(1)
    x_val = np.random.random_sample(x_shape).astype(x_dtype)
    scale_val = np.random.random_sample(scale_shape).astype(scale_dtype)
    offset_val = np.random.random_sample(scale_shape).astype(scale_dtype)

    with self.cached_session(use_gpu=use_gpu):
      x = constant_op.constant(x_val, name='x')
      scale = constant_op.constant(scale_val, name='scale')
      offset = constant_op.constant(offset_val, name='offset')
      if is_training:
        pop_mean = np.zeros(scale_shape).astype(scale_dtype)
        pop_var = np.zeros(scale_shape).astype(scale_dtype)
      else:
        pop_mean = np.random.random_sample(scale_shape).astype(scale_dtype)
        pop_var = np.random.random_sample(scale_shape).astype(scale_dtype)
      y, _, _, _, _ = array_ops.identity_n(tf.raw_ops.FusedBatchNorm(
          x = x,
          scale = scale,
          offset = offset,
          mean=pop_mean,
          variance=pop_var,
          exponential_avg_factor=exponential_avg_factor,
          data_format=data_format,
          is_training=is_training))
      if x_dtype != np.float16:
        err_x = gradient_checker.compute_gradient_error(x, x_shape, y, x_shape)
        err_scale = gradient_checker.compute_gradient_error(
            scale, scale_shape, y, x_shape)
        err_offset = gradient_checker.compute_gradient_error(
            offset, scale_shape, y, x_shape)
      else:
        x32 = constant_op.constant(x_val, name='x32', dtype=dtypes.float32)
        y32, _, _, _, _ = array_ops.identity_n(tf.raw_ops.FusedBatchNorm(
            x = x32,
            scale = scale,
            offset = offset,
            mean=pop_mean,
            variance=pop_var,
            exponential_avg_factor=exponential_avg_factor,
            data_format=data_format,
            is_training=is_training))
        err_x = self._compute_gradient_error_float16(x, x32, x_shape, y, y32,
                                                     x_shape)
        err_scale = self._compute_gradient_error_float16(
            scale, scale, scale_shape, y, y32, x_shape)
        err_offset = self._compute_gradient_error_float16(
            offset, offset, scale_shape, y, y32, x_shape)

    x_err_tolerance = 2e-3 if x_dtype == np.float16 else 1e-3
    scale_err_tolerance = 1e-3
    self.assertLess(err_x, x_err_tolerance)
    self.assertLess(err_scale, scale_err_tolerance)
    self.assertLess(err_offset, scale_err_tolerance)

  def _run_raw_ops_fusedbatchnorm_tests(self, x_shape, is_training, gradient_test=False,
                cpu_only=False):
    if len(x_shape) == 4:
      data_format_list = ['NHWC', 'NCHW']
    else:
      data_format_list = ['NCDHW', 'NDHWC']
    use_gpu_vals = [False]
    if test.is_gpu_available(cuda_only=True) and not cpu_only:
      use_gpu_vals += [True]
    factors = [1.0, 0.6]
    for dtype in [np.float32]:
      for use_gpu in use_gpu_vals:
        for data_format in data_format_list:
          if data_format == 'NHWC' or data_format == 'NDHWC':
            scale_shape = x_shape[-1:]
          else:
            scale_shape = x_shape[1:2]
          for exponential_avg_factor in factors:
            if gradient_test:
              self._test_raw_ops_fusedbatchnorm_gradient(
                  x_shape,
                  dtype,
                  scale_shape,
                  np.float32,
                  use_gpu=use_gpu,
                  data_format=data_format,
                  is_training=is_training,
                  exponential_avg_factor=exponential_avg_factor)
            else:
              if is_training:
                self._test_raw_ops_fusedbatchnorm_training(
                    x_shape,
                    dtype,
                    scale_shape,
                    np.float32,
                    use_gpu=use_gpu,
                    data_format=data_format,
                    exponential_avg_factor=exponential_avg_factor)
              else:
                self._test_raw_ops_fusedbatchnorm_inference(
                    x_shape,
                    dtype,
                    scale_shape,
                    np.float32,
                    use_gpu=use_gpu,
                    data_format=data_format,
                    exponential_avg_factor=exponential_avg_factor)

  def testInferenceShape1(self):
    x_shape = [1, 1, 6, 1]
    self._runtests(x_shape, False)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, False)

  def testInferenceShape2(self):
    x_shape = [1, 1, 6, 2]
    self._runtests(x_shape, False)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, False)

  def testInferenceShape3(self):
    x_shape = [1, 2, 1, 6]
    self._runtests(x_shape, False)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, False)

  def testInferenceShape4(self):
    x_shape = [27, 131, 127, 6]
    self._runtests(x_shape, False)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, False)

  def testInferenceShape5(self):
    x_shape = [0, 131, 127, 6]
    self._runtests(x_shape, False)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, False)

  def testInferenceShape6(self):
    x_shape = [1, 1, 1, 1]
    #GPU kernel doesn't properly handle case where non-channel dimensions are 1
    self._runtests(x_shape, False, cpu_only=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, False, cpu_only=True)

  def testInferenceShape7(self):
    x_shape = [1, 2, 6, 1, 3]
    self._runtests(x_shape, False)

  @test_util.run_deprecated_v1
  def testFusedBatchNormExShape1(self):
    np.random.seed(1)

    x_shape = [1, 1, 6, 1]
    scale_shape = x_shape[-1:]
    input_p = tf.compat.v1.placeholder(dtype=tf.float32, shape=x_shape)

    x_np = np.random.random_sample(x_shape).astype(np.float32)
    scale_np = np.random.random_sample(scale_shape).astype(np.float32)
    offset_np = np.random.random_sample(scale_shape).astype(np.float32)
    x_tf = tf.constant(x_np, tf.float32)
    scale_tf = tf.constant(scale_np, tf.float32)
    offset_tf = tf.constant(offset_np, tf.float32)

    with self.cached_session(use_gpu=False) as sess:
      my_fbn, _, _ = nn_impl.fused_batch_norm(input_p, scale_tf, offset_tf)
      relu = tf.nn.relu(my_fbn)
      cpu_result = sess.run(relu, feed_dict={input_p: x_np})

    with self.cached_session(use_gpu=True) as sess:
      my_fbn, _, _ = nn_impl.fused_batch_norm(input_p, scale_tf, offset_tf)
      relu = array_ops.identity(tf.nn.relu(my_fbn))
      gpu_result = sess.run(relu, feed_dict={input_p: x_np})

    self.assertAllClose(cpu_result, gpu_result)

  @test_util.run_deprecated_v1
  def testFusedBatchNormGradExShape1(self):
    if test.is_gpu_available(cuda_only=True):
      use_gpu = True
    else:
      use_gpu = False

    with self.cached_session(use_gpu=use_gpu):
      x_shape = [0, 7, 11, 4]
      x_dtype = np.float32
      scale_shape = x_shape[-1:]
      scale_dtype = np.float32

      x = tf.random.normal(x_shape, seed=0, dtype=x_dtype)
      scale = tf.random.normal(scale_shape, seed=0, dtype=scale_dtype)
      offset = tf.random.normal(scale_shape, seed=0, dtype=scale_dtype)
      pop_mean = None
      pop_var = None

      y = nn_ops.relu(x)
      y, _, _ = nn_impl.fused_batch_norm(
          y,
          scale,
          offset,
          mean=pop_mean,
          variance=pop_var,
          exponential_avg_factor=1.0,
          data_format='NHWC',
          is_training=True)
      y = array_ops.identity(nn_ops.relu(y))
      err_x = gradient_checker.compute_gradient_error(x, x_shape, y, x_shape)

    self.assertLess(err_x, 1e-3)

  def testTrainingShape1(self):
    x_shape = [1, 1, 6, 1]
    self._runtests(x_shape, True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, True)

  def testTrainingShape2(self):
    x_shape = [1, 1, 6, 2]
    self._runtests(x_shape, True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, True)

  def testTrainingShape3(self):
    x_shape = [1, 2, 1, 6]
    self._runtests(x_shape, True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, True)

  def testTrainingShape4(self):
    x_shape = [27, 131, 127, 6]
    self._runtests(x_shape, True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, True)

  @test_util.disable_xla('b/141236973: Empty inputs wrong on CPU.')
  def testTrainingShape5(self):
    x_shape = [0, 131, 127, 6]
    self._runtests(x_shape, True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, True)

  @test_util.run_deprecated_v1
  def testTrainingShape6(self):
    x_shape = [1, 1, 1, 1]
    # GPU kernel doesn't properly handle case where non-channel dimensions are 1
    self._runtests(x_shape, True, cpu_only=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, True, cpu_only=True)

  def testTrainingShape7(self):
    x_shape = [1, 2, 6, 1, 3]
    self._runtests(x_shape, True)

  def _compute_bf16(self, use_gpu, is_training):
    with self.cached_session(use_gpu=use_gpu):
      x_shape = [1, 1, 6, 1]
      x_dtype = tf.bfloat16
      scale_shape = [1]
      scale_dtype = tf.float32
      data_format = 'NCHW'

      x = tf.random.normal(x_shape, seed=0, dtype=x_dtype)
      scale = tf.random.normal(scale_shape, seed=0, dtype=scale_dtype)
      offset = tf.random.normal(scale_shape, seed=0, dtype=scale_dtype)

      if is_training:
        pop_mean = None
        pop_var = None
      else:
        pop_mean = tf.random.normal(scale_shape, seed = 0, dtype =scale_dtype)
        pop_var = tf.random.normal(scale_shape, seed = 0, dtype =scale_dtype)

      y, _, _ = nn_impl.fused_batch_norm(
      x,
      scale,
      offset,
      mean=pop_mean,
      variance=pop_var,
      data_format=data_format,
      is_training=is_training)

      return self.evaluate(tf.gradients(y,x)) 

  # TODO(itex): reenable it after fixed in intel-tf
  # @test_util.run_deprecated_v1
  # def testBatchNormGradBF16(self):
  #   cpu_grad = self._compute_bf16(False, False)
  #   cpu_grad_training = self._compute_bf16(False, True)
  #   gpu_grad = self._compute_bf16(True, False)
  #   gpu_grad_training = self._compute_bf16(True, True)
  #   self.assertAllClose(cpu_grad, gpu_grad)
  #   self.assertAllClose(cpu_grad_training, gpu_grad_training)

  @test_util.run_deprecated_v1
  def testBatchNormGradInferenceShape1(self):
    x_shape = [1, 1, 6, 1]
    self._runtests(x_shape, is_training=False, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=False, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradInferenceShape2(self):
    x_shape = [1, 1, 6, 2]
    self._runtests(x_shape, is_training=False, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=False, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradInferenceShape3(self):
    x_shape = [1, 2, 1, 6]
    self._runtests(x_shape, is_training=False, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=False, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradInferenceShape4(self):
    x_shape = [5, 7, 11, 4]
    self._runtests(x_shape, is_training=False, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=False, gradient_test=True)

  @test_util.run_deprecated_v1
  @test_util.disable_xla('This test never passed for XLA')
  def testBatchNormGradInferenceShape5(self):
    x_shape = [0, 7, 11, 4]
    self._runtests(x_shape, is_training=False, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=False, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradInferenceShape6(self):
    x_shape = [1, 1, 1, 1]
    # GPU kernel doesn't properly handle case where non-channel dimensions are 1
    self._runtests(x_shape, is_training=False, gradient_test=True,
                   cpu_only=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=False, gradient_test=True,
                   cpu_only=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradInferenceShape7(self):
    x_shape = [1, 2, 6, 1, 3]
    self._runtests(x_shape, is_training=False, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradTrainingShape1(self):
    x_shape = [1, 1, 6, 1]
    self._runtests(x_shape, is_training=True, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=True, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradTrainingShape2(self):
    x_shape = [1, 1, 6, 2]
    self._runtests(x_shape, is_training=True, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=True, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradTrainingShape3(self):
    x_shape = [1, 2, 1, 6]
    self._runtests(x_shape, is_training=True, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=True, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradTrainingShape4(self):
    x_shape = [5, 7, 11, 4]
    self._runtests(x_shape, is_training=True, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=True, gradient_test=True)

  @test_util.run_deprecated_v1
  @test_util.disable_xla('This test never passed for XLA')
  def testBatchNormGradTrainingShape5(self):
    x_shape = [0, 7, 11, 4]
    self._runtests(x_shape, is_training=True, gradient_test=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=True, gradient_test=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradTrainingShape6(self):
    x_shape = [1, 1, 1, 1]
    # GPU kernel doesn't properly handle case where non-channel dimensions are 1
    self._runtests(x_shape, is_training=True, gradient_test=True, cpu_only=True)
    self._run_raw_ops_fusedbatchnorm_tests(x_shape, is_training=True, gradient_test=True, cpu_only=True)

  @test_util.run_deprecated_v1
  def testBatchNormGradTrainingShape7(self):
    x_shape = [1, 2, 6, 1, 3]
    self._runtests(x_shape, is_training=True, gradient_test=True)

  def _testBatchNormGradGrad(self, config):
    shape = config['shape']
    err_tolerance = config['err_tolerance']
    dtype = config['dtype']
    rank = len(shape)
    if rank == 4:
      data_format_nhwc, features_nhwc = 'NHWC', shape[3]
      data_format_nchw, features_nchw = 'NCHW', shape[1]
    else:
      data_format_nhwc, features_nhwc = 'NDHWC', shape[4]
      data_format_nchw, features_nchw = 'NCDHW', shape[1]
    for is_training in [True, False]:
      if test.is_gpu_available(cuda_only=True):
        self._test_grad_grad(
            shape,
            dtype, [features_nhwc],
            np.float32,
            use_gpu=True,
            data_format=data_format_nhwc,
            is_training=is_training,
            err_tolerance=err_tolerance)
        self._test_grad_grad(
            shape,
            dtype, [features_nchw],
            np.float32,
            use_gpu=True,
            data_format=data_format_nchw,
            is_training=is_training,
            err_tolerance=err_tolerance)
      self._test_grad_grad(
          shape,
          dtype, [features_nhwc],
          np.float32,
          use_gpu=False,
          data_format=data_format_nhwc,
          is_training=is_training,
          err_tolerance=err_tolerance)
      self._test_grad_grad(
          shape,
          dtype, [features_nchw],
          np.float32,
          use_gpu=False,
          data_format=data_format_nchw,
          is_training=is_training,
          err_tolerance=err_tolerance)

  @test_util.run_deprecated_v1
  def testBatchNormGradGradConfig1(self):
    config = {
        'shape': [2, 3, 4, 5],
        'err_tolerance': 1e-2,
        'dtype': np.float32,
    }
    self._testBatchNormGradGrad(config)

  @test_util.run_deprecated_v1
  def testBatchNormGradGradConfig2(self):
    config = {
        'shape': [2, 3, 2, 2],
        'err_tolerance': 1e-3,
        'dtype': np.float32,
    }
    self._testBatchNormGradGrad(config)

  @test_util.run_deprecated_v1
  def testBatchNormGradGradConfig3(self):
    config = {
        'shape': [2, 3, 4, 5],
        'err_tolerance': 2e-2,
        'dtype': np.float16,
    }
    self._testBatchNormGradGrad(config)

  @test_util.run_deprecated_v1
  def testBatchNormGradGradConfig4(self):
    config = {
        'shape': [2, 3, 2, 2],
        'err_tolerance': 2e-3,
        'dtype': np.float16,
    }
    self._testBatchNormGradGrad(config)

  @test_util.run_deprecated_v1
  def testBatchNormGradGradConfig5(self):
    config = {
        'shape': [2, 3, 2, 2, 2],
        'err_tolerance': 2e-3,
        'dtype': np.float32,
    }
    self._testBatchNormGradGrad(config)

  @test_util.run_deprecated_v1
  def testBatchNormGradGradConfig6(self):
    config = {
        'shape': [2, 3, 2, 2, 2],
        'err_tolerance': 3e-3,
        'dtype': np.float16,
    }
    self._testBatchNormGradGrad(config)

if __name__ == '__main__':
  test.main()
