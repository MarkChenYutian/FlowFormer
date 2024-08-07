import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import einsum
import numpy as np

from einops import rearrange

from ...utils.utils import coords_grid
from .attention import BroadMultiHeadAttention, MultiHeadAttention, LinearPositionEmbeddingSine, ExpPositionEmbeddingSine
from ..encoders import twins_svt_large
from typing import Tuple
from .twins import Size_
from .cnn import BasicEncoder
from .mlpmixer import MLPMixerLayer
from .convnext import ConvNextLayer

from timm.layers import DropPath

class PatchEmbed(nn.Module):
    def __init__(self, patch_size=16, in_chans=1, embed_dim=64, pe='linear', device='cuda'):
        super().__init__()
        self.device = device
        self.patch_size = patch_size
        self.dim = embed_dim
        self.pe = pe

        if patch_size == 8:
            self.proj = nn.Sequential(
                nn.Conv2d(in_chans, embed_dim//4, kernel_size=6, stride=2, padding=2),
                nn.ReLU(inplace=True),
                nn.Conv2d(embed_dim//4, embed_dim//2, kernel_size=6, stride=2, padding=2),
                nn.ReLU(inplace=True),
                nn.Conv2d(embed_dim//2, embed_dim, kernel_size=6, stride=2, padding=2),
            )
        elif patch_size == 4:
            self.proj = nn.Sequential(
                nn.Conv2d(in_chans, embed_dim//4, kernel_size=6, stride=2, padding=2),
                nn.ReLU(inplace=True),
                nn.Conv2d(embed_dim//4, embed_dim, kernel_size=6, stride=2, padding=2),
            )
        else:
            print(f"patch size = {patch_size} is unacceptable.")

        self.ffn_with_coord = nn.Sequential(
            nn.Conv2d(embed_dim*2, embed_dim*2, kernel_size=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(embed_dim*2, embed_dim*2, kernel_size=1)
        )
        self.norm = nn.LayerNorm(embed_dim*2)

    def forward(self, x) -> Tuple[torch.Tensor, list[int]]:
        B, C, H, W = x.shape    # C == 1

        pad_l = pad_t = 0
        pad_r = (self.patch_size - W % self.patch_size) % self.patch_size
        pad_b = (self.patch_size - H % self.patch_size) % self.patch_size
        x = F.pad(x, (pad_l, pad_r, pad_t, pad_b))

        x = self.proj(x)
        out_size = x.shape[2:] 

        patch_coord = coords_grid(
            B, out_size[0], out_size[1], torch.device(self.device), x.dtype
        ) * self.patch_size + (self.patch_size / 2) # in feature coordinate space
        patch_coord = patch_coord.view(B, 2, -1).permute(0, 2, 1)
        if self.pe == 'linear':
            patch_coord_enc = LinearPositionEmbeddingSine(patch_coord, dim=self.dim)
        elif self.pe == 'exp':
            patch_coord_enc = ExpPositionEmbeddingSine(patch_coord, dim=self.dim)
        else:
            raise ValueError(f"Unsupported position encoding method {self.pe}")
        
        patch_coord_enc = patch_coord_enc.permute(0, 2, 1).view(B, -1, out_size[0], out_size[1])

        x_pe = torch.cat([x, patch_coord_enc], dim=1)
        x = self.ffn_with_coord(x_pe)
        x = self.norm(x.flatten(2).transpose(1, 2))

        return x, out_size

from .twins import Block

class GroupVerticalSelfAttentionLayer(nn.Module):
    def __init__(self, dim, cfg, num_heads=8, attn_drop=0., proj_drop=0., drop_path=0., dropout=0.):
        super(GroupVerticalSelfAttentionLayer, self).__init__()
        self.cfg = cfg
        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        embed_dim = dim
        mlp_ratio = 4
        ws = 7
        sr_ratio = 4
        dpr = 0.
        drop_rate = dropout
        attn_drop_rate=0.

        self.block = Block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, drop=drop_rate,
                attn_drop=attn_drop_rate, drop_path=dpr, sr_ratio=sr_ratio, ws=ws, with_rpe=True, vert_c_dim=cfg.vert_c_dim, groupattention=True, cfg=self.cfg)

    def forward(self, x, size, context=None):
        x = self.block(x, size, context)

        return x

class VerticalSelfAttentionLayer(nn.Module):
    def __init__(self, dim, cfg, num_heads=8, attn_drop=0., proj_drop=0., drop_path=0., dropout=0.):
        super(VerticalSelfAttentionLayer, self).__init__()
        self.cfg = cfg
        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        embed_dim = dim
        mlp_ratio = 4
        ws = 7
        sr_ratio = 4
        dpr = 0.
        drop_rate = dropout
        attn_drop_rate=0.

        self.local_block = Block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, drop=drop_rate,
                attn_drop=attn_drop_rate, drop_path=dpr, sr_ratio=sr_ratio, ws=ws, with_rpe=True, vert_c_dim=cfg.vert_c_dim)
        self.global_block = Block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, drop=drop_rate,
                attn_drop=attn_drop_rate, drop_path=dpr, sr_ratio=sr_ratio, ws=1, with_rpe=True, vert_c_dim=cfg.vert_c_dim)

    def forward(self, x: torch.Tensor, size: tuple[int, int], context=None):
        x = self.local_block(x, size, context)
        x = self.global_block(x, size, context)

        return x

    def compute_params(self):
        num = 0
        for param in self.parameters():
            num +=  np.prod(param.size())

        return num

class SelfAttentionLayer(nn.Module):
    def __init__(self, dim, cfg, num_heads=8, attn_drop=0., proj_drop=0., drop_path=0., dropout=0.):
        super(SelfAttentionLayer, self).__init__()
        assert dim % num_heads == 0, f"dim {dim} should be divided by num_heads {num_heads}."

        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.multi_head_attn = MultiHeadAttention(dim, num_heads)
        self.q, self.k, self.v = nn.Linear(dim, dim, bias=True), nn.Linear(dim, dim, bias=True), nn.Linear(dim, dim, bias=True)

        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.ffn = nn.Sequential(
            nn.Linear(dim, dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim, dim),
            nn.Dropout(dropout)
        )

    def forward(self, x):
        """
            x: [BH1W1, H3W3, D]
        """
        short_cut = x
        x = self.norm1(x)

        q, k, v = self.q(x), self.k(x), self.v(x)

        x = self.multi_head_attn(q, k, v)

        x = self.proj(x)
        x = short_cut + self.proj_drop(x)

        x = x + self.drop_path(self.ffn(self.norm2(x)))

        return x

    def compute_params(self):
        num = 0
        for param in self.parameters():
            num +=  np.prod(param.size())

        return num

class CrossAttentionLayer(nn.Module):
    def __init__(self, qk_dim, v_dim, query_token_dim, tgt_token_dim, num_heads=8, attn_drop=0., proj_drop=0., drop_path=0., dropout=0.):
        super(CrossAttentionLayer, self).__init__()
        assert qk_dim % num_heads == 0, f"dim {qk_dim} should be divided by num_heads {num_heads}."
        assert v_dim % num_heads == 0, f"dim {v_dim} should be divided by num_heads {num_heads}."
        """
            Query Token:    [N, C]  -> [N, qk_dim]  (Q)
            Target Token:   [M, D]  -> [M, qk_dim]  (K),    [M, v_dim]  (V)
        """
        self.num_heads = num_heads
        head_dim = qk_dim // num_heads
        self.scale = head_dim ** -0.5

        self.norm1 = nn.LayerNorm(query_token_dim)
        self.norm2 = nn.LayerNorm(query_token_dim)
        self.multi_head_attn = BroadMultiHeadAttention(qk_dim, num_heads)
        self.q, self.k, self.v = nn.Linear(query_token_dim, qk_dim, bias=True), nn.Linear(tgt_token_dim, qk_dim, bias=True), nn.Linear(tgt_token_dim, v_dim, bias=True)

        self.proj = nn.Linear(v_dim, query_token_dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.ffn = nn.Sequential(
            nn.Linear(query_token_dim, query_token_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(query_token_dim, query_token_dim),
            nn.Dropout(dropout)
        )

    def forward(self, query, tgt_token):
        """
            x: [BH1W1, H3W3, D]
        """
        short_cut = query
        query = self.norm1(query)
        q, k, v = self.q(query), self.k(tgt_token), self.v(tgt_token)
        x = self.multi_head_attn(q, k, v)
        x = short_cut + self.proj_drop(self.proj(x))
        x = x + self.drop_path(self.ffn(self.norm2(x)))

        return x

class CostPerceiverEncoder(nn.Module):
    def __init__(self, cfg, device: str):
        super(CostPerceiverEncoder, self).__init__()
        self.cfg = cfg
        self.device = device
        self.patch_size: int = cfg.patch_size
        self.cost_heads_num: int = cfg.cost_heads_num
        self.vertical_conv: bool = cfg.vertical_conv
        self.cost_latent_token_num: int = cfg.cost_latent_token_num
        self.cost_encoder_res: bool = cfg.cost_encoder_res
        
        self.patch_embed = PatchEmbed(in_chans=self.cfg.cost_heads_num, patch_size=self.patch_size, embed_dim=cfg.cost_latent_input_dim, pe=cfg.pe, device=device)

        self.depth = cfg.encoder_depth

        self.latent_tokens = nn.Parameter(torch.randn(1, cfg.cost_latent_token_num, cfg.cost_latent_dim))

        query_token_dim, tgt_token_dim = cfg.cost_latent_dim, cfg.cost_latent_input_dim*2
        qk_dim, v_dim = query_token_dim, query_token_dim
        self.input_layer = CrossAttentionLayer(qk_dim, v_dim, query_token_dim, tgt_token_dim, dropout=cfg.dropout)

        if cfg.use_mlp:
            self.encoder_layers = nn.ModuleList([MLPMixerLayer(cfg.cost_latent_dim, cfg, dropout=cfg.dropout) for idx in range(self.depth)])
        else:
            self.encoder_layers = nn.ModuleList([SelfAttentionLayer(cfg.cost_latent_dim, cfg, dropout=cfg.dropout) for idx in range(self.depth)])

        # if self.cfg.vertical_conv:
        #     self.vertical_encoder_layers = nn.ModuleList([ConvNextLayer(cfg.cost_latent_dim) for idx in range(self.depth)])
        # else:
        assert self.cfg.vertical_conv == False, "Vertical Convolution is not supported for now."
        self.vertical_encoder_layers = nn.ModuleList([VerticalSelfAttentionLayer(cfg.cost_latent_dim, cfg, dropout=cfg.dropout) for idx in range(self.depth)])
        
        self.cost_scale_aug = None
        if ('cost_scale_aug' in cfg.keys()):
            self.cost_scale_aug = cfg.cost_scale_aug
            print("[Using cost_scale_aug: {}]".format(self.cost_scale_aug))



    def forward(self, cost_volume: torch.Tensor, context=None) -> tuple[torch.Tensor, torch.Tensor, tuple[int, int]]:
        B, heads, H1, W1, H2, W2 = cost_volume.shape
        cost_maps = cost_volume.permute(0, 2, 3, 1, 4, 5).contiguous().view(B*H1*W1, self.cost_heads_num, H2, W2)

        if self.cost_scale_aug is not None:
            # scale_factor = torch.FloatTensor(B*H1*W1, self.cost_heads_num, H2, W2).uniform_(self.cost_scale_aug[0], self.cost_scale_aug[1]).to(cost_maps.device)
            scale_factor = self.cost_scale_aug[0] + torch.rand(
                (B * H1 * W1, self.cost_heads_num, H2, W2),
                device=cost_maps.device, dtype=cost_maps.dtype
            ) * (self.cost_scale_aug[1] - self.cost_scale_aug[0])
            cost_maps = cost_maps * scale_factor
        
        x, size = self.patch_embed(cost_maps)   # B*H1*W1, size[0]*size[1], C

        x = self.input_layer(self.latent_tokens, x)

        short_cut = x

        for layer, vert_layer in zip(self.encoder_layers, self.vertical_encoder_layers):
            x = layer(x)
            # NOTE: for using JIT to speedup inference, I removed this if (vertical_conv) is always False for current configuration.
            # if self.vertical_conv:
            #     # B, H1*W1, K, D -> B, K, D, H1*W1 -> B*K, D, H1, W1
            #     x = x.view(B, H1*W1, self.cost_latent_token_num, -1).permute(0, 3, 1, 2).reshape(B*self.cost_latent_token_num, -1, H1, W1)
            #     x = vert_layer(x, (H1, W1))
            #     # B*K, D, H1, W1 -> B, K, D, H1*W1 -> B, H1*W1, K, D
            #     x = x.view(B, self.cost_latent_token_num, -1, H1*W1).permute(0, 2, 3, 1).reshape(B*H1*W1, self.cost_latent_token_num, -1)
            # else:
            #     x = x.view(B, H1*W1, self.cost_latent_token_num, -1).permute(0, 2, 1, 3).reshape(B*self.cost_latent_token_num, H1*W1, -1)
            #     x = vert_layer(x, (H1, W1), context)
            #     x = x.view(B, self.cost_latent_token_num, H1*W1, -1).permute(0, 2, 1, 3).reshape(B*H1*W1, self.cost_latent_token_num, -1)
            if self.vertical_conv: raise NotImplementedError("Disabled for JIT acceleration")
            
            x = x.view(B, H1*W1, self.cost_latent_token_num, -1).permute(0, 2, 1, 3).reshape(B*self.cost_latent_token_num, H1*W1, -1)
            x = vert_layer(x, (H1, W1), context)
            x = x.view(B, self.cost_latent_token_num, H1*W1, -1).permute(0, 2, 1, 3).reshape(B*H1*W1, self.cost_latent_token_num, -1)

        if self.cost_encoder_res is True:
            x = x + short_cut
        return x, cost_maps, (size[0], size[1])

class MemoryEncoder(nn.Module):
    def __init__(self, cfg, device: str, use_jit_inference: bool):
        super(MemoryEncoder, self).__init__()
        self.cfg = cfg
        self.device = device
        self.use_jit_inference = use_jit_inference

        if cfg.fnet == 'twins':
            self.feat_encoder = twins_svt_large(pretrained=self.cfg.pretrain)
        elif cfg.fnet == 'basicencoder':
            self.feat_encoder = BasicEncoder(output_dim=256, norm_fn='instance')
        else:
            exit()
        self.channel_convertor = nn.Conv2d(cfg.encoder_latent_dim, cfg.encoder_latent_dim, 1, padding=0, bias=False)
        if use_jit_inference:
            self.cost_perceiver_encoder = torch.jit.script(CostPerceiverEncoder(cfg, device)) #type: ignore
        else:
            self.cost_perceiver_encoder = CostPerceiverEncoder(cfg, device)
        
        self.freeze_handle = self.register_load_state_dict_post_hook(MemoryEncoder.__freeze_cost_encoder)

    def corr(self, fmap1, fmap2):

        batch, dim, ht, wd = fmap1.shape
        fmap1 = rearrange(fmap1, 'b (heads d) h w -> b heads (h w) d', heads=self.cfg.cost_heads_num)
        fmap2 = rearrange(fmap2, 'b (heads d) h w -> b heads (h w) d', heads=self.cfg.cost_heads_num)
        corr = einsum('bhid, bhjd -> bhij', fmap1, fmap2)
        corr = corr.permute(0, 2, 1, 3).view(batch*ht*wd, self.cfg.cost_heads_num, ht, wd)
        #corr = self.norm(self.relu(corr))
        corr = corr.view(batch, ht*wd, self.cfg.cost_heads_num, ht*wd).permute(0, 2, 1, 3)
        corr = corr.view(batch, self.cfg.cost_heads_num, ht, wd, ht, wd)

        return corr
    
    @staticmethod
    def __freeze_cost_encoder(module, _):
        if not module.use_jit_inference: return
        module.cost_perceiver_encoder = torch.jit.optimize_for_inference(module.cost_perceiver_encoder)
        
        # Should not be triggered twice.
        module.freeze_handle.remove()

    def forward(self, img1, img2, data, context=None):
        # The original implementation
        # feat_s = self.feat_encoder(img1)
        # feat_t = self.feat_encoder(img2)
        # feat_s = self.channel_convertor(feat_s)
        # feat_t = self.channel_convertor(feat_t)

        imgs = torch.cat([img1, img2], dim=0)
        feats = self.feat_encoder(imgs)
        feats = self.channel_convertor(feats)
        B = feats.shape[0] // 2

        feat_s = feats[:B]
        feat_t = feats[B:]

        B, C, H, W = feat_s.shape
        size = (H, W)

        if self.cfg.feat_cross_attn:
            feat_s = feat_s.flatten(2).transpose(1, 2)
            feat_t = feat_t.flatten(2).transpose(1, 2)

            for layer in self.layers:
                feat_s, feat_t = layer(feat_s, feat_t, size)

            feat_s = feat_s.reshape(B, *size, -1).permute(0, 3, 1, 2).contiguous()
            feat_t = feat_t.reshape(B, *size, -1).permute(0, 3, 1, 2).contiguous()

        cost_volume = self.corr(feat_s, feat_t)
        x, cost_maps, h3w3 = self.cost_perceiver_encoder(cost_volume, context)
        
        data['cost_maps'] = cost_maps
        data['H3W3'] = h3w3
        return x
