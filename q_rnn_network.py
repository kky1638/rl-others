"""Recurrent network for DQN.

Based on QRnnNetwork and LSTMEncodingNetwork in tf.agents library.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from tf_agents.networks import dynamic_unroll_layer
from tf_agents.networks import encoding_network
from tf_agents.networks import network
from tf_agents.specs import tensor_spec
from tf_agents.trajectories import time_step
from tf_agents.utils import nest_utils

FUSED_IMPLEMENTATION = 2


def get_cell(cell_type, hidden_size, dtype):
  """Returns an RNNCell in tf.keras.layers."""

  def get_single_cell(cell_type, num_units):
    if cell_type == 'simple_rnn':
      cell = tf.keras.layers.SimpleRNNCell(
          num_units, dtype=dtype, implementation=FUSED_IMPLEMENTATION)
    elif cell_type == 'gru':
      cell = tf.keras.layers.GRUCell(
          num_units, dtype=dtype, implementation=FUSED_IMPLEMENTATION)
    elif cell_type == 'lstm':
      cell = tf.keras.layers.LSTMCell(
          num_units, dtype=dtype, implementation=FUSED_IMPLEMENTATION)
    else:
      raise ValueError('Unsupported cell type %s' % cell_type)
    return cell

  if len(hidden_size) == 1:
    cell = get_single_cell(cell_type, hidden_size[0])
  else:
    cell = tf.keras.layers.StackedRNNCells(
        [get_single_cell(cell_type, size) for size in hidden_size])
  return cell


class RnnNetwork(network.Network):
  """Recurrent network."""

  def __init__(
      self,
      input_tensor_spec,
      action_spec,
      preprocessing_layers=None,
      preprocessing_combiner=None,
      conv_layer_params=None,
      input_fc_layer_params=(75, 40),
      cell_type='lstm',
      hidden_size=(40,),
      output_fc_layer_params=(75, 40),
      activation_fn=tf.keras.activations.relu,
      dtype=tf.float32,
      name='RnnNetwork',
  ):
    """Creates an instance of `RnnNetwork`.

    Input preprocessing is possible via `preprocessing_layers` and
    `preprocessing_combiner` Layers.  If the `preprocessing_layers` nest is
    shallower than `input_tensor_spec`, then the layers will get the subnests.
    For example, if:

    ```python
    input_tensor_spec = ([TensorSpec(3)] * 2, [TensorSpec(3)] * 5)
    preprocessing_layers = (Layer1(), Layer2())
    ```

    then preprocessing will call:

    ```python
    preprocessed = [preprocessing_layers[0](observations[0]),
                    preprocessing_layers[1](obsrevations[1])]
    ```

    However if

    ```python
    preprocessing_layers = ([Layer1() for _ in range(2)],
                            [Layer2() for _ in range(5)])
    ```

    then preprocessing will call:
    ```python
    preprocessed = [
      layer(obs) for layer, obs in zip(flatten(preprocessing_layers),
                                       flatten(observations))
    ]
    ```

    Args:
      input_tensor_spec: A nest of `tensor_spec.TensorSpec` representing the
        observations.
      action_spec: A nest of `tensor_spec.BoundedTensorSpec` representing the
        actions.
      preprocessing_layers: (Optional.) A nest of `tf.keras.layers.Layer`
        representing preprocessing for the different observations. All of these
        layers must not be already built.
      preprocessing_combiner: (Optional.) A keras layer that takes a flat list
        of tensors and combines them.  Good options include
        `tf.keras.layers.Add` and `tf.keras.layers.Concatenate(axis=-1)`. This
        layer must not be already built.
      conv_layer_params: Optional list of convolution layers parameters, where
        each item is a length-three tuple indicating (filters, kernel_size,
        stride).
      input_fc_layer_params: Optional list of fully connected parameters, where
        each item is the number of units in the layer. These feed into the
        recurrent layer.
      cell_type: Type of RNNCell implementation to use.
      hidden_size: An iterable of ints specifying the LSTM cell sizes to use.
      output_fc_layer_params: Optional list of fully connected parameters, where
        each item is the number of units in the layer. These are applied on top
        of the recurrent layer.
      activation_fn: Activation function, e.g. tf.keras.activations.relu,.
      dtype: The dtype to use by the convolution, LSTM, and fully connected
        layers.
      name: A string representing name of the network.

    Raises:
      ValueError: If any of `preprocessing_layers` is already built.
      ValueError: If `preprocessing_combiner` is already built.
    """
    kernel_initializer = tf.variance_scaling_initializer(
        scale=2.0, mode='fan_in', distribution='truncated_normal')

    input_encoder = encoding_network.EncodingNetwork(
        input_tensor_spec,
        preprocessing_layers=preprocessing_layers,
        preprocessing_combiner=preprocessing_combiner,
        conv_layer_params=conv_layer_params,
        fc_layer_params=input_fc_layer_params,
        activation_fn=activation_fn,
        kernel_initializer=kernel_initializer,
        dtype=dtype)

    # Create RNN cell
    cell = get_cell(cell_type, hidden_size, dtype)

    output_encoder = []
    if output_fc_layer_params:
      output_encoder = [
          tf.keras.layers.Dense(
              num_units,
              activation=activation_fn,
              kernel_initializer=kernel_initializer,
              dtype=dtype,
              name='/'.join([name, 'dense']))
          for num_units in output_fc_layer_params
      ]

    action_spec = tf.nest.flatten(action_spec)[0]
    num_actions = action_spec.maximum - action_spec.minimum + 1
    q_projection = tf.keras.layers.Dense(
        num_actions,
        activation=None,
        kernel_initializer=tf.compat.v1.initializers.random_uniform(
            minval=-0.03, maxval=0.03),
        bias_initializer=tf.compat.v1.initializers.constant(-0.2),
        dtype=dtype,
        name='num_action_project/dense')
    output_encoder.append(q_projection)

    counter = [-1]

    def create_spec(size):
      counter[0] += 1
      return tensor_spec.TensorSpec(
          size, dtype=dtype, name='network_state_%d' % counter[0])

    state_spec = tf.nest.map_structure(create_spec, cell.state_size)

    super(RnnNetwork, self).__init__(
        input_tensor_spec=input_tensor_spec, state_spec=state_spec, name=name)

    self._conv_layer_params = conv_layer_params
    self._input_encoder = input_encoder
    self._dynamic_unroll = dynamic_unroll_layer.DynamicUnroll(cell)
    self._output_encoder = output_encoder

  def call(self, observation, step_type, network_state=(), training=False):
    """Applies the network.

    Args:
      observation: A tuple of tensors matching `input_tensor_spec`.
      step_type: A tensor of `StepType.
      network_state: (optional.) The network state.

    Returns:
      `(outputs, network_state)` - the network output and next network state.

    Raises:
      ValueError: If observation tensors lack outer `(batch,)` or
        `(batch, time)` axes.
    """
    num_outer_dims = nest_utils.get_outer_rank(observation,
                                               self.input_tensor_spec)
    if num_outer_dims not in (1, 2):
      raise ValueError(
          'Input observation must have a batch or batch x time outer shape.')

    has_time_dim = num_outer_dims == 2
    if not has_time_dim:
      # Add a time dimension to the inputs.
      observation = tf.nest.map_structure(lambda t: tf.expand_dims(t, 1),
                                          observation)
      step_type = tf.nest.map_structure(lambda t: tf.expand_dims(t, 1),
                                        step_type)

    state, _ = self._input_encoder(observation, step_type,
                                   network_state=(), training=training)

    with tf.name_scope('reset_mask'):
      reset_mask = tf.equal(step_type, time_step.StepType.FIRST)

    # Unroll over the time sequence.
    state, network_state = self._dynamic_unroll(
        state, reset_mask, initial_state=network_state)

    for layer in self._output_encoder:
      state = layer(state)

    if not has_time_dim:
      # Remove time dimension from the state.
      state = tf.squeeze(state, [1])

    return state, network_state
