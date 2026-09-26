"""Generation handler for Flow2API"""
import asyncio
import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, AsyncGenerator, List, Dict, Any
from ..core.logger import debug_logger
from ..core.config import config
from ..core.monitoring import record_generation_result
from ..core.models import Task, RequestLog
from ..core.account_tiers import (
    PAYGATE_TIER_NOT_PAID,
    PAYGATE_TIER_TWO,
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .file_cache import FileCache
from .flow_client import classify_upsample_error
from .token_manager import upsample_quota_key
from .watermark_remover import clean_image_bytes
from .characters import (
    MAX_CHARACTERS_IMAGE,
    MAX_CHARACTERS_VIDEO,
    CharacterService,
    CharacterSetupError,
    LoadedCharacter,
    build_prompt_parts,
    validate_characters,
)


# Reference-image limits, from Flow's own model list (GET aisandbox-pa /v1/flow/models,
# usages[].inputSpec.maxImageReferences, read 2026-09-22): Nano Banana Pro / 2 / 2 Lite = 10,
# Omni 1.1 Flash abra_r2v_* = 7, Veo 3.1 Fast and Lite r2v = 3.
FLOW_IMAGE_MAX_REFERENCES = 10
FLOW_OMNI_MAX_REFERENCES = 7
# Reference images upload this many at a time (one by one took ~6.6 s each).
REFERENCE_UPLOAD_CONCURRENCY = 3


# Model configuration
MODEL_CONFIG = {
    # Image generation - GEM_PIX_2 (Gemini 3.0 Pro)
    "gemini-3.0-pro-image-landscape": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.0-pro-image-portrait": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.0-pro-image-square": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.0-pro-image-four-three": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.0-pro-image-three-four": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },

    # Image generation - GEM_PIX_2 (Gemini 3.0 Pro) 2K upscale
    "gemini-3.0-pro-image-landscape-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-portrait-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-square-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-four-three-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.0-pro-image-three-four-2k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },

    # Image generation - GEM_PIX_2 (Gemini 3.0 Pro) 4K upscale
    "gemini-3.0-pro-image-landscape-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-portrait-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-square-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-four-three-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.0-pro-image-three-four-4k": {
        "type": "image",
        "model_name": "GEM_PIX_2",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # Image generation - IMAGEN_3_5 (Imagen 4.0)
    "imagen-4.0-generate-preview-landscape": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "imagen-4.0-generate-preview-portrait": {
        "type": "image",
        "model_name": "IMAGEN_3_5",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },

    # Image generation - NARWHAL (new)
    "gemini-3.1-flash-image-landscape": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "gemini-3.1-flash-image-portrait": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "gemini-3.1-flash-image-square": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "gemini-3.1-flash-image-four-three": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "gemini-3.1-flash-image-three-four": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },
    "gemini-3.1-flash-image-landscape-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-portrait-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-square-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-four-three-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-three-four-2k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"
    },
    "gemini-3.1-flash-image-landscape-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-portrait-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-square-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-four-three-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },
    "gemini-3.1-flash-image-three-four-4k": {
        "type": "image",
        "model_name": "NARWHAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR",
        "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"
    },

    # Image generation - HARBOR_SEAL (Nano Banana 2 Lite)
    # Lite: 1K only (no 2K/4K upscale), 5 aspect ratios
    "nano-banana-2-lite-landscape": {
        "type": "image",
        "model_name": "HARBOR_SEAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"
    },
    "nano-banana-2-lite-portrait": {
        "type": "image",
        "model_name": "HARBOR_SEAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT"
    },
    "nano-banana-2-lite-square": {
        "type": "image",
        "model_name": "HARBOR_SEAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_SQUARE"
    },
    "nano-banana-2-lite-four-three": {
        "type": "image",
        "model_name": "HARBOR_SEAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"
    },
    "nano-banana-2-lite-three-four": {
        "type": "image",
        "model_name": "HARBOR_SEAL",
        "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"
    },

    # ========== Text to video (T2V) ==========
    # No image upload; text prompt only

    # veo_3_1_t2v_fast_portrait (portrait)
    # Upstream model name: veo_3_1_t2v_fast_portrait
    "veo_3_1_t2v_fast_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
    },
    # veo_3_1_t2v_fast_landscape (landscape)
    # Upstream model name: veo_3_1_t2v_fast
    "veo_3_1_t2v_fast_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
    },

    # veo_3_1_t2v_fast_ultra (landscape + portrait)
    "veo_3_1_t2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
    },
    "veo_3_1_t2v_fast_ultra": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
    },

    # veo_3_1_t2v_fast_ultra_relaxed (landscape + portrait)
    "veo_3_1_t2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
    },
    "veo_3_1_t2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
    },

    # veo_3_1_t2v (landscape + portrait)
    "veo_3_1_t2v_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
    },
    "veo_3_1_t2v_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
    },
    # veo_3_1_t2v_lite (landscape + portrait, from labs.google.har)
    "veo_3_1_t2v_lite_portrait": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_t2v_lite_landscape": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },

    # ========== First/last frame models (I2V - Image to Video) ==========
    # 1-2 images: 1 = first frame, 2 = first + last frame

    # veo_3_1_i2v_s_fast_fl (needs landscape + portrait added)
    "veo_3_1_i2v_s_fast_portrait_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra (landscape + portrait)
    "veo_3_1_i2v_s_fast_portrait_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_fl": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s_fast_ultra_relaxed (needs landscape + portrait added)
    "veo_3_1_i2v_s_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },

    # veo_3_1_i2v_s (needs landscape + portrait added)
    "veo_3_1_i2v_s_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    "veo_3_1_i2v_s_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2
    },
    # veo_3_1_i2v_lite (landscape + portrait, first frame only, from labs.google.har)
    "veo_3_1_i2v_lite_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 1,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_i2v_lite_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 1,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    # veo_3_1_interpolation_lite (landscape + portrait, first + last frame, from labs.google.har)
    "veo_3_1_interpolation_lite_portrait": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_interpolation_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 2,
        "max_images": 2,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },
    "veo_3_1_interpolation_lite_landscape": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_interpolation_lite",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 2,
        "max_images": 2,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False
    },

    # ========== Multi-image (R2V - Reference Images to Video) ==========
    # Upstream currently allows at most 3 reference images

    # veo_3_1_r2v_fast (landscape + portrait)
    "veo_3_1_r2v_fast_portrait": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra (landscape + portrait)
    "veo_3_1_r2v_fast_portrait_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # veo_3_1_r2v_fast_ultra_relaxed (landscape + portrait)
    "veo_3_1_r2v_fast_portrait_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },
    "veo_3_1_r2v_fast_ultra_relaxed": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra_relaxed",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3
    },

    # ========== Video upscale (Video Upsampler) ==========
    # 3.1 only; generates the video then upscales, can take 30 minutes

    # T2V 4K upscale
    "veo_3_1_t2v_fast_portrait_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_t2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # T2V 1080P upscale
    "veo_3_1_t2v_fast_portrait_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_t2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "t2v",
        "model_key": "veo_3_1_t2v_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # I2V 4K upscale
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_4k": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # I2V 1080P upscale
    "veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_portrait_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_i2v_s_fast_ultra_fl_1080p": {
        "type": "video",
        "video_type": "i2v",
        "model_key": "veo_3_1_i2v_s_fast_ultra_fl",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 1,
        "max_images": 2,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # R2V 4K upscale
    "veo_3_1_r2v_fast_portrait_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },
    "veo_3_1_r2v_fast_ultra_4k": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
    },

    # R2V 1080P upscale
    "veo_3_1_r2v_fast_portrait_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },
    "veo_3_1_r2v_fast_ultra_1080p": {
        "type": "video",
        "video_type": "r2v",
        "model_key": "veo_3_1_r2v_fast_landscape_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": 3,
        "upsample": {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}
    },

    # ========== Video extend (Video Continuation) ==========
    # Extends a generated video by 7s, up to 20 times (max 148s)
    # Needs the source video's mediaGenerationId

    # VEO 3.1 Extend (landscape + portrait)
    "veo_3_1_extend_portrait": {
        "type": "video",
        "video_type": "extend",
        "model_key": "veo_3_1_extend_fast_portrait_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": False,
        "requires_video_id": True,
    },
    "veo_3_1_extend": {
        "type": "video",
        "video_type": "extend",
        "model_key": "veo_3_1_extend_fast_ultra",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": False,
        "requires_video_id": True,
    },
    # ========== Gemini Omni Flash ==========
    # 2026-05-26 real upstream requests observed:
    # - text only -> video:batchAsyncGenerateVideoText, videoModelKey=abra_t2v_8s
    # - reference images -> video:batchAsyncGenerateVideoReferenceImages, videoModelKey=abra_r2v_8s
    "omni": {
        "type": "video",
        "video_type": "omni",
        "model_key": "abra_t2v_8s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": FLOW_OMNI_MAX_REFERENCES,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False,
        "reference_model_key": "abra_r2v_8s",
        "reference_duration": 8,
        "reference_model_display_name": "Omni Flash",
    },
    "omni_portrait": {
        "type": "video",
        "video_type": "omni",
        "model_key": "abra_t2v_8s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": FLOW_OMNI_MAX_REFERENCES,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False,
        "reference_model_key": "abra_r2v_8s",
        "reference_duration": 8,
        "reference_model_display_name": "Omni Flash",
    },
    # Omni Flash pinned to 720p output (outputSpec.resolution). Same upstream model
    # key as "omni"; only the requested output resolution differs. Adapted from
    # Gurumigun/flow2api 4093385a.
    "omni-flash": {
        "type": "video",
        "video_type": "omni",
        "model_key": "abra_t2v_8s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        "supports_images": True,
        "min_images": 0,
        "max_images": FLOW_OMNI_MAX_REFERENCES,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False,
        "output_resolution": "VIDEO_RESOLUTION_720P",
        "reference_model_key": "abra_r2v_8s",
        "reference_duration": 8,
        "reference_model_display_name": "Omni Flash",
    },
    "omni-flash-portrait": {
        "type": "video",
        "video_type": "omni",
        "model_key": "abra_t2v_8s",
        "aspect_ratio": "VIDEO_ASPECT_RATIO_PORTRAIT",
        "supports_images": True,
        "min_images": 0,
        "max_images": FLOW_OMNI_MAX_REFERENCES,
        "use_v2_model_config": True,
        "allow_tier_upgrade": False,
        "output_resolution": "VIDEO_RESOLUTION_720P",
        "reference_model_key": "abra_r2v_8s",
        "reference_duration": 8,
        "reference_model_display_name": "Omni Flash",
    },
}


# ── Omni 1.1: first-frame (1 image) / first+last frame (2 images) use abra_i2v_*s;
#    3+ images stay on the reference-images route (abra_r2v_*s). Adapted from
#    Danborad/flow2api 7059fe14 + f9b8e8e2.
for _omni_key in ("omni", "omni_portrait", "omni-flash", "omni-flash-portrait"):
    MODEL_CONFIG[_omni_key].setdefault("first_frame_model_key", "abra_i2v_8s")
    MODEL_CONFIG[_omni_key].setdefault("start_end_model_key", "abra_i2v_8s")
    MODEL_CONFIG[_omni_key]["reference_model_display_name"] = "Omni 1.1 Flash"

# ── Duration families: omni_4s / omni_6s / omni_8s / omni_10s (+ _portrait), and the
#    720p omni-flash_{d}s twins. Each duration is its own upstream model key.
for _duration in (4, 6, 8, 10):
    for _base_key, _new_key in (
        ("omni", f"omni_{_duration}s"),
        ("omni_portrait", f"omni_{_duration}s_portrait"),
        ("omni-flash", f"omni-flash_{_duration}s"),
        ("omni-flash-portrait", f"omni-flash_{_duration}s_portrait"),
    ):
        _cfg = dict(MODEL_CONFIG[_base_key])
        _cfg["model_key"] = f"abra_t2v_{_duration}s"
        _cfg["first_frame_model_key"] = f"abra_i2v_{_duration}s"
        _cfg["start_end_model_key"] = f"abra_i2v_{_duration}s"
        _cfg["reference_model_key"] = f"abra_r2v_{_duration}s"
        _cfg["reference_duration"] = _duration
        MODEL_CONFIG[_new_key] = _cfg

# ── Omni ingredients-only: every image is a reference, even with 1 or 2 images
#    (plain "omni" treats 1-2 images as first/last frames).
for _duration in (None, 4, 6, 8, 10):
    for _base_key, _new_key in (
        ("omni", "omni_r2v"),
        ("omni_portrait", "omni_r2v_portrait"),
    ):
        _src = _base_key if _duration is None else (
            f"omni_{_duration}s" if _base_key == "omni" else f"omni_{_duration}s_portrait"
        )
        if _duration is not None:
            _new_key = f"omni_r2v_{_duration}s" + ("_portrait" if _base_key == "omni_portrait" else "")
        _cfg = dict(MODEL_CONFIG[_src])
        _cfg["reference_only"] = True
        _cfg["min_images"] = 1
        MODEL_CONFIG[_new_key] = _cfg


def _estimate_video_credit_cost_for_log(model: str, model_config: Dict[str, Any]) -> int:
    """Flow credit cost of a video request, for the request log only."""
    if model_config.get("type") != "video":
        return 0
    model_text = f"{model} {model_config.get('model_key', '')}".lower()
    if "veo_3_1" in model_text and "lite" in model_text:
        return 10
    if model_config.get("video_type") == "omni" or "abra_" in model_text:
        duration = int(model_config.get("reference_duration") or 8)
        return {4: 7, 6: 10, 8: 12, 10: 15}.get(duration, 12)
    return 0


def _make_t2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "t2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": False,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _make_i2v_config(
    model_key: str,
    aspect_ratio: str,
    *,
    min_images: int = 1,
    max_images: int = 2,
    use_v2_model_config: bool = False,
    allow_tier_upgrade: bool = True,
    upsample: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "type": "video",
        "video_type": "i2v",
        "model_key": model_key,
        "aspect_ratio": aspect_ratio,
        "supports_images": True,
        "min_images": min_images,
        "max_images": max_images,
    }
    if use_v2_model_config:
        cfg["use_v2_model_config"] = True
    if not allow_tier_upgrade:
        cfg["allow_tier_upgrade"] = False
    if upsample:
        cfg["upsample"] = upsample
    return cfg


def _apply_veo_3_1_model_updates():
    """Keep the public aliases aligned with the current Veo 3.1 model families."""
    landscape = "VIDEO_ASPECT_RATIO_LANDSCAPE"
    portrait = "VIDEO_ASPECT_RATIO_PORTRAIT"

    def add_alias(alias: str, target: str):
        MODEL_CONFIG[alias] = dict(MODEL_CONFIG[target])

    def add_default_duration_aliases(
        base_alias: str,
        landscape_target: str,
        portrait_target: str,
        *,
        fl_suffix: bool = False,
    ):
        if fl_suffix:
            add_alias(f"{base_alias}_8s_fl", landscape_target)
            add_alias(f"{base_alias}_portrait_8s_fl", portrait_target)
            add_alias(f"{base_alias}_landscape_8s_fl", landscape_target)
            return

        add_alias(f"{base_alias}_8s", landscape_target)
        add_alias(f"{base_alias}_portrait_8s", portrait_target)
        add_alias(f"{base_alias}_landscape_8s", landscape_target)

    def add_default_duration_upsample_aliases(
        base_alias: str,
        resolution_name: str,
        landscape_target: str,
        portrait_target: str,
    ):
        add_alias(f"{base_alias}_8s_{resolution_name}", landscape_target)
        add_alias(f"{base_alias}_portrait_8s_{resolution_name}", portrait_target)
        add_alias(f"{base_alias}_landscape_8s_{resolution_name}", landscape_target)

    # Non-fast/non-lite Veo 3.1 aliases must call Quality upstream keys.
    MODEL_CONFIG["veo_3_1_t2v_landscape"].update({"model_key": "veo_3_1_t2v"})
    MODEL_CONFIG["veo_3_1_t2v_portrait"].update({"model_key": "veo_3_1_t2v_portrait"})
    MODEL_CONFIG["veo_3_1_i2v_s_landscape"].update({"model_key": "veo_3_1_i2v_s_fl"})
    MODEL_CONFIG["veo_3_1_i2v_s_portrait"].update({"model_key": "veo_3_1_i2v_s_portrait_fl"})
    MODEL_CONFIG["veo_3_1_extend"].update({"model_key": "veo_3_1_extend_landscape"})
    MODEL_CONFIG["veo_3_1_extend_portrait"].update({"model_key": "veo_3_1_extend_portrait"})

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # T2V duration variants.
        MODEL_CONFIG[f"veo_3_1_t2v_fast_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_fast_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_fast_{suffix}", portrait
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_landscape"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            landscape,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_lite_{suffix}_portrait"] = _make_t2v_config(
            f"veo_3_1_t2v_lite_{suffix}",
            portrait,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_t2v_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", landscape
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}"] = _make_t2v_config(
            f"veo_3_1_t2v_quality_{suffix}", portrait
        )

        # I2V duration variants. FL keys are used for 2 images; the single-image path strips "_fl".
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_fast_portrait_{suffix}_fl"] = _make_i2v_config(
            f"veo_3_1_i2v_s_fast_{suffix}_fl", portrait
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            landscape,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}",
            portrait,
            min_images=1,
            max_images=1,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_landscape"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            landscape,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_interpolation_lite_{suffix}_portrait"] = _make_i2v_config(
            f"veo_3_1_i2v_s_lite_{suffix}_fl",
            portrait,
            min_images=2,
            max_images=2,
            use_v2_model_config=True,
            allow_tier_upgrade=False,
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}"] = _make_i2v_config(
            f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait
        )

        for resolution_name, resolution, upsampler_model_key in (
            ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
            ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
        ):
            upsample = {"resolution": resolution, "model_key": upsampler_model_key}
            MODEL_CONFIG[f"veo_3_1_t2v_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_t2v_portrait_{suffix}_{resolution_name}"] = _make_t2v_config(
                f"veo_3_1_t2v_quality_{suffix}", portrait, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", landscape, upsample=upsample
            )
            MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{suffix}_{resolution_name}"] = _make_i2v_config(
                f"veo_3_1_i2v_s_quality_{suffix}_fl", portrait, upsample=upsample
            )

    for resolution_name, resolution, upsampler_model_key in (
        ("4k", "VIDEO_RESOLUTION_4K", "veo_3_1_upsampler_4k"),
        ("1080p", "VIDEO_RESOLUTION_1080P", "veo_3_1_upsampler_1080p"),
    ):
        upsample = {"resolution": resolution, "model_key": upsampler_model_key}
        MODEL_CONFIG[f"veo_3_1_t2v_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_t2v_portrait_{resolution_name}"] = _make_t2v_config(
            "veo_3_1_t2v_portrait", portrait, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_fl", landscape, upsample=upsample
        )
        MODEL_CONFIG[f"veo_3_1_i2v_s_portrait_{resolution_name}"] = _make_i2v_config(
            "veo_3_1_i2v_s_portrait_fl", portrait, upsample=upsample
        )

    for seconds in (4, 6):
        suffix = f"{seconds}s"

        # Explicit landscape names for /v1/models; short landscape names remain compatible.
        add_alias(f"veo_3_1_t2v_fast_landscape_{suffix}", f"veo_3_1_t2v_fast_{suffix}")
        add_alias(f"veo_3_1_t2v_landscape_{suffix}", f"veo_3_1_t2v_{suffix}")
        add_alias(f"veo_3_1_i2v_s_fast_landscape_{suffix}_fl", f"veo_3_1_i2v_s_fast_{suffix}_fl")
        add_alias(f"veo_3_1_i2v_s_landscape_{suffix}", f"veo_3_1_i2v_s_{suffix}")

        add_alias(f"veo_3_1_t2v_lite_landscape_{suffix}", f"veo_3_1_t2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_t2v_lite_portrait_{suffix}", f"veo_3_1_t2v_lite_{suffix}_portrait")
        add_alias(f"veo_3_1_i2v_lite_landscape_{suffix}", f"veo_3_1_i2v_lite_{suffix}_landscape")
        add_alias(f"veo_3_1_i2v_lite_portrait_{suffix}", f"veo_3_1_i2v_lite_{suffix}_portrait")
        add_alias(
            f"veo_3_1_interpolation_lite_landscape_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_landscape",
        )
        add_alias(
            f"veo_3_1_interpolation_lite_portrait_{suffix}",
            f"veo_3_1_interpolation_lite_{suffix}_portrait",
        )

        for resolution_name in ("4k", "1080p"):
            add_alias(
                f"veo_3_1_t2v_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_t2v_{suffix}_{resolution_name}",
            )
            add_alias(
                f"veo_3_1_i2v_s_landscape_{suffix}_{resolution_name}",
                f"veo_3_1_i2v_s_{suffix}_{resolution_name}",
            )

    for resolution_name in ("4k", "1080p"):
        add_alias(f"veo_3_1_t2v_landscape_{resolution_name}", f"veo_3_1_t2v_{resolution_name}")
        add_alias(f"veo_3_1_i2v_s_landscape_{resolution_name}", f"veo_3_1_i2v_s_{resolution_name}")

    # Veo 3.1 Lite ingredients: one upstream key for both orientations, 8 s only, max 3 refs.
    for orientation, aspect in (("landscape", landscape), ("portrait", portrait)):
        MODEL_CONFIG[f"veo_3_1_r2v_lite_{orientation}"] = {
            "type": "video",
            "video_type": "r2v",
            "model_key": "veo_3_1_r2v_lite",
            "aspect_ratio": aspect,
            "supports_images": True,
            "min_images": 1,
            "max_images": 3,
            "use_v2_model_config": True,
            "allow_tier_upgrade": False,
        }
        add_alias(f"veo_3_1_r2v_lite_{orientation}_8s", f"veo_3_1_r2v_lite_{orientation}")
        add_alias(f"veo_3_1_r2v_lite_8s_{orientation}", f"veo_3_1_r2v_lite_{orientation}")

    add_alias("veo_3_1_r2v_fast_landscape", "veo_3_1_r2v_fast")
    add_alias("veo_3_1_r2v_fast_landscape_ultra", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_relaxed", "veo_3_1_r2v_fast_ultra_relaxed")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_4k", "veo_3_1_r2v_fast_ultra_4k")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_1080p", "veo_3_1_r2v_fast_ultra_1080p")

    add_default_duration_aliases(
        "veo_3_1_t2v_fast",
        "veo_3_1_t2v_fast_landscape",
        "veo_3_1_t2v_fast_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_t2v",
        "veo_3_1_t2v_landscape",
        "veo_3_1_t2v_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_i2v_s_fast",
        "veo_3_1_i2v_s_fast_fl",
        "veo_3_1_i2v_s_fast_portrait_fl",
        fl_suffix=True,
    )
    add_default_duration_aliases(
        "veo_3_1_i2v_s",
        "veo_3_1_i2v_s_landscape",
        "veo_3_1_i2v_s_portrait",
    )
    add_default_duration_aliases(
        "veo_3_1_r2v_fast",
        "veo_3_1_r2v_fast",
        "veo_3_1_r2v_fast_portrait",
    )
    add_alias("veo_3_1_r2v_fast_ultra_8s", "veo_3_1_r2v_fast_ultra")
    add_alias("veo_3_1_r2v_fast_portrait_ultra_8s", "veo_3_1_r2v_fast_portrait_ultra")
    add_alias("veo_3_1_r2v_fast_landscape_ultra_8s", "veo_3_1_r2v_fast_ultra")
    add_alias(
        "veo_3_1_r2v_fast_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_ultra_relaxed",
    )
    add_alias(
        "veo_3_1_r2v_fast_portrait_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_portrait_ultra_relaxed",
    )
    add_alias(
        "veo_3_1_r2v_fast_landscape_ultra_relaxed_8s",
        "veo_3_1_r2v_fast_ultra_relaxed",
    )

    add_alias("veo_3_1_t2v_lite_8s_landscape", "veo_3_1_t2v_lite_landscape")
    add_alias("veo_3_1_t2v_lite_8s_portrait", "veo_3_1_t2v_lite_portrait")
    add_alias("veo_3_1_t2v_lite_landscape_8s", "veo_3_1_t2v_lite_landscape")
    add_alias("veo_3_1_t2v_lite_portrait_8s", "veo_3_1_t2v_lite_portrait")
    add_alias("veo_3_1_i2v_lite_8s_landscape", "veo_3_1_i2v_lite_landscape")
    add_alias("veo_3_1_i2v_lite_8s_portrait", "veo_3_1_i2v_lite_portrait")
    add_alias("veo_3_1_i2v_lite_landscape_8s", "veo_3_1_i2v_lite_landscape")
    add_alias("veo_3_1_i2v_lite_portrait_8s", "veo_3_1_i2v_lite_portrait")
    add_alias(
        "veo_3_1_interpolation_lite_8s_landscape",
        "veo_3_1_interpolation_lite_landscape",
    )
    add_alias(
        "veo_3_1_interpolation_lite_8s_portrait",
        "veo_3_1_interpolation_lite_portrait",
    )
    add_alias(
        "veo_3_1_interpolation_lite_landscape_8s",
        "veo_3_1_interpolation_lite_landscape",
    )
    add_alias(
        "veo_3_1_interpolation_lite_portrait_8s",
        "veo_3_1_interpolation_lite_portrait",
    )

    for resolution_name in ("4k", "1080p"):
        add_default_duration_upsample_aliases(
            "veo_3_1_t2v",
            resolution_name,
            f"veo_3_1_t2v_{resolution_name}",
            f"veo_3_1_t2v_portrait_{resolution_name}",
        )
        add_default_duration_upsample_aliases(
            "veo_3_1_i2v_s",
            resolution_name,
            f"veo_3_1_i2v_s_{resolution_name}",
            f"veo_3_1_i2v_s_portrait_{resolution_name}",
        )


_apply_veo_3_1_model_updates()


def _known_video_model_keys() -> set[str]:
    return {
        cfg["model_key"]
        for cfg in MODEL_CONFIG.values()
        if cfg.get("type") == "video" and cfg.get("model_key")
    }


def _resolve_tier_two_model_key(model_key: str) -> str:
    """Only upgrade to an ultra key when that exact upstream key is known valid."""
    if "ultra" in model_key:
        return model_key
    if "_fl" in model_key:
        candidate = model_key.replace("_fl", "_ultra_fl")
    else:
        candidate = model_key + "_ultra"
    return candidate if candidate in _known_video_model_keys() else model_key



# 2K/4K enlarge (26 Sep 2026, tmp/upscale_honest_plan.md): "too much traffic" gets one more try after a
# real pause (before: 5 tries 3 s apart on the same IP, all refused), then this account's enlarge rests.
UPSAMPLE_TRAFFIC_ATTEMPTS = 2
UPSAMPLE_TRAFFIC_RETRY_DELAY_S = 10
UPSAMPLE_TRAFFIC_COOLDOWN_MIN = 15

class GenerationHandler:
    """Unified generation handler"""

    def __init__(self, flow_client, token_manager, load_balancer, db, concurrency_manager, proxy_manager):
        cache_dir = Path(__file__).resolve().parents[2] / "tmp"
        self.flow_client = flow_client
        self.token_manager = token_manager
        self.load_balancer = load_balancer
        self.db = db
        self.concurrency_manager = concurrency_manager
        self.file_cache = FileCache(
            cache_dir=str(cache_dir),
            default_timeout=config.cache_timeout,
            proxy_manager=proxy_manager,
            flow_client=flow_client,
        )
        self._watermark_unknown_sizes: set = set()

    def _create_generation_result(self) -> Dict[str, Any]:
        """????????????????"""
        return dict(success=False, error_message=None, error_emitted=False)

    def _create_response_state(self) -> Dict[str, Any]:
        """Create per-request response state so concurrent requests do not mix."""
        return {
            "url": None,
            "generated_assets": None,
            "base_url": None,
        }

    def _mark_generation_failed(self, generation_result: Optional[Dict[str, Any]], error_message: str):
        """????????????????????"""
        if isinstance(generation_result, dict):
            generation_result["success"] = False
            generation_result["error_message"] = error_message
            generation_result["error_emitted"] = True

    def _mark_generation_succeeded(self, generation_result: Optional[Dict[str, Any]]):
        """???????"""
        if isinstance(generation_result, dict):
            generation_result["success"] = True
            generation_result["error_message"] = None
            generation_result["error_emitted"] = False

    async def _resolve_video_asset(
        self,
        token,
        operation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve video assets per current upstream logic: status comes from media, URL via a second redirect call."""
        metadata = (operation.get("operation") or {}).get("metadata", {}) or {}
        video_info = metadata.get("video", {}) if isinstance(metadata.get("video"), dict) else {}
        media_name = (
            operation.get("mediaName")
            or video_info.get("mediaName")
            or video_info.get("mediaGenerationId")
            or operation.get("name")
            or (operation.get("operation") or {}).get("name")
        )

        video_url = ""
        if media_name and getattr(token, "st", None):
            video_url = await self.flow_client.get_media_url_redirect(
                token.st,
                media_name,
                media_url_type="MEDIA_URL_TYPE_FULL_MEDIA",
            ) or ""

        import re as _re
        uuid_match = _re.search(r"/video/([0-9a-f-]{36})", video_url or "")
        video_media_id = (
            uuid_match.group(1)
            if uuid_match
            else str(media_name or video_info.get("mediaGenerationId") or "")
        )

        return {
            "media_name": media_name,
            "video_url": video_url,
            "video_media_id": video_media_id,
            "aspect_ratio": video_info.get("aspectRatio", "VIDEO_ASPECT_RATIO_LANDSCAPE"),
            "model": video_info.get("model"),
            "duration": video_info.get("duration"),
            "metadata": metadata,
            "video_info": video_info,
        }

    def _normalize_error_message(self, error_message: Any, max_length: int = 1000) -> str:
        """Normalize error text so overly long content is not stored."""
        text = str(error_message or "").strip() or "Unknown error"
        if len(text) <= max_length:
            return text
        return f"{text[:max_length - 3]}..."

    async def _upload_reference_images(
        self,
        token,
        images: List[bytes],
        aspect_ratio: str,
        project_id: str,
    ) -> List[str]:
        """Upload images a few at a time; media ids come back in input order."""
        semaphore = asyncio.Semaphore(REFERENCE_UPLOAD_CONCURRENCY)

        async def _upload(image_bytes: bytes) -> str:
            async with semaphore:
                return await self.flow_client.upload_image(
                    token.at, image_bytes, aspect_ratio, project_id=project_id
                )

        tasks = [asyncio.create_task(_upload(img)) for img in images]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # First failure (or cancellation) stops the rest; the original error propagates.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _resolve_video_model_key_for_tier(self, model_config: Dict[str, Any], user_tier: str) -> tuple[str, Optional[str]]:
        """Adjust the video model key by account tier."""
        model_key = model_config["model_key"]
        allow_tier_upgrade = bool(model_config.get("allow_tier_upgrade", True))

        if user_tier == "PAYGATE_TIER_TWO":
            if allow_tier_upgrade and "ultra" not in model_key:
                upgraded_model_key = _resolve_tier_two_model_key(model_key)
                if upgraded_model_key != model_key:
                    return upgraded_model_key, f"TIER_TWO account automatically switched to ultra model: {upgraded_model_key}"
            return model_key, None

        if user_tier == "PAYGATE_TIER_ONE" and "ultra" in model_key:
            model_key = model_key.replace("_ultra_fl", "_fl").replace("_ultra", "")
            return model_key, f"TIER_ONE account automatically switched to standard model: {model_key}"

        return model_key, None

    async def _fail_video_task(self, operations: Optional[List[Dict[str, Any]]], error_message: str):
        """Move video tasks to failed so none stay stuck in processing."""
        if not operations:
            return

        operation = operations[0] if operations else {}
        task_id = (operation.get("operation") or {}).get("name")
        if not task_id:
            return

        try:
            await self.db.update_task(
                task_id,
                status="failed",
                error_message=self._normalize_error_message(error_message),
                completed_at=time.time()
            )
        except Exception as exc:
            debug_logger.log_error(f"[VIDEO] Failed to mark task as failed: {exc}")

    async def check_token_availability(self, is_image: bool, is_video: bool) -> bool:
        """Check token availability

        Args:
            is_image: check image generation tokens
            is_video: check video generation tokens

        Returns:
            True if a token is available, False if not
        """
        token_obj = await self.load_balancer.select_token(
            for_image_generation=is_image,
            for_video_generation=is_video
        )
        return token_obj is not None

    async def handle_generation(
        self,
        model: str,
        prompt: str,
        images: Optional[List[bytes]] = None,
        stream: bool = False,
        base_url_override: Optional[str] = None,
        video_media_id: Optional[str] = None,
        pool: str = "auto",
        client: str = "",
        characters: Optional[List[LoadedCharacter]] = None,
    ) -> AsyncGenerator:
        """Unified generation entry point

        Args:
            model: model name
            prompt: prompt text
            images: image list (bytes)
            stream: whether to stream output
        """
        start_time = time.time()
        token = None
        generation_type = None
        pending_token_state = {"active": False}
        request_id = f"gen-{int(start_time * 1000)}-{id(asyncio.current_task())}"
        perf_trace: Dict[str, Any] = {
            "request_id": request_id,
            "model": model,
            "status": "processing",
        }
        generation_result = self._create_generation_result()
        response_state = self._create_response_state()
        response_state["base_url"] = (base_url_override or "").strip().rstrip("/") or None
        request_log_state: Dict[str, Any] = {"id": None, "progress": 0}

        # Stop concurrent flows from reusing the previous request's fingerprint context
        if hasattr(self.flow_client, "clear_request_fingerprint"):
            self.flow_client.clear_request_fingerprint()

        # 1. Validate model
        if model not in MODEL_CONFIG:
            error_msg = f"Unsupported model: {model}"
            debug_logger.log_error(error_msg)
            record_generation_result("unknown", "invalid", time.time() - start_time)
            yield self._create_error_response(error_msg, status_code=400)
            return

        model_config = MODEL_CONFIG[model]
        generation_type = model_config["type"]
        video_type_for_op = model_config.get("video_type", "")
        request_operation = "extend_video" if video_type_for_op == "extend" else f"generate_{generation_type}"
        prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
        request_payload = {
            "model": model,
            "prompt": prompt_for_log,
            "has_images": images is not None and len(images) > 0,
            "client": client or "default",
        }
        if generation_type == "video":
            request_payload["video_duration_seconds"] = model_config.get("reference_duration")
            request_payload["video_credit_cost"] = _estimate_video_credit_cost_for_log(model, model_config)
        if characters:
            request_payload["characters"] = [{"name": c.name, "images": len(c.images)} for c in characters]
        debug_logger.log_info(f"[GENERATION] Starting generation - model: {model}, type: {generation_type}, Prompt: {prompt[:50]}...")

        # Create the request log BEFORE any network work, for streaming and
        # non-streaming callers alike, so in-flight requests (status 102) are visible
        # in the admin log while they run. Previously only streaming requests were
        # logged at start; n8n/CLI callers appeared only once finished.
        request_log_state["id"] = await self._log_request(
            token_id=None,
            operation=request_operation,
            request_data=request_payload,
            response_data={"status": "processing", "status_text": "started", "progress": 0, "request_id": request_id},
            status_code=102,
            duration=0,
            status_text="started",
            progress=0,
        )

        # Show start message to the user
        if stream:
            yield self._create_stream_chunk(
                f"✨ {'Video' if generation_type == 'video' else 'Image'} generation task started\n",
                role="assistant"
            )

        # 2. Select token
        debug_logger.log_info(f"[GENERATION] Selecting an available token...")
        token_select_started_at = time.time()

        if generation_type == "image":
            token = await self.load_balancer.select_token(
                for_image_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
                pool=pool,
                client=client,
            )
        else:
            token = await self.load_balancer.select_token(
                for_video_generation=True,
                model=model,
                reserve=False,
                enforce_concurrency_filter=False,
                track_pending=True,
                pool=pool,
                client=client,
            )
        perf_trace["token_select_ms"] = int((time.time() - token_select_started_at) * 1000)

        if not token:
            error_msg = None
            error_extra = None
            if self.load_balancer and hasattr(self.load_balancer, "get_unavailable_detail"):
                detail = await self.load_balancer.get_unavailable_detail(
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                    model=model,
                    pool=pool,
                    client=client,
                )
                if detail:
                    error_msg = detail.get("message")
                    error_extra = detail.get("extra")
            if not error_msg:
                error_msg = self._get_no_token_error_message(generation_type)
            debug_logger.log_error(f"[GENERATION] {error_msg}")
            debug_logger.op_warning(f"[GEN] req={request_id} model={model} type={generation_type} NO_TOKEN: {error_msg}")
            record_generation_result(generation_type, "no_token", time.time() - start_time)
            await self._log_request(
                token_id=None,
                operation=request_operation,
                request_data=request_payload,
                response_data={"error": error_msg, "error_detail": error_extra, "performance": perf_trace},
                status_code=503,
                duration=time.time() - start_time,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            if stream:
                yield self._create_stream_chunk(f"Error: {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=503, extra=error_extra)
            return

        debug_logger.log_info(f"[GENERATION] Selected token: {token.id} ({token.email})")
        debug_logger.event(f"[GEN] req={request_id} token={token.id}({token.email}) model={model} type={generation_type}")
        pending_token_state["active"] = True
        await self._update_request_log_progress(
            request_log_state,
            token_id=token.id,
            status_text="token_selected",
            progress=8,
            response_extra={"token_email": token.email},
        )

        try:
            # 3. Make sure AT is valid
            debug_logger.log_info(f"[GENERATION] Checking token AT...")
            if stream:
                yield self._create_stream_chunk("Initializing generation environment...\n")

            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="token_ready",
                progress=15,
            )
            ensure_at_started_at = time.time()
            token = await self.token_manager.ensure_valid_token(token)
            perf_trace["ensure_at_ms"] = int((time.time() - ensure_at_started_at) * 1000)
            if not token:
                error_msg = "Token AT is invalid or failed to refresh"
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                if stream:
                    yield self._create_stream_chunk(f"Error: {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=503)
                return

            # 4. Make sure project exists
            debug_logger.log_info(f"[GENERATION] Checking/creating project...")

            # Image generation is NOT paygate-tier gated: Flow lets free accounts
            # generate images (incl. 2k/4k). Only video keeps tier requirements.
            if generation_type != "image" and not supports_model_for_tier(model, token.user_paygate_tier):
                required_tier = get_required_paygate_tier_for_model(model)
                error_msg = "This model requires a " + get_paygate_tier_label(required_tier) + " account: " + model
                debug_logger.log_error(f"[GENERATION] {error_msg}")
                record_generation_result(generation_type, "failed", time.time() - start_time)
                if stream:
                    yield self._create_stream_chunk(f"Error: {error_msg}\n")
                yield self._create_error_response(error_msg, status_code=403)
                return

            ensure_project_started_at = time.time()
            project_id = await self.token_manager.ensure_project_exists(token.id)
            if characters:
                # Characters live inside ONE project of the account; projects rotate per
                # request, so steer this request to the project that already has them.
                try:
                    preferred = await self.db.find_project_with_characters(
                        token.id, [(c.name, c.digest) for c in characters]
                    )
                except Exception:
                    preferred = None
                if preferred and preferred != project_id:
                    debug_logger.event(f"[CHARACTER] token={token.id} steering to project {preferred[:8]} that already holds the character(s)")
                    project_id = preferred
            perf_trace["ensure_project_ms"] = int((time.time() - ensure_project_started_at) * 1000)
            debug_logger.log_info(f"[GENERATION] Project ID: {project_id}")
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="project_ready",
                progress=22,
                response_extra={"project_id": project_id},
            )
            prefill_action = "IMAGE_GENERATION" if generation_type == "image" else "VIDEO_GENERATION"
            await self.flow_client.prefill_remote_browser_pool(
                project_id=project_id,
                action=prefill_action,
                token_id=token.id,
            )

            # 5. Handle by type
            generation_pipeline_started_at = time.time()
            if generation_type == "image":
                debug_logger.log_info(f"[GENERATION] Starting image generation...")
                async for chunk in self._handle_image_generation(
                    token, project_id, model_config, prompt, images, stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                    characters=characters,
                ):
                    yield chunk
            else:  # video
                debug_logger.log_info(f"[GENERATION] Starting video generation...")
                async for chunk in self._handle_video_generation(
                    token, project_id, model_config, prompt, images, stream,
                    perf_trace=perf_trace,
                    generation_result=generation_result,
                    response_state=response_state,
                    request_log_state=request_log_state,
                    pending_token_state=pending_token_state,
                    video_media_id=video_media_id,
                    characters=characters,
                ):
                    yield chunk
            perf_trace["generation_pipeline_ms"] = int((time.time() - generation_pipeline_started_at) * 1000)

            # 6. Record usage
            if not generation_result.get("success"):
                error_msg = generation_result.get("error_message") or "Generation did not complete successfully"
                debug_logger.log_warning(f"[GENERATION] Generation failed, not counted: {error_msg}")
                if token:
                    await self.token_manager.record_error(token.id, error_msg, model)
                duration = time.time() - start_time
                record_generation_result(generation_type, "failed", duration)
                perf_trace["status"] = "failed"
                perf_trace["total_ms"] = int(duration * 1000)
                perf_trace["error"] = error_msg
                prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
                await self._log_request(
                    token.id if token else None,
                    request_operation,
                    request_payload,
                    {"error": error_msg, "performance": perf_trace},
                    500,
                    duration,
                    log_id=request_log_state.get("id"),
                    status_text="failed",
                    progress=request_log_state.get("progress", 0),
                )
                if not generation_result.get("error_emitted"):
                    if stream:
                        yield self._create_stream_chunk(f"Error: {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=500)
                return

            is_video = (generation_type == "video")
            await self.token_manager.record_usage(token.id, is_video=is_video)

            # Reset error count (success clears the consecutive error count)
            await self.token_manager.record_success(token.id)

            debug_logger.log_info(f"[GENERATION] ✅ Generation completed")

            # 7. Log success
            duration = time.time() - start_time
            record_generation_result(generation_type, "success", duration)
            debug_logger.event(
                f"[GEN] req={request_id} token={token.id}({token.email}) model={model} "
                f"type={generation_type} RESULT=success {int(duration * 1000)}ms"
            )
            perf_trace["status"] = "success"
            perf_trace["total_ms"] = int(duration * 1000)
            # Keep a fuller prompt in the log so the admin page does not show too little
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"

            # Build response data with the generated URL
            response_data = {
                "status": "success",
                "model": model,
                "prompt": prompt_for_log,
                "performance": perf_trace
            }

            # Add the generated URL (if any)
            if response_state.get("url"):
                response_data["url"] = response_state["url"]
            if response_state.get("generated_assets"):
                response_data["generated_assets"] = response_state["generated_assets"]
            image_perf = perf_trace.get("image_generation", {}) if isinstance(perf_trace, dict) else {}
            video_perf = perf_trace.get("video_generation", {}) if isinstance(perf_trace, dict) else {}
            debug_logger.log_info(
                f"[PERF] [{request_id}] total={perf_trace.get('total_ms', 0)}ms, "
                f"select={perf_trace.get('token_select_ms', 0)}ms, "
                f"ensure_at={perf_trace.get('ensure_at_ms', 0)}ms, "
                f"project={perf_trace.get('ensure_project_ms', 0)}ms, "
                f"pipeline={perf_trace.get('generation_pipeline_ms', 0)}ms, "
                f"slot_wait={image_perf.get('slot_wait_ms', 0)}ms, "
                f"launch_queue={image_perf.get('launch_queue_wait_ms', 0)}ms, "
                f"launch_stagger={image_perf.get('launch_stagger_wait_ms', 0)}ms, "
                f"video_slot_wait={video_perf.get('slot_wait_ms', 0)}ms"
            )

            await self._log_request(
                token.id,
                request_operation,
                request_payload,
                response_data,
                200,
                duration,
                log_id=request_log_state.get("id"),
                status_text="completed",
                progress=100,
            )

        except asyncio.CancelledError:
            error_msg = "Generation cancelled: client connection was closed"
            debug_logger.log_warning(f"[GENERATION] ⚠️ {error_msg}")
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "cancelled", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                499,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            raise
        except Exception as e:
            error_msg = f"Generation failed: {str(e)}"
            debug_logger.log_error(f"[GENERATION] ❌ {error_msg}")
            if token:
                # Record the error (environment/captcha/capacity errors do not count toward auto-disable, so good tokens are not disabled;
                # classification lives in TokenManager.record_error: capacity/solver -> ignored,
                # environmental -> reCAPTCHA cooldown, quota -> per-model cooldown)
                await self.token_manager.record_error(token.id, error_msg, model)

            # Save the final failed state first, then return the error, so the log does not stop at 102.
            duration = time.time() - start_time
            record_generation_result(generation_type or "unknown", "failed", duration)
            perf_trace["status"] = "failed"
            perf_trace["total_ms"] = int(duration * 1000)
            perf_trace["error"] = error_msg
            prompt_for_log = prompt if len(prompt) <= 2000 else f"{prompt[:2000]}...(truncated)"
            await self._log_request(
                token.id if token else None,
                request_operation if generation_type else "generate_unknown",
                request_payload if 'request_payload' in locals() else {"model": model},
                {"error": error_msg, "performance": perf_trace},
                500,
                duration,
                log_id=request_log_state.get("id"),
                status_text="failed",
                progress=request_log_state.get("progress", 0),
            )
            if stream:
                yield self._create_stream_chunk(f"Error: {error_msg}\n")
            yield self._create_error_response(error_msg, status_code=500)
        finally:
            if pending_token_state.get("active") and token and self.load_balancer:
                await self.load_balancer.release_pending(
                    token.id,
                    for_image_generation=(generation_type == "image"),
                    for_video_generation=(generation_type == "video"),
                )
                pending_token_state["active"] = False


    async def _remove_watermark_bytes(
        self,
        image_bytes: bytes,
        image_trace: Optional[Dict[str, Any]],
    ) -> tuple:
        """Strip Flow's visible watermark off the event loop. Never raises.

        Returns (bytes, info); bytes are the input unchanged unless info["applied"].
        """
        started_at = time.time()
        try:
            cleaned, result = await asyncio.to_thread(clean_image_bytes, image_bytes)
            info = result.as_dict()
        except Exception as e:
            debug_logger.log_warning(f"[WATERMARK] remover failed, image returned unchanged: {str(e)}")
            cleaned, info = image_bytes, {"applied": False, "reason": f"error:{type(e).__name__}"}
        if info.get("reason") == "unknown-size":
            size_key = (info.get("width"), info.get("height"))
            if size_key not in self._watermark_unknown_sizes:
                self._watermark_unknown_sizes.add(size_key)
                debug_logger.log_info(
                    f"[WATERMARK] no calibration for {size_key[0]}x{size_key[1]}, image returned unchanged"
                )
        if image_trace is not None:
            image_trace["watermark_ms"] = int((time.time() - started_at) * 1000)
        return cleaned, info

    async def _remove_watermark_base64(
        self,
        encoded_image: str,
        image_trace: Optional[Dict[str, Any]],
    ) -> tuple:
        """Same as _remove_watermark_bytes for a base64 image; returns (base64, info)."""
        try:
            image_bytes = base64.b64decode(encoded_image)
        except Exception:
            return encoded_image, {"applied": False, "reason": "error:bad-base64"}
        cleaned, info = await self._remove_watermark_bytes(image_bytes, image_trace)
        if info.get("applied"):
            return base64.b64encode(cleaned).decode("ascii"), info
        return encoded_image, info

    async def _fetch_and_remove_watermark(
        self,
        image_url: str,
        image_trace: Optional[Dict[str, Any]],
    ) -> tuple:
        """Download a generated image and strip its watermark; returns (bytes or None, info)."""
        try:
            image_bytes = await self.file_cache.fetch_image_bytes(image_url)
        except Exception as e:
            debug_logger.log_warning(f"[WATERMARK] image fetch failed, returning source link: {str(e)}")
            return None, {"applied": False, "reason": "fetch-failed"}
        return await self._remove_watermark_bytes(image_bytes, image_trace)

    def _get_no_token_error_message(self, generation_type: str) -> str:
        """Get a detailed error message when no token is available"""
        if generation_type == "image":
            return "No token available for image generation. All tokens are disabled, cooling down, locked, or expired."
        else:
            return "No token available for video generation. All tokens are disabled, cooling down, out of quota, or expired."

    async def _handle_image_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
        characters: Optional[List[LoadedCharacter]] = None,
    ) -> AsyncGenerator:
        """Handle image generation (synchronous return)"""
        reset_mint = getattr(self.flow_client, "reset_mint_context", None)
        if callable(reset_mint):
            reset_mint(getattr(token, "id", None))

        if response_state is None:
            response_state = self._create_response_state()

        image_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            image_trace = perf_trace.setdefault("image_generation", {})
            image_trace["input_image_count"] = len(images) if images else 0

        # Do not wait locally for a hard image concurrency slot; submit upstream as soon as the request arrives.
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if image_trace is not None:
            image_trace["slot_wait_ms"] = 0

        if images and len(images) > 0:
            await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="uploading_images", progress=28)
        else:
            await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="submitting_image", progress=28)

        # Flow Characters: validated and created BEFORE any upload or generation, so a bad
        # request or a Google-side setup failure costs nothing and is never an account strike.
        character_entities: Dict[str, str] = {}
        if characters:
            char_error = validate_characters(characters, MAX_CHARACTERS_IMAGE)
            if char_error:
                self._mark_generation_failed(generation_result, char_error)
                yield self._create_error_response(char_error, status_code=400)
                return
            try:
                characters_started_at = time.time()
                if stream:
                    yield self._create_stream_chunk(f"Preparing {len(characters)} character(s)...\n")
                character_entities = await CharacterService.get_instance().ensure(
                    self.flow_client, self.db, token, project_id, characters
                )
                if image_trace is not None:
                    image_trace["characters_ms"] = int((time.time() - characters_started_at) * 1000)
            except CharacterSetupError as exc:
                self._mark_generation_failed(generation_result, str(exc))
                yield self._create_error_response(str(exc), status_code=exc.status_code)
                return
        prompt_parts, reference_entities = (build_prompt_parts(prompt, character_entities) if character_entities else (None, None))

        try:
            # Upload images (if any)
            upload_started_at = time.time()
            image_inputs = []
            if images and len(images) > FLOW_IMAGE_MAX_REFERENCES:
                error_msg = (
                    f"Image models support at most {FLOW_IMAGE_MAX_REFERENCES} reference images; "
                    f"{len(images)} provided"
                )
                if stream:
                    yield self._create_stream_chunk(f"{error_msg}\n")
                self._mark_generation_failed(generation_result, error_msg)
                yield self._create_error_response(error_msg, status_code=400)
                return
            if images and len(images) > 0:
                if stream:
                    yield self._create_stream_chunk(f"Uploading {len(images)} reference image(s)...\n")

                media_ids = await self._upload_reference_images(
                    token, images, model_config["aspect_ratio"], project_id
                )
                image_inputs = [
                    {"name": media_id, "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}
                    for media_id in media_ids
                ]
                if stream:
                    yield self._create_stream_chunk(f"Uploaded {len(media_ids)} image(s)\n")
            if image_trace is not None:
                image_trace["upload_images_ms"] = int((time.time() - upload_started_at) * 1000)

            # Call the generation API
            if stream:
                if images and len(images) > 0:
                    yield self._create_stream_chunk("Reference images uploaded, running verification...\n")
                else:
                    yield self._create_stream_chunk("Running verification and submitting image generation request...\n")

            async def _image_progress_callback(status_text: str, progress: int):
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text=status_text,
                    progress=progress,
                )

            generate_started_at = time.time()
            result, generation_session_id, upstream_trace = await self.flow_client.generate_image(
                at=token.at,
                project_id=project_id,
                prompt=prompt,
                model_name=model_config["model_name"],
                aspect_ratio=model_config["aspect_ratio"],
                image_inputs=image_inputs,
                token_id=token.id,
                token_image_concurrency=token.image_concurrency,
                progress_callback=_image_progress_callback,
                prompt_parts=prompt_parts,
                reference_entities=reference_entities,
            )
            if image_trace is not None:
                image_trace["generate_api_ms"] = int((time.time() - generate_started_at) * 1000)
                image_trace["upstream_trace"] = upstream_trace
                attempts = upstream_trace.get("generation_attempts") if isinstance(upstream_trace, dict) else None
                if isinstance(attempts, list) and attempts:
                    first_attempt = attempts[0] if isinstance(attempts[0], dict) else {}
                    image_trace["launch_queue_wait_ms"] = int(first_attempt.get("launch_queue_ms") or 0)
                    image_trace["launch_stagger_wait_ms"] = int(first_attempt.get("launch_stagger_ms") or 0)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="image_generated",
                progress=72,
            )

            # Extract URL and mediaId
            media = result.get("media", [])
            if not media:
                self._mark_generation_failed(generation_result, "Generation result is empty")
                yield self._create_error_response("Generation result is empty", status_code=502)
                return

            image_url = media[0]["image"]["generatedImage"]["fifeUrl"]
            media_id = media[0].get("name")  # used for upsample
            response_state["generated_assets"] = {
                "type": "image",
                "origin_image_url": image_url
            }
            watermark_info: Optional[Dict[str, Any]] = None

            # Check whether upsample is needed (2K/4K). Rewritten 26 Sep 2026
            # (tmp/upscale_honest_plan.md): one layer owns retries, the account's enlarge limit is
            # remembered, and a failed enlarge is delivered as 1K WITH A NOTE (flow_upscale), never
            # silently as if it were 2K/4K.
            upsample_resolution = model_config.get("upsample")
            upscale_note: Optional[Dict[str, Any]] = None
            if upsample_resolution:
                upsample_started_at = time.time()
                request_id = (perf_trace or {}).get("request_id", "-")
                resolution_name = "4K" if "4K" in upsample_resolution else "2K"
                fail_kind: Optional[str] = None
                fail_msg = ""
                upsample_attempt = 0
                upscale_key = upsample_quota_key(resolution_name)
                if not media_id:
                    fail_kind = "no_media_id"
                elif self.token_manager.is_model_quota_exhausted(token.id, upscale_key):
                    # This account's enlarge is resting (daily limit / refused / busy): no call to
                    # Google, so the rest is real; the picture is still delivered, as 1K.
                    fail_kind = "resting"
                elif resolution_name == "4K" and normalized_tier != PAYGATE_TIER_TWO:
                    # Google answers MODEL_ACCESS_DENIED to 4K below Ultra (live 26 Sep, tokens 30/88/89).
                    fail_kind = "needs_ultra"
                else:
                    await self._update_request_log_progress(request_log_state, token_id=token.id, status_text=f"upsampling_{resolution_name.lower()}", progress=82)
                    if stream:
                        yield self._create_stream_chunk(f"Upscaling image to {resolution_name}...\n")
                while fail_kind is None:
                    upsample_attempt += 1
                    try:
                        # One call = one enlarge attempt; network/5xx retries happen inside it,
                        # quota/tier/traffic/refusals come straight back here.
                        encoded_image = await self.flow_client.upsample_image(
                            at=token.at,
                            project_id=project_id,
                            media_id=media_id,
                            target_resolution=upsample_resolution,
                            user_paygate_tier=normalized_tier,
                            session_id=generation_session_id,
                            token_id=token.id
                        )
                    except Exception as e:
                        kind = classify_upsample_error(e)
                        fail_msg = str(e)
                        if kind == "traffic" and upsample_attempt < UPSAMPLE_TRAFFIC_ATTEMPTS:
                            # Google says "too much traffic": one more try, after a real pause.
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Upscale refused (busy), retrying in {UPSAMPLE_TRAFFIC_RETRY_DELAY_S}s...\n")
                            await asyncio.sleep(UPSAMPLE_TRAFFIC_RETRY_DELAY_S)
                            continue
                        fail_kind = kind
                        break
                    if not encoded_image:
                        fail_kind = "empty"
                        break
                    debug_logger.event(f"[UPSAMPLE] req={request_id} token={token.id} res={resolution_name} outcome=ok attempts={upsample_attempt} ms={int((time.time() - upsample_started_at) * 1000)}")

                    if stream:
                        yield self._create_stream_chunk(f"✅ Image upscaled to {resolution_name}\n")

                    if config.remove_watermark:
                        encoded_image, watermark_info = await self._remove_watermark_base64(
                            encoded_image, image_trace
                        )
                        if stream and watermark_info.get("applied"):
                            yield self._create_stream_chunk("✅ Watermark removed\n")

                    # 2K/4K images are always saved to real files; the log keeps only the link.
                    response_state["generated_assets"] = {
                        "type": "image",
                        "origin_image_url": image_url,
                        "upscaled_image": {
                            "resolution": resolution_name
                        }
                    }
                    response_state["flow_upscale"] = {"requested": resolution_name, "delivered": resolution_name}
                    if watermark_info is not None:
                        response_state["generated_assets"]["watermark"] = watermark_info

                    try:
                        await self._update_request_log_progress(
                            request_log_state,
                            token_id=token.id,
                            status_text="caching_image",
                            progress=90,
                        )
                        if stream:
                            yield self._create_stream_chunk(f"Caching {resolution_name} image...\n")
                        cached_filename = await self.file_cache.cache_base64_image(encoded_image, resolution_name)
                        local_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                        response_state["url"] = local_url
                        response_state["generated_assets"]["upscaled_image"]["local_url"] = local_url
                        response_state["generated_assets"]["upscaled_image"]["url"] = local_url
                        self._mark_generation_succeeded(generation_result)
                        if stream:
                            yield self._create_stream_chunk(f"✅ {resolution_name} image cached successfully\n")
                            yield self._create_stream_chunk(
                                f"![Generated Image]({local_url})",
                                finish_reason="stop",
                                extra={"flow_upscale": response_state.get("flow_upscale")},
                            )
                        else:
                            yield self._create_completion_response(
                                local_url,
                                media_type="image",
                                upscale=response_state.get("flow_upscale"),
                            )
                        if image_trace is not None:
                            image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                        return
                    except Exception as e:
                        debug_logger.log_error(f"Failed to cache {resolution_name} image: {str(e)}")
                        response_state["url"] = image_url
                        response_state["generated_assets"]["upscaled_image"]["local_url"] = None
                        response_state["generated_assets"]["upscaled_image"]["url"] = image_url
                        response_state["generated_assets"]["upscaled_image"]["delivery_mode"] = "inline_base64_fallback"
                        self._mark_generation_succeeded(generation_result)
                        base64_url = f"data:image/jpeg;base64,{encoded_image}"
                        if stream:
                            cache_error = self._normalize_error_message(e, max_length=120)
                            yield self._create_stream_chunk(f"⚠️ Cache failed: {cache_error}, returning inline image...\n")
                            yield self._create_stream_chunk(
                                f"![Generated Image]({base64_url})",
                                finish_reason="stop",
                                extra={"flow_upscale": response_state.get("flow_upscale")},
                            )
                        else:
                            yield self._create_completion_response(
                                base64_url,
                                media_type="image",
                                upscale=response_state.get("flow_upscale"),
                            )
                        if image_trace is not None:
                            image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)
                        return
                if fail_kind in ("resting", "needs_ultra"):
                    debug_logger.event(f"[UPSAMPLE] req={request_id} token={token.id} res={resolution_name} outcome={fail_kind} delivered=1K (no call)")
                else:
                    await self._record_upscale_failure(token, resolution_name, fail_kind, fail_msg, request_id)
                upscale_note = {"requested": resolution_name, "delivered": "1K", "reason": fail_kind}
                response_state["flow_upscale"] = upscale_note
                if stream:
                    yield self._create_stream_chunk(f"⚠️ Not enlarged — delivering 1K (reason: {fail_kind})\n")
                if image_trace is not None:
                    image_trace["upsample_ms"] = int((time.time() - upsample_started_at) * 1000)

            local_url = image_url
            cache_started_at = time.time()
            # Free/Pro images carry Flow's visible watermark; Ultra images do not,
            # so they keep the direct link and skip the download.
            image_bytes = None
            if config.remove_watermark and normalized_tier != PAYGATE_TIER_TWO:
                image_bytes, watermark_info = await self._fetch_and_remove_watermark(image_url, image_trace)
                if stream and watermark_info.get("applied"):
                    yield self._create_stream_chunk("✅ Watermark removed\n")
            if image_bytes is not None and (watermark_info.get("applied") or config.cache_enabled):
                try:
                    cached_filename = await self.file_cache.cache_image_bytes(image_bytes, "1K")
                    local_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                except Exception as e:
                    debug_logger.log_error(f"Failed to cache 1K image: {str(e)}")
                    if watermark_info.get("applied"):
                        watermark_info = {**watermark_info, "applied": False, "reason": "cache-failed"}
                    if stream:
                        cache_error = self._normalize_error_message(e, max_length=120)
                        yield self._create_stream_chunk(f"⚠️ Cache failed: {cache_error}\nReturning source link...\n")
            elif config.cache_enabled:
                await self._update_request_log_progress(
                    request_log_state,
                    token_id=token.id,
                    status_text="caching_image",
                    progress=90,
                )
                if stream:
                    yield self._create_stream_chunk("Caching 1K image file...\n")
                try:
                    cached_filename = await self.file_cache.download_and_cache(image_url, "image")
                    local_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                    if stream:
                        yield self._create_stream_chunk("✅ 1K image cached successfully, preparing to return cached URL...\n")
                except Exception as e:
                    debug_logger.log_error(f"Failed to cache 1K image: {str(e)}")
                    local_url = image_url
                    if stream:
                        cache_error = self._normalize_error_message(e, max_length=120)
                        yield self._create_stream_chunk(f"⚠️ Cache failed: {cache_error}\nReturning source link...\n")
            elif stream:
                yield self._create_stream_chunk("Cache disabled, returning official image link...\n")
            if image_trace is not None:
                image_trace["cache_image_ms"] = int((time.time() - cache_started_at) * 1000)

            # Return result
            # Store URL for logging
            response_state["url"] = local_url
            response_state["generated_assets"] = {
                "type": "image",
                "origin_image_url": image_url,
                "final_image_url": local_url
            }
            if upscale_note:
                response_state["generated_assets"]["upscale"] = upscale_note
            if watermark_info is not None:
                response_state["generated_assets"]["watermark"] = watermark_info
            self._mark_generation_succeeded(generation_result)

            if stream:
                yield self._create_stream_chunk(
                    f"![Generated Image]({local_url})",
                    finish_reason="stop",
                    extra={"flow_upscale": upscale_note} if upscale_note else None,
                )
            else:
                yield self._create_completion_response(
                    local_url,  # Pass the URL; the method formats it
                    media_type="image",
                    upscale=upscale_note,
                )

        finally:
            pass

    async def _handle_video_generation(
        self,
        token,
        project_id: str,
        model_config: dict,
        prompt: str,
        images: Optional[List[bytes]],
        stream: bool,
        perf_trace: Optional[Dict[str, Any]] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        pending_token_state: Optional[Dict[str, bool]] = None,
        video_media_id: Optional[str] = None,
        characters: Optional[List[LoadedCharacter]] = None,
    ) -> AsyncGenerator:
        """Handle video generation (async polling)"""
        reset_mint = getattr(self.flow_client, "reset_mint_context", None)
        if callable(reset_mint):
            reset_mint(getattr(token, "id", None))

        if response_state is None:
            response_state = self._create_response_state()

        video_trace: Optional[Dict[str, Any]] = None
        if isinstance(perf_trace, dict):
            video_trace = perf_trace.setdefault("video_generation", {})
            video_trace["input_image_count"] = len(images) if images else 0

        # Do not wait locally for a hard video concurrency slot; submit upstream as soon as the request arrives.
        normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)

        if video_trace is not None:
            video_trace["slot_wait_ms"] = 0

        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="preparing_video", progress=24)

        try:
            # Get model type and config
            video_type = model_config.get("video_type")
            supports_images = model_config.get("supports_images", False)
            min_images = model_config.get("min_images", 0)
            max_images = model_config.get("max_images", 0)
            use_v2_model_config = bool(model_config.get("use_v2_model_config", False))

            # Auto-adjust the model key by account tier
            user_tier = normalized_tier

            original_model_key = model_config["model_key"]
            model_key, tier_message = self._resolve_video_model_key_for_tier(model_config, user_tier)
            if tier_message:
                if stream:
                    yield self._create_stream_chunk(f"{tier_message}\n")
                debug_logger.log_info(f"[VIDEO] Tier model adjustment: {original_model_key} -> {model_key}")
            elif user_tier == "PAYGATE_TIER_TWO" and original_model_key == model_key:
                debug_logger.log_info(f"[VIDEO] TIER_TWO account, no valid ultra variant, keeping model: {model_key}")

            # Update model_key in model_config
            model_config = dict(model_config)  # Copy so the original config is not changed
            model_config["model_key"] = model_key

            # Image count
            image_count = len(images) if images else 0

            # Flow Characters: only ingredients models can carry them; omni* is forced onto the
            # reference route (a first/last-frame video cannot reference a character).
            character_entities: Dict[str, str] = {}
            if characters:
                if video_type not in ("r2v", "omni"):
                    error_msg = "Characters need an ingredients model (omni-r2v, omni, veo-r2v, veo-r2v-lite)"
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return
                char_error = validate_characters(characters, MAX_CHARACTERS_VIDEO)
                if char_error:
                    self._mark_generation_failed(generation_result, char_error)
                    yield self._create_error_response(char_error, status_code=400)
                    return
                if video_type == "omni":
                    model_config["reference_only"] = True

            # ========== Validate and process images ==========

            # T2V: text to video - no images
            if video_type == "t2v":
                if image_count > 0:
                    if stream:
                        yield self._create_stream_chunk("⚠️ Text-to-video models do not support image uploads; images will be ignored and only the text prompt will be used\n")
                    debug_logger.log_warning(f"[T2V] Model {model_config['model_key']} does not take images, ignored {image_count} image(s)")
                images = None  # Drop images
                image_count = 0

            # Omni: no images -> T2V; with images -> upstream Reference Images path
            elif video_type == "omni":
                if model_config.get("reference_only") and image_count < 1 and not characters:
                    error_msg = "Omni ingredients models need at least 1 reference image"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return
                if max_images is not None and image_count > max_images:
                    error_msg = f"Omni models support at most {max_images} reference images; {image_count} provided"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # I2V: first/last frame model - needs 1-2 images
            elif video_type == "i2v":
                if image_count < min_images or image_count > max_images:
                    error_msg = f"❌ First/last-frame models require {min_images}-{max_images} image(s); {image_count} provided"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            # R2V: multi-image - upstream allows at most 3 reference images
            elif video_type == "r2v":
                if max_images is not None and image_count > max_images:
                    error_msg = f"❌ Multi-image video models support at most {max_images} reference image(s); {image_count} provided"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

            if characters:
                try:
                    characters_started_at = time.time()
                    if stream:
                        yield self._create_stream_chunk(f"Preparing {len(characters)} character(s)...\n")
                    character_entities = await CharacterService.get_instance().ensure(
                        self.flow_client, self.db, token, project_id, characters
                    )
                    if video_trace is not None:
                        video_trace["characters_ms"] = int((time.time() - characters_started_at) * 1000)
                except CharacterSetupError as exc:
                    self._mark_generation_failed(generation_result, str(exc))
                    yield self._create_error_response(str(exc), status_code=exc.status_code)
                    return
            prompt_parts, reference_entities = (build_prompt_parts(prompt, character_entities) if character_entities else (None, None))

            # ========== Upload images ==========
            start_media_id = None
            end_media_id = None
            reference_images = []

            # I2V: first/last frame handling
            if video_type == "i2v" and images:
                if image_count == 1:
                    # 1 image: first frame only
                    if stream:
                        yield self._create_stream_chunk("Uploading first-frame image...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] Uploaded first frame only: {start_media_id}")

                elif image_count == 2:
                    # 2 images: first + last frame
                    if stream:
                        yield self._create_stream_chunk("Uploading first-frame and last-frame images...\n")
                    start_media_id = await self.flow_client.upload_image(
                        token.at, images[0], model_config["aspect_ratio"], project_id=project_id
                    )
                    end_media_id = await self.flow_client.upload_image(
                        token.at, images[1], model_config["aspect_ratio"], project_id=project_id
                    )
                    debug_logger.log_info(f"[I2V] Uploaded first + last frame: {start_media_id}, {end_media_id}")

            # R2V: multi-image handling
            elif video_type == "r2v" and images:
                if stream:
                    yield self._create_stream_chunk(f"Uploading {image_count} reference image(s)...\n")

                media_ids = await self._upload_reference_images(
                    token, images, model_config["aspect_ratio"], project_id
                )
                reference_images = [
                    {"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": media_id}
                    for media_id in media_ids
                ]
                debug_logger.log_info(f"[R2V] Uploaded {len(reference_images)} reference image(s)")

            # Omni 1.1: 1 image = first frame, 2 images = first + last frame (abra_i2v_*s);
            # 3+ images, or any count on the omni_r2v models = reference-images route (abra_r2v_*s).
            elif video_type == "omni" and images:
                if stream:
                    yield self._create_stream_chunk(f"Uploading {image_count} Omni 1.1 image(s)...\n")

                use_frames = image_count <= 2 and not model_config.get("reference_only")
                uploaded_ids = await self._upload_reference_images(
                    token, images, model_config["aspect_ratio"], project_id
                )
                if use_frames:
                    start_media_id = uploaded_ids[0]
                    end_media_id = uploaded_ids[1] if image_count == 2 else None
                    debug_logger.log_info(
                        f"[VIDEO OMNI-I2V] uploaded first{'+last' if end_media_id else ''} frame: {uploaded_ids}"
                    )
                else:
                    reference_images = [
                        {"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": media_id}
                        for media_id in uploaded_ids
                    ]
                    debug_logger.log_info(f"[VIDEO OMNI-R2V] uploaded {len(reference_images)} reference images")

            # ========== Call the generation API ==========
            if stream:
                yield self._create_stream_chunk("Submitting video generation task...\n")
            submit_started_at = time.time()

            # I2V: first/last frame generation
            if video_type == "i2v" and start_media_id:
                if end_media_id:
                    # Has first + last frame
                    result = await self.flow_client.generate_video_start_end(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config["model_key"],
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        end_media_id=end_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )
                else:
                    # First frame only - strip _fl from model_key
                    # Case 1: _fl_ in the middle (e.g. veo_3_1_i2v_s_fast_fl_ultra_relaxed -> veo_3_1_i2v_s_fast_ultra_relaxed)
                    # Case 2: _fl at the end (e.g. veo_3_1_i2v_s_fast_ultra_fl -> veo_3_1_i2v_s_fast_ultra)
                    actual_model_key = model_config["model_key"].replace("_fl_", "_")
                    if actual_model_key.endswith("_fl"):
                        actual_model_key = actual_model_key[:-3]
                    debug_logger.log_info(f"[I2V] Single-frame mode, model_key: {model_config['model_key']} -> {actual_model_key}")
                    result = await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=actual_model_key,
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        use_v2_model_config=use_v2_model_config,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )

            # R2V: multi-image generation
            elif video_type == "r2v" and (reference_images or character_entities):
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                    prompt_parts=prompt_parts,
                    reference_entities=reference_entities,
                )

            # Omni: with images -> Reference Images path; no images -> text-only path
            # Omni 1.1: single image = first frame, two images = first + last frame.
            elif video_type == "omni" and start_media_id:
                if stream:
                    yield self._create_stream_chunk(
                        "Submitting Omni 1.1 first+last-frame video task...\n" if end_media_id
                        else "Submitting Omni 1.1 first-frame video task...\n"
                    )
                if end_media_id:
                    result = await self.flow_client.generate_video_start_end(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config.get("start_end_model_key", "abra_i2v_8s"),
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        end_media_id=end_media_id,
                        use_v2_model_config=True,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )
                else:
                    result = await self.flow_client.generate_video_start_image(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_key=model_config.get("first_frame_model_key", "abra_i2v_8s"),
                        aspect_ratio=model_config["aspect_ratio"],
                        start_media_id=start_media_id,
                        use_v2_model_config=True,
                        user_paygate_tier=normalized_tier,
                        token_id=token.id,
                        token_video_concurrency=token.video_concurrency,
                    )

            # Omni 1.1: three or more images → reference-images route.
            elif video_type == "omni" and (reference_images or character_entities):
                if stream:
                    yield self._create_stream_chunk("Submitting Omni reference-image video task...\n")
                result = await self.flow_client.generate_video_reference_images(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config.get("reference_model_key", "abra_r2v_8s"),
                    aspect_ratio=model_config["aspect_ratio"],
                    reference_images=reference_images,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                    prompt_parts=prompt_parts,
                    reference_entities=reference_entities,
                )

            # Extend: video continuation
            elif video_type == "extend":
                if not video_media_id:
                    error_msg = "❌ Video extension requires the source video's mediaGenerationId; pass extend://VIDEO_MEDIA_ID in image_url"
                    if stream:
                        yield self._create_stream_chunk(f"{error_msg}\n")
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=400)
                    return

                debug_logger.log_info(f"[EXTEND] Extending video: {video_media_id}")
                if stream:
                    yield self._create_stream_chunk(f"Submitting video extension task, source video: {video_media_id[:8]}...\n")
                result = await self.flow_client.generate_video_extend(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    video_media_id=video_media_id,
                    model_key=model_config["model_key"],
                    aspect_ratio=model_config["aspect_ratio"],
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )

            # T2V or R2V without images: text-only generation
            else:
                result = await self.flow_client.generate_video_text(
                    at=token.at,
                    project_id=project_id,
                    prompt=prompt,
                    model_key=model_config["model_key"],
                    output_resolution=model_config.get("output_resolution"),
                    aspect_ratio=model_config["aspect_ratio"],
                    use_v2_model_config=use_v2_model_config,
                    user_paygate_tier=normalized_tier,
                    token_id=token.id,
                    token_video_concurrency=token.video_concurrency,
                )
            if video_trace is not None:
                video_trace["submit_generation_ms"] = int((time.time() - submit_started_at) * 1000)

            # Get task_id and operations
            operations = result.get("operations", [])
            if not operations:
                self._mark_generation_failed(generation_result, "Failed to create generation task")
                yield self._create_error_response("Failed to create generation task", status_code=502)
                return

            operation = operations[0]
            task_id = operation["operation"]["name"]
            scene_id = operation.get("sceneId")

            # Save task to the database
            task = Task(
                task_id=task_id,
                token_id=token.id,
                model=model_config["model_key"],
                prompt=prompt,
                status="processing",
                scene_id=scene_id
            )
            await self.db.create_task(task)
            await self._update_request_log_progress(
                request_log_state,
                token_id=token.id,
                status_text="video_submitted",
                progress=45,
                response_extra={"task_id": task_id, "scene_id": scene_id},
            )

            # Poll for result
            if stream:
                yield self._create_stream_chunk(f"Generating video...\n")

            # Check whether upscale is needed
            upsample_config = model_config.get("upsample")

            # For extend, pass the source video media_id for later concatenation
            extend_source_id = video_media_id if video_type == "extend" else None
            async for chunk in self._poll_video_result(
                token,
                project_id,
                operations,
                stream,
                upsample_config,
                generation_result,
                response_state,
                request_log_state,
                extend_source_media_id=extend_source_id,
            ):
                yield chunk

        finally:
            pass

    async def _poll_video_result(
        self,
        token,
        project_id: str,
        operations: List[Dict],
        stream: bool,
        upsample_config: Optional[Dict] = None,
        generation_result: Optional[Dict[str, Any]] = None,
        response_state: Optional[Dict[str, Any]] = None,
        request_log_state: Optional[Dict[str, Any]] = None,
        extend_source_media_id: Optional[str] = None,
    ) -> AsyncGenerator:
        """Poll video generation result
        
        Args:
            upsample_config: upscale config {"resolution": "VIDEO_RESOLUTION_4K", "model_key": "veo_3_1_upsampler_4k"}
        """

        if response_state is None:
            response_state = self._create_response_state()

        max_attempts = config.max_poll_attempts
        poll_interval = config.poll_interval
        
        # If upscaling, allow more poll attempts (upscale can take 30 minutes)
        if upsample_config:
            max_attempts = max_attempts * 3  # upscale takes longer

        consecutive_poll_errors = 0
        last_poll_error: Optional[Exception] = None
        max_consecutive_poll_errors = 3
        # Upstream sometimes reports the video as done before its download URL is
        # resolvable. Keep polling (bounded) instead of failing with 502.
        # Adapted from Danborad/flow2api f9b8e8e2.
        successful_without_url_count = 0
        max_successful_without_url_count = 40

        for attempt in range(max_attempts):
            await asyncio.sleep(poll_interval)

            try:
                result = await self.flow_client.check_video_status(token.at, operations)
                checked_operations = result.get("operations", [])
                consecutive_poll_errors = 0
                last_poll_error = None

                if not checked_operations:
                    continue

                operation = checked_operations[0]
                status = operation.get("status")

                # Status update - report every ~20s (poll_interval=3s, ~7 polls per 20s)
                progress_update_interval = 7  # every 7 polls = 21s
                if stream and attempt % progress_update_interval == 0:  # report every ~20s
                    progress = min(int((attempt / max_attempts) * 100), 95)
                    await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="video_polling", progress=max(45, progress), response_extra={"upstream_status": status})
                    yield self._create_stream_chunk(f"Generation progress: {progress}%\n")

                # Check status
                if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                    # Upstream refactor: state comes from media; URL via two-step CDN
                    # redirect (fifeUrl is gone from the new schema). _resolve_video_asset
                    # also extracts the short /video/UUID id that extend + concat need.
                    try:
                        resolved_video = await self._resolve_video_asset(token, operation)
                    except Exception as redirect_error:
                        media_name = (
                            operation.get("mediaName")
                            or operation.get("name")
                            or operation["operation"].get("name")
                        )
                        error_msg = f"Video generated but fetching media URL failed: {self._normalize_error_message(redirect_error)}"
                        debug_logger.log_warning(
                            f"[VIDEO POLL] Failed to get video URL: media={media_name}, error={redirect_error}"
                        )
                        successful_without_url_count += 1
                        await self._update_request_log_progress(
                            request_log_state,
                            token_id=token.id,
                            status_text="video_waiting_url",
                            progress=min(95, 45 + successful_without_url_count),
                        )
                        if stream and successful_without_url_count == 1:
                            yield self._create_stream_chunk("Video generated, waiting for upstream download URL...\n")
                        if successful_without_url_count < max_successful_without_url_count:
                            continue
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(generation_result, error_msg)
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    video_url = resolved_video["video_url"]
                    video_media_id = resolved_video["video_media_id"]
                    aspect_ratio = resolved_video["aspect_ratio"]
                    media_name = resolved_video["media_name"]
                    metadata = resolved_video["metadata"]
                    video_info = resolved_video["video_info"]

                    if not video_url:
                        media_name_for_fetch = (
                            operation.get("mediaName")
                            or operation["operation"].get("name", "")
                        )
                        if media_name_for_fetch:
                            if stream:
                                yield self._create_stream_chunk("Video generated, downloading video file...\n")
                            try:
                                media_result = await self.flow_client.get_media(
                                    token.at, media_name_for_fetch
                                )
                                encoded_video = (
                                    media_result.get("video", {}).get("encodedVideo", "")
                                )
                                if encoded_video:
                                    cached_filename = await self.file_cache.cache_base64_video(
                                        encoded_video
                                    )
                                    video_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                                    video_info["fifeUrl"] = video_url
                                    debug_logger.log_info(
                                        f"[VIDEO] Video fetched via get_media and cached: {cached_filename}"
                                    )
                                else:
                                    debug_logger.log_error(
                                        "[VIDEO] get_media returned empty encodedVideo"
                                    )
                            except Exception as fetch_err:
                                debug_logger.log_error(
                                    f"[VIDEO] Failed to fetch video via get_media: {fetch_err}"
                                )

                    if not video_url:
                        error_msg = "Video generated but no media URL was returned"
                        await self._fail_video_task(checked_operations, error_msg)
                        self._mark_generation_failed(generation_result, error_msg)
                        yield self._create_error_response(error_msg, status_code=502)
                        return

                    video_info["url"] = video_url
                    video_info["mediaName"] = media_name
                    video_info["mediaGenerationId"] = video_media_id
                    metadata.setdefault("video", video_info)
                    operation["operation"]["metadata"] = metadata

                    # ========== Video upscale ==========
                    if upsample_config and video_media_id:
                        if stream:
                            resolution_name = "4K" if "4K" in upsample_config["resolution"] else "1080P"
                            yield self._create_stream_chunk(f"\nVideo generation complete, starting {resolution_name} upscaling... (may take up to 30 minutes)\n")
                        
                        try:
                            # Submit upscale task
                            upsample_result = await self.flow_client.upsample_video(
                                at=token.at,
                                project_id=project_id,
                                video_media_id=video_media_id,
                                aspect_ratio=aspect_ratio,
                                resolution=upsample_config["resolution"],
                                model_key=upsample_config["model_key"],
                                user_paygate_tier=normalized_tier,
                                token_id=token.id,
                                token_video_concurrency=token.video_concurrency,
                            )
                            
                            upsample_operations = upsample_result.get("operations", [])
                            if upsample_operations:
                                if stream:
                                    yield self._create_stream_chunk("Upscaling task submitted, continuing to poll...\n")
                                
                                # Recursively poll the upscale result (no further upscale)
                                async for chunk in self._poll_video_result(
                                    token,
                                    project_id,
                                    upsample_operations,
                                    stream,
                                    None,
                                    generation_result,
                                    response_state,
                                    request_log_state,
                                ):
                                    yield chunk
                                return
                            else:
                                if stream:
                                    yield self._create_stream_chunk("⚠️ Failed to create upscaling task, returning original video\n")
                        except Exception as e:
                            debug_logger.log_error(f"Video upsample failed: {str(e)}")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Upscale failed: {str(e)}, returning original video\n")

                    # ========== Extend video concatenation ==========
                    if extend_source_media_id and video_media_id:
                        try:
                            if stream:
                                yield self._create_stream_chunk("\nVideo extension complete, stitching full video...\n")
                            debug_logger.log_info(f"[CONCAT] Starting concat: original={extend_source_media_id[:12]}..., extend={video_media_id[:12]}...")
                            
                            # Submit concat task
                            concat_result = await self.flow_client.run_concatenation(
                                at=token.at,
                                original_media_id=extend_source_media_id,
                                extend_media_id=video_media_id,
                            )
                            
                            # Get operation name
                            concat_op = concat_result.get("operation", {}).get("operation", {}).get("name", "")
                            if concat_op:
                                if stream:
                                    yield self._create_stream_chunk("Stitching task submitted, waiting for completion...\n")
                                
                                # Poll concat status
                                concat_status = await self.flow_client.poll_concatenation_status(
                                    at=token.at,
                                    operation_name=concat_op,
                                    timeout=300,
                                    poll_interval=3,
                                )
                                
                                concat_url = concat_status.get("outputUri", "")
                                if concat_url:
                                    # If it is a local path (/tmp/xxx.mp4), build a full URL
                                    if concat_url.startswith("/tmp/"):
                                        server_host = config.server_host or "0.0.0.0"
                                        server_port = config.server_port or 8000
                                        # Use localhost externally
                                        host = "localhost" if server_host == "0.0.0.0" else server_host
                                        concat_url = f"http://{host}:{server_port}{concat_url}"
                                    video_url = concat_url  # Replace with the full concatenated video URL
                                    if stream:
                                        yield self._create_stream_chunk("✅ Video stitching complete! Returning the full 16s video\n")
                                    debug_logger.log_info(f"[CONCAT] Concat succeeded: {concat_url[:80]}...")
                                else:
                                    if stream:
                                        yield self._create_stream_chunk("⚠️ Stitching completed but no URL returned, returning the extension clip\n")
                            else:
                                debug_logger.log_warning("[CONCAT] Concat task creation failed, returning the extend clip")
                                if stream:
                                    yield self._create_stream_chunk("⚠️ Failed to create stitching task, returning the extension clip\n")
                        except Exception as e:
                            import traceback
                            debug_logger.log_error(f"[CONCAT] Concat failed: {str(e)}")
                            debug_logger.log_error(f"[CONCAT] traceback: {traceback.format_exc()}")
                            if stream:
                                yield self._create_stream_chunk(f"⚠️ Stitching failed: {str(e)}, returning the extension clip\n")
                            # Concat failure does not block the response; keep the extend clip URL

                    # Cache video (if enabled)
                    local_url = video_url
                    if config.cache_enabled:
                        await self._update_request_log_progress(request_log_state, token_id=token.id, status_text="caching_video", progress=92)
                        try:
                            if stream:
                                yield self._create_stream_chunk("Caching video file...\n")
                            cached_filename = await self.file_cache.download_and_cache(video_url, "video")
                            local_url = f"{self._get_base_url(response_state)}/tmp/{cached_filename}"
                            if stream:
                                yield self._create_stream_chunk("✅ Video cached successfully, preparing to return cached URL...\n")
                        except Exception as e:
                            debug_logger.log_error(f"Failed to cache video: {str(e)}")
                            # Cache failure does not block the result; use the original URL
                            local_url = video_url
                            if stream:
                                cache_error = self._normalize_error_message(e, max_length=120)
                                yield self._create_stream_chunk(f"⚠️ Cache failed: {cache_error}\nReturning source link...\n")
                    else:
                        if stream:
                            yield self._create_stream_chunk("Cache disabled, returning source link...\n")

                    # Update the database
                    task_id = operation["operation"]["name"]
                    await self.db.update_task(
                        task_id,
                        status="completed",
                        progress=100,
                        result_urls=[local_url],
                        completed_at=time.time()
                    )

                    # Store URL for logging
                    response_state["url"] = local_url
                    response_state["generated_assets"] = {
                        "type": "video",
                        "final_video_url": local_url,
                        "mediaGenerationId": video_media_id,
                        "mediaName": media_name,
                        "aspectRatio": aspect_ratio,
                        "model": resolved_video.get("model"),
                        "duration": resolved_video.get("duration"),
                    }

                    # Return result
                    self._mark_generation_succeeded(generation_result)

                    if stream:
                        yield self._create_stream_chunk(
                            f"<video src='{local_url}' data-media-id='{video_media_id}' controls style='max-width:100%'></video>",
                            finish_reason="stop"
                        )

                    else:
                        yield self._create_completion_response(
                            local_url,  # Pass the URL; the method formats it
                            media_type="video"
                        )
                    return

                elif status == "MEDIA_GENERATION_STATUS_FAILED":
                    # Generation failed - extract error info
                    error_info = operation.get("operation", {}).get("error", {})
                    error_code = error_info.get("code", "unknown")
                    error_message = error_info.get("message", "Unknown error")
                    
                    # Update the task status in the database
                    await self._fail_video_task(
                        checked_operations,
                        f"{error_message} (code: {error_code})"
                    )
                    
                    # Return a friendly error asking the user to retry
                    friendly_error = f"Video generation failed: {error_message}. Please try again"
                    self._mark_generation_failed(generation_result, friendly_error)
                    if stream:
                        yield self._create_stream_chunk(f"Error: {friendly_error}\n")
                    yield self._create_error_response(friendly_error, status_code=502)
                    return

                elif status.startswith("MEDIA_GENERATION_STATUS_ERROR"):
                    # ??????
                    error_msg = f"Video generation failed: {status}"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                    
                elif status == "MEDIA_GENERATION_STATUS_ACTIVE" and attempt > 80:
                    # If still ACTIVE after 4 minutes (80 * 3s = 240s), treat it as stuck
                    error_msg = "Video generation timed out (upstream stalled for over 4 minutes, automatically cancelled)"
                    await self._fail_video_task(checked_operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"Error: {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=504)
                    return

            except Exception as e:
                last_poll_error = e
                consecutive_poll_errors += 1
                debug_logger.log_error(f"Poll error: {str(e)}")
                if consecutive_poll_errors >= max_consecutive_poll_errors:
                    error_msg = f"Video status query failed: {self._normalize_error_message(e)}"
                    await self._fail_video_task(operations, error_msg)
                    self._mark_generation_failed(generation_result, error_msg)
                    if stream:
                        yield self._create_stream_chunk(f"Error: {error_msg}\n")
                    yield self._create_error_response(error_msg, status_code=502)
                    return
                continue

        # Timeout
        if last_poll_error is not None:
            error_msg = f"Video status query kept failing: {self._normalize_error_message(last_poll_error)}"
        else:
            error_msg = f"Video generation timed out (polled {max_attempts} times)"
        await self._fail_video_task(operations, error_msg)
        self._mark_generation_failed(generation_result, error_msg)
        yield self._create_error_response(error_msg, status_code=504)

    # ========== Response formatting ==========

    def _create_stream_chunk(self, content: str, role: str = None, finish_reason: str = None, extra: Optional[Dict[str, Any]] = None) -> str:
        """Create a streaming response chunk"""
        import json
        import time

        chunk = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "flow2api",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason
            }]
        }

        if role:
            chunk["choices"][0]["delta"]["role"] = role

        if finish_reason:
            chunk["choices"][0]["delta"]["content"] = content
        else:
            chunk["choices"][0]["delta"]["reasoning_content"] = content

        # Additive top-level fields (e.g. flow_upscale on the final image chunk).
        for key, value in (extra or {}).items():
            if value is not None:
                chunk[key] = value

        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def _create_completion_response(
        self,
        content: str,
        media_type: str = "image",
        is_availability_check: bool = False,
        upscale: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a non-streaming response

        Args:
            content: media URL or plain text message
            media_type: media type ("image" or "video")
            is_availability_check: whether this is an availability check response (plain text)

        Returns:
            JSON response
        """
        import json
        import time

        # Availability check: return plain text
        if is_availability_check:
            formatted_content = content
        else:
            # Media generation: format content as Markdown by media type
            if media_type == "video":
                formatted_content = f"```html\n<video src='{content}' controls></video>\n```"
            else:  # image
                formatted_content = f"![Generated Image]({content})"

        response = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "flow2api",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": formatted_content
                },
                "finish_reason": "stop"
            }]
        }
        # 2K/4K requests: what was asked vs delivered ({"requested":"2K","delivered":"1K","reason":"quota"}).
        # The image content stays unchanged so existing URL parsers keep working.
        if upscale:
            response["flow_upscale"] = upscale

        return json.dumps(response, ensure_ascii=False)

    async def _record_upscale_failure(self, token, resolution_name: str, kind: str, message: str, request_id: str) -> None:
        """Remember why this account could not enlarge, so the next 2K/4K request prefers another
        account (load_balancer._can_enlarge). Not a generation failure: no record_error, no
        account-wide cooldown, the generation model stays available."""
        until = None
        if kind in ("quota", "access"):
            until = self.token_manager._next_pt_daily_reset()
        elif kind == "traffic":
            until = datetime.now(timezone.utc) + timedelta(minutes=UPSAMPLE_TRAFFIC_COOLDOWN_MIN)
        if until is not None:
            try:
                await self.token_manager.mark_model_quota_exhausted(
                    token.id, upsample_quota_key(resolution_name), message, until=until
                )
            except Exception as e:
                debug_logger.op_warning(f"[UPSAMPLE] could not record cooldown: {e}")
        debug_logger.op_warning(
            f"[UPSAMPLE] req={request_id} token={token.id} res={resolution_name} outcome={kind} "
            f"delivered=1K cooldown_until={until.isoformat(timespec='minutes') if until else '-'} "
            f"reason={(message or '')[:160]}"
        )

    def _create_error_response(self, error_message: str, status_code: int = 500, extra: Optional[Dict[str, Any]] = None) -> str:
        """Create an error response

        `extra` (e.g. client_policy.no_account_error) may override `code` and add fields;
        `message`, `type` and `status_code` keep their shape — `status_code` is what
        routes._get_error_status_code turns into the HTTP status.
        """
        import json

        error = {
            "error": {
                "message": error_message,
                "type": "server_error" if status_code >= 500 else "invalid_request_error",
                "code": "generation_failed",
                "status_code": status_code,
            }
        }
        if extra:
            error["error"].update(extra)

        return json.dumps(error, ensure_ascii=False)

    def _get_base_url(self, response_state: Optional[Dict[str, Any]] = None) -> str:
        """Get the base URL for cached file access"""
        # When a cache domain is configured, always prefer it so the request Host/IP does not override it.
        if config.cache_base_url:
            return config.cache_base_url.rstrip("/")

        request_base_url = ""
        if isinstance(response_state, dict):
            request_base_url = (response_state.get("base_url") or "").strip().rstrip("/")
        if request_base_url:
            return request_base_url

        # Fall back to the service address so the listen address 0.0.0.0 / :: is not returned to clients
        server_host = (config.server_host or "").strip()
        if server_host in {"", "0.0.0.0", "::", "[::]"}:
            server_host = "127.0.0.1"

        return f"http://{server_host}:{config.server_port}"

    async def _update_request_log_progress(
        self,
        request_log_state: Optional[Dict[str, Any]],
        *,
        token_id: Optional[int] = None,
        status_text: str,
        progress: int,
        response_extra: Optional[Dict[str, Any]] = None,
    ):
        """?????????????"""
        if not isinstance(request_log_state, dict):
            return
        log_id = request_log_state.get("id")
        if not log_id:
            return

        safe_progress = max(0, min(100, int(progress)))
        now = time.time()
        last_status_text = str(request_log_state.get("last_status_text") or "").strip()
        last_progress = int(request_log_state.get("last_progress") or 0)
        last_updated_at = float(request_log_state.get("last_progress_update_at") or 0)

        request_log_state["progress"] = safe_progress
        request_log_state["last_status_text"] = status_text
        request_log_state["last_progress"] = safe_progress
        payload = {
            "status": "processing",
            "status_text": status_text,
            "progress": safe_progress,
        }
        if isinstance(response_extra, dict):
            payload.update(response_extra)

        should_write = (
            safe_progress in (0, 100)
            or status_text != last_status_text
            or safe_progress >= last_progress + 5
            or (now - last_updated_at) >= 1.0
        )
        if not should_write:
            return

        request_log_state["last_progress_update_at"] = now

        try:
            await self.db.update_request_log(
                log_id,
                token_id=token_id,
                response_body=json.dumps(payload, ensure_ascii=False),
                status_code=102,
                duration=0,
                status_text=status_text,
                progress=safe_progress,
            )
        except Exception as e:
            debug_logger.log_error(f"Failed to update request log progress: {e}")

    async def _log_request(
        self,
        token_id: Optional[int],
        operation: str,
        request_data: Dict[str, Any],
        response_data: Dict[str, Any],
        status_code: int,
        duration: float,
        log_id: Optional[int] = None,
        status_text: Optional[str] = None,
        progress: Optional[int] = None,
    ):
        """???????????? log_id ????????"""
        try:
            effective_status_text = status_text or (
                "completed" if status_code == 200 else "failed" if status_code >= 400 else "processing"
            )
            effective_progress = progress
            if effective_progress is None:
                effective_progress = 100 if status_code == 200 else 0 if status_code >= 400 else 0
            effective_progress = max(0, min(100, int(effective_progress)))

            request_body = json.dumps(request_data, ensure_ascii=False)
            response_body = json.dumps(response_data, ensure_ascii=False)

            if log_id:
                await self.db.update_request_log(
                    log_id,
                    token_id=token_id,
                    operation=operation,
                    request_body=request_body,
                    response_body=response_body,
                    status_code=status_code,
                    duration=duration,
                    status_text=effective_status_text,
                    progress=effective_progress,
                )
                return log_id

            log = RequestLog(
                token_id=token_id,
                operation=operation,
                request_body=request_body,
                response_body=response_body,
                status_code=status_code,
                duration=duration,
                status_text=effective_status_text,
                progress=effective_progress,
            )
            return await self.db.add_request_log(log)
        except Exception as e:
            debug_logger.log_error(f"Failed to log request: {e}")
            return None
