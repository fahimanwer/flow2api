"""Plain-words description of every model id (2026-10-11).

One source of truth for /v1/models, /v1beta/models, the admin test page and docs/models.md.
Ids never change here: this only DESCRIBES MODEL_CONFIG (family, Google's official name,
mode, orientation, duration, resolution, which accounts can run it, accepted inputs) and lists
the readable aliases from model_resolver.READABLE_MODEL_ALIASES.

Names come from Google's live Flow model list (GET aisandbox-pa /v1/flow/models, read
2026-10-11) and Google's model docs: Nano Banana 2.1 = gemini-nano-banana-2.1 (GA 2026-10-06),
Nano Banana Pro = gemini-3-pro-image, Nano Banana 2 Lite = gemini-3.1-flash-lite-image,
Gemini Omni 1.1 Flash = gemini-omni-1.1-flash, Veo 3.1 Quality / Fast / Lite.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..core.account_tiers import PAYGATE_TIER_ONE, PAYGATE_TIER_TWO, get_required_paygate_tier_for_model
from ..core.model_resolver import READABLE_MODEL_ALIASES, readable_aliases_for
from .characters import MAX_CHARACTERS_IMAGE, MAX_CHARACTERS_VIDEO, MAX_PHOTOS_PER_CHARACTER
from .generation_handler import FLOW_IMAGE_MAX_REFERENCES, FLOW_OMNI_MAX_REFERENCES, MODEL_CONFIG

IMAGE_FAMILIES = {
    "GEM_PIX_2": ("Nano Banana Pro", "gemini-3-pro-image"),
    "BELUGA": ("Nano Banana 2.1", "gemini-nano-banana-2.1"),
    "HARBOR_SEAL": ("Nano Banana 2 Lite", "gemini-3.1-flash-lite-image"),
}

ORIENTATIONS = {
    "IMAGE_ASPECT_RATIO_LANDSCAPE": "Landscape 16:9",
    "IMAGE_ASPECT_RATIO_PORTRAIT": "Portrait 9:16",
    "IMAGE_ASPECT_RATIO_SQUARE": "Square 1:1",
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": "Landscape 4:3",
    "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": "Portrait 3:4",
    "VIDEO_ASPECT_RATIO_LANDSCAPE": "Landscape 16:9",
    "VIDEO_ASPECT_RATIO_PORTRAIT": "Portrait 9:16",
}

ACCOUNT_LABELS = {PAYGATE_TIER_TWO: "Ultra only", PAYGATE_TIER_ONE: "Pro or Ultra"}


def _video_family(model_key: str) -> tuple:
    if model_key.startswith("abra"):
        return "Omni 1.1 Flash", "gemini-omni-1.1-flash"
    if "lite" in model_key:
        return "Veo 3.1 Lite", "veo-3.1-lite"
    if "fast" in model_key:
        return "Veo 3.1 Fast", "veo-3.1-fast"
    return "Veo 3.1 Quality", "veo-3.1-quality"


def _video_mode(cfg: Dict[str, Any]) -> str:
    video_type = cfg.get("video_type")
    if video_type == "t2v":
        return "Text to video"
    if video_type == "r2v":
        return "Ingredients to video (reference images)"
    if video_type == "extend":
        return "Extend a video"
    if video_type == "omni":
        if cfg.get("reference_only"):
            return "Ingredients to video (reference images)"
        return "Auto: text only, 1-2 frames, or 3+ ingredients"
    # i2v
    if cfg.get("min_images") == 2:
        return "First + last frame to video"
    if cfg.get("max_images") == 1:
        return "First frame to video"
    return "Frames to video (1 = first frame, 2 = first + last)"


def _duration_seconds(model_id: str, cfg: Dict[str, Any]) -> Optional[int]:
    if cfg.get("video_type") == "extend":
        return None  # adds a clip to the source video; the total length depends on the source
    for text in (model_id, cfg.get("model_key", "")):
        found = re.search(r"_(\d+)s(?:_|$)", text)
        if found:
            return int(found.group(1))
    return int(cfg.get("reference_duration") or 8)


def _resolution(cfg: Dict[str, Any]) -> str:
    upsample = cfg.get("upsample")
    if cfg["type"] == "image":
        if isinstance(upsample, str) and "4K" in upsample:
            return "4K"
        if isinstance(upsample, str) and "2K" in upsample:
            return "2K"
        return "1K"
    if isinstance(upsample, dict):
        target = "4K" if "4K" in upsample.get("resolution", "") else "1080p"
        if cfg.get("output_resolution"):
            return f"{target} (upscaled from pinned 720p)"
        return f"{target} (upscaled from 720p)"
    if cfg.get("output_resolution"):
        return "720p (pinned)"
    return "720p"


def _accounts(model_id: str, cfg: Dict[str, Any], resolution: str) -> str:
    if cfg["type"] == "image":
        # Images are not tier-gated; only the 2K/4K enlarge step is (Google's list: 2K every tier, 4K Ultra).
        if resolution == "4K":
            return "Any account; the 4K enlarge needs Ultra (others deliver 1K with a flow_upscale note)"
        return "Any account"
    return ACCOUNT_LABELS.get(get_required_paygate_tier_for_model(model_id), "Any account")


def _inputs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    if cfg["type"] == "image":
        return {"min_images": 0, "max_images": FLOW_IMAGE_MAX_REFERENCES,
                "characters": MAX_CHARACTERS_IMAGE, "photos_per_character": MAX_PHOTOS_PER_CHARACTER}
    video_type = cfg.get("video_type")
    if video_type == "t2v":
        return {"min_images": 0, "max_images": 0, "characters": 0}
    if video_type == "extend":
        return {"min_images": 0, "max_images": 0, "characters": 0, "requires_video_id": True}
    if video_type == "i2v":
        return {"min_images": int(cfg.get("min_images", 1)), "max_images": int(cfg.get("max_images", 2)), "characters": 0}
    if video_type == "r2v":
        return {"min_images": 0, "max_images": int(cfg.get("max_images", 3)),
                "characters": MAX_CHARACTERS_VIDEO, "photos_per_character": MAX_PHOTOS_PER_CHARACTER}
    # omni: ingredients-only needs an image or a character; plain omni also runs text only
    return {"min_images": 1 if cfg.get("reference_only") else 0, "max_images": FLOW_OMNI_MAX_REFERENCES,
            "characters": MAX_CHARACTERS_VIDEO, "photos_per_character": MAX_PHOTOS_PER_CHARACTER,
            **({"images_or_characters": True} if cfg.get("reference_only") else {})}


def describe_model(model_id: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Every field /v1/models shows for `model_id` (an existing MODEL_CONFIG key)."""
    cfg = cfg or MODEL_CONFIG[model_id]
    orientation = ORIENTATIONS.get(cfg.get("aspect_ratio", ""), "")
    resolution = _resolution(cfg)
    if cfg["type"] == "image":
        family, google_model = IMAGE_FAMILIES.get(cfg.get("model_name"), (cfg.get("model_name", ""), ""))
        mode = "Text to image, or edit / combine reference images"
        duration = None
        parts = [family, orientation, resolution]
    else:
        family, google_model = _video_family(cfg.get("model_key", ""))
        mode = _video_mode(cfg)
        duration = _duration_seconds(model_id, cfg)
        parts = [family, mode, f"{duration} s" if duration else "", orientation, resolution]
    retired = "relaxed" in cfg.get("model_key", "")  # Google lists every *_relaxed key as deprecated (2026-10-11)
    if "relaxed" in model_id:
        parts.append("Relaxed queue")
    name = " · ".join(p for p in parts if p)
    if retired:
        name += " (retired by Google)"
    accounts = _accounts(model_id, cfg, resolution)
    inputs = _inputs(cfg)
    aliases = readable_aliases_for(model_id)
    sentence = f"{name}. Google model: {google_model or 'n/a'}. Accounts: {accounts}."
    if inputs.get("requires_video_id"):
        sentence += " Needs a source video: send an image_url of extend://<media id>."
    elif cfg["type"] == "video" and inputs["max_images"]:
        sentence += f" Images: {inputs['min_images']}-{inputs['max_images']}."
    if inputs.get("characters"):
        sentence += f" Characters (@Name): up to {inputs['characters']}, {MAX_PHOTOS_PER_CHARACTER} photos each."
    if aliases:
        sentence += f" Also callable as: {', '.join(aliases)}."
    if retired:
        sentence += " Google retired this queue; requests will likely fail."
    return {
        "name": name,
        "family": family,
        "google_model": google_model,
        "type": cfg["type"],
        "mode": mode,
        "orientation": orientation,
        "duration_seconds": duration,
        "resolution": resolution,
        "accounts": accounts,
        "inputs": inputs,
        "aliases": aliases,
        "retired_by_google": retired,
        "description": sentence,
    }


def model_catalog() -> List[Dict[str, Any]]:
    """/v1/models entries: stable id first, then the plain-words fields."""
    return [
        {"id": model_id, "object": "model", "owned_by": "flow2api", **describe_model(model_id, cfg)}
        for model_id, cfg in MODEL_CONFIG.items()
    ]


def readable_alias_entries() -> List[Dict[str, Any]]:
    """/v1/models/aliases entries for the readable full-name aliases."""
    entries = []
    for alias, target in sorted(READABLE_MODEL_ALIASES.items()):
        if target not in MODEL_CONFIG:
            continue
        info = describe_model(target)
        entries.append({
            "id": alias, "object": "model", "owned_by": "flow2api", "is_alias": True, "alias_of": target,
            "name": info["name"], "description": f"Alias of {target}. {info['description']}",
        })
    return entries
