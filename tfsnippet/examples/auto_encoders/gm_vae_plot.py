# -*- coding: utf-8 -*-
import functools
import os

import seaborn as sns
import numpy as np
import tensorflow as tf
from matplotlib import pyplot as plt
from tensorflow import keras as K
from tensorflow.contrib.framework import arg_scope

from tfsnippet.bayes import BayesianNet
from tfsnippet.modules import VAE, Sequential, DictMapper
from tfsnippet.dataflow import DataFlow
from tfsnippet.distributions import Normal, Categorical
from tfsnippet.examples.nn import (dense,
                                   resnet_block,
                                   deconv_resnet_block,
                                   reshape_conv2d_to_flat,
                                   l2_regularizer,
                                   regularization_loss)
from tfsnippet.examples.utils import (create_session,
                                      Config,
                                      anneal_after,
                                      Results,
                                      isolate_graph,
                                      collect_outputs)
from tfsnippet.scaffold import TrainLoop
from tfsnippet.trainer import AnnealingDynamicValue, LossTrainer, Evaluator
from tfsnippet.utils import global_reuse, get_default_session_or_error


class ExpConfig(Config):
    # model parameters
    x_dim = 2
    z_dim = 5
    n_clusters = 4  # for gmvae
    n_samples = 64  # for training and testing
    soft_clustering = True  # whether we use one network to build q(z|y,x),
                            # rather than `n_clusters` networks.

    # training parameters
    max_epoch = 300
    batch_size = 128
    l2_reg = 0.0001
    initial_lr = 0.0001
    lr_anneal_factor = 0.9995
    lr_anneal_epoch_freq = None
    lr_anneal_step_freq = 1
    valid_epoch_freq = 1

    # evaluation parameters
    test_batch_size = 128


config = ExpConfig()
results = Results()


@global_reuse
def h_for_q_z(x, is_training):
    with arg_scope([resnet_block],
                   activation_fn=tf.nn.relu,
                   normalizer_fn=functools.partial(
                       tf.layers.batch_normalization,
                       axis=1,
                       training=is_training
                   ),
                   dropout_fn=functools.partial(
                       tf.layers.dropout,
                       training=is_training
                   ),
                   kernel_regularizer=l2_regularizer(config.l2_reg)):
        x = tf.to_float(x)
        x = tf.reshape(x, [-1, 1, 28, 28])
        x = resnet_block(x, 16)  # output: (16, 28, 28)
        x = resnet_block(x, 32, strides=2)  # output: (32, 14, 14)
        x = resnet_block(x, 32)  # output: (32, 14, 14)
        x = resnet_block(x, 64, strides=2)  # output: (64, 7, 7)
        x = resnet_block(x, 64)  # output: (64, 7, 7)
    x = reshape_conv2d_to_flat(x)
    return {
        'mean': dense(x, config.z_dim, name='z_mean'),
        'logstd': dense(x, config.z_dim, name='z_logstd'),
    }


@global_reuse
def h_for_p_x(z, is_training):
    with arg_scope([deconv_resnet_block],
                   activation_fn=tf.nn.leaky_relu,
                   kernel_regularizer=l2_regularizer(config.l2_reg)):
        origin_shape = tf.shape(z)[:-1]  # might be (n_z, n_batch)
        z = tf.reshape(z, [-1, config.z_dim])
        z = tf.reshape(dense(z, 64 * 7 * 7), [-1, 64, 7, 7])
        z = deconv_resnet_block(z, 64)  # output: (64, 7, 7)
        z = deconv_resnet_block(z, 32, strides=2)  # output: (32, 14, 14)
        z = deconv_resnet_block(z, 32)  # output: (32, 14, 14)
        z = deconv_resnet_block(z, 16, strides=2)  # output: (16, 28, 28)
        z = tf.layers.conv2d(
            z, 1, (1, 1), padding='same', name='feature_map_to_pixel',
            data_format='channels_first')  # output: (1, 28, 28)
    x_logits = tf.reshape(z, tf.concat([origin_shape, [784]], axis=0))
    return {'logits': x_logits}


def make_data(n_samples):
    # Generate random sample, two components
    C = np.array([[0., -0.1], [1.0, 0]])
    X = np.r_[np.dot(np.random.randn(n_samples, 2), C),
              .7 * np.random.randn(n_samples, 2) + np.array([-6, 3])]
    return X


def plot_data(x, filename):
    # scatters
    plt.figure()
    plt.scatter(x[:, 0], x[:, 1], s=1, alpha=.2)
    plt.tight_layout(h_pad=0, w_pad=0)
    plt.savefig(results.prepare_parent(filename), format='PNG')

    # kdeplot
    kde_filename = '_kde'.join(os.path.splitext(filename))
    plt.figure()
    sns.kdeplot(x[:, 0], x[:, 1], shade=True)
    plt.savefig(results.prepare_parent(kde_filename), format='PNG')


def plot_log_p(x, log_p, filename):
    plt.figure()
    cmap = plt.get_cmap('jet')
    z = np.exp(log_p)
    h = plt.pcolormesh(x[:, :, 0], x[:, :, 1], z[:-1, :-1], cmap=cmap)
    plt.colorbar(h)
    plt.tight_layout(h_pad=0, w_pad=0)
    plt.savefig(results.prepare_parent(filename), format='PNG')


def experiment_common(tag, x_train, x_valid, x_test,
                      input_x, loss, log_p_per_x, nll, samples):
    # derive the optimizer
    learning_rate = tf.placeholder(shape=(), dtype=tf.float32)
    learning_rate_var = AnnealingDynamicValue(config.initial_lr,
                                              config.lr_anneal_factor)
    params = tf.trainable_variables()
    optimizer = tf.train.AdamOptimizer(learning_rate)
    with tf.control_dependencies(
            tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
        train_op = optimizer.minimize(loss, var_list=params)

    # derive the data-flow
    train_flow = DataFlow.arrays([x_train], config.batch_size, shuffle=True,
                                 skip_incomplete=True)
    valid_flow = DataFlow.arrays([x_valid], config.test_batch_size)
    test_flow = DataFlow.arrays([x_test], config.test_batch_size)

    # train the network
    with TrainLoop(params,
                   max_epoch=config.max_epoch,
                   summary_dir=results.make_dir(tag + '/train_summary'),
                   valid_metric_name='valid_loss',
                   early_stopping=True) as loop:
        trainer = LossTrainer(
            loop, loss, train_op, [input_x], train_flow,
            feed_dict={learning_rate: learning_rate_var}
        )
        anneal_after(
            trainer, learning_rate_var, epochs=config.lr_anneal_epoch_freq,
            steps=config.lr_anneal_step_freq
        )
        evaluator = Evaluator(
            loop,
            metrics={'valid_nll': nll, 'valid_loss': loss},
            inputs=[input_x],
            data_flow=valid_flow,
            time_metric_name='valid_time'
        )
        trainer.evaluate_after_epochs(
            evaluator, freq=config.valid_epoch_freq)
        trainer.log_after_epochs(freq=1)
        trainer.run()

    # compute the final nll for each point
    [log_p_out] = collect_outputs(
        outputs=[log_p_per_x],
        inputs=[input_x],
        data_flow=test_flow,
    )
    x_out = get_default_session_or_error().run(samples)

    # prepare for plotting the heatmap of log p(x)
    heatmap_x = np.stack(
        np.meshgrid(np.linspace(-10, 5, 200),
                    np.linspace(-5, 10, 200)),
        axis=-1
    )
    heatmap_flow = DataFlow.arrays([heatmap_x.reshape([-1, 2])],
                                   batch_size=config.test_batch_size)
    heatmap_log_p = np.reshape(
        collect_outputs(
            outputs=[log_p_per_x],
            inputs=[input_x],
            data_flow=heatmap_flow
        )[0],
        heatmap_x.shape[:2]
    )

    # write the final test_nll, plot the samples and the nll
    results.commit({tag + '_test_nll': -np.mean(log_p_out)})
    plot_data(x_out, tag + '/samples.png')
    plot_log_p(heatmap_x, heatmap_log_p, tag + '/log_p.png')


@isolate_graph
def vae_experiment(x_train, x_valid, x_test):
    input_x = tf.placeholder(
        dtype=tf.float32, shape=(None, config.x_dim), name='input_x')
    vae = VAE(
        p_z=Normal(mean=tf.zeros([1, config.z_dim]),
                   std=tf.ones([1, config.z_dim])),
        p_x_given_z=Normal,
        q_z_given_x=Normal,
        h_for_p_x=Sequential([
            K.layers.Dense(100, activation=tf.nn.relu),
            K.layers.Dense(100, activation=tf.nn.relu),
            DictMapper({
                'mean': K.layers.Dense(config.x_dim, name='x_mean'),
                'std': lambda x: 1e-4 + tf.nn.softplus(
                    K.layers.Dense(config.x_dim, name='x_std')(x)
                )
            })
        ]),
        h_for_q_z=Sequential([
            K.layers.Dense(100, activation=tf.nn.relu),
            K.layers.Dense(100, activation=tf.nn.relu),
            DictMapper({
                'mean': K.layers.Dense(config.z_dim, name='z_mean'),
                'std': lambda x: 1e-4 + tf.nn.softplus(
                    K.layers.Dense(config.z_dim, name='z_std')(x)
                )
            })
        ]),
    )
    chain = vae.chain(input_x, n_z=config.n_samples)

    # train loss
    vae_loss = tf.reduce_mean(chain.vi.training.iwae())
    loss = vae_loss + regularization_loss()

    # test outputs
    log_p_per_x = chain.vi.evaluation.importance_sampling_log_likelihood()
    nll = tf.reduce_mean(-log_p_per_x)
    samples = tf.reshape(vae.model(n_z=10000)['x'], [-1, config.x_dim])

    with create_session().as_default():
        experiment_common('vae', x_train, x_valid, x_test, input_x, loss,
                          log_p_per_x, nll, samples)


def softplus_std(inputs, n_dims, name=None):
    with tf.name_scope(name, default_name='std', values=[inputs]):
        return 1e-4 + tf.nn.softplus(dense(inputs, n_dims))


@global_reuse
def gmvae_q_net(x, observed=None, n_samples=None):
    net = BayesianNet(observed=observed)

    # compute the hidden features
    features = x
    features = dense(features, 100, activation_fn=tf.nn.relu)
    features = dense(features, 100, activation_fn=tf.nn.relu)
    y_logits = dense(features, config.n_clusters, name='y_logits')

    # sample y ~ q(y|x)
    y = net.add('y', Categorical(y_logits), n_samples=n_samples)

    # sample z ~ q(z|y,x)
    if n_samples is not None:
        x_tiled = tf.tile(tf.reshape(x, [1, -1, config.x_dim]),
                          tf.stack([n_samples, 1, 1]))
    else:
        x_tiled = x
    z_features = dense(
        tf.concat([x_tiled, tf.one_hot(y, config.n_clusters)], axis=-1), 100,
        activation_fn=tf.nn.relu
    )
    z_mean = dense(z_features, config.z_dim, name='z_mean')
    z_std = softplus_std(z_features, config.z_dim, name='z_std')
    z = net.add('z', Normal(mean=z_mean, std=z_std, is_reparameterized=False),
                group_ndims=1)

    return net


@global_reuse
def gmvae_p_net(observed=None, n_samples=None):
    net = BayesianNet(observed=observed)

    # sample y
    y = net.add('y', Categorical(tf.zeros([1, config.n_clusters])),
                n_samples=n_samples)

    # sample z ~ p(z|y)
    theta = 2 * np.pi / config.n_clusters
    R = np.sqrt(18. / (1 - np.cos(theta)))
    y_float = tf.to_float(tf.stop_gradient(y))
    z_prior_mean = tf.stack(
        [R * tf.cos(y_float * theta), R * tf.sin(y_float * theta)] +
        [tf.zeros_like(y_float)] * (config.z_dim - 2),
        axis=-1
    )
    z_prior_std = tf.ones([1, config.z_dim])
    z = net.add('z', Normal(mean=z_prior_mean, std=z_prior_std),
                group_ndims=1)

    # compute the hidden features for x
    features = z
    features = dense(features, 100, activation_fn=tf.nn.relu)
    features = dense(features, 100, activation_fn=tf.nn.relu)
    x_mean = dense(features, config.x_dim, name='x_mean')
    x_std = softplus_std(features, config.x_dim, name='x_std')

    # sample x ~ p(x|z)
    x = net.add('x', Normal(mean=x_mean, std=x_std), group_ndims=1)

    return net


@isolate_graph
def gmvae_experiment(x_train, x_valid, x_test):
    input_x = tf.placeholder(
        dtype=tf.float32, shape=(None, config.x_dim), name='input_x')

    # train loss
    q_net = gmvae_q_net(input_x, n_samples=config.n_samples)
    chain = q_net.chain(gmvae_p_net, observed={'x': input_x},
                        latent_names=['y', 'z'], latent_axis=0)
    gmvae_loss = tf.reduce_mean(chain.vi.training.vimco())
    loss = gmvae_loss + regularization_loss()

    # test outputs
    log_p_per_x = chain.vi.evaluation.importance_sampling_log_likelihood()
    nll = tf.reduce_mean(-log_p_per_x)

    p_net = gmvae_p_net(n_samples=10000)
    x_samples = tf.reshape(p_net['x'], [-1, config.x_dim])
    z_prior_samples = tf.reshape(p_net['z'], [-1, config.z_dim])

    # get posterior z ~ q(z|x)
    q_net = gmvae_q_net(input_x, n_samples=None)
    z_posterior_samples = q_net['z']

    with create_session().as_default() as session:
        experiment_common('gmvae', x_train, x_valid, x_test, input_x, loss,
                          log_p_per_x, nll, x_samples)

        [z_out] = collect_outputs(
            outputs=[z_posterior_samples],
            inputs=[input_x],
            data_flow=DataFlow.arrays([x_test], config.test_batch_size)
        )
        plot_data(z_out[:, :2], results.prepare_parent('gmvae/z_posterior.png'))
        [z_out] = session.run([z_prior_samples])
        plot_data(z_out[:, :2], results.prepare_parent('gmvae/z_prior.png'))


def main():
    # prepare the data
    x_data = make_data(130000 // 2)
    plot_data(x_data, 'data.png')
    np.random.shuffle(x_data)
    x_train, x_valid, x_test = \
        x_data[:70000], x_data[70000: 100000], x_data[100000:]

    # do VAE experiment
    vae_experiment(x_train, x_valid, x_test)

    # do GMVAE experiment
    gmvae_experiment(x_train, x_valid, x_test)


if __name__ == '__main__':
    main()