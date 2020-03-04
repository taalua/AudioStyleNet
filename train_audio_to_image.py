import argparse
import numpy as np
import os
import random
import torch

from datetime import datetime
from glob import glob
from lpips import PerceptualLoss
from my_models.style_gan_2 import Generator
from my_models import models
from torch.utils.tensorboard import SummaryWriter
from torchvision.utils import save_image
from tqdm import tqdm
from utils import datasets, utils


HOME = os.path.expanduser('~')


class Solver:
    def __init__(self, args):
        super().__init__()

        self.device = args.device
        self.args = args

        self.initial_lr = self.args.lr
        self.lr = self.args.lr
        self.lr_rampdown_length = 0.4
        self.lr_rampup_length = 0.1

        # Load generator
        self.g = Generator(
            1024, 512, 8, pretrained=True).eval().to(self.device)
        self.g.noises = [n.to(self.device) for n in self.g.noises]
        for param in self.g.parameters():
            param.requires_grad = False
        self.latent_avg = self.g.latent_avg.repeat(
            18, 1).unsqueeze(0).to(self.device)

        # Init global step
        self.global_step = 0
        self.step_start = 0

        # Define audio encoder
        self.audio_encoder = models.AudioExpressionNet().to(self.device).train()

        if self.args.cont or self.args.test:
            path = self.args.model_path
            self.audio_encoder.load_state_dict(torch.load(path))
            self.global_step = int(path.split(
                '/')[-1].split('.')[0].split('model')[-1])
            self.step_start = int(path.split(
                '/')[-1].split('.')[0].split('model')[-1])

        # Print # parameters
        print("# params {} (trainable {})".format(
            utils.count_params(self.audio_encoder),
            utils.count_trainable_params(self.audio_encoder)
        ))

        # Select optimizer and loss criterion
        self.opt = torch.optim.Adam(self.audio_encoder.parameters(), lr=self.initial_lr)
        self.criterion = PerceptualLoss(
            model='net-lin', net='vgg').to(self.device)
        # self.criterion = torch.nn.MSELoss()

        # Set up tensorboard
        if not self.args.debug and not self.args.test:
            tb_dir = self.args.save_dir
            self.writer = SummaryWriter(tb_dir)
            print(f"Logging run to {tb_dir}")

            # Create save dir
            os.makedirs(self.args.save_dir + 'models', exist_ok=True)

    def save(self):
        save_path = f"{self.args.save_dir}models/model{self.global_step}.pt"
        print(f"Saving: {save_path}")
        torch.save(self.audio_encoder.state_dict(), save_path)

    def update_lr(self, t):
        lr_ramp = min(1.0, (1.0 - t) / self.lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        lr_ramp = lr_ramp * min(1.0, t / self.lr_rampup_length)
        self.lr = self.initial_lr * lr_ramp
        self.opt.param_groups[0]['lr'] = self.lr

    def train(self, data_loaders, n_iters):
        print("Start training")
        pbar = tqdm(total=n_iters)
        i_iter = 0
        val_loss = 0.
        while i_iter < n_iters:
            for batch in data_loaders['train']:
                # Unpack batch
                target_img = batch['img'].to(self.device)
                mean = batch['mean'].to(self.device)
                target_latent = batch['latent'].to(self.device)
                audio = batch['audio'].to(self.device)

                # Update learning rate
                t = i_iter / n_iters
                self.update_lr(t)

                # Encode
                latent_offset = self.audio_encoder(audio)
                # Add mean (we only want to compute offset to mean latent)
                latent = latent_offset + mean

                # Decode
                img, _ = self.g([latent], input_is_latent=True, noise=self.g.noises)

                # Downsample to 256 x 256
                img = utils.downsample_256(img)

                # Compute perceptual loss
                loss = self.criterion(img, target_img).mean()

                # Optimize
                self.opt.zero_grad()
                loss.backward()
                self.opt.step()

                self.global_step += 1
                i_iter += 1
                pbar.update()

                pbar.set_description('step [{gs}/{ni}] - '
                                     'train loss {tl:.4f} - '
                                     'val loss {vl:.4f} - '
                                     'lr {lr:.4f}'.format(
                                         gs=self.global_step,
                                         ni=n_iters,
                                         tl=loss,
                                         vl=val_loss,
                                         lr=self.lr
                                     ))

                # Logging and evaluating
                if not self.args.debug:
                    if self.global_step % self.args.log_train_every == 0:
                        self.writer.add_scalars('loss', {'train': loss}, self.global_step)

                    if self.global_step % self.args.log_val_every == 0:
                        val_loss = self.validate(data_loaders)
                        self.writer.add_scalars('loss', {'val': val_loss}, self.global_step)

                    if self.global_step % self.args.save_every == 0:
                        self.save()

                    if self.global_step % self.args.eval_every == 0:
                        self.eval(data_loaders)

                # Break if n_iters is reached and still in epoch
                if i_iter == n_iters:
                    break

        self.save()
        print('Done.')

    def validate(self, data_loaders):
        sample = next(iter(data_loaders['train']))
        target_img = sample['img'].to(self.device)
        audio = sample['audio'].to(self.device)
        target_latent = sample['latent'].to(self.device)
        mean = sample['mean'].to(self.device)

        with torch.no_grad():
            # Encode
            latent_offset = self.audio_encoder(audio)
            latent = latent_offset + mean

            # Decode
            img, _ = self.g([latent], input_is_latent=True, noise=self.g.noises)

            # Downsample to 256 x 256
            img = utils.downsample_256(img)

        val_loss = self.criterion(img, target_img).mean()
        return val_loss

    def eval(self, data_loaders):
        # Train sample
        sample = next(iter(data_loaders['train']))
        n_display = min(4, self.args.batch_size)
        target_img = sample['img'][:n_display].to(self.device)
        audio = sample['audio'][:n_display].to(self.device)
        target_latent = sample['latent'][:n_display].to(self.device)
        mean = sample['mean'][:n_display].to(self.device)

        with torch.no_grad():
            latent_offset = self.audio_encoder(audio)
            latent = latent_offset + mean
            img, _ = self.g([latent], input_is_latent=True, noise=self.g.noises)
            # target_img, _ = self.g([target_latent], input_is_latent=True, noise=self.g.noises)
            mean_img, _ = self.g([mean], input_is_latent=True, noise=self.g.noises)

            img = utils.downsample_256(img)
            mean_img = utils.downsample_256(mean_img)

        img_tensor = torch.cat((img, target_img, mean_img), dim=0)
        save_image(
            img_tensor,
            f'{self.args.save_dir}train_gen_{self.global_step}.png',
            normalize=True,
            range=(-1, 1),
            nrow=min(8, self.args.batch_size)
        )

        # Val sample

    def test_model(self, test_latent_path, test_sentence_path):
        self.audio_encoder.eval()
        test_latent = torch.load(test_latent_path).unsqueeze(0).to(self.device)
        audios = [torch.tensor(np.load(p), dtype=torch.float32)
                  for p in glob(test_sentence_path + '*.npy')]
        audios = torch.stack(audios).to(self.device)

        tmp_dir = self.args.save_dir + '.temp/'
        os.makedirs(tmp_dir, exist_ok=True)
        for i, audio in enumerate(tqdm(audios)):
            audio = audio.unsqueeze(0)

            with torch.no_grad():
                latent_offset = self.audio_encoder(audio)
                latent = latent_offset + test_latent
                gen, _ = self.g([latent], input_is_latent=True, noise=self.g.noises)

            save_image(gen.cpu(), f"{tmp_dir}{str(i).zfill(5)}.png", normalize=True, range=(-1, 1))

        # Convert output frames to video
        original_dir = os.getcwd()
        os.chdir(tmp_dir)
        os.system(
            f'ffmpeg -framerate 25 -i %05d.png -c:v libx264 -r 25 -pix_fmt yuv420p ../out.mp4')

        # Remove generated frames and keep only video
        os.chdir(original_dir)
        os.system(f'rm -r {tmp_dir}')

        self.audio_encoder.train()


if __name__ == '__main__':

    # Random seeds
    seed = 0
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--cont', action='store_true')
    parser.add_argument('--model_path', type=str, default=None)

    parser.add_argument('--batch_size', type=int, default=4)
    parser.add_argument('--lr', type=int, default=0.01)
    parser.add_argument('--n_iters', type=int, default=100000)
    parser.add_argument('--log_train_every', type=int, default=1)
    parser.add_argument('--log_val_every', type=int, default=10)
    parser.add_argument('--save_every', type=int, default=10000)
    parser.add_argument('--eval_every', type=int, default=10000)
    parser.add_argument('--save_dir', type=str, default='saves/audio_encoder/')

    parser.add_argument('--test_latent', type=str, default='saves/projected_images/obama.pt')
    parser.add_argument('--test_sentence', type=str,
                        default='/home/meissen/Datasets/Tagesschau/test_sentence_trump_deepspeech/')
    args = parser.parse_args()

    if args.cont or args.test:
        assert args.model_path is not None
        assert args.test_latent is not None

    # Correct path
    if args.save_dir[-1] != '/':
        args.save_dir += '/'
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S/")
    # args.tensorboard_dir = args.save_dir + 'tensorboard/' + timestamp
    args.save_dir += timestamp

    if args.cont or args.test:
        args.save_dir = '/'.join(args.model_path.split('/')[:-2]) + '/'

    print("Saving run to {}".format(args.save_dir))

    # Select device
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    args.device = device

    # Load data
    ds = datasets.TagesschauDataset(
        root_path=HOME + '/Datasets/Tagesschau/Aligned256/',
        load_img=True,
        load_latent=False,
        load_audio=True,
        load_mean=True,
        shuffled=False,
        flat=False,
        normalize=True,
        mean=[0.5, 0.5, 0.5],
        std=[0.5, 0.5, 0.5],
        image_size=256
    )
    train_len = int(0.8 * len(ds))
    val_len = len(ds) - train_len
    train_ds, val_ds = torch.utils.data.random_split(ds, [train_len, val_len])
    data_loaders = {
        'train': torch.utils.data.DataLoader(
            train_ds,
            batch_size=args.batch_size,
            num_workers=4,
            shuffle=True,
            drop_last=False,
            pin_memory=True
        ),
        'val': torch.utils.data.DataLoader(
            val_ds,
            batch_size=args.batch_size,
            num_workers=4,
            shuffle=True,
            drop_last=False,
            pin_memory=True
        )
    }

    # Init solver
    solver = Solver(args)

    # Train
    if args.test:
        solver.test_model(args.test_latent, args.test_sentence)
    else:
        solver.train(data_loaders, args.n_iters)
        print("Finished training.")
