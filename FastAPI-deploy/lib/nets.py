import torch
from torch import nn
import torch.nn.functional as F

from lib import layers


class BaseNet(nn.Module):

    def __init__(self, nin, nout, nin_lstm, nout_lstm, dilations=((4, 2), (8, 4), (12, 6))):
        super(BaseNet, self).__init__()
        self.enc1 = layers.Conv2DBNActiv(nin, nout, 3, 1, 1)
        self.enc2 = layers.Encoder(nout, nout * 2, 3, 2, 1)
        self.enc3 = layers.Encoder(nout * 2, nout * 4, 3, 2, 1)
        self.enc4 = layers.Encoder(nout * 4, nout * 6, 3, 2, 1)
        self.enc5 = layers.Encoder(nout * 6, nout * 8, 3, 2, 1)

        self.aspp = layers.ASPPModule(nout * 8, nout * 8, dilations, dropout=True)

        self.dec4 = layers.Decoder(nout * (6 + 8), nout * 6, 3, 1, 1)
        self.dec3 = layers.Decoder(nout * (4 + 6), nout * 4, 3, 1, 1)
        self.dec2 = layers.Decoder(nout * (2 + 4), nout * 2, 3, 1, 1)
        self.lstm_dec2 = layers.LSTMModule(nout * 2, nin_lstm, nout_lstm)
        self.dec1 = layers.Decoder(nout * (1 + 2) + 1, nout * 1, 3, 1, 1)

    def __call__(self, x):
        e1 = self.enc1(x)
        e2 = self.enc2(e1)
        e3 = self.enc3(e2)
        e4 = self.enc4(e3)
        e5 = self.enc5(e4)

        h = self.aspp(e5)

        h = self.dec4(h, e4)
        h = self.dec3(h, e3)
        h = self.dec2(h, e2)
        h = torch.cat([h, self.lstm_dec2(h)], dim=1)
        h = self.dec1(h, e1)

        return h


class CascadedNet(nn.Module):

    def __init__(self, n_fft, hop_length, nout=32, nout_lstm=128, is_complex=False):
        super(CascadedNet, self).__init__()
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.is_complex = is_complex

        self.max_bin = n_fft // 2
        self.output_bin = n_fft // 2 + 1
        self.nin_lstm = self.max_bin // 2
        self.offset = 64

        nin = 4 if is_complex else 2

        self.stg1_low_band_net = nn.Sequential(
            BaseNet(nin, nout // 2, self.nin_lstm // 2, nout_lstm),
            layers.Conv2DBNActiv(nout // 2, nout // 4, 1, 1, 0)
        )
        self.stg1_high_band_net = BaseNet(
            nin, nout // 4, self.nin_lstm // 2, nout_lstm // 2
        )

        self.stg2_low_band_net = nn.Sequential(
            BaseNet(nout // 4 + nin, nout, self.nin_lstm // 2, nout_lstm),
            layers.Conv2DBNActiv(nout, nout // 2, 1, 1, 0)
        )
        self.stg2_high_band_net = BaseNet(
            nout // 4 + nin, nout // 2, self.nin_lstm // 2, nout_lstm // 2
        )

        self.stg3_full_band_net = BaseNet(
            3 * nout // 4 + nin, nout, self.nin_lstm, nout_lstm
        )

        self.out_y = nn.Conv2d(nout, nin, 1, bias=False)
        self.out_v = nn.Conv2d(nout, nin, 1, bias=False)

    def forward(self, x):
        if self.is_complex:
            x = torch.cat([x.real, x.imag], dim=1)

        x = x[:, :, :self.max_bin]

        bandw = x.size()[2] // 2
        l1_in = x[:, :, :bandw]
        h1_in = x[:, :, bandw:]
        l1 = self.stg1_low_band_net(l1_in)
        h1 = self.stg1_high_band_net(h1_in)
        aux1 = torch.cat([l1, h1], dim=2)

        l2_in = torch.cat([l1_in, l1], dim=1)
        h2_in = torch.cat([h1_in, h1], dim=1)
        l2 = self.stg2_low_band_net(l2_in)
        h2 = self.stg2_high_band_net(h2_in)
        aux2 = torch.cat([l2, h2], dim=2)

        f3_in = torch.cat([x, aux1, aux2], dim=1)
        f3 = self.stg3_full_band_net(f3_in)

        if self.is_complex:
            mask_y = self.out_y(f3)
            mask_y = torch.complex(mask_y[:, :2], mask_y[:, 2:])
            mask_y = self.bounded_mask(mask_y)
            mask_v = self.out_v(f3)
            mask_v = torch.complex(mask_v[:, :2], mask_v[:, 2:])
            mask_v = self.bounded_mask(mask_v)
        else:
            mask_y = torch.sigmoid(self.out_y(f3))
            mask_v = torch.sigmoid(self.out_v(f3))

        mask_y = F.pad(
            input=mask_y,
            pad=(0, 0, 0, self.output_bin - mask_y.size()[2]),
            mode='replicate'
        )
        mask_v = F.pad(
            input=mask_v,
            pad=(0, 0, 0, self.output_bin - mask_v.size()[2]),
            mode='replicate'
        )

        return mask_y, mask_v

    def bounded_mask(self, mask, eps=1e-8):
        mask_mag = torch.abs(mask)
        mask = torch.tanh(mask_mag) * mask / (mask_mag + eps)
        return mask

    def predict_mask(self, x):
        mask_y, mask_v = self.forward(x)

        if self.offset > 0:
            mask_y = mask_y[:, :, :, self.offset:-self.offset]
            mask_v = mask_v[:, :, :, self.offset:-self.offset]
            assert mask_y.size()[3] > 0 and mask_v.size()[3] > 0

        return mask_y, mask_v

    def predict(self, x):
        mask_y, mask_v = self.forward(x)
        pred_y = x * mask_y
        pred_v = x * mask_v

        if self.offset > 0:
            pred_y = pred_y[:, :, :, self.offset:-self.offset]
            pred_v = pred_v[:, :, :, self.offset:-self.offset]
            assert pred_y.size()[3] > 0 and pred_v.size()[3] > 0

        return pred_y, pred_v