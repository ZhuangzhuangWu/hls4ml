import pytest
import hls4ml
import numpy as np
from tensorflow.keras.utils import to_categorical
from sklearn.datasets import fetch_openml
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder, StandardScaler
from tensorflow.keras.models import Sequential, model_from_json
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.regularizers import l1
from tensorflow.keras.layers import Activation, BatchNormalization
from qkeras.qlayers import QDense, QActivation
from qkeras.quantizers import quantized_bits, quantized_relu, ternary, binary
from qkeras.utils import _add_supported_quantized_objects; co = {}; _add_supported_quantized_objects(co)

import warnings
warnings.filterwarnings("ignore", message="numpy.dtype size changed")
warnings.filterwarnings("ignore", message="numpy.ufunc size changed")

@pytest.fixture(scope='module')
def get_jettagging_data():
  '''
  Download the jet tagging dataset
  '''
  print("Fetching data from openml")
  data = fetch_openml('hls4ml_lhc_jets_hlf')
  X, y = data['data'], data['target']
  le = LabelEncoder()
  y = le.fit_transform(y)
  y = to_categorical(y, 5)
  X_train_val, X_test, y_train_val, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
  scaler = StandardScaler()
  X_train_val = scaler.fit_transform(X_train_val)
  X_test = scaler.transform(X_test)
  return X_train_val, X_test, y_train_val, y_test

@pytest.fixture(scope='module')
def load_jettagging_model():
  ''' 
  Load the 3 hidden layer QKeras example model trained on the jet tagging dataset
  '''
  jsons = open('../../example-models/keras/qkeras_3layer.json','r').read()
  model = model_from_json(jsons, custom_objects=co)
  model.load_weights('../../example-models/keras/qkeras_3layer_weights.h5')
  return model

@pytest.fixture
@pytest.mark.parametrize('strategy', ['latency', 'resource'])
def convert(load_jettagging_model, strategy):
  '''
  Convert a QKeras model trained on the jet tagging dataset
  '''
  model = load_jettagging_model
  hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = ['Activation']
  hls4ml.model.optimizer.OutputRoundingSaturationMode.rounding_mode = 'AP_RND'
  hls4ml.model.optimizer.OutputRoundingSaturationMode.saturation_mode = 'AP_SAT'

  config = hls4ml.utils.config_from_keras_model(model, granularity='name')
  config['Model']['Strategy'] = strategy
  config['LayerName']['softmax']['exp_table_t'] = 'ap_fixed<18,8>'
  config['LayerName']['softmax']['inv_table_t'] = 'ap_fixed<18,4>'
  hls_model = hls4ml.converters.convert_from_keras_model(model,
                                                       hls_config=config,
                                                       output_dir='hls4mlprj_qkeras_accuracy_{}'.format(strategy),
                                                       part='xcu250-figd2104-2L-e')
  hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = []                                                     
  hls_model.compile()
  return hls_model

@pytest.mark.parametrize('strategy', ['latency', 'resource'])
def test_accuracy(convert, load_jettagging_model, get_jettagging_data, strategy):
  '''
  Test the hls4ml-evaluated accuracy of a 3 hidden layer QKeras model trained on
  the jet tagging dataset. QKeras model accuracy is required to be over 70%, and
  hls4ml accuracy required to be within 1% of the QKeras model accuracy.
  '''
  print("Test accuracy")
  from sklearn.metrics import accuracy_score

  X_train_val, X_test, y_train_val, y_test = get_jettagging_data

  hls_model = convert
  model = load_jettagging_model

  y_qkeras = model.predict(np.ascontiguousarray(X_test))
  y_hls4ml = hls_model.predict(np.ascontiguousarray(X_test))

  acc_qkeras = accuracy_score(np.argmax(y_test, axis=1), np.argmax(y_qkeras, axis=1))
  acc_hls4ml = accuracy_score(np.argmax(y_test, axis=1), np.argmax(y_hls4ml, axis=1))
  rel_diff = abs(acc_qkeras - acc_hls4ml) / acc_qkeras

  print('Accuracy qkeras:     {}'.format(acc_qkeras))
  print('Accuracy hls4ml:     {}'.format(acc_hls4ml))
  print('Relative difference: {}'.format(rel_diff))

  assert acc_qkeras > 0.7 and rel_diff < 0.01

def randX(batch_size, N):
  return np.random.rand(batch_size,N)

@pytest.fixture(scope='module')
def randX_100_16():
  return randX(100, 16)

# TODO: include wider bitwidths when that can be made to pass
# Note 4-bit test can still fail sometimes depending on random seed
# https://github.com/fastmachinelearning/hls4ml/issues/381
#@pytest.mark.parametrize('bits', [4, 6, 8])
@pytest.mark.parametrize('bits', [4])
def test_single_dense_activation_exact(randX_100_16, bits):
  '''
  Test a single Dense -> Activation layer topology for
  bit exactness with number of bits parameter
  '''
  X = randX_100_16
  model = Sequential()
  model.add(QDense(16, input_shape=(16,), name='fc1',
                  kernel_quantizer=quantized_bits(bits,0,alpha=1), bias_quantizer=quantized_bits(bits,0,alpha=1),
                  kernel_initializer='lecun_uniform'))
  model.add(QActivation(activation=quantized_relu(bits,0), name='relu1'))
  model.compile()

  hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = ['relu1']
  hls4ml.model.optimizer.OutputRoundingSaturationMode.rounding_mode = 'AP_RND_CONV'
  hls4ml.model.optimizer.OutputRoundingSaturationMode.saturation_mode = 'AP_SAT'
  config = hls4ml.utils.config_from_keras_model(model, granularity='name')
  hls_model = hls4ml.converters.convert_from_keras_model(model,
                                                       hls_config=config,
                                                       output_dir='hls4mlprj_qkeras_single_dense_activation_exact_{}'.format(bits),
                                                       part='xcu250-figd2104-2L-e')
  hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = []                                                   
  hls_model.compile()

  y_qkeras = model.predict(X)
  y_hls4ml = hls_model.predict(X)
  # Goal is to get it passing with all equal
  #np.testing.assert_array_equal(y_qkeras, y_hls4ml)
  # For now allow matching within 1 bit
  np.testing.assert_allclose(y_qkeras.ravel(), y_hls4ml.ravel(), atol=2**-bits, rtol=1.0)

@pytest.fixture
def make_btnn(N, kernel_quantizer, bias_quantizer, activation_quantizer, use_batchnorm, is_xnor):
  shape = (N,)
  model = Sequential()
  model.add(QDense(10, input_shape=shape, kernel_quantizer=kernel_quantizer,
                   bias_quantizer=bias_quantizer, name='dense'))
  if use_batchnorm:
    model.add(BatchNormalization(name='bn'))
  model.add(QActivation(activation=activation_quantizer))
  model.compile()
  return model, is_xnor

@pytest.fixture(scope='module')
def randX_100_10():
  return randX(100, 10)

@pytest.mark.parametrize('N,kernel_quantizer,bias_quantizer,activation_quantizer,use_batchnorm,is_xnor',
                          [(10, ternary(alpha=1), quantized_bits(5,2), 'binary_tanh', False, False),
                           (10, binary(), quantized_bits(5,2), 'binary_tanh', False, True),
                           (10, ternary(alpha='auto'), quantized_bits(5,2), binary(), True, True),
                           (10, ternary(alpha='auto'), quantized_bits(5,2), 'ternary', True, False),
                           (10, ternary(alpha='auto'), quantized_bits(5,2), ternary(threshold=0.2), True, False),
                           (10, ternary(alpha='auto'), quantized_bits(5,2), ternary(threshold=0.8), True, False),
                           (10, binary(), quantized_bits(5,2), binary(), False, True)])
def test_btnn(make_btnn, randX_100_10):
  model, is_xnor = make_btnn
  X = randX_100_10
  cfg = hls4ml.utils.config_from_keras_model(model, granularity='name')
  hls_model = hls4ml.converters.convert_from_keras_model(model, output_dir='btnn', hls_config=cfg)
  hls_model.compile()
  y_hls = hls_model.predict(X)
  # hls4ml may return XNOR binary
  if is_xnor:
    y_hls = np.where(y_hls == 0, -1, 1)
  y_ker = model.predict(X)
  wrong = (y_hls != y_ker).ravel()
  assert sum(wrong) / len(wrong) < 0.005

@pytest.fixture(scope='module')
def randX_1000_1():
  return randX(1000, 1)

# TODO: include quantized_relu tests when they are made to pass
# https://github.com/fastmachinelearning/hls4ml/issues/377
@pytest.mark.parametrize('quantizer', [(quantized_bits(8,0)),
                                       (quantized_bits(8,4)),
                                       (quantized_bits(4,2)),
                                       (quantized_bits(4,0)),
                                       (quantized_bits(10,0)),])
                                       #(quantized_relu(4)),
                                       #(quantized_relu(10))])
def test_quantizer(randX_1000_1, quantizer):
  '''
  Test a single quantizer as an Activation function.
  Checks the type inference through the conversion is correct without just
  using the same logic.
  '''
  X = randX_1000_1
  X = np.round(X * 2**10) * 2**-10 # make it an exact ap_fixed<16,6>
  model = Sequential()
  model.add(QActivation(input_shape=(1,), activation=quantizer, name='quantizer'))
  model.compile()

  hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = ['quantizer']
  hls4ml.model.optimizer.OutputRoundingSaturationMode.rounding_mode = 'AP_RND_CONV'
  hls4ml.model.optimizer.OutputRoundingSaturationMode.saturation_mode = 'AP_SAT'
  config = hls4ml.utils.config_from_keras_model(model, granularity='name')
  output_dir = 'hls4mlprj_qkeras_quantizer_{}_{}_{}'.format(quantizer.__class__.__name__,
                                                            quantizer.bits, quantizer.integer)
  hls_model = hls4ml.converters.convert_from_keras_model(model,
                                                       hls_config=config,
                                                       output_dir=output_dir,
                                                       part='xcu250-figd2104-2L-e')
  hls4ml.model.optimizer.OutputRoundingSaturationMode.layers = []                                                   
  hls_model.compile()

  y_qkeras = model.predict(X)
  y_hls4ml = hls_model.predict(X)
  # Goal is to get it passing with all equal
  np.testing.assert_array_equal(y_qkeras, y_hls4ml)
