"""Remove the visible Gemini sparkle watermark from Flow-generated images.

Method: Reverse Alpha Blending, from GargantuaX/gemini-watermark-remover and
AllenK's Gemini Watermark Tool (both MIT, see src/assets/watermark/LICENSE).
Flow composites a white logo onto the picture:

    watermarked = alpha * 255 + (1 - alpha) * original

so with the logo's alpha mask the original pixel is recovered exactly:

    original = (watermarked - alpha * 255) / (1 - alpha)

Calibrated on flow2api output (2026-09-22): Flow stamps the 48px mask at about
0.6 of its strength, 73px from the right/bottom edge on 1K images and 89px on
2K. The logo sits on a fixed pixel, so nothing is searched and an unknown image
size is left untouched. Only the visible logo is touched: the file's metadata
(C2PA content credentials, IPTC/XMP "Made with Google AI") is carried over.
"""

from __future__ import annotations

import io
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
from PIL import Image, ImageFilter, JpegImagePlugin

ALPHA_NOISE_FLOOR = 3 / 255
ALPHA_THRESHOLD = 0.002
MAX_ALPHA = 0.99
LOGO_VALUE = 255.0

# Removing a real watermark erases logo-shaped edges; "removing" one that is
# not there draws them. Measured: real >= +0.31, clean <= -0.04.
DETECT_MIN_EDGE_DROP = 0.15
# After removal a dark logo-shaped outline means the stamp was weaker than
# calibrated (Google changed it): put the original back. Good removals peak
# at 0.24.
DARK_GHOST_MIN_EDGE_MATCH = 0.35
POSITION_JITTER = 1
# JPEG ringing around the original logo edge survives the inverse blend as a
# faint outline. Smooth a thin band along the outline, only on smooth colour.
OUTLINE_BLUR_RADIUS = 1.2
OUTLINE_PAD = 17
FLAT_NOISE_SCALE = 4.0
NEAR_BLACK = 5
MAX_NEAR_BLACK_INCREASE = 0.05
# The surroundings count as black a little above NEAR_BLACK: on a dark background
# at level 5-9, JPEG noise in the restored logo dips under 5 while none of the
# untouched ring does, which would refuse the most visible case (grey on black).
SURROUND_NEAR_BLACK = NEAR_BLACK + 4


@dataclass(frozen=True)
class WatermarkSpec:
    logo_size: int
    margin_right: int
    margin_bottom: int
    mask: str
    gain: float


# (width, height) -> where the logo sits and how strong it is. Only sizes
# measured on real output; add a size by re-running the calibration in
# tmp/watermark_test on a few free/Pro images of that size.
KNOWN_SPECS: Dict[Tuple[int, int], WatermarkSpec] = {
    (1536, 2752): WatermarkSpec(48, 89, 89, "bg_48.png", 0.59),  # 2K portrait (upscaled)
    (1376, 768): WatermarkSpec(48, 73, 73, "bg_48.png", 0.60),   # 1K landscape
    (768, 1376): WatermarkSpec(48, 73, 73, "bg_48.png", 0.60),   # 1K portrait
    # 3:4 1K (gemini-3.0-pro-image-three-four, e.g. a failed 4K enlarge delivered as 1K): measured
    # 26 Sep 2026 on 4 Pro images, logo at 76 px in all 4 (NCC 0.41-0.98), strength 0.59-0.62.
    (896, 1200): WatermarkSpec(48, 76, 76, "bg_48.png", 0.60),
    # 4:3 1K: same stamp assumed by symmetry with the 1K portrait/landscape pair; NOT yet seen on
    # a real image — the edge-drop detection leaves it untouched if the logo is not there.
    (1200, 896): WatermarkSpec(48, 76, 76, "bg_48.png", 0.60),
}

_MASK_DIR = Path(__file__).resolve().parent.parent / "assets" / "watermark"
_mask_cache: Dict[str, np.ndarray] = {}


@dataclass
class WatermarkResult:
    applied: bool
    reason: str
    width: int = 0
    height: int = 0
    x: int = 0
    y: int = 0
    size: int = 0
    edge_drop: float = 0.0
    edge_after: float = 0.0
    step_after: float = 0.0
    gain: float = 0.0
    ms: float = 0.0

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("edge_drop", "edge_after", "step_after"):
            data[key] = round(data[key], 3)
        data["ms"] = round(data["ms"], 1)
        return data


def _mask(name: str) -> np.ndarray:
    if name not in _mask_cache:
        rgb = np.asarray(Image.open(_MASK_DIR / name).convert("RGB"), dtype=np.float64)
        _mask_cache[name] = rgb.max(axis=2) / 255.0
    return _mask_cache[name]


def _gray(rgb: np.ndarray) -> np.ndarray:
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0


def _sobel_xy(g: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[1:-1, 1:-1] = (-g[:-2, :-2] - 2 * g[1:-1, :-2] - g[2:, :-2]
                      + g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:])
    gy[1:-1, 1:-1] = (-g[:-2, :-2] - 2 * g[:-2, 1:-1] - g[:-2, 2:]
                      + g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:])
    return gx, gy


def _sobel(g: np.ndarray) -> np.ndarray:
    gx, gy = _sobel_xy(g)
    return np.sqrt(gx * gx + gy * gy)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a.ravel() - a.mean()
    b = b.ravel() - b.mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / den) if den > 1e-9 else 0.0


def _reverse_blend(region: np.ndarray, alpha: np.ndarray, gain: float) -> np.ndarray:
    active = (np.maximum(0.0, alpha - ALPHA_NOISE_FLOOR) * gain >= ALPHA_THRESHOLD)[..., None]
    a = np.minimum(alpha * gain, MAX_ALPHA)[..., None]
    restored = np.clip(np.round((region - a * LOGO_VALUE) / (1.0 - a)), 0, 255)
    return np.where(active, restored, region)


def _edge_match(region: np.ndarray, alpha: np.ndarray) -> float:
    return _ncc(_sobel(_gray(region)), _sobel(alpha))


def _edge_step(region: np.ndarray, alpha: np.ndarray) -> float:
    """Signed brightness step across the logo outline, in grey levels.

    > 0: the logo area is still brighter than its surroundings (under-removed);
    < 0: it is darker (over-removed).
    """
    ax, ay = _sobel_xy(alpha)
    gx, gy = _sobel_xy(_gray(region) * 255.0)
    den = (ax * ax + ay * ay).sum()
    return float((gx * ax + gy * ay).sum() / den) if den > 0 else 0.0


def _near_black_ratio(region: np.ndarray) -> float:
    return float((region.max(axis=2) <= NEAR_BLACK).mean())


def _surround_near_black_ratio(rgb: np.ndarray, x: int, y: int, s: int, ring: int = 8) -> float:
    """Near-black share of the untouched ring just outside the logo box.

    Removal must not leave the logo area blacker than its surroundings. The
    upstream check compares against the watermarked pixels instead, which the
    logo has lifted, so it refused every sparkle on a black background.
    """
    h, w, _ = rgb.shape
    x0, y0, x1, y1 = max(0, x - ring), max(0, y - ring), min(w, x + s + ring), min(h, y + s + ring)
    patch = rgb[y0:y1, x0:x1]
    inner = np.zeros(patch.shape[:2], bool)
    inner[y - y0:y - y0 + s, x - x0:x - x0 + s] = True
    return float((patch.max(axis=2)[~inner] <= SURROUND_NEAR_BLACK).mean())


def _outline_band(alpha: np.ndarray) -> np.ndarray:
    g = np.hypot(*np.gradient(alpha))
    band = (np.clip(g / g.max() * 3, 0, 1) * 255).astype(np.uint8)
    band = Image.fromarray(band).filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(0.7))
    return np.asarray(band, dtype=np.float64) / 255.0


def _repair_outline(out_rgb: np.ndarray, src_rgb: np.ndarray, alpha: np.ndarray, x: int, y: int, s: int) -> None:
    """Blend a thin band along the logo outline toward a blurred copy, in place.

    The blend weight falls to zero on textured backgrounds, measured from the
    untouched ring around the logo with a median so one strong edge in the
    picture (a colour band, a letter) does not count as texture.
    """
    p = OUTLINE_PAD
    h, w, _ = out_rgb.shape
    x0, y0, x1, y1 = x - p, y - p, x + s + p, y + s + p
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
        return
    lum = np.asarray(Image.fromarray(src_rgb[y0:y1, x0:x1]).convert("L"), dtype=np.float64)
    detail = lum - np.asarray(Image.fromarray(lum.astype(np.uint8)).filter(ImageFilter.GaussianBlur(3)),
                              dtype=np.float64)
    ring = np.ones(lum.shape, bool)
    ring[p - 4:p + s + 4, p - 4:p + s + 4] = False
    noise = 1.4826 * float(np.median(np.abs(detail[ring])))
    flat = float(np.exp(-(noise / FLAT_NOISE_SCALE) ** 2))
    if flat < 0.01:
        return
    patch = out_rgb[y0:y1, x0:x1]
    smooth = np.asarray(Image.fromarray(patch).filter(ImageFilter.GaussianBlur(OUTLINE_BLUR_RADIUS)),
                        dtype=np.float64)[p:p + s, p:p + s]
    wgt = (_outline_band(alpha) * flat)[..., None]
    mixed = patch[p:p + s, p:p + s].astype(np.float64) * (1 - wgt) + smooth * wgt
    out_rgb[y:y + s, x:x + s] = np.clip(np.round(mixed), 0, 255).astype(out_rgb.dtype)


def remove_watermark(rgb: np.ndarray) -> Tuple[np.ndarray, WatermarkResult]:
    """Return (cleaned rgb, result). The input array is never modified."""
    t0 = time.perf_counter()
    h, w, _ = rgb.shape
    spec = KNOWN_SPECS.get((w, h))
    if spec is None:
        return rgb, WatermarkResult(False, "unknown-size", width=w, height=h,
                                    ms=(time.perf_counter() - t0) * 1000)

    alpha = _mask(spec.mask)
    s = spec.logo_size
    bx, by = w - spec.margin_right - s, h - spec.margin_bottom - s

    best = None
    for dy in range(-POSITION_JITTER, POSITION_JITTER + 1):
        for dx in range(-POSITION_JITTER, POSITION_JITTER + 1):
            x, y = bx + dx, by + dy
            region = rgb[y:y + s, x:x + s].astype(np.float64)
            drop = _edge_match(region, alpha) - _edge_match(_reverse_blend(region, alpha, spec.gain), alpha)
            if best is None or drop > best[0]:
                best = (drop, x, y, region)
    drop, x, y, region = best
    res = WatermarkResult(False, "", width=w, height=h, x=x, y=y, size=s, edge_drop=drop, gain=spec.gain)

    def done(reason: str) -> Tuple[np.ndarray, WatermarkResult]:
        res.reason, res.ms = reason, (time.perf_counter() - t0) * 1000
        return rgb, res

    if drop < DETECT_MIN_EDGE_DROP:
        return done("not-detected")

    cleaned = _reverse_blend(region, alpha, spec.gain)
    if _near_black_ratio(cleaned) > _surround_near_black_ratio(rgb, x, y, s) + MAX_NEAR_BLACK_INCREASE:
        return done("safety-near-black")

    out_rgb = rgb.copy()
    out_rgb[y:y + s, x:x + s] = cleaned.astype(rgb.dtype)
    _repair_outline(out_rgb, rgb, alpha, x, y, s)

    after = out_rgb[y:y + s, x:x + s].astype(np.float64)
    res.edge_after = _edge_match(after, alpha)
    res.step_after = _edge_step(after, alpha)
    if res.edge_after >= DARK_GHOST_MIN_EDGE_MATCH and res.step_after < 0:
        return done("reverted-dark-ghost")

    res.applied = True
    res.reason = "removed"
    res.ms = (time.perf_counter() - t0) * 1000
    return out_rgb, res


_SOI = b"\xff\xd8"


def _jpeg_header_segments(data: bytes):
    """Yield (marker, raw segment bytes) for the segments before the first SOS/SOF."""
    if not data.startswith(_SOI):
        return
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xDA or 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return  # SOS or a frame header: metadata segments are over
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        end = pos + 2 + length
        if length < 2 or end > len(data):
            return
        yield marker, data[pos:end]
        pos = end


def _carry_metadata(src: bytes, out: bytes) -> bytes:
    """Copy the source's APP1-APP15 and COM segments into the re-encoded JPEG.

    Keeps C2PA (APP11), XMP/EXIF (APP1), IPTC (APP13) and ICC (APP2) exactly as
    Google wrote them. The encoder's own APP0 (JFIF) stays first.
    """
    carried = [seg for marker, seg in _jpeg_header_segments(src) if 0xE1 <= marker <= 0xEF or marker == 0xFE]
    if not carried or not out.startswith(_SOI):
        return out
    pos = 2
    head = b""
    for marker, seg in _jpeg_header_segments(out):
        if marker == 0xE0:
            head += seg
            pos += len(seg)
            continue
        break
    return _SOI + head + b"".join(carried) + out[pos:]


def clean_image_bytes(data: bytes) -> Tuple[bytes, WatermarkResult]:
    """Bytes in, bytes out. Never raises; returns the input unchanged unless a watermark was removed."""
    t0 = time.perf_counter()
    try:
        src = Image.open(io.BytesIO(data))
        rgb = np.asarray(src.convert("RGB"), dtype=np.uint8)
        out_rgb, result = remove_watermark(rgb)
        if not result.applied:
            result.ms = (time.perf_counter() - t0) * 1000
            return data, result
        out = Image.fromarray(out_rgb)
        buf = io.BytesIO()
        if src.format == "JPEG":
            out.save(buf, "JPEG", qtables=src.quantization,
                     subsampling=JpegImagePlugin.get_sampling(src), optimize=True)
            cleaned = _carry_metadata(data, buf.getvalue())
        else:
            out.save(buf, src.format or "PNG")
            cleaned = buf.getvalue()
        result.ms = (time.perf_counter() - t0) * 1000
        return cleaned, result
    except Exception as exc:  # a broken image must never break a generation
        return data, WatermarkResult(False, f"error:{type(exc).__name__}", ms=(time.perf_counter() - t0) * 1000)
