# reference: https://github.com/ml-explore/mlx-examples/tree/main/stable_diffusion

#
# For licensing see accompanying LICENSE.md file.
# Copyright (C) 2024 Argmax, Inc. All Rights Reserved.
#

import gc
import math
import time
from typing import Optional, Tuple

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from argmaxtools.utils import get_logger
from diffusionkit.utils import bytes2gigabytes
from PIL import Image

from .model_io import (
    _DEFAULT_MODEL,
    load_flux,
    load_mmdit,
    load_t5_encoder,
    load_t5_tokenizer,
    load_text_encoder,
    load_tokenizer,
    load_vae_decoder,
    load_vae_encoder,
)
from .sampler import ModelSamplingDiscreteFlow

logger = get_logger(__name__)

MMDIT_CKPT = {
    "2b": "mmdit_2b",
    "8b": "models/sd3_8b_beta.safetensors",
    "flux": "argmaxinc/mlx-FLUX.1-schnell",
}

# FIXME(arda): DEBUG, delete after testing
IS_FLUX = False


class DiffusionPipeline:
    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        w16: bool = False,
        shift: float = 1.0,
        use_t5: bool = True,
        model_size: str = "2b",
        low_memory_mode: bool = True,
        a16: bool = False,
        local_ckpt=None,
        is_flux: bool = False,
    ):
        model_io.LOCAl_SD3_CKPT = local_ckpt
        self.dtype = mx.bfloat16 if w16 else mx.float32
        self.activation_dtype = mx.bfloat16 if a16 else mx.float32
        self.use_t5 = use_t5
        mmdit_ckpt = MMDIT_CKPT[model_size]
        self.low_memory_mode = low_memory_mode
        self.is_flux = IS_FLUX or is_flux
        if is_flux:
            self.mmdit = load_flux(float16=w16)
        else:
            self.mmdit = load_mmdit(float16=w16, model_key=mmdit_ckpt)
        self.sampler = ModelSamplingDiscreteFlow(shift=shift)
        self.decoder = load_vae_decoder(float16=w16, key=mmdit_ckpt)
        self.encoder = load_vae_encoder(float16=False, key=mmdit_ckpt)

        if is_flux and not use_t5:
            logger.info("FLUX model is being used without T5. Setting use_t5 to True.")
            self.use_t5 = True

        self.clip_l = load_text_encoder(
            model,
            w16,
            model_key="clip_l",
        )
        self.tokenizer_l = load_tokenizer(
            model,
            merges_key="tokenizer_l_merges",
            vocab_key="tokenizer_l_vocab",
            pad_with_eos=True,
        )
        if not is_flux:
            self.clip_g = load_text_encoder(
                model,
                w16,
                model_key="clip_g",
            )
            self.tokenizer_g = load_tokenizer(
                model,
                merges_key="tokenizer_g_merges",
                vocab_key="tokenizer_g_vocab",
                pad_with_eos=False,
            )
        self.t5_encoder = None
        self.t5_tokenizer = None
        if self.use_t5:
            self.set_up_t5()

    def set_up_t5(self):
        if self.t5_encoder is None:
            self.t5_encoder = load_t5_encoder(
                float16=True if self.dtype == mx.bfloat16 else False,
                low_memory_mode=self.low_memory_mode,
            )
        if self.t5_tokenizer is None:
            self.t5_tokenizer = load_t5_tokenizer()
        self.use_t5 = True

    def unload_t5(self):
        if self.t5_encoder is not None:
            del self.t5_encoder
            self.t5_encoder = None
        if self.t5_tokenizer is not None:
            del self.t5_tokenizer
            self.t5_tokenizer = None
        gc.collect()
        self.use_t5 = False

    def ensure_models_are_loaded(self):
        mx.eval(self.mmdit.parameters())
        mx.eval(self.clip_l.parameters())
        mx.eval(self.clip_g.parameters())
        mx.eval(self.decoder.parameters())
        if self.use_t5:
            mx.eval(self.t5_encoder.parameters())

    def _tokenize(self, tokenizer, text: str, negative_text: Optional[str] = None):
        if negative_text is None:
            negative_text = ""
        if tokenizer.pad_with_eos:
            pad_token = tokenizer.eos_token
        else:
            pad_token = 0

        # Tokenize the text
        tokens = [tokenizer.tokenize(text)]
        if tokenizer.pad_to_max_length:
            tokens[0].extend([pad_token] * (tokenizer.max_length - len(tokens[0])))
        if negative_text is not None:
            tokens += [tokenizer.tokenize(negative_text)]
        lengths = [len(t) for t in tokens]
        N = max(lengths)
        tokens = [t + [pad_token] * (N - len(t)) for t in tokens]
        tokens = mx.array(tokens)

        return tokens

    def encode_text(
        self,
        text: str,
        cfg_weight: float = 7.5,
        negative_text: str = "",
    ):
        # FIXME(arda): Fast hack to get the model working
        if self.is_flux:
            tokens_l = self._tokenize(
                self.tokenizer_l,
                text,
                (negative_text if cfg_weight > 1 else None),
            )
            conditioning_l = self.clip_l(tokens_l[[0], :])  # Ignore negative text
            pooled_conditioning = conditioning_l.pooled_output

            tokens_t5 = self._tokenize(
                self.t5_tokenizer,
                text,
                (negative_text if cfg_weight > 1 else None),
            )
            padded_tokens_t5 = mx.zeros((1, 256)).astype(tokens_t5.dtype)
            padded_tokens_t5[:, : tokens_t5.shape[1]] = tokens_t5[
                [0], :
            ]  # Ignore negative text
            t5_conditioning = self.t5_encoder(padded_tokens_t5)
            mx.eval(t5_conditioning)
            conditioning = t5_conditioning
        else:
            tokens_l = self._tokenize(
                self.tokenizer_l,
                text,
                (negative_text if cfg_weight > 1 else None),
            )
            tokens_g = self._tokenize(
                self.tokenizer_g,
                text,
                (negative_text if cfg_weight > 1 else None),
            )

            conditioning_l = self.clip_l(tokens_l)
            conditioning_g = self.clip_g(tokens_g)
            conditioning = mx.concatenate(
                [conditioning_l.hidden_states[-2], conditioning_g.hidden_states[-2]],
                axis=-1,
            )
            pooled_conditioning = mx.concatenate(
                [conditioning_l.pooled_output, conditioning_g.pooled_output],
                axis=-1,
            )

            conditioning = mx.concatenate(
                [
                    conditioning,
                    mx.zeros(
                        (
                            conditioning.shape[0],
                            conditioning.shape[1],
                            4096 - conditioning.shape[2],
                        )
                    ),
                ],
                axis=-1,
            )

            if self.use_t5:
                tokens_t5 = self._tokenize(
                    self.t5_tokenizer,
                    text,
                    (negative_text if cfg_weight > 1 else None),
                )
                t5_conditioning = self.t5_encoder(tokens_t5)
                mx.eval(t5_conditioning)
            else:
                t5_conditioning = mx.zeros_like(conditioning)
            conditioning = mx.concatenate([conditioning, t5_conditioning], axis=1)

        return conditioning, pooled_conditioning

    def denoise_latents(
        self,
        conditioning,
        pooled_conditioning,
        num_steps: int = 2,
        cfg_weight: float = 0.0,
        latent_size: Tuple[int] = (64, 64),
        seed=None,
        image_path: Optional[str] = None,
        denoise: float = 1.0,
    ):
        # Set the PRNG state
        seed = int(time.time()) if seed is None else seed
        logger.info(f"Seed: {seed}")
        mx.random.seed(seed)

        x_T = self.get_empty_latent(*latent_size)
        if image_path is None:
            denoise = 1.0
        else:
            x_T = self.encode_image_to_latents(image_path, seed=seed)
            # FIXME(arda): Fast hack to get the model working
            if self.is_flux:
                x_T = FluxLatentFormat().process_in(x_T)
            else:
                x_T = SD3LatentFormat().process_in(x_T)
        noise = self.get_noise(seed, x_T)
        sigmas = self.get_sigmas(self.sampler, num_steps)
        sigmas = sigmas[int(num_steps * (1 - denoise)) :]
        extra_args = {
            "conditioning": conditioning,
            "cfg_weight": cfg_weight,
            "pooled_conditioning": pooled_conditioning,
        }
        noise_scaled = self.sampler.noise_scaling(
            sigmas[0], noise, x_T, self.max_denoise(sigmas)
        )
        latent, iter_time = sample_euler(
            CFGDenoiser(self), noise_scaled, sigmas, extra_args=extra_args
        )
        # FIXME(arda): Fast hack to get the model working
        if self.is_flux:
            latent = FluxLatentFormat().process_out(latent)
        else:
            latent = SD3LatentFormat().process_out(latent)
        return latent, iter_time

    def generate_image(
        self,
        text: str,
        num_steps: int = 2,
        cfg_weight: float = 0.0,
        negative_text: str = "",
        latent_size: Tuple[int] = (64, 64),
        seed=None,
        verbose: bool = True,
        image_path: Optional[str] = None,
        denoise: float = 1.0,
    ):
        # Start timing
        start_time = time.time()

        # Initialize the memory log
        log = {
            "text_encoding": {
                "pre": {
                    "peak_memory": round(
                        bytes2gigabytes(mx.metal.get_peak_memory()), 3
                    ),
                    "active_memory": round(
                        bytes2gigabytes(mx.metal.get_active_memory()), 3
                    ),
                },
                "post": {"peak_memory": None, "active_memory": None},
            },
            "denoising": {
                "pre": {"peak_memory": None, "active_memory": None},
                "post": {"peak_memory": None, "active_memory": None},
            },
            "decoding": {
                "pre": {"peak_memory": None, "active_memory": None},
                "post": {"peak_memory": None, "active_memory": None},
            },
            "peak_memory": 0.0,
        }

        # Get the text conditioning
        text_encoding_start_time = time.time()
        if verbose:
            logger.info(
                f"Pre text encoding peak memory: {log['text_encoding']['pre']['peak_memory']}GB"
            )
            logger.info(
                f"Pre text encoding active memory: {log['text_encoding']['pre']['active_memory']}GB"
            )
        conditioning, pooled_conditioning = self.encode_text(
            text, cfg_weight, negative_text
        )
        mx.eval(conditioning)
        mx.eval(pooled_conditioning)
        log["text_encoding"]["post"]["peak_memory"] = round(
            bytes2gigabytes(mx.metal.get_peak_memory()), 3
        )
        log["text_encoding"]["post"]["active_memory"] = round(
            bytes2gigabytes(mx.metal.get_active_memory()), 3
        )
        log["peak_memory"] = max(
            log["peak_memory"], log["text_encoding"]["post"]["peak_memory"]
        )
        log["text_encoding"]["time"] = round(time.time() - text_encoding_start_time, 3)
        if verbose:
            logger.info(
                f"Post text encoding peak memory: {log['text_encoding']['post']['peak_memory']}GB"
            )
            logger.info(
                f"Post text encoding active memory: {log['text_encoding']['post']['active_memory']}GB"
            )
            logger.info(f"Text encoding time: {log['text_encoding']['time']}s")

        # unload T5 and CLIP models after obtaining conditioning in low memory mode
        if self.low_memory_mode:
            if hasattr(self, "t5_encoder"):
                del self.t5_encoder
            if hasattr(self, "clip_g"):
                del self.clip_g
            del self.clip_l
            gc.collect()

        logger.debug(f"Conditioning dtype before casting: {conditioning.dtype}")
        logger.debug(
            f"Pooled Conditioning dtype before casting: {pooled_conditioning.dtype}"
        )
        conditioning = conditioning.astype(self.activation_dtype)
        pooled_conditioning = pooled_conditioning.astype(self.activation_dtype)
        logger.debug(f"Conditioning dtype after casting: {conditioning.dtype}")
        logger.debug(
            f"Pooled Conditioning dtype after casting: {pooled_conditioning.dtype}"
        )

        # Generate img_ids and txt_ids if flux model is used
        img_ids, txt_ids = None, None
        if self.is_flux:
            img_ids, txt_ids = self.generate_ids(latent_size=latent_size)

        # Reset peak memory info
        mx.metal.reset_peak_memory()

        # Generate the latents
        denoising_start_time = time.time()
        log["denoising"]["pre"]["peak_memory"] = round(
            bytes2gigabytes(mx.metal.get_peak_memory()), 3
        )
        log["denoising"]["pre"]["active_memory"] = round(
            bytes2gigabytes(mx.metal.get_active_memory()), 3
        )
        log["peak_memory"] = max(
            log["peak_memory"], log["denoising"]["pre"]["peak_memory"]
        )
        if verbose:
            logger.info(
                f"Pre denoise peak memory: {log['denoising']['pre']['peak_memory']}GB"
            )
            logger.info(
                f"Pre denoise active memory: {log['denoising']['pre']['active_memory']}GB"
            )

        # TODO(arda): Implement denoising for FLUX model, wire up img_ids and txt_ids
        latents, iter_time = self.denoise_latents(
            conditioning,
            pooled_conditioning,
            num_steps=num_steps,
            cfg_weight=cfg_weight,
            latent_size=latent_size,
            seed=seed,
            image_path=image_path,
            denoise=denoise,
        )
        mx.eval(latents)

        log["denoising"]["post"]["peak_memory"] = round(
            bytes2gigabytes(mx.metal.get_peak_memory()), 3
        )
        log["denoising"]["post"]["active_memory"] = round(
            bytes2gigabytes(mx.metal.get_active_memory()), 3
        )
        log["peak_memory"] = max(
            log["peak_memory"], log["denoising"]["post"]["peak_memory"]
        )
        log["denoising"]["time"] = round(time.time() - denoising_start_time, 3)
        log["denoising"]["iter_time"] = iter_time
        if verbose:
            logger.info(
                f"Post denoise peak memory: {log['denoising']['post']['peak_memory']}GB"
            )
            logger.info(
                f"Post denoise active memory: {log['denoising']['post']['active_memory']}GB"
            )
            logger.info(f"Denoising time: {log['denoising']['time']}s")

        # unload MMDIT and Sampler models after obtaining latents in low memory mode
        if self.low_memory_mode:
            del self.mmdit
            del self.sampler
            gc.collect()

        logger.debug(f"Latents dtype before casting: {latents.dtype}")
        latents = latents.astype(self.activation_dtype)
        logger.debug(f"Latents dtype after casting: {latents.dtype}")

        # Reset peak memory info
        mx.metal.reset_peak_memory()

        # Decode the latents
        decoding_start_time = time.time()
        log["decoding"]["pre"]["peak_memory"] = round(
            bytes2gigabytes(mx.metal.get_peak_memory()), 3
        )
        log["decoding"]["pre"]["active_memory"] = round(
            bytes2gigabytes(mx.metal.get_active_memory()), 3
        )
        log["peak_memory"] = max(
            log["peak_memory"], log["decoding"]["pre"]["peak_memory"]
        )
        if verbose:
            logger.info(
                f"Pre decode peak memory: {log['decoding']['pre']['peak_memory']}GB"
            )
            logger.info(
                f"Pre decode active memory: {log['decoding']['pre']['active_memory']}GB"
            )
        latents = latents.astype(mx.float32)  # FIXME
        decoded = self.decode_latents_to_image(latents)
        mx.eval(decoded)

        log["decoding"]["post"]["peak_memory"] = round(
            bytes2gigabytes(mx.metal.get_peak_memory()), 3
        )
        log["decoding"]["post"]["active_memory"] = round(
            bytes2gigabytes(mx.metal.get_active_memory()), 3
        )
        log["peak_memory"] = max(
            log["peak_memory"], log["decoding"]["post"]["peak_memory"]
        )
        log["decoding"]["time"] = round(time.time() - decoding_start_time, 3)
        if verbose:
            logger.info(
                f"Post decode peak memory: {log['decoding']['post']['peak_memory']}GB"
            )
            logger.info(
                f"Post decode active memory: {log['decoding']['post']['active_memory']}GB"
            )
            logger.info(f"Decoding time: {log['decoding']['time']}s")
            logger.info(f"Peak memory: {log['peak_memory']}GB")

        # unload VAE Decoder model after decoding in low memory mode
        if self.low_memory_mode:
            del self.decoder
            gc.collect()

        # Convert the decoded images to uint8
        x = mx.concatenate(decoded, axis=0)
        x = (x * 255).astype(mx.uint8)

        # End timing
        end_time = time.time()
        log["total_time"] = round(end_time - start_time, 3)
        if verbose:
            logger.info(f"Total time: {log['total_time']}s")

        return Image.fromarray(np.array(x)), log

    def generate_ids(self, latent_size: Tuple[int]):
        h, w = latent_size
        img_ids = mx.zeros((h // 2, w // 2, 3))
        img_ids[..., 1] = img_ids[..., 1] + mx.arange(h // 2)[:, None]
        img_ids[..., 2] = img_ids[..., 2] + mx.arange(w // 2)[None, :]
        img_ids = img_ids.reshape(1, -1, 3)

        txt_ids = mx.zeros((1, 256, 3))  # Hardcoded to context length of T5
        return img_ids, txt_ids

    def read_image(self, image_path: str):
        # Read the image
        img = Image.open(image_path)

        # Make sure image shape is divisible by 64
        W, H = (dim - dim % 64 for dim in (img.width, img.height))
        if W != img.width or H != img.height:
            print(
                f"Warning: image shape is not divisible by 64, downsampling to {W}x{H}"
            )
            img = img.resize((W, H), Image.LANCZOS)  # use desired downsampling filter

        img = mx.array(np.array(img))
        img = (img[:, :, :3].astype(mx.float32) / 255) * 2 - 1.0

        return mx.expand_dims(img, axis=0)

    def get_noise(self, seed, x_T):
        np.random.seed(seed)
        noise = np.random.randn(*x_T.transpose(0, 3, 1, 2).shape)
        noise = mx.array(noise).transpose(0, 2, 3, 1)
        return noise

    def get_sigmas(self, sampler, num_steps: int):
        start = sampler.timestep(sampler.sigma_max).item()
        end = sampler.timestep(sampler.sigma_min).item()
        timesteps = mx.linspace(start, end, num_steps)
        sigs = []
        for x in range(len(timesteps)):
            ts = timesteps[x]
            sigs.append(sampler.sigma(ts))
        sigs += [0.0]
        return mx.array(sigs)

    def get_empty_latent(self, *shape):
        return mx.ones([1, *shape, 16]) * 0.0609

    def max_denoise(self, sigmas):
        max_sigma = float(self.sampler.sigma_max.item())
        sigma = float(sigmas[0].item())
        return math.isclose(max_sigma, sigma, rel_tol=1e-05) or sigma > max_sigma

    def decode_latents_to_image(self, x_t):
        x = self.decoder(x_t)
        x = mx.clip(x / 2 + 0.5, 0, 1)
        return x

    def encode_image_to_latents(self, image_path: str, seed):
        image = self.read_image(image_path)
        hidden = self.encoder(image)
        mean, logvar = hidden.split(2, axis=-1)
        logvar = mx.clip(logvar, -30.0, 20.0)
        std = mx.exp(0.5 * logvar)
        noise = self.get_noise(seed, mean)

        return mean + std * noise


class CFGDenoiser(nn.Module):
    """Helper for applying CFG Scaling to diffusion outputs"""

    def __init__(self, model: DiffusionPipeline):
        super().__init__()
        self.model = model

    def __call__(
        self, x_t, t, conditioning, cfg_weight: float = 7.5, pooled_conditioning=None
    ):
        if cfg_weight <= 0:
            logger.info("CFG Weight disabled")
            x_t_mmdit = x_t.astype(self.model.activation_dtype)
        else:
            x_t_mmdit = mx.concatenate([x_t] * 2, axis=0).astype(
                self.model.activation_dtype
            )
        t_mmdit = mx.broadcast_to(t, [len(x_t_mmdit)])
        timestep = self.model.sampler.timestep(t_mmdit).astype(
            self.model.activation_dtype
        )
        mmdit_input = {
            "latent_image_embeddings": x_t_mmdit,
            "token_level_text_embeddings": mx.expand_dims(conditioning, 2),
            "pooled_text_embeddings": mx.expand_dims(
                mx.expand_dims(pooled_conditioning, 1), 1
            ),
            "timestep": timestep,
        }
        print("timestep", timestep)  # FIXME
        mmdit_output = self.model.mmdit(**mmdit_input)
        eps_pred = self.model.sampler.calculate_denoised(
            t_mmdit, mmdit_output, x_t_mmdit
        )
        if cfg_weight <= 0:
            return eps_pred
        else:
            eps_text, eps_neg = eps_pred.split(2)
            return eps_neg + cfg_weight * (eps_text - eps_neg)


class LatentFormat:
    """Base class for latent format conversion"""

    def __init__(self):
        self.scale_factor = 1.0
        self.shift_factor = 0.0

    def process_in(self, latent):
        return (latent - self.shift_factor) * self.scale_factor

    def process_out(self, latent):
        return (latent / self.scale_factor) + self.shift_factor


class SD3LatentFormat(LatentFormat):
    """Latents are slightly shifted from center - this class must be called after VAE Decode to correct for the shift"""

    def __init__(self):
        super().__init__()
        self.scale_factor = 1.5305
        self.shift_factor = 0.0609


class FluxLatentFormat(LatentFormat):
    def __init__(self):
        super().__init__()
        self.scale_factor = 0.3611
        self.shift_factor = 0.1159


def append_dims(x, target_dims):
    """Appends dimensions to the end of a tensor until it has target_dims dimensions."""
    dims_to_append = target_dims - x.ndim
    return x[(...,) + (None,) * dims_to_append]


def to_d(x, sigma, denoised):
    """Converts a denoiser output to a Karras ODE derivative."""
    return (x - denoised) / append_dims(sigma, x.ndim)


def sample_euler(model: CFGDenoiser, x, sigmas, extra_args=None):
    """Implements Algorithm 2 (Euler steps) from Karras et al. (2022)."""
    extra_args = {} if extra_args is None else extra_args
    s_in = mx.ones([x.shape[0]])
    from tqdm import trange

    sigmas = mx.array([1.0, 0.75, 0.5, 0.25, 0.0])  # FIXME
    t = trange(len(sigmas) - 1)
    iter_time = []
    for i in t:
        start_time = t.format_dict["elapsed"]
        sigma_hat = sigmas[i]
        print(sigma_hat)  # FIXME
        denoised = model(x, sigma_hat * s_in, **extra_args)
        d = to_d(x, sigma_hat, denoised)
        dt = sigmas[i + 1] - sigma_hat
        # Euler method
        x = x + d * dt
        mx.eval(x)
        end_time = t.format_dict["elapsed"]
        iter_time.append(round((end_time - start_time), 3))
    return x, iter_time
