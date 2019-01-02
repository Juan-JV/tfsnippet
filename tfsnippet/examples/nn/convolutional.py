from functools import wraps, partial, WRAPPER_ASSIGNMENTS

import numpy as np
import tensorflow as tf
from tensorflow.contrib.framework import add_arg_scope

from tfsnippet.examples.utils import (validate_strides_or_kernel_size,
                                      add_variable_scope)
from tfsnippet.utils import int_shape
from tfsnippet.layers import conv2d, deconv2d

__all__ = [
    'resnet_block',
    'deconv_resnet_block',
]

# This code snippet is to deal with Python 2.x fail to apply `wraps` on a
# lambda funciton.
#
# Source: https://stackoverflow.com/questions/20594193/dynamic-create-method-and-decorator-got-error-functools-partial-object-has-no
try:
    wraps(partial(wraps))(wraps)
except AttributeError:
    @wraps(wraps)
    def wraps(obj, attr_names=WRAPPER_ASSIGNMENTS, wraps=wraps):
        return wraps(
            obj,
            assigned=(name for name in attr_names if hasattr(obj, name))
        )


def _resnet_block(conv_fn, inputs, input_shape, output_shape,
                  kernel_size, strides, shortcut_kernel_size, channels_last,
                  resize_last, activation_fn, normalizer_fn, dropout_fn):
    # check the arguments
    kernel_size = validate_strides_or_kernel_size('kernel_size', kernel_size)
    strides = validate_strides_or_kernel_size('strides', strides)
    shortcut_kernel_size = validate_strides_or_kernel_size(
        'shortcut_kernel_size', shortcut_kernel_size)

    # normalization and activation functions
    def add_scope(method):
        @wraps(method)
        def wrapper(x, name):
            with tf.name_scope(name):
                return method(x)
        return wrapper

    activation_fn = add_scope(activation_fn or (lambda x: x))
    normalizer_fn = add_scope(normalizer_fn or (lambda x: x))
    dropout_fn = add_scope(dropout_fn or (lambda x: x))

    # convolutional functions
    resize_conv = lambda shape: (lambda x, k_size, name: conv_fn(
        x, shape, kernel_size=k_size, strides=strides,
        name=name
    ))
    keep_conv = lambda shape: (lambda x, k_size, name: conv_fn(
        x, shape, kernel_size=k_size, strides=(1, 1),
        name=name
    ))

    # build the shortcut path
    if strides != (1, 1):
        shortcut_conv = resize_conv(output_shape)
        shortcut = shortcut_conv(inputs, shortcut_kernel_size, 'shortcut')
    else:
        shortcut = inputs

    # build the residual path
    if resize_last:
        conv1, conv2 = keep_conv(input_shape), resize_conv(output_shape)
    else:
        conv1, conv2 = resize_conv(output_shape), keep_conv(output_shape)
    residual = inputs
    residual = normalizer_fn(residual, 'norm1')
    residual = activation_fn(residual, 'nonlinear1')
    residual = conv1(residual, kernel_size, 'conv1')
    residual = dropout_fn(residual, 'dropout1')
    residual = normalizer_fn(residual, 'norm2')
    residual = activation_fn(residual, 'nonlinear2')
    residual = conv2(residual, kernel_size, 'conv2')

    # final do the merge
    return shortcut + residual


def _partial_conv(conv_fn,
                  channels_last,
                  use_bias,
                  kernel_initializer=None,
                  bias_initializer=tf.zeros_initializer(),
                  kernel_regularizer=None,
                  bias_regularizer=None,
                  kernel_constraint=None,
                  bias_constraint=None,
                  trainable=True):
    return partial(
        conv_fn,
        padding='same',
        channels_last=channels_last,
        use_bias=use_bias,
        kernel_initializer=kernel_initializer,
        bias_initializer=bias_initializer,
        kernel_regularizer=kernel_regularizer,
        bias_regularizer=bias_regularizer,
        kernel_constraint=kernel_constraint,
        bias_constraint=bias_constraint,
        trainable=trainable
    )


@add_arg_scope
@add_variable_scope
def resnet_block(inputs,
                 output_dims,
                 kernel_size=(3, 3),
                 strides=(1, 1),
                 shortcut_kernel_size=(1, 1),
                 channels_last=False,
                 activation_fn=None,
                 normalizer_fn=None,
                 dropout_fn=None,
                 use_bias=True,
                 kernel_initializer=None,
                 bias_initializer=tf.zeros_initializer(),
                 kernel_regularizer=None,
                 bias_regularizer=None,
                 kernel_constraint=None,
                 bias_constraint=None,
                 trainable=True):
    inputs = tf.convert_to_tensor(inputs)
    input_shape = int(inputs.get_shape()[3 if channels_last else 1])
    output_shape = int(output_dims)
    return _resnet_block(
        conv_fn=_partial_conv(
            conv2d,
            channels_last=channels_last,
            use_bias=use_bias and (normalizer_fn is None),
            kernel_initializer=kernel_initializer,
            bias_initializer=bias_initializer,
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer,
            kernel_constraint=kernel_constraint,
            bias_constraint=bias_constraint,
            trainable=trainable,
        ),
        inputs=inputs,
        input_shape=input_shape,
        output_shape=output_shape,
        kernel_size=kernel_size,
        strides=strides,
        shortcut_kernel_size=shortcut_kernel_size,
        channels_last=channels_last,
        resize_last=True,
        activation_fn=activation_fn,
        normalizer_fn=normalizer_fn,
        dropout_fn=dropout_fn,
    )


@add_arg_scope
@add_variable_scope
def deconv_resnet_block(inputs,
                        output_dims,
                        kernel_size=(3, 3),
                        strides=(1, 1),
                        shortcut_kernel_size=(1, 1),
                        channels_last=False,
                        activation_fn=None,
                        normalizer_fn=None,
                        dropout_fn=None,
                        use_bias=True,
                        kernel_initializer=None,
                        bias_initializer=tf.zeros_initializer(),
                        kernel_regularizer=None,
                        bias_regularizer=None,
                        kernel_constraint=None,
                        bias_constraint=None,
                        trainable=True):
    inputs = tf.convert_to_tensor(inputs)
    input_shape = int(inputs.get_shape()[3 if channels_last else 1])
    output_shape = int(output_dims)
    return _resnet_block(
        conv_fn=_partial_conv(
            deconv2d,
            channels_last=channels_last,
            use_bias=use_bias and (normalizer_fn is None),
            kernel_initializer=kernel_initializer,
            bias_initializer=bias_initializer,
            kernel_regularizer=kernel_regularizer,
            bias_regularizer=bias_regularizer,
            kernel_constraint=kernel_constraint,
            bias_constraint=bias_constraint,
            trainable=trainable,
        ),
        inputs=inputs,
        input_shape=input_shape,
        output_shape=output_shape,
        kernel_size=kernel_size,
        strides=strides,
        shortcut_kernel_size=shortcut_kernel_size,
        channels_last=channels_last,
        resize_last=False,
        activation_fn=activation_fn,
        normalizer_fn=normalizer_fn,
        dropout_fn=dropout_fn,
    )
