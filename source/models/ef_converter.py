import logging, sys, time, json
import numpy as np
from itertools import chain
import multiprocessing as mp
import matplotlib.pyplot as plt
from pathlib import Path

import tensorflow as tf
from tensorflow.keras import Model
from tensorflow.keras.layers import *
from tensorflow.keras.optimizers import Adam
from tensorflow.keras import losses

import sklearn.metrics as sk_metrics


import source.utils as utils

from source.constants import ROOT_LOGGER_STR

# get the logger
logger = logging.getLogger(ROOT_LOGGER_STR + '.' + __name__)

LEOMED_RUN = "--leomed" in sys.argv # True if running on LEOMED

# ------------------------------------------------------- Generators --------------------------------------------------------
class EF_converter_FFNN(Layer):

    def __init__(self, dense_layers, name='EF_converter_FFNN', **kwargs):
        super(EF_converter_FFNN, self).__init__(name=name, **kwargs)

        # make sure the last dense layer outputs one value
        assert dense_layers[-1]['units'] == 1

        layers = []
        for dense_layer in dense_layers:
            act_str = dense_layer['activation']['name'].lower() # get activation in lowercase
            if act_str == 'leakyrelu':
                dense_layer['activation'] = LeakyReLU(alpha=dense_layer['activation']['alpha'])
            if act_str == 'prelu':
                dense_layer['activation'] = PReLU()
            elif act_str == 'linear':
                dense_layer['activation'] = None
            elif act_str == 'relu':
                dense_layer['activation'] = 'relu'
            elif act_str == 'sigmoid':
                dense_layer['activation'] = 'sigmoid'
            dense = Dense(**dense_layer)
            layers.append(dense)

        self.dense_layers = layers

    def call(self, inputs):
        """
        inputs is expected to be of shape [batch_size, 1]
        """
        h = inputs # inputs are the 2d efs
        
        for dense in self.dense_layers:
            h = dense(h)

        return h
# ---------------------------------------------------------------------------------------------------------------------------

class EF_converter(Model):

    def __init__(self, log_dir, training_params, model_params, save_metrics=True, name='EF_converter', **kwargs):

        super(EF_converter, self).__init__(name=name, **kwargs)

        # save experiment path in self
        self.log_dir = log_dir # current experiment log dir (datetime dir)
        
        # save training and model parameters from config file in self
        self.training_params = training_params
        self.model_params = model_params

        # build the Mesh EF Predictor model
        self.build_model(model_params)

        if save_metrics:
            self.create_metrics_writers()

        logger.info("Model initialized and built.")

    def build_model(self, model_params):
        self.ef_ffnn = EF_converter_FFNN(dense_layers=model_params['dense_layers'])
    
    def create_metrics_writers(self):
        
        train_metrics = ['loss', 'learning_rate']
        val_metrics = ['loss']

        self._val_metrics = dict()
        self._train_metrics = dict()

        train_log_dir = self.log_dir / 'metrics' / 'train'
        val_log_dir = self.log_dir / 'metrics' / 'validation'
        self._train_summary_writer = tf.summary.create_file_writer(str(train_log_dir))
        self._val_summary_writer = tf.summary.create_file_writer(str(val_log_dir))

        for metric in train_metrics:
            self._train_metrics[metric] = tf.keras.metrics.Mean(metric)

        for metric in val_metrics:
            self._val_metrics[metric] = tf.keras.metrics.Mean(metric)
    
    @tf.function
    def call(self, efs_2d):
        return self.ef_ffnn(efs_2d)

    # --------------------------------------------------- Computing losses ----------------------------------------------

    def mse(self, y_true, y_pred):
        mse = tf.reduce_mean(losses.mean_squared_error(y_true, y_pred))
        return mse
    
    def mae(self, y_true, y_pred):
        mae = tf.reduce_mean(losses.mean_absolute_error(y_true, y_pred))
        return mae

    def _loss(self, y_true, y_pred):

        return self.mse(y_true, y_pred)
    # -------------------------------------------------------------------------------------------------------------------
    
    @tf.function
    def _train_step(self, true_efs_2d, true_efs_3d):
        with tf.GradientTape() as tape:
            # run model end-to-end in train mode:
            pred_efs_3d = self.call(true_efs_2d)
            # compute loss
            loss = self._loss(true_efs_3d, pred_efs_3d)
        
        # update model weights
        variables = self.trainable_variables
        grads = tape.gradient(loss, variables)
        self.optimizer.apply_gradients(zip(grads, variables))

        return {"loss": loss}
    
    def _val_step(self, true_efs_2d_v, true_efs_3d_v):
        # run model end-to-end in non-training mode
        pred_efs_3d_v = self.call(true_efs_2d_v)
        # compute loss
        loss = self._loss(true_efs_3d_v, pred_efs_3d_v)

        return loss
    
    def fit(self, train_dataset, train_plotting_dataset, val_dataset, test_dataset):

        opt_early_stopping_metric = np.inf
        count = epoch = epoch_step = global_step = 0
        opt_weights = self.get_weights()

        optimizer_params = self.training_params['optimizer']
        patience = self.training_params['patience']
        learning_rate_decay = self.training_params['decay_rate']
        lr = optimizer_params['learning_rate']

        # create optimizer
        self.optimizer = Adam(**optimizer_params)

        num_steps = self.training_params['num_steps']
        max_epochs = self.training_params['num_epochs']

        # log number of trainable weights before training
        logger.info("Start training...\n")

        logged_weights = False

        t1_steps = time.time() # start epoch timer
        
        self.epoch = epoch # save epoch nb in self

        train_plotting_counter = 0

        for true_efs_2d, true_efs_3d in train_dataset:
            losses = self._train_step(true_efs_2d, true_efs_3d)
            self._log_train_metrics(losses)

            if not logged_weights:
                self.log_weights()
                logged_weights = True
            
            global_step += 1 # increment nb total of steps

            if global_step % num_steps == 0:
                self._train_metrics['learning_rate'](lr) # log learning rate with train metrics

                # stop "steps timer" log train metrics and log "steps time"
                t2_steps = time.time()
                h, m, s = utils.get_HH_MM_SS_from_sec(t2_steps - t1_steps)
                self.write_summaries(self._train_summary_writer, self._train_metrics, global_step, "Train")
                logger.info("{} steps done in {}:{}:{}\n".format(num_steps, h, m, s))

                # Validation at the end of every epoch.
                logger.info("Computing validation loss...")
                t1_val = time.time() # start validation timer

                for true_efs_2d_v, true_efs_3d_v in val_dataset:
                    # perform a validation step and log metrics
                    loss = self._val_step(true_efs_2d_v, true_efs_3d_v)
                    self._val_metrics['loss'](loss)
                
                # stop validation timer, log validation metrics and log epoch time
                t2_val = time.time()
                h, m, s = utils.get_HH_MM_SS_from_sec(t2_val - t1_val)
                self.write_summaries(self._val_summary_writer, self._val_metrics, global_step, "Validation")
                logger.info("Validation done in {}:{}:{}\n".format(h, m, s))

                # get validation loss
                val_loss = self._val_metrics['loss'].result()
                # if new validation loss worse than previous one:
                if val_loss >= opt_early_stopping_metric:
                    count += 1
                    logger.info(f"Validation loss did not improve from {opt_early_stopping_metric}. Counter: {count}")

                    # update learning rate after waiting "patience" validations, decay learning rate (by multiplying by the decay_rate) 
                    # to see if any improvement on validation loss can be seen
                    if count % patience == 0:
                        lr = float(self.optimizer.learning_rate * learning_rate_decay) # new learning rate
                        logger.info(f"Reduced learning rate to {lr} using decay_rate {learning_rate_decay}")
                        self.optimizer.learning_rate = lr
                else: # validation loss improved
                    logger.info("Validation loss improved, saving model.")
                    opt_early_stopping_metric = float(val_loss)
                    opt_weights = self.get_weights()

                    # save best model
                    self.save_me()

                    # reset counter
                    count = 0

                    true_efs_3d_test, pred_efs_3d_test = self.plot_efs_3d_vs_3d(test_dataset, "test", global_step)

                    rmse = np.sqrt(sk_metrics.mean_squared_error(true_efs_3d_test, pred_efs_3d_test))
                    mae = sk_metrics.mean_absolute_error(true_efs_3d_test, pred_efs_3d_test)
                    r2 = sk_metrics.r2_score(true_efs_3d_test, pred_efs_3d_test)

                    logger.info(f'Test data: RMSE: {rmse:.5f} - MAE: {mae:.5f} - R2 Score: {r2:.5f}')

                    self.plot_efs_2d_vs_3d(test_dataset, "test", global_step)
                
                if train_plotting_counter % 5 == 0:
                    true_efs_3d_train, pred_efs_3d_train = self.plot_efs_3d_vs_3d(train_plotting_dataset, "train", global_step)
                    rmse = np.sqrt(sk_metrics.mean_squared_error(true_efs_3d_train, pred_efs_3d_train))
                    mae = sk_metrics.mean_absolute_error(true_efs_3d_train, pred_efs_3d_train)
                    r2 = sk_metrics.r2_score(true_efs_3d_train, pred_efs_3d_train)

                    logger.info(f'Train data: RMSE: {rmse:.5f} - MAE: {mae:.5f} - R2 Score: {r2:.5f}')

                    self.plot_efs_2d_vs_3d(train_plotting_dataset, "train", global_step)
                
                train_plotting_counter += 1
                    

                # reset metrics
                self.reset_metrics()

                # start steps timer again
                t1_steps = time.time()

    def plot_efs_3d_vs_3d(self, dataset, type_folder, global_step):
        logger.info(f"\nPlotting EF correlation plot on {type_folder} dataset...")
        t1 = time.time()

        # plot true vs pred ef on test set
        all_true_efs = []
        all_pred_efs = []
        for true_efs_2d, true_efs_3d in dataset:
            pred_efs_3d = self.call(true_efs_2d)
            all_true_efs.append(true_efs_3d.numpy())
            all_pred_efs.append(pred_efs_3d.numpy())
        
        # convert to range [0.0, 100.0]
        all_true_efs = np.reshape(np.concatenate(all_true_efs, axis=0), -1) * 100.0
        all_pred_efs = np.reshape(np.concatenate(all_pred_efs, axis=0), -1) * 100.0
        # ef correlation plot 
        plots_dir = self.log_dir / 'plots' / type_folder / "3D vs 3D"
        ef_plots_dir = plots_dir / "ejection_fraction"
        ef_plots_dir.mkdir(parents=True, exist_ok=True)
        plt.clf()
        filename = "EjectionFractionPlot_GS{}.png".format(str(global_step).zfill(3))
        plt.plot(all_true_efs, all_pred_efs, 'bo')
        plt.title(f'EF correlation plot')
        min_true_efs = min(all_true_efs)
        max_true_efs = max(all_true_efs)
        plt.xlabel('True 3D EF\nmin val: {:.5f}, max val: {:.5f}'.format(min_true_efs, max_true_efs))
        min_pred_efs = min(all_pred_efs)
        max_pred_efs = max(all_pred_efs)
        plt.ylabel('Pred 3D EF\nmin val: {:.5f}, max val: {:.5f}'.format(min_pred_efs, max_pred_efs))
        plt.savefig(ef_plots_dir / filename, bbox_inches='tight')

        t2 = time.time()
        h, m, s = utils.get_HH_MM_SS_from_sec(t2 - t1)
        logger.info(f"Done plotting in {h}:{m}:{s}")

        return all_true_efs, all_pred_efs
    
    def plot_efs_2d_vs_3d(self, dataset, type_folder, global_step):
        logger.info(f"\nPlotting EF correlation plot on {type_folder} dataset...")
        t1 = time.time()

        # plot true vs pred ef on test set
        all_true_efs = []
        all_pred_efs = []
        for true_efs_2d, _ in dataset:
            pred_efs_3d = self.call(true_efs_2d)
            all_true_efs.append(true_efs_2d.numpy())
            all_pred_efs.append(pred_efs_3d.numpy())
        
        # convert to range [0.0, 100.0]
        all_true_efs = np.reshape(np.concatenate(all_true_efs, axis=0), -1) * 100.0
        all_pred_efs = np.reshape(np.concatenate(all_pred_efs, axis=0), -1) * 100.0
        # ef correlation plot 
        plots_dir = self.log_dir / 'plots' / type_folder / "2D vs 3D"
        ef_plots_dir = plots_dir / "ejection_fraction"
        ef_plots_dir.mkdir(parents=True, exist_ok=True)
        plt.clf()
        filename = "EjectionFractionPlot_GS{}.png".format(str(global_step).zfill(3))
        plt.plot(all_true_efs, all_pred_efs, 'bo')
        plt.title(f'EF correlation plot')
        min_true_efs = min(all_true_efs)
        max_true_efs = max(all_true_efs)
        plt.xlabel('True 3D EF\nmin val: {:.5f}, max val: {:.5f}'.format(min_true_efs, max_true_efs))
        min_pred_efs = min(all_pred_efs)
        max_pred_efs = max(all_pred_efs)
        plt.ylabel('Pred 3D EF\nmin val: {:.5f}, max val: {:.5f}'.format(min_pred_efs, max_pred_efs))
        plt.savefig(ef_plots_dir / filename, bbox_inches='tight')

        t2 = time.time()
        h, m, s = utils.get_HH_MM_SS_from_sec(t2 - t1)
        logger.info(f"Done plotting in {h}:{m}:{s}")

        return all_true_efs, all_pred_efs

    # ----------------------------------------------- Logging, Saving and Loading -----------------------------------------------
    def log_weights(self):
        # log number of trainable weights
        n_trainable = np.sum([np.prod(v.get_shape().as_list()) for v in self.trainable_weights])
        logger.info(f"Number of trainable weights: {n_trainable}")

    def _log_train_metrics(self, losses):
        for metric in losses:
            self._train_metrics[metric](losses[metric])
    
    @staticmethod
    def write_summaries(summary_writer, metrics, global_step, log_str):
        with summary_writer.as_default():
            for metric, value in metrics.items():
                tf.summary.scalar(metric, value.result(), step=global_step)

        # print train metrics
        strings = ['%s: %.5e' % (k, v.result()) for k, v in metrics.items()]
        logger.info(f"\nGlobal Step: {global_step} | {log_str}: {' - '.join(strings)} ")
    
    def reset_metrics(self):
        metrics = chain(self._train_metrics.values(), self._val_metrics.values())
        for metric in metrics:
            metric.reset_states()
    
    def save_me(self):
        
        trained_model_path = self.log_dir / "trained_models" / "EF_pred"
        self.save_weights(str(trained_model_path) + "_best")
        
        logger.info(f"Model saved to file {trained_model_path}")
    # ---------------------------------------------------------------------------------------------------------------------------




