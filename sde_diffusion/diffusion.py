# Copyright (C) 2021. Huawei Technologies Co., Ltd. All rights reserved.
# This program is free software; you can redistribute it and/or modify
# it under the terms of the MIT License.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# MIT License for more details.

import math
import torch
from einops import rearrange
import numpy as np
from functools import partial

class BaseModule(torch.nn.Module):
    def __init__(self):
        super(BaseModule, self).__init__()

    @property
    def nparams(self):
        """
        Returns number of trainable parameters of the module.
        """
        num_params = 0
        for name, param in self.named_parameters():
            if param.requires_grad:
                num_params += np.prod(param.detach().cpu().numpy().shape)
        return num_params


    def relocate_input(self, x: list):
        """
        Relocates provided tensors to the same device set for the module.
        """
        device = next(self.parameters()).device
        for i in range(len(x)):
            if isinstance(x[i], torch.Tensor) and x[i].device != device:
                x[i] = x[i].to(device)
        return x

class Mish(BaseModule):
    def forward(self, x):
        return x * torch.tanh(torch.nn.functional.softplus(x))


class Upsample(BaseModule):
    def __init__(self, dim):
        super(Upsample, self).__init__()
        self.conv = torch.nn.ConvTranspose2d(dim, dim, 4, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Downsample(BaseModule):
    def __init__(self, dim):
        super(Downsample, self).__init__()
        self.conv = torch.nn.Conv2d(dim, dim, 3, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Rezero(BaseModule):
    def __init__(self, fn):
        super(Rezero, self).__init__()
        self.fn = fn
        self.g = torch.nn.Parameter(torch.zeros(1))

    def forward(self, x):
        return self.fn(x) * self.g


class Block(BaseModule):
    def __init__(self, dim, dim_out, groups=8):
        super(Block, self).__init__()
        self.block = torch.nn.Sequential(torch.nn.Conv2d(dim, dim_out, 3, 
                                         padding=1), torch.nn.GroupNorm(
                                         groups, dim_out), Mish())

    def forward(self, x, mask):
        output = self.block(x * mask)
        return output * mask


class ResnetBlock(BaseModule):
    def __init__(self, dim, dim_out, time_emb_dim, groups=8):
        super(ResnetBlock, self).__init__()
        self.mlp = torch.nn.Sequential(Mish(), torch.nn.Linear(time_emb_dim, 
                                                               dim_out))

        self.block1 = Block(dim, dim_out, groups=groups)
        self.block2 = Block(dim_out, dim_out, groups=groups)
        if dim != dim_out:
            self.res_conv = torch.nn.Conv2d(dim, dim_out, 1)
        else:
            self.res_conv = torch.nn.Identity()

    def forward(self, x, mask, time_emb):
        h = self.block1(x, mask)
        h += self.mlp(time_emb).unsqueeze(-1).unsqueeze(-1)
        h = self.block2(h, mask)
        output = h + self.res_conv(x * mask)
        return output


class LinearAttention(BaseModule):
    def __init__(self, dim, heads=4, dim_head=32):
        super(LinearAttention, self).__init__()
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_qkv = torch.nn.Conv2d(dim, hidden_dim * 3, 1, bias=False)
        self.to_out = torch.nn.Conv2d(hidden_dim, dim, 1)            

    def forward(self, x):
        b, c, h, w = x.shape
        qkv = self.to_qkv(x)
        q, k, v = rearrange(qkv, 'b (qkv heads c) h w -> qkv b heads c (h w)', 
                            heads = self.heads, qkv=3)            
        k = k.softmax(dim=-1)
        context = torch.einsum('bhdn,bhen->bhde', k, v)
        out = torch.einsum('bhde,bhdn->bhen', context, q)
        out = rearrange(out, 'b heads c (h w) -> b (heads c) h w', 
                        heads=self.heads, h=h, w=w)
        return self.to_out(out)


class Residual(BaseModule):
    def __init__(self, fn):
        super(Residual, self).__init__()
        self.fn = fn

    def forward(self, x, *args, **kwargs):
        output = self.fn(x, *args, **kwargs) + x
        return output


class SinusoidalPosEmb(BaseModule):
    def __init__(self, dim):
        super(SinusoidalPosEmb, self).__init__()
        self.dim = dim

    def forward(self, x, scale=1000):
        device = x.device
        half_dim = self.dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device).float() * -emb)
        emb = scale * x.unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb


class GradLogPEstimator2d(BaseModule):
    def __init__(self, dim, dim_mults=(1, 2, 4), groups=8,
                 n_spks=None, spk_emb_dim=64, n_feats=80, pe_scale=1000):
        super(GradLogPEstimator2d, self).__init__()
        self.dim = dim
        self.dim_mults = dim_mults
        self.groups = groups
        self.n_spks = n_spks if not isinstance(n_spks, type(None)) else 1
        self.spk_emb_dim = spk_emb_dim
        self.pe_scale = pe_scale
        
        if n_spks > 1:
            self.spk_mlp = torch.nn.Sequential(torch.nn.Linear(spk_emb_dim, spk_emb_dim * 4), Mish(),
                                               torch.nn.Linear(spk_emb_dim * 4, n_feats))
        self.time_pos_emb = SinusoidalPosEmb(dim)
        self.mlp = torch.nn.Sequential(torch.nn.Linear(dim, dim * 4), Mish(),
                                       torch.nn.Linear(dim * 4, dim))

        dims = [2 + (1 if n_spks > 1 else 0), *map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))
        self.downs = torch.nn.ModuleList([])
        self.ups = torch.nn.ModuleList([])
        num_resolutions = len(in_out)

        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)
            self.downs.append(torch.nn.ModuleList([
                       ResnetBlock(dim_in, dim_out, time_emb_dim=dim),
                       ResnetBlock(dim_out, dim_out, time_emb_dim=dim),
                       Residual(Rezero(LinearAttention(dim_out))),
                       Downsample(dim_out) if not is_last else torch.nn.Identity()]))

        mid_dim = dims[-1]
        self.mid_block1 = ResnetBlock(mid_dim, mid_dim, time_emb_dim=dim)
        self.mid_attn = Residual(Rezero(LinearAttention(mid_dim)))
        self.mid_block2 = ResnetBlock(mid_dim, mid_dim, time_emb_dim=dim)

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out[1:])):
            self.ups.append(torch.nn.ModuleList([
                     ResnetBlock(dim_out * 2, dim_in, time_emb_dim=dim),
                     ResnetBlock(dim_in, dim_in, time_emb_dim=dim),
                     Residual(Rezero(LinearAttention(dim_in))),
                     Upsample(dim_in)]))
        self.final_block = Block(dim, dim)
        self.final_conv = torch.nn.Conv2d(dim, 1, 1)
        self.act_func = torch.nn.Tanh()

    def forward(self, x, mask, mu, t, spk=None):
        if not isinstance(spk, type(None)):
            s = self.spk_mlp(spk)
        
        t = self.time_pos_emb(t, scale=self.pe_scale)
        t = self.mlp(t)

        if self.n_spks < 2:
            x = torch.stack([mu, x], 1)
        else:
            s = s.unsqueeze(-1).repeat(1, 1, x.shape[-1])
            x = torch.stack([mu, x, s], 1)
        mask = mask.unsqueeze(1)

        hiddens = []
        masks = [mask]
        for resnet1, resnet2, attn, downsample in self.downs:
            mask_down = masks[-1]
            x = resnet1(x, mask_down, t)
            x = resnet2(x, mask_down, t)
            x = attn(x)
            hiddens.append(x)
            x = downsample(x * mask_down)
            masks.append(mask_down[:, :, :, ::2])

        masks = masks[:-1]
        mask_mid = masks[-1]
        x = self.mid_block1(x, mask_mid, t)
        x = self.mid_attn(x)
        x = self.mid_block2(x, mask_mid, t)

        for resnet1, resnet2, attn, upsample in self.ups:
            mask_up = masks.pop()
            x = torch.cat((x, hiddens.pop()), dim=1)
            x = resnet1(x, mask_up, t)
            x = resnet2(x, mask_up, t)
            x = attn(x)
            x = upsample(x * mask_up)

        x = self.final_block(x, mask)
        output = self.final_conv(x * mask)

        #output = self.act_func(output)

        return (output * mask).squeeze(1)


def get_noise(t, beta_init, beta_term, cumulative=False):
    if cumulative:
        noise = beta_init*t + 0.5*(beta_term - beta_init)*(t**2)
    else:
        noise = beta_init + (beta_term - beta_init)*t
    return noise


class Diffusion(BaseModule):
    def __init__(self, n_feats, dim,
                 n_spks=1, spk_emb_dim=64, dim_mults=(1, 2, 4),
                 beta_min=0.05, beta_max=20, pe_scale=1000):
        super(Diffusion, self).__init__()
        self.n_feats = n_feats
        self.dim = dim
        self.n_spks = n_spks
        self.spk_emb_dim = spk_emb_dim
        self.beta_min = beta_min
        self.beta_max = beta_max
        self.pe_scale = pe_scale
        
        self.estimator = GradLogPEstimator2d(dim, n_spks=n_spks,
                                             dim_mults=dim_mults,
                                             spk_emb_dim=spk_emb_dim,
                                             pe_scale=pe_scale)

    def forward_diffusion(self, x0, mask, mu, t):
        time = t.unsqueeze(-1).unsqueeze(-1)
        cum_noise = get_noise(time, self.beta_min, self.beta_max, cumulative=True)
        mean = x0*torch.exp(-0.5*cum_noise) + mu*(1.0 - torch.exp(-0.5*cum_noise))
        variance = 1.0 - torch.exp(-cum_noise)
        z = torch.randn(x0.shape, dtype=x0.dtype, device=x0.device, 
                        requires_grad=False)
        xt = mean + z * torch.sqrt(variance)
        return xt * mask, z * mask

    @torch.no_grad()
    def reverse_diffusion(self, z, mask, mu, n_timesteps, stoc=False, spk=None, cond=None):
        h = 1.0 / n_timesteps
        xt = z * mask
        for i in range(n_timesteps):
            t = (1.0 - (i + 0.5)*h) * torch.ones(z.shape[0], dtype=z.dtype, 
                                                 device=z.device)
            time = t.unsqueeze(-1).unsqueeze(-1)
            noise_t = get_noise(time, self.beta_min, self.beta_max, 
                                cumulative=False)
            if stoc:  # adds stochastic term
                dxt_det = 0.5 * (mu - xt) - self.estimator(xt, mask, cond, t, spk)
                dxt_det = dxt_det * noise_t * h
                dxt_stoc = torch.randn(z.shape, dtype=z.dtype, device=z.device,
                                       requires_grad=False)
                dxt_stoc = dxt_stoc * torch.sqrt(noise_t * h)
                dxt = dxt_det + dxt_stoc
            else:
                dxt = 0.5 * (mu - xt - self.estimator(xt, mask, cond, t, spk))
                dxt = dxt * noise_t * h
            xt = (xt - dxt) * mask
        return xt

    @torch.no_grad()
    def forward(self, z, mask, mu, n_timesteps, stoc=False, spk=None, cond=None):
        if cond is None:
            cond = mu.clone()
        return self.reverse_diffusion(z, mask, mu, n_timesteps, stoc, spk, cond)

    def loss_t(self, x0, mask, mu, t, spk=None, cond=None):
        xt, z = self.forward_diffusion(x0, mask, mu, t)
        time = t.unsqueeze(-1).unsqueeze(-1)
        cum_noise = get_noise(time, self.beta_min, self.beta_max, cumulative=True)
        noise_estimation = self.estimator(xt, mask, cond, t, spk)
        noise_estimation *= torch.sqrt(1.0 - torch.exp(-cum_noise))
        loss = torch.sum((noise_estimation + z)**2) / (torch.sum(mask)*self.n_feats)
        return loss, xt

    def compute_loss(self, x0, mask, mu, spk=None, cond=None, offset=1e-5):
        if cond is None:
            cond = mu.clone()
        t = torch.rand(x0.shape[0], dtype=x0.dtype, device=x0.device,
                       requires_grad=False)
        t = torch.clamp(t, offset, 1.0 - offset)
        return self.loss_t(x0, mask, mu, t, spk, cond)

class DiffusionEDM(BaseModule):
    def __init__(self, n_feats, dim,
                 n_spks=1, spk_emb_dim=64, dim_mults=(1, 2, 4),
                 beta_min=0.05, beta_max=20, pe_scale=1000):
        super(DiffusionEDM, self).__init__()
        self.n_feats = n_feats
        self.dim = dim
        self.n_spks = n_spks
        self.spk_emb_dim = spk_emb_dim
        self.beta_min = beta_min
        self.beta_max = beta_max
        self.pe_scale = pe_scale

        self.P_mean =-1.2 # P_mean
        self.P_std =1.2# P_std
        self.sigma_data =0.5# sigma_data
 
        self.sigma_min= 0.002
        self.sigma_max= 80
        self.rho=7

        self.N = 50         #100   
        
        # Time step discretization
        step_indices = torch.arange(self.N )   
        t_steps = (self.sigma_min ** (1 / self.rho) + step_indices / (self.N - 1) * (self.sigma_max ** (1 / self.rho) - self.sigma_min ** (1 / self.rho))) ** self.rho
        self.t_steps = torch.cat([torch.zeros_like(t_steps[:1]), self.round_sigma(t_steps)])  
        
        self.estimator = GradLogPEstimator2d(dim, n_spks=n_spks,
                                             dim_mults=dim_mults,
                                             spk_emb_dim=spk_emb_dim,
                                             pe_scale=pe_scale)

    def EDMPrecond(self, x, sigma, cond, mask):
 
        sigma = sigma.reshape(-1, 1, 1)
 
        c_skip = self.sigma_data ** 2 / ((sigma - self.sigma_min) ** 2 + self.sigma_data ** 2)
        c_out = (sigma - self.sigma_min) * self.sigma_data / (sigma ** 2 + self.sigma_data ** 2).sqrt()
        c_in = 1 / (self.sigma_data ** 2 + sigma ** 2).sqrt()
        c_noise = sigma.log() / 4
 
        F_x =  self.estimator((c_in * x), mask, cond, c_noise.flatten()) 
        D_x = c_skip * x + c_out * F_x
        return D_x

    def EDMLoss(self, x_start, cond, mask):
 
        rnd_normal = torch.randn([x_start.shape[0], 1,  1], device=x_start.device)
        sigma = (rnd_normal * self.P_std + self.P_mean).exp()
        weight = (sigma ** 2 + self.sigma_data ** 2) / (sigma * self.sigma_data) ** 2
 
        #n = (torch.randn_like(x_start) + cond) * sigma
        n = (torch.randn_like(x_start)) * sigma
        D_yn = self.EDMPrecond(x_start + n, sigma ,cond, mask)
        loss = (weight * ((D_yn - x_start) ** 2))
        loss = loss * mask
        loss = loss.mean() 
        return loss, D_yn

    def EDMLoss_CFG(self, x_start, cond, mask, cond_scale=3.0, rescaled_phi=0.):
 
        rnd_normal = torch.randn([x_start.shape[0], 1,  1], device=x_start.device)
        sigma = (rnd_normal * self.P_std + self.P_mean).exp()
        weight = (sigma ** 2 + self.sigma_data ** 2) / (sigma * self.sigma_data) ** 2
 
        #n = (torch.randn_like(x_start) + cond) * sigma
        n = (torch.randn_like(x_start)) * sigma
        D_yn1 = self.EDMPrecond(x_start + n, sigma ,cond, mask)
        D_yn2 = self.EDMPrecond(x_start + n, sigma ,torch.zeros_like(cond), mask)
        scaled_D_yn = D_yn2 + (D_yn1 - D_yn2) * cond_scale

        std_fn = partial(torch.std, dim = tuple(range(1, scaled_D_yn.ndim)), keepdim = True)
        rescaled_D_yn = scaled_D_yn * (std_fn(D_yn1) / std_fn(scaled_D_yn))
        D_yn = rescaled_D_yn * rescaled_phi + scaled_D_yn * (1. - rescaled_phi)

        loss = (weight * ((D_yn - x_start) ** 2))
        loss = loss * mask
        loss = loss.mean() 
        return loss, D_yn


    def round_sigma(self, sigma):
        return torch.as_tensor(sigma)

    def edm_sampler(self,
         latents, mu, nonpadding,
        num_steps=50, sigma_min=0.002, sigma_max=80, rho=7, 
        S_churn=0, S_min=0, S_max=float('inf'), S_noise=1, cond=None,
        use_cfg=False, cond_scale=3.0, rescaled_phi=0.
    ):
        # Time step discretization.
        num_steps=num_steps+1
        step_indices = torch.arange(num_steps,   device=latents.device)

        t_steps = (sigma_max ** (1 / rho) + step_indices / (num_steps - 1) * (sigma_min ** (1 / rho) - sigma_max ** (1 / rho))) ** rho
        t_steps = torch.cat([self.round_sigma(t_steps), torch.zeros_like(t_steps[:1])])  

        # Main sampling loop.
        x_next = latents * t_steps[0]
        for i, (t_cur, t_next) in enumerate(zip(t_steps[:-1], t_steps[1:])):  
            x_cur = x_next
            # print('step',i+1)
            # Increase noise temporarily.
            gamma = min(S_churn / num_steps, np.sqrt(2) - 1) if S_min <= t_cur <= S_max else 0
            t_hat = self.round_sigma(t_cur + gamma * t_cur)
            x_hat = x_cur + (t_hat ** 2 - t_cur ** 2).sqrt() * S_noise * torch.randn_like(x_cur)

            """
            denoised = self.EDMPrecond(x_hat, t_hat, mu, nonpadding) 
            d_cur = (x_hat - denoised) / t_hat #!
            x_next = x_hat + (t_next - t_hat) * d_cur
            if i < num_steps - 1:
                denoised = self.EDMPrecond(x_next, t_next, mu ,nonpadding) 
                d_prime = (x_next - denoised) / t_next
                x_next = x_hat + (t_next - t_hat) * (0.5 * d_cur + 0.5 * d_prime)
            """
            		
            # Euler step.

            if use_cfg:
                D_yn1 = self.EDMPrecond(x_hat, t_hat, mu, nonpadding)
                D_yn2 = self.EDMPrecond(x_hat, t_hat, torch.zeros_like(mu), nonpadding)
                scaled_D_yn = D_yn2 + (D_yn1 - D_yn2) * cond_scale

                std_fn = partial(torch.std, dim = tuple(range(1, scaled_D_yn.ndim)), keepdim = True)
                rescaled_D_yn = scaled_D_yn * (std_fn(D_yn1) / std_fn(scaled_D_yn))
                denoised = rescaled_D_yn * rescaled_phi + scaled_D_yn * (1. - rescaled_phi)
            else:
                denoised = self.EDMPrecond(x_hat, t_hat, mu, nonpadding)


            d_cur = (x_hat - denoised) / t_hat
            x_next = x_hat + (t_next - t_hat) * d_cur
            
            # add Heun’s 2nd order method 

            #if i < num_steps - 1:
            #    denoised = self.EDMPrecond(x_next, t_next, mu, nonpadding) 
            #    #d_cur = (x_hat - denoised) / t_hat
            #    d_prime = (x_next - denoised) / t_next
            #    x_next = x_hat + (t_next - t_hat) * (0.5 * d_cur + 0.5 * d_prime)


        return x_next

    def forward(self, z, mask, mu, t_steps, spk=None, cond=None, use_cfg=False):
        t_steps = self.N
        #shape = (cond.shape[0], self.n_feats, cond.shape[2])
        #z = torch.randn(shape, device=x.device)+cond
        x = self.edm_sampler(z, mu, mask, t_steps, use_cfg=use_cfg)
        return x

    def compute_loss(self, x0, mask, mu, spk=None, cond=None, offset=1e-5, cfg=False):
        if cfg:
            loss = self.EDMLoss_CFG(x0, mu, mask)
        else:
            loss = self.EDMLoss(x0, mu, mask)
        return loss