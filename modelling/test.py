
import numpy as np
import sys
import argparse
import pickle  # for handling the new data source
import h5py  # for saving the model
import keras
import tensorflow as tf
from keras.layers import Conv2D, MaxPooling2D, AveragePooling2D, Dropout, Flatten, Dense, Input
from keras.layers.merge import concatenate
from keras.layers.normalization import BatchNormalization
from keras.layers.advanced_activations import LeakyReLU
from keras.models import Model
from datetime import datetime  # for filename conventions

from tensorflow.python.lib.io import file_io  # for better file I/O


def multiinput_generator(full, med, low, label):
    '''custom generator to be passed to main training
       note samplewise std normalization + batch size
    '''
    while True:
        # shuffled indices
        idx = np.random.permutation(full.shape[0])
        # create image generator
        datagen = ImageDataGenerator(
                featurewise_center=False,  # set input mean to 0 over the dataset
                samplewise_center=False,  # set each sample mean to 0
                featurewise_std_normalization=False,  # divide inputs by std of the dataset
                samplewise_std_normalization=True,  # divide each input by its std
                zca_whitening=False)  # randomly flip images
        batches = datagen.flow(full[idx], label[idx], batch_size=16, shuffle=False)
        idx0 = 0
        for batch in batches:
            idx1 = idx0 + batch[0].shape[0]
            yield [batch[0], med[idx[idx0:idx1]], low[idx[idx0:idx1]]], batch[1]
            idx0 = idx1
            if idx1 >= full.shape[0]:
                break


def resizer(arrays, size, method):
    return tf.map_fn(lambda array: 
                     tf.image.resize_images(array,
                                            [size, size],
                                            method=method), 
                     arrays)


def singleres_to_multires(arrays, size1=64, size2=32, 
                          method=tf.image.ResizeMethod.BILINEAR):
    with tf.Session() as session:
        size1_arrays = resizer(arrays, size1, method).eval()
        size2_arrays = resizer(arrays, size2, method).eval()
    return [arrays, size1_arrays, size2_arrays]


def load_multires(images, labels):
    images_reshape = reshape(images)
    multires_images = singleres_to_multires(images)
    return multires_images, labels


def get_input_shape(data):
    num_samples = data.shape[0]
    channels = 3
    img_rows = data.shape[2]
    img_cols = data.shape[3]
    return (num_samples, img_rows, img_cols, channels)


def reshape(data):
    return np.reshape(data, get_input_shape(data))


def train_test_split(array, proportion=0.8):
    '''non randomised train split
    '''
    index = int(len(array) * proportion)
    index = 10
    train = array[:index]
    test = array[index:]
    return train, test


def radian_to_angle(radian_array):
    '''converts original radian to angle which
       will be error metric
    '''
    return (radian_array * 180 / np.pi) - 90


def reverse_mean_std(standardized_array, prev_mean, prev_std):
    '''undo transformation in order to calculate
       angle loss
    '''
    de_std = standardized_array * prev_std
    de_mean = de_std + prev_mean
    return de_mean


def generator_train(images, labels):
    '''main entry point
       calls customised  multiinput generator
       and tests angle loss
    '''
    multires_data, labels = load_multires(images, labels)
    multires_data = [x.astype('float32') for x in multires_data]
    multires_data = [x / 255 for x in multires_data]
    model = multires_CNN(16, 5, multires_data)
    full = multires_data[0]
    med = multires_data[1]
    low = multires_data[2]
    train_full, test_full = train_test_split(full)
    train_med, test_med = train_test_split(med)
    train_low, test_low = train_test_split(low)
    labels_angles = radian_to_angle(labels)
    train_orig_lab, test_orig_lab = train_test_split(labels_angles)
    labels_standardised, mean_, std_ = mean_std_norm(labels_angles)
    train_labels, test_labels = train_test_split(labels_standardised)
    model.fit_generator(multiinput_generator(train_full, train_med, train_low, train_labels),
                        steps_per_epoch=16,
                        epochs=50)
    return model, test_full, test_med, test_low, test_labels


def calculate_error(model, test_full, test_med, test_low, test_labels):
    std_angles = model.predict([test_full, test_med, test_low])
    unstd_angles = reverse_mean_std(std_angles, mean_, std_)
    error = unstd_angles - test_labels
    mean_error_elevation = np.mean(abs(error[:, 0]))
    mean_error_zenith = np.mean(abs(error[:, 1]))
    print(mean_error_zenith)
    print(mean_error_zenith)
    return mean_error_elevation, mean_error_zenith


def mean_std_norm(array):
    '''standardization for labels
    '''
    mean_ = mean(array)
    std_ = std(array)
    standardized = (array - mean_) / std_
    return standardized, mean_, std_


def multires_CNN(filters, kernel_size, multires_data):
    '''uses Functional API for Keras 2.x support.
       multires data is output from load_standardized_multires()
    '''
    input_fullres = Input(multires_data[0].shape[1:], name = 'input_fullres')
    fullres_branch = Conv2D(filters, (kernel_size, kernel_size),
                     activation = LeakyReLU())(input_fullres)
    fullres_branch = MaxPooling2D(pool_size = (2,2))(fullres_branch)
    fullres_branch = BatchNormalization()(fullres_branch)
    fullres_branch = Flatten()(fullres_branch)

    input_medres = Input(multires_data[1].shape[1:], name = 'input_medres')
    medres_branch = Conv2D(filters, (kernel_size, kernel_size),
                     activation=LeakyReLU())(input_medres)
    medres_branch = MaxPooling2D(pool_size = (2,2))(medres_branch)
    medres_branch = BatchNormalization()(medres_branch)
    medres_branch = Flatten()(medres_branch)

    input_lowres = Input(multires_data[2].shape[1:], name = 'input_lowres')
    lowres_branch = Conv2D(filters, (kernel_size, kernel_size),
                     activation = LeakyReLU())(input_lowres)
    lowres_branch = MaxPooling2D(pool_size = (2,2))(lowres_branch)
    lowres_branch = BatchNormalization()(lowres_branch)
    lowres_branch = Flatten()(lowres_branch)

    merged_branches = concatenate([fullres_branch, medres_branch, lowres_branch])
    merged_branches = Dense(128, activation=LeakyReLU())(merged_branches)
    merged_branches = Dropout(0.5)(merged_branches)
    merged_branches = Dense(2,activation='linear')(merged_branches)

    model = Model(inputs=[input_fullres, input_medres ,input_lowres],
                  outputs=[merged_branches])
    model.compile(loss='mean_absolute_error', optimizer='adam')

    return model


def train_model():

    images = np.load('AllImages.npy')
    #labels_ = np.load(labelsio)
    #labels = labels_[:500]
    labels = np.load('AllAngles.npy')

    model, test_full, test_med, test_low, test_labels = generator_train(images, labels)

    error = calculate_error(model, test_full, test_med, test_low, test_labels)
    #file_stream_images = file_io.FileIO(train_files+'/AllImages.npy', mode='r')
    #file_stream_labels = file_io.FileIO(train_files+'/AllAngles.npy', mode='r')
    #images = np.load(file_stream_images)
    #labels = np.load(file_stream_labels)



if __name__ == '__main__':
    train_model()

