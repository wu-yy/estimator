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
# pylint: disable=protected-access
"""Home of estimator related functions."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import re
import tensorflow as tf
from tensorflow.python.framework import ops
from tensorflow.python.keras import backend as K
from tensorflow.python.keras import models
from tensorflow.python.keras.engine import training_utils
from tensorflow.python.training.tracking import graph_view
from tensorflow.python.training.tracking import util as trackable_util
from tensorflow_estimator.python.estimator import estimator as estimator_lib
from tensorflow_estimator.python.estimator import model_fn as model_fn_lib
from tensorflow_estimator.python.estimator.export import export_lib
from tensorflow_estimator.python.estimator.mode_keys import ModeKeys

_DEFAULT_SERVING_KEY = tf.saved_model.DEFAULT_SERVING_SIGNATURE_DEF_KEY


class FormattedKeyError(KeyError):
  """KeyError with formatted error message.

  Python's `KeyError` has special casing around formatting
  (see https://bugs.python.org/issue2651). Use this class when the error
  message has newlines and other special format characters.

  Needed by https://github.com/tensorflow/tensorflow/issues/36857.
  """

  def __init__(self, message):
    self.message = message

  def __str__(self):
    return self.message


def _cast_tensor_to_floatx(x):
  """Cast tensor to keras's floatx dtype if it is not already the same dtype."""
  if x.dtype == K.floatx():
    return x
  else:
    return tf.cast(x, K.floatx())


def _convert_tensor(x):
  """Create or cast tensor if needed."""
  if not tf.is_tensor(x):
    # x is a numpy array
    x = tf.compat.v1.convert_to_tensor_or_sparse_tensor(x)
  return x


def _any_weight_initialized(keras_model):
  """Check if any weights has been initialized in the Keras model.

  Args:
    keras_model: An instance of compiled keras model.

  Returns:
    boolean, True if at least one weight has been initialized, else False.
    Currently keras initialize all weights at get_session().
  """
  if keras_model is None:
    return False
  if ops.executing_eagerly_outside_functions():
    return True
  for layer in keras_model.layers:
    for weight in layer.weights:
      if hasattr(weight, '_keras_initialized'):
        return True
  return False


def _convert_estimator_io_to_keras(keras_model, features, labels):
  """Converts estimator features and labels to keras input and target tensors.

  Args:
    keras_model: a compiled `tf.keras.Model` instance, used to determine the
      order of the returned lists.
    features: Dict of tensors or `None`.
    labels: Dict of tensors, a single tensor, or `None`.

  Returns:
    Tuple of (
      list of input tensors or `None`,
      list of target tensors or `None`,
      list of sample weight tensors or `None`)
    The order of tensors is determined by the order set in the keras model.
  """

  def _to_ordered_tensor_list(obj, key_order, obj_name, order_name):
    """Convert obj to an ordered list of tensors.

    Args:
      obj: List, dict, or single tensor. May be `None`.
      key_order: List of strings with the order to return (used if obj is a
        dict).
      obj_name: String name of object (e.g. "features" or "labels")
      order_name: String name of the key order (e.g. "inputs" or "outputs")

    Returns:
      List of tensors, or `None`

    Raises:
      KeyError: If obj has invalid keys.
    """
    if obj is None:
      return None
    elif isinstance(obj, (list, tuple)):
      return [_convert_tensor(x) for x in obj]
    elif isinstance(obj, dict):
      # Ensure that keys in key_order are contained in obj keys.
      # One can provide more data keys described in obj, as long as the keys
      # requested by model are provided.
      different_keys = set(key_order) - set(obj.keys())

      if different_keys:
        raise FormattedKeyError(
            'The dictionary passed into {obj_name} does not cover requested '
            '{order_name} keys defined in the keras model.'
            '\n\tExpected keys: {order_keys}'
            '\n\t{obj_name} keys: {obj_keys}'
            '\n\tMissed keys: {different_keys}'.format(
                order_name=order_name,
                order_keys=set(key_order),
                obj_name=obj_name,
                obj_keys=set(obj.keys()),
                different_keys=different_keys))

      return [_convert_tensor(obj[key]) for key in key_order]
    else:  # Assume obj is a tensor.
      return [_convert_tensor(obj)]

  features, sample_weight_tensors = _extract_sample_weight_tensors(features)
  input_names = None
  output_names = None
  if isinstance(features, dict):
    input_names = (
        keras_model.input_names if keras_model._is_graph_network else
        ['input_%d' % i for i in range(1,
                                       len(features) + 1)])
  if isinstance(labels, dict):
    output_names = (
        keras_model.output_names if keras_model._is_graph_network else
        ['output_%d' % i for i in range(1,
                                        len(labels) + 1)])

  if isinstance(keras_model.inputs, dict):
    # Keep input tensors as a dict if keras_model is built with dict input.
    input_tensors = {
        k: _convert_tensor(features[k])
        for (k, v) in keras_model.inputs.items()
    }
  elif keras_model.inputs is None and isinstance(features, dict):
    # Keep input tensors as a dict if keras_model input structure is unknown.
    input_tensors = {k: _convert_tensor(v) for (k, v) in features.items()}
  else:
    # converting input tensors into sorted list.
    input_tensors = _to_ordered_tensor_list(features, input_names, 'features',
                                            'inputs')
  target_tensors = _to_ordered_tensor_list(labels, output_names, 'labels',
                                           'outputs')

  return input_tensors, target_tensors, sample_weight_tensors


def _extract_sample_weight_tensors(features):
  if isinstance(features, dict) and set(
      features.keys()) == {'features', 'sample_weights'}:
    feature_tensor = features['features']
    sample_weight_tensors = features['sample_weights']
  else:
    feature_tensor = features
    sample_weight_tensors = None
  return feature_tensor, sample_weight_tensors


def _clone_and_build_model(mode,
                           keras_model,
                           custom_objects,
                           features=None,
                           labels=None,
                           optimizer_config=None):
  """Clone and build the given keras_model.

  Args:
    mode: training mode.
    keras_model: an instance of compiled keras model.
    custom_objects: Dictionary for custom objects.
    features: Dict of tensors.
    labels: Dict of tensors, or single tensor instance.
    optimizer_config: Optimizer config dictionary, returned by
      `optimizer.get_config()`. This is used when cloning a model with an
      optimizer. Since `_clone_and_build_model` is called in a different graph
      and session from the model, `optimizer.get_config()` may raise an error
      during the attempt to serialize the optimizer hyperparameter values.

  Returns:
    The newly built model.
  """
  # Set to True during training, False for inference or testing.
  K.set_learning_phase(mode == ModeKeys.TRAIN)
  input_tensors, target_tensors, sample_weight_tensors = (
      _convert_estimator_io_to_keras(keras_model, features, labels))

  compile_clone = (mode != ModeKeys.PREDICT)

  global_step = None
  if compile_clone:
    # Set iterations to the global step created by tf.train.create_global_step()
    # which is automatically run in the estimator framework.
    global_step = tf.compat.v1.train.get_or_create_global_step()
    K.track_variable(global_step)

  clone = models.clone_and_build_model(
      keras_model,
      input_tensors,
      target_tensors,
      custom_objects,
      compile_clone=compile_clone,
      in_place_reset=(not keras_model._is_graph_network),
      optimizer_iterations=global_step,
      optimizer_config=optimizer_config)

  if sample_weight_tensors is not None:
    sample_weight_tensors = training_utils.standardize_sample_weights(
        sample_weight_tensors, clone.output_names)
    # Update calculated loss (model.total_loss) to include sample weights.
    clone._compile_weights_loss_and_weighted_metrics(sample_weight_tensors)
  return clone


def _convert_keras_metrics_to_estimator(model, metric_names_map=None):
  """Convert metrics from a Keras model to ops used by the Estimator framework.

  Args:
    model: A `tf.keras.Model` object.
    metric_names_map: Optional dictionary mapping Keras model output metric
      names to custom names.

  Returns:
    Dictionary mapping metric names to tuples of (value, update) ops. May return
    `None` if the model does not contain any metrics.
  """
  if not getattr(model, '_compile_metrics', None):
    return None

  # We are not using model.metrics here because we want to exclude the metrics
  # added using `add_metric` API.
  compiled_metrics = model._compile_metric_functions

  if metric_names_map:
    custom_map_keys = set(metric_names_map.keys())
    expected_keys = {m.name for m in compiled_metrics}
    unknown = expected_keys.difference(custom_map_keys)
    if unknown:
      raise ValueError(
          'Invalid `metric_names_map`. '
          'The following keras model metric names:"{}" do not exist in '
          'the `metric_names_map` dictionary'.format(list(unknown)))

    extra = custom_map_keys.difference(expected_keys)
    if extra:
      raise ValueError('Invalid `metric_names_map`. '
                       'There are unexpected keys in the `metric_names_map` '
                       'dictionary. Expected keys: {}, Received: {}'.format(
                           list(expected_keys), list(extra)))

    return {metric_names_map[m.name]: m for m in compiled_metrics}
  else:
    return {m.name: m for m in compiled_metrics}


def _create_keras_model_fn(keras_model,
                           custom_objects=None,
                           save_object_ckpt=False,
                           metric_names_map=None):
  """Creates model_fn for keras Estimator.

  Args:
    keras_model: an instance of compiled keras model.
    custom_objects: Dictionary for custom objects.
    save_object_ckpt: Whether to save an object-based checkpoint.
    metric_names_map: Optional dictionary mapping Keras model output metric
      names to custom names.

  Returns:
    The model_fn for a keras Estimator.
  """
  # Get optimizer config in the current context (since model_fn is called in the
  # estimator graph and session). OptimizerV2 objects serialize variable/tensor
  # hyperparameters in their configs, resulting to wrong-session errors during
  # model cloning.
  try:
    if isinstance(keras_model.optimizer, (tuple, list)):
      optimizer_config = [opt.get_config() for opt in keras_model.optimizer]
    else:
      optimizer_config = keras_model.optimizer.get_config()
  except (NotImplementedError, AttributeError):
    # TFOptimizers and other custom optimizers do not have a config.
    optimizer_config = None

  def model_fn(features, labels, mode):
    """model_fn for keras Estimator."""
    model = _clone_and_build_model(
        mode=mode,
        keras_model=keras_model,
        custom_objects=custom_objects,
        features=features,
        labels=labels,
        optimizer_config=optimizer_config)
    model_output_names = []
    # We need to make sure that the output names of the last layer in the model
    # is the same for each of the cloned models. This is required for mirrored
    # strategy when we call regroup.
    if tf.distribute.has_strategy():
      for name in model.output_names:
        name = re.compile(r'_\d$').sub('', name)
        model_output_names.append(name)
    else:
      model_output_names = model.output_names

    # Get inputs to EstimatorSpec
    predictions = dict(zip(model_output_names, model.outputs))

    loss = None
    train_op = None
    eval_metric_ops = None

    # Set loss and metric only during train and evaluate.
    if mode is not ModeKeys.PREDICT:
      if mode is ModeKeys.TRAIN:
        model._make_train_function()  # pylint: disable=protected-access
      else:
        model._make_test_function()  # pylint: disable=protected-access
      loss = model.total_loss

      eval_metric_ops = _convert_keras_metrics_to_estimator(
          model, metric_names_map)

    # Set train_op only during train.
    if mode is ModeKeys.TRAIN:
      train_op = model.train_function.updates_op

    if (not model._is_graph_network and
        hasattr(keras_model, '_original_attributes_cache') and
        keras_model._original_attributes_cache is not None):
      # To avoid `model_fn` being destructive for the initial model argument.
      models.in_place_subclassed_model_state_restoration(keras_model)

    scaffold = None
    if save_object_ckpt:
      model._track_trackable(tf.compat.v1.train.get_global_step(),
                             'estimator_global_step')
      # Create saver that maps variable names to object-checkpoint keys.
      object_graph = graph_view.ObjectGraphView(model)
      var_list = object_graph.frozen_saveable_objects()
      saver = tf.compat.v1.train.Saver(var_list=var_list, sharded=True)
      saver._object_restore_saver = trackable_util.frozen_saver(model)
      scaffold = tf.compat.v1.train.Scaffold(saver=saver)

    return model_fn_lib.EstimatorSpec(
        mode=mode,
        predictions=predictions,
        loss=loss,
        train_op=train_op,
        eval_metric_ops=eval_metric_ops,
        export_outputs={
            _DEFAULT_SERVING_KEY: export_lib.PredictOutput(predictions)
        },
        scaffold=scaffold)

  return model_fn


def _save_first_checkpoint(keras_model, custom_objects, config,
                           save_object_ckpt):
  """Save first checkpoint for the keras Estimator.

  Args:
    keras_model: an instance of compiled keras model.
    custom_objects: Dictionary for custom objects.
    config: Estimator config.
    save_object_ckpt: Whether to save an object-based checkpoint.

  Returns:
    The path where keras model checkpoint is saved.
  """
  # save checkpoint into subdirectory to allow warm start
  keras_model_dir = os.path.join(config.model_dir, 'keras')
  # Load weights and save to checkpoint if there is no checkpoint
  latest_path = tf.train.latest_checkpoint(keras_model_dir)
  if not latest_path:
    keras_weights = None
    if _any_weight_initialized(keras_model):
      keras_weights = keras_model.get_weights()
    if not tf.compat.v1.gfile.IsDirectory(keras_model_dir):
      tf.compat.v1.gfile.MakeDirs(keras_model_dir)
    with tf.Graph().as_default():
      tf.compat.v1.random.set_random_seed(config.tf_random_seed)
      tf.compat.v1.train.create_global_step()
      model = _clone_and_build_model(ModeKeys.TRAIN, keras_model,
                                     custom_objects)

      # Init the train_function outside of the context of session. This is due
      # to the fact that train function will update the graph by adding backprop
      # parts. This will potentially trying to update the node in forward graph
      # which will fail if it is done within same session.
      # Always create the train_function here since the model is just cloned.
      # See https://github.com/tensorflow/tensorflow/issues/27750 for details.
      model._make_train_function()  # pylint: disable=protected-access

      # save to checkpoint
      with tf.compat.v1.Session(config=config.session_config) as sess:
        if keras_weights:
          model.set_weights(keras_weights)
        # model._make_train_function() will potentially create the optimizer
        # variable, which will require another variable initialization.
        K._initialize_variables(sess)  # pylint: disable=protected-access

        if save_object_ckpt:
          model._track_trackable(  # pylint: disable=protected-access
              tf.compat.v1.train.get_global_step(), 'estimator_global_step')
          latest_path = os.path.join(keras_model_dir, 'keras_model.ckpt')
          model.save_weights(latest_path)
        else:
          saver = tf.compat.v1.train.Saver()
          latest_path = os.path.join(keras_model_dir, 'keras_model.ckpt')
          saver.save(sess, latest_path)

  return latest_path


def _get_file_from_google_storage(keras_model_path, model_dir):
  """Get file from google storage and download to local file.

  Args:
    keras_model_path: a google storage path for compiled keras model.
    model_dir: the directory from estimator config.

  Returns:
    The path where keras model is saved.

  Raises:
    ValueError: if storage object name does not end with .h5.
  """
  try:
    from google.cloud import storage  # pylint:disable=g-import-not-at-top
  except ImportError:
    raise TypeError('Could not save model to Google cloud storage; please '
                    'install `google-cloud-storage` via '
                    '`pip install google-cloud-storage`.')
  storage_client = storage.Client()
  path, blob_name = os.path.split(keras_model_path)
  _, bucket_name = os.path.split(path)
  keras_model_dir = os.path.join(model_dir, 'keras')
  if not tf.compat.v1.gfile.Exists(keras_model_dir):
    tf.compat.v1.gfile.MakeDirs(keras_model_dir)
  file_name = os.path.join(keras_model_dir, 'keras_model.h5')
  try:
    blob = storage_client.get_bucket(bucket_name).blob(blob_name)
    blob.download_to_filename(file_name)
  except:
    raise ValueError('Failed to download keras model, please check '
                     'environment variable GOOGLE_APPLICATION_CREDENTIALS '
                     'and model path storage.googleapis.com/{bucket}/{object}.')
  tf.compat.v1.logging.info('Saving model to {}'.format(file_name))
  del storage_client
  return file_name


# LINT.IfChange
# TODO(b/139699640): let model_to_estimator only rely on public Keras APIs.
def model_to_estimator(keras_model=None,
                       keras_model_path=None,
                       custom_objects=None,
                       model_dir=None,
                       config=None,
                       checkpoint_format=None,
                       use_v2_estimator=False,
                       metric_names_map=None):
  # LINT.ThenChange(//tensorflow/python/keras/estimator/__init__.py)
  """Constructs an `Estimator` instance from given keras model.

  If you use infrastructure or other tooling that relies on Estimators, you can
  still build a Keras model and use model_to_estimator to convert the Keras
  model to an Estimator for use with downstream systems.

  For usage example, please see:
  [Creating estimators from Keras
  Models](https://www.tensorflow.org/guide/estimators#creating_estimators_from_keras_models).

  Sample Weights:
  Estimators returned by `model_to_estimator` are configured so that they can
  handle sample weights (similar to `keras_model.fit(x, y, sample_weights)`).

  To pass sample weights when training or evaluating the Estimator, the first
  item returned by the input function should be a dictionary with keys
  `features` and `sample_weights`. Example below:

  ```python
  keras_model = tf.keras.Model(...)
  keras_model.compile(...)

  estimator = tf.keras.estimator.model_to_estimator(keras_model)

  def input_fn():
    return dataset_ops.Dataset.from_tensors(
        ({'features': features, 'sample_weights': sample_weights},
         targets))

  estimator.train(input_fn, steps=1)
  ```
  
  Note: We do not support creating weighted metrics in Keras and converting them
  to weighted metrics in the Estimator API using `model_to_estimator`.
  You will have to create these metrics directly on the estimator spec using the
  `add_metrics` function.

  Args:
    keras_model: A compiled Keras model object. This argument is mutually
      exclusive with `keras_model_path`. Estimator's `model_fn` uses the
      structure of the model to clone the model. Defaults to `None`.
    keras_model_path: Path to a compiled Keras model saved on disk, in HDF5
      format, which can be generated with the `save()` method of a Keras model.
      This argument is mutually exclusive with `keras_model`.
      Defaults to `None`.
    custom_objects: Dictionary for cloning customized objects. This is
      used with classes that is not part of this pip package. For example, if
      user maintains a `relu6` class that inherits from `tf.keras.layers.Layer`,
      then pass `custom_objects={'relu6': relu6}`. Defaults to `None`.
    model_dir: Directory to save `Estimator` model parameters, graph, summary
      files for TensorBoard, etc. If unset a directory will be created with
      `tempfile.mkdtemp`
    config: `RunConfig` to config `Estimator`. Allows setting up things in
      `model_fn` based on configuration such as `num_ps_replicas`, or
      `model_dir`. Defaults to `None`. If both `config.model_dir` and the
      `model_dir` argument (above) are specified the `model_dir` **argument**
      takes precedence.
    checkpoint_format: Sets the format of the checkpoint saved by the estimator
      when training. May be `saver` or `checkpoint`, depending on whether to
      save checkpoints from `tf.compat.v1.train.Saver` or `tf.train.Checkpoint`.
      The default is `checkpoint`. Estimators use name-based `tf.train.Saver`
      checkpoints, while Keras models use object-based checkpoints from
      `tf.train.Checkpoint`. Currently, saving object-based checkpoints from
      `model_to_estimator` is only supported by Functional and Sequential
      models.
    use_v2_estimator: Whether to convert the model to a V2 Estimator or V1
      Estimator. Defaults to `False`.
    metric_names_map: Optional dictionary mapping Keras model output metric
      names to custom names. This can be used to override the default Keras
      model output metrics names in a multi IO model use case and provide custom
      names for the `eval_metric_ops` in Estimator.
      The Keras model metric names can be obtained using `model.metrics_names`
      excluding any loss metrics such as total loss and output losses.
      For example, if your Keras model has two outputs `out_1` and `out_2`,
      with `mse` loss and `acc` metric, then `model.metrics_names` will be
      `['loss', 'out_1_loss', 'out_2_loss', 'out_1_acc', 'out_2_acc']`.
      The model metric names excluding the loss metrics will be
      `['out_1_acc', 'out_2_acc']`.

  Returns:
    An Estimator from given keras model.

  Raises:
    ValueError: If neither keras_model nor keras_model_path was given.
    ValueError: If both keras_model and keras_model_path was given.
    ValueError: If the keras_model_path is a GCS URI.
    ValueError: If keras_model has not been compiled.
    ValueError: If an invalid checkpoint_format was given.
  """

  if not (keras_model or keras_model_path):
    raise ValueError(
        'Either `keras_model` or `keras_model_path` needs to be provided.')
  if keras_model and keras_model_path:
    raise ValueError(
        'Please specity either `keras_model` or `keras_model_path`, '
        'but not both.')

  if keras_model:
    _assert_valid_model(keras_model, custom_objects)

  config = estimator_lib.maybe_overwrite_model_dir_and_session_config(
      config, model_dir)
  if not keras_model:
    if keras_model_path.startswith(
        'gs://') or 'storage.googleapis.com' in keras_model_path:
      keras_model_path = _get_file_from_google_storage(keras_model_path,
                                                       config.model_dir)
    tf.compat.v1.logging.info('Loading models from %s', keras_model_path)
    keras_model = models.load_model(keras_model_path)
  else:
    tf.compat.v1.logging.info('Using the Keras model provided.')
    keras_model = keras_model

  if checkpoint_format is None or checkpoint_format == 'checkpoint':
    if not (keras_model._is_graph_network or
            isinstance(keras_model, models.Sequential)):
      raise ValueError('Object-based checkpoints are currently not supported '
                       'with subclassed models.')
    save_object_ckpt = True
  elif checkpoint_format == 'saver':
    save_object_ckpt = False
  else:
    raise ValueError(
        'Checkpoint format must be one of "checkpoint" or "saver". Got {}'
        .format(checkpoint_format))

  if not hasattr(keras_model, 'optimizer') or not keras_model.optimizer:
    raise ValueError('The given keras model has not been compiled yet. '
                     'Please compile the model with `model.compile()` '
                     'before calling `model_to_estimator()`.')

  keras_model_fn = _create_keras_model_fn(keras_model, custom_objects,
                                          save_object_ckpt, metric_names_map)
  if _any_weight_initialized(keras_model):
    # Warn if config passed to estimator tries to update GPUOptions. If a
    # session has already been created, the GPUOptions passed to the first
    # session sticks.
    if config.session_config.HasField('gpu_options'):
      tf.compat.v1.logging.warn(
          'The Keras backend session has already been set. '
          'The _session_config passed to model_to_estimator will not be used.')
  else:
    # Pass the config into keras backend's default session.
    sess = tf.compat.v1.Session(config=config.session_config)
    K.set_session(sess)

  warm_start_path = None
  if keras_model._is_graph_network and config.is_chief:
    warm_start_path = _save_first_checkpoint(keras_model, custom_objects,
                                             config, save_object_ckpt)
  elif keras_model.built:
    tf.compat.v1.logging.warn(
        'You are creating an Estimator from a Keras model manually '
        'subclassed from `Model`, that was already called on some '
        'inputs (and thus already had weights). We are currently '
        'unable to preserve the model\'s state (its weights) as '
        'part of the estimator in this case. Be warned that the '
        'estimator has been created using a freshly initialized '
        'version of your model.\n'
        'Note that this doesn\'t affect the state of the model '
        'instance you passed as `keras_model` argument.')
  if use_v2_estimator:
    estimator_cls = estimator_lib.EstimatorV2
  else:
    estimator_cls = estimator_lib.Estimator

  estimator = estimator_cls(
      keras_model_fn, config=config, warm_start_from=warm_start_path)

  return estimator


def _assert_valid_model(model, custom_objects=None):
  is_subclass = (not model._is_graph_network and
                 not isinstance(model, models.Sequential))
  if is_subclass:
    try:
      custom_objects = custom_objects or {}
      with tf.keras.utils.CustomObjectScope(custom_objects):
        model.__class__.from_config(model.get_config())
    except NotImplementedError:
      raise ValueError(
          'Subclassed `Model`s passed to `model_to_estimator` must '
          'implement `Model.get_config` and `Model.from_config`.')
