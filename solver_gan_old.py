import datetime
import numpy as np
import os
import random
import time
import torch
import wandb

from torch.utils.tensorboard import SummaryWriter
from torchvision.utils import make_grid, save_image

import utils

from models.model_utils import weights_init


class GANSolver(object):
    def __init__(self, config):

        random.seed(config.random_seed)
        np.random.seed(config.random_seed)
        torch.manual_seed(config.random_seed)

        # General
        self.config = config
        self.device = 'cuda' if (torch.cuda.is_available() and config.use_cuda) else 'cpu'
        self.global_step = 0
        self.t_start = 0

        print("Training on {}".format(self.device))

        # Models
        self.model_names = ['generator', 'discriminator', 'classifier']
        self.generator = config.generator.to(self.device)
        self.generator.apply(weights_init)

        self.discriminator = config.discriminator.to(self.device)
        self.discriminator.apply(weights_init)

        self.classifier = config.classifier.train().to(self.device)
        self.classifier.load_state_dict(torch.load(config.classifier_path, map_location=self.device))
        for param in self.classifier.parameters():
            param.requires_grad = False

        for name in self.model_names:
            if isinstance(name, str):
                model = getattr(self, name)
                print("{}: # params {} (trainable {})".format(
                    name,
                    utils.count_params(model),
                    utils.count_trainable_params(model)
                ))

        if self.config.log_run:
            self.save_dir = 'saves/pix2pix/' + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.writer = SummaryWriter(self.save_dir)
            wandb.init(project="emotion-pix2pix", config=config, sync_tensorboard=True)
            wandb.watch(self.generator)
            wandb.watch(self.discriminator)
        else:
            self.writer = None

        # Optimizers
        self.optimizer_G = self.config.optimizer_G
        self.optimizer_D = self.config.optimizer_D

        # Loss Functions
        self.criterionGAN = utils.GANLoss(config.GAN_mode, self.device,
                                          flip_p=config.flip_prob,
                                          noisy_labels=config.noisy_labels,
                                          label_range_real=config.label_range_real,
                                          label_range_fake=config.label_range_fake)
        self.criterionPix = config.criterion_pix
        self.criterionEmotion = config.criterion_emotion

        # Losses
        self.loss_G_GAN = torch.tensor(0.)
        self.loss_G_pixel = torch.tensor(0.)
        self.loss_G_emotion = torch.tensor(0.)
        self.loss_G_total = torch.tensor(0.)
        self.loss_D_real = torch.tensor(0.)
        self.loss_D_fake = torch.tensor(0.)
        self.loss_D_total = torch.tensor(0.)

        self.epoch_loss_G_GAN = torch.tensor(0.)
        self.epoch_loss_G_pixel = torch.tensor(0.)
        self.epoch_loss_G_emotion = torch.tensor(0.)
        self.epoch_loss_D_fake = torch.tensor(0.)
        self.epoch_loss_D_real = torch.tensor(0.)
        self.epoch_loss_D_total = torch.tensor(0.)

        # Other metrics
        self.acc_G = torch.tensor(0.)
        self.acc_D_real = torch.tensor(0.)
        self.acc_D_fake = torch.tensor(0.)
        self.epoch_acc_G_ = torch.tensor(0.)
        self.epoch_acc_D_real = torch.tensor(0.)
        self.epoch_acc_D_fake = torch.tensor(0.)

        self.epoch_maxNorm_D = 0
        self.epoch_maxNorm_G = 0

        self.iteration_metric_names = [
            'loss_G_GAN',
            'loss_G_pixel',
            'loss_G_emotion',
            'loss_D_total']

        self.epoch_metric_names = [
            'epoch_loss_D_total',
            'epoch_loss_D_fake',
            'epoch_loss_D_real',
            'epoch_loss_G_emotion',
            'epoch_loss_G_pixel',
            'epoch_loss_G_GAN',
            'epoch_acc_G_',
            'epoch_acc_D_real',
            'epoch_acc_D_fake',
            'epoch_maxNorm_D',
            'epoch_maxNorm_G'
        ]

    def save(self):
        """
        Save models

        args:
            epoch (int): Current epoch
        """
        for name in self.model_names:
            if isinstance(name, str):
                save_filename = '%s.pt' % (name)
                save_path = os.path.join(self.save_dir, save_filename)
                print('Saving {} to {}'.format(name, save_path))

                os.makedirs(self.save_dir, exist_ok=True)
                model = getattr(self, name)
                torch.save(model.state_dict(), save_path)

                if self.config.log_run:
                    torch.save(model.state_dict(), os.path.join(wandb.run.dir, save_filename))

        print()

    def _make_grid_image(self, real_A, real_B, fake_B):
        # Denormalize one sequence
        transform = utils.denormalize(self.config.mean, self.config.std)
        real_A = torch.stack([transform(a) for a in real_A[0]], 0).detach()
        real_B = torch.stack([transform(a) for a in real_B[0]], 0).detach()
        fake_B = torch.stack([transform(a) for a in fake_B[0]], 0).detach()

        # Make grid image
        grid_image = torch.cat((real_A, fake_B, real_B), -2)
        grid_image = make_grid(grid_image, nrow=real_A.size(0), normalize=False)

        return grid_image

    def sample_images(self, data_loaders):
        """
        Saves a generated sample
        """
        # Get sample from train set
        train_batch = next(iter(data_loaders['train']))
        self.set_inputs(train_batch)

        # Generate fake sequence
        # fake_B = self.generator(self.real_A[0].unsqueeze(0),
        #                         self.cond[0].unsqueeze(0))
        fake_B = self.generator(self.real_A[0].unsqueeze(0))
        grid_image_train = self._make_grid_image(self.real_A, self.real_B, fake_B)

        # Get sample from val set
        val_batch = next(iter(data_loaders['val']))
        self.set_inputs(val_batch)

        # Generate fake sequence
        # fake_B = self.generator(self.real_A[0].unsqueeze(0),
        #                         self.cond[0].unsqueeze(0))
        fake_B = self.generator(self.real_A[0].unsqueeze(0))
        grid_image_val = self._make_grid_image(self.real_A, self.real_B, fake_B)

        # Pad train grid
        grid_image_train = torch.nn.functional.pad(grid_image_train, [0, 30, 0, 0], mode='constant')

        # Cat train and val together
        img_sample = torch.cat((grid_image_train, grid_image_val), -1)
        img_sample = make_grid(img_sample, nrow=1, normalize=False)

        return img_sample

    def _zero_running_metrics(self):
        for name in self.epoch_metric_names:
            if isinstance(name, str):
                setattr(self, name, torch.tensor(0.))

    def _mean_running_metrics(self, len_loader):
        for name in self.epoch_metric_names:
            if isinstance(name, str):
                metric = getattr(self, name)
                setattr(self, name, metric / len_loader)

    @staticmethod
    def _set_requires_grad(nets, requires_grad=False):
        """
        Source: https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/blob/master/models/base_model.py
        Set requies_grad=Fasle for all the networks to avoid unnecessary computations

        args:
            nets (network list): a list of networks
            requires_grad (bool): whether the networks require gradients or not
        """
        if not isinstance(nets, list):
            nets = [nets]
        for net in nets:
            if net is not None:
                for param in net.parameters():
                    param.requires_grad = requires_grad

    @staticmethod
    def _get_max_grad_norm(net, current_max):
        for p in list(filter(lambda p: p.grad is not None, net.parameters())):
            norm = p.grad.data.norm(2).item()
            if norm > current_max:
                current_max = norm
        return current_max

    @staticmethod
    def _clip_gradient(net, max_grad):
        torch.nn.utils.clip_grad_norm_(net.parameters(), max_grad)

    def log_tensorboard(self):
        """
        Log metrics to tensorboard
        """
        for metric_name in self.iteration_metric_names:
            if isinstance(metric_name, str):
                # e.g. loss_G_GAN
                metric, model, name = metric_name.split('_')
                m = getattr(self, metric_name)
                self.writer.add_scalar(model + '/' + metric + '/' + name,
                                       m, self.global_step)

    def log_console(self, i_epoch):
        print("G loss GAN: {:.3f}\tG loss Pix: {:.3f}\tG loss Emo: {:.3f}".format(
            self.epoch_loss_G_GAN, self.epoch_loss_G_pixel, self.epoch_loss_G_emotion
        ))
        print("D loss real: {:.3f}\tD loss fake: {:.3f}\tD loss total: {:.3f}".format(
            self.epoch_loss_D_real, self.epoch_loss_D_fake, self.epoch_loss_D_total
        ))
        print("D acc real: {:.3f}\tD acc fake: {:.3f}\tG acc: {:.3f}".format(
            self.epoch_acc_D_real, self.epoch_acc_D_fake, self.epoch_acc_G_
        ))
        print("Max gradient norm D: {:.3f} | Max gradient norm G: {:.3f}".format(
            self.epoch_maxNorm_D, self.epoch_maxNorm_G))
        print('Time elapsed {} | Time left: {}\n'.format(
            utils.time_to_str(time.time() - self.t_start),
            utils.time_left(self.t_start, self.config.num_epochs, i_epoch)
        ))

    def set_inputs(self, inputs):
        """
        Unpack input data from the dataloader

        args:
            inputs (dict): Packaged input data
        """
        self.real_A = inputs['A'].to(self.device)
        self.real_B = inputs['B'].to(self.device)

        if 'y' in inputs.keys():
            self.cond = inputs['y'].to(self.device)
        else:
            self.cond = torch.tensor(0.).to(self.device)

    def forward(self):
        """
        Run forward pass
        """
        self.fake_B = self.generator(self.real_A)

    def backward_D(self):
        """
        Compute losses for the discriminator
        """
        # All real batch
        pred_real = self.discriminator(self.real_B)

        self.loss_D_real = self.criterionGAN(pred_real, True, discriminator=True)

        # Metrics
        # self.acc_D_real = (torch.sigmoid(pred_real).round() == self.criterionGAN.real_label.data).double().mean()
        self.epoch_loss_D_real += self.loss_D_real.item()
        # self.epoch_acc_D_real += self.acc_D_real.item()

        # All fake batch
        pred_fake = self.discriminator(self.fake_B.detach())

        self.loss_D_fake = self.criterionGAN(pred_fake, False, discriminator=True)

        # Metrics
        # self.acc_D_fake = (torch.sigmoid(pred_fake).round() == self.criterionGAN.fake_label.data).double().mean()
        self.epoch_loss_D_fake += self.loss_D_fake.item()
        # self.epoch_acc_D_fake += self.acc_D_fake.item()

        # Combined loss
        self.loss_D_total = self.loss_D_fake + self.loss_D_real
        self.epoch_loss_D_total += self.loss_D_total.item()

        self.loss_D_real.backward()
        self.loss_D_fake.backward()

    def backward_G(self):
        """
        Compute losses for the generator
        """
        # GAN loss
        # pred_fake = self.discriminator(self.real_A, self.fake_B, self.cond)
        pred_fake = self.discriminator(self.fake_B)

        self.loss_G_GAN = self.criterionGAN(pred_fake, True)

        # Metrics
        # self.acc_G = (torch.sigmoid(pred_fake).round() == self.criterionGAN.real_label.data).double().mean()
        self.epoch_loss_G_GAN += self.loss_G_GAN.item()
        # self.epoch_acc_G_ += self.acc_G.item()

        # Pixelwise loss
        # self.loss_G_pixel = self.criterionPix(self.fake_B, self.real_B)
        # self.epoch_loss_G_pixel += self.loss_G_pixel.item()

        # Emotion loss
        # embedding_fake = self.classifier(self.fake_B)
        # embedding_real = self.classifier(self.real_B)
        # self.loss_G_emotion = self.criterionEmotion(embedding_fake, embedding_real)
        # self.epoch_loss_G_emotion += self.loss_G_emotion.item()

        # Combined loss
        # self.loss_G_total = self.loss_G_GAN * self.config.lambda_G_GAN \
        #                     + self.loss_G_pixel * self.config.lambda_pixel \
        #                     + self.loss_G_emotion * self.config.lambda_emotion

        self.loss_G_total = self.loss_G_GAN

        self.loss_G_total.backward()

    def optimize_parameters(self):
        """
        Do forward and backward step and optimize parameters
        """
        self.forward()

        # Train Discriminator
        self.optimizer_D.zero_grad()
        self.backward_D()
        # if self.config.grad_clip_val:
        #     self._clip_gradient(self.discriminator, self.config.grad_clip_val)
        self.epoch_maxNorm_D = self._get_max_grad_norm(self.discriminator,
                                                       self.epoch_maxNorm_D)
        self.optimizer_D.step()

        # Train Generator
        self.optimizer_G.zero_grad()
        self.backward_G()
        self.epoch_maxNorm_G = self._get_max_grad_norm(self.generator,
                                                       self.epoch_maxNorm_G)
        self.optimizer_G.step()

    def train_model(self, data_loaders, plot_grads=False):

        print("Starting training")
        self.t_start = time.time()

        for i_epoch in range(1, self.config.num_epochs + 1):
            print('Epoch {}/{}'.format(i_epoch, self.config.num_epochs))
            print('-' * 10)

            self._zero_running_metrics()

            for batch in data_loaders['train']:

                # Increment step counter
                self.global_step += 1

                # Inputs
                self.set_inputs(batch)

                # Update parameters
                self.optimize_parameters()

                # Tensorboard logging
                if self.config.log_run:
                    self.log_tensorboard()

            # ---------------
            #  Epoch finished
            # ---------------

            self._mean_running_metrics(len(data_loaders['train']))

            # Epoch logging
            self.log_console(i_epoch)

            if self.config.log_run:
                # Generate sample images
                img_sample = self.sample_images(data_loaders)

                # Specify and create target folder
                target_dir = os.path.join(self.save_dir, 'images')
                os.makedirs(target_dir, exist_ok=True)

                # Save image (Important: wirter.add_image has to be before save_image!!!)
                self.writer.add_image('sample', img_sample, i_epoch)
                save_image(img_sample, os.path.join(target_dir, 'sample_{}.png'.format(i_epoch)))

                # Save model
                self.save()

        time_elapsed = time.time() - self.t_start
        print('\nTraining complete in {:.0f}m {:.0f}s'.format(
            time_elapsed // 60, time_elapsed % 60))

        if self.config.log_run:
            self.save()
            self.eval_model(data_loaders)

    def eval_model(self, data_loaders):
        # Real images vs fake images
        batch = next(iter(data_loaders['val']))
        real_B = batch['B'].to(self.device)

        fake_B = self.generator(real_B)

        # Denormalize
        if self.config.normalize:
            transform = utils.denormalize(self.config.mean, self.config.std)
            real_B = torch.stack([transform(a) for a in real_B[:, 0]], 0).detach()
            fake_B = torch.stack([transform(a) for a in fake_B[:, 0]], 0).detach()

        real_img = make_grid(real_B[:64], padding=5, normalize=False)
        fake_img = make_grid(fake_B[:64], padding=5, normalize=False)

        real_img = torch.nn.functional.pad(real_img, [0, 30, 0, 0], mode='constant')

        # Cat real and fake together
        imgs = torch.cat((real_img, fake_img), -1)
        imgs = make_grid(imgs, nrow=1, normalize=False)

        self.writer.add_image('random_samples', imgs)
        target_dir = os.path.join(self.save_dir, 'images')
        save_image(imgs, os.path.join(target_dir, 'random_samples.png'))