# reference: https://github.com/ml-explore/mlx-examples/tree/main/stable_diffusion

#
# For licensing see accompanying LICENSE.md file.
# Copyright (C) 2024 Argmax, Inc. All Rights Reserved.
#
from dataclasses import dataclass
from typing import Optional, Tuple

import mlx.core as mx


@dataclass
class MMDiTConfig:
    """Multi-modal Diffusion Transformer Configuration"""

    # Transformer spec
    num_heads: int = 24
    depth: int = 24  # 24, 38
    depth_unimodal: int = 0
    mlp_ratio: int = 4
    vae_latent_dim: int = 16  # = in_channels = out_channels
    layer_norm_eps: float = 1e-6

    @property
    def hidden_size(self) -> int:
        return 64 * self.depth

    # x: Latent image input spec
    max_latent_resolution: int = 192
    patch_size: int = 2
    # If true, reshapes input to enact (patch_size, patch_size) space-to-depth operation
    # If false, uses 2D convolution with kernel_size=patch_size and stride=patch_size
    patchify_via_reshape: bool = False

    # y: Text input spec
    pooled_text_embed_dim: int = 2048  # 768 (CLIP-L/14) + 1280 (CLIP-G/14) = 2048
    token_level_text_embed_dim: int = (
        4096  # 4096 (T5-XXL) = 768 (CLIP-L/14) + 1280 (CLIP-G/14) + 2048 (zero padding)
    )

    # t: Timestep input spec
    frequency_embed_dim: int = 256
    max_period: int = 10000

    # qk norm
    use_qk_norm: bool = False
    qk_scale: float = 1.0

    # positional encoding
    use_pe: bool = False

    # axes_dim
    axes_dim: Tuple[int] = (16, 56, 56)

    dtype: mx.Dtype = mx.float16


SD3_8b = MMDiTConfig(depth=38, num_heads=38)
SD3_2b = MMDiTConfig(depth=24, num_heads=24)

FLUX_SCHNELL = MMDiTConfig(num_heads=24,
                           depth=19,
                           depth_unimodal=38,
                           mlp_ratio=4,
                           patchify_via_reshape=True)


@dataclass
class AutoencoderConfig:
    in_channels: int = 3
    out_channels: int = 3
    latent_channels_out: int = 8
    latent_channels_in: int = 4
    block_out_channels: Tuple[int] = (128, 256, 512, 512)
    layers_per_block: int = 2
    norm_num_groups: int = 32
    scaling_factor: float = 0.18215


@dataclass
class VAEDecoderConfig:
    in_channels: int = 16
    out_channels: int = 3
    block_out_channels: Tuple[int] = (128, 256, 512, 512)
    layers_per_block: int = 3
    resnet_groups: int = 32


@dataclass
class VAEEncoderConfig:
    in_channels: int = 3
    out_channels: int = 32
    block_out_channels: Tuple[int] = (128, 256, 512, 512)
    layers_per_block: int = 2
    resnet_groups: int = 32


@dataclass
class CLIPTextModelConfig:
    num_layers: int = 23
    model_dims: int = 1024
    num_heads: int = 16
    max_length: int = 77
    vocab_size: int = 49408
    projection_dim: Optional[int] = None
    hidden_act: str = "quick_gelu"
