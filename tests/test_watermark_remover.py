import io
import unittest

import numpy as np
from PIL import Image

from src.services import watermark_remover as wm


def _background(kind: str, width: int, height: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:height, 0:width]
    if kind == "flat":
        img = np.zeros((height, width, 3)) + (150, 110, 100)
    elif kind == "gradient":
        img = np.stack([80 + 100 * xx / width, 120 + 60 * yy / height, 90 + 0 * xx], axis=-1)
    elif kind == "texture":
        base = np.zeros((height, width, 3)) + (140, 90, 60)
        img = base + rng.normal(0, 18, (height, width, 1)) + 12 * np.sin(xx / 3.0)[..., None]
    elif kind == "near_black":
        img = np.zeros((height, width, 3)) + (4, 5, 6)
    else:
        raise ValueError(kind)
    return np.clip(img, 0, 255)


def _stamp(rgb: np.ndarray, gain: float) -> np.ndarray:
    h, w, _ = rgb.shape
    spec = wm.KNOWN_SPECS[(w, h)]
    s = spec.logo_size
    x, y = w - spec.margin_right - s, h - spec.margin_bottom - s
    a = (wm._mask(spec.mask) * gain)[..., None]
    out = rgb.copy()
    out[y:y + s, x:x + s] = a * 255 + (1 - a) * out[y:y + s, x:x + s]
    return out


def _jpeg(rgb: np.ndarray, app_segments=()) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.round(rgb), 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=92, subsampling=2)
    data = buf.getvalue()
    if app_segments:
        # insert after SOI + APP0 like Google's files
        app0_len = int.from_bytes(data[4:6], "big")
        head = data[:4 + app0_len]
        data = head + b"".join(app_segments) + data[4 + app0_len:]
    return data


def _app(marker: int, payload: bytes) -> bytes:
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _logo_region(data: bytes, spec_size) -> np.ndarray:
    w, h = spec_size
    spec = wm.KNOWN_SPECS[spec_size]
    s = spec.logo_size
    x, y = w - spec.margin_right - s, h - spec.margin_bottom - s
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float64)
    return rgb[y:y + s, x:x + s], wm._mask(spec.mask)


class WatermarkRemoverTests(unittest.TestCase):
    def test_removes_stamp_on_every_calibrated_size(self):
        for size, spec in wm.KNOWN_SPECS.items():
            for kind in ("flat", "gradient", "texture", "near_black"):
                with self.subTest(size=size, background=kind):
                    clean = _background(kind, *size)
                    data = _jpeg(_stamp(clean, spec.gain))
                    before, alpha = _logo_region(data, size)

                    cleaned, result = wm.clean_image_bytes(data)

                    self.assertTrue(result.applied, result.reason)
                    after, _ = _logo_region(cleaned, size)
                    self.assertGreater(wm._edge_match(before, alpha), 0.3)
                    self.assertLess(wm._edge_match(after, alpha), 0.3)
                    s = spec.logo_size
                    truth = clean[size[1] - spec.margin_bottom - s:size[1] - spec.margin_bottom,
                                  size[0] - spec.margin_right - s:size[0] - spec.margin_right]
                    # back within JPEG noise of the unstamped picture
                    self.assertLess(np.abs(after - truth).mean(), 6.0)

    def test_clean_image_is_returned_byte_identical(self):
        for size in wm.KNOWN_SPECS:
            for kind in ("flat", "texture"):
                with self.subTest(size=size, background=kind):
                    data = _jpeg(_background(kind, *size))
                    cleaned, result = wm.clean_image_bytes(data)
                    self.assertFalse(result.applied)
                    self.assertEqual(result.reason, "not-detected")
                    self.assertIs(cleaned, data)

    def test_unknown_size_is_left_alone(self):
        data = _jpeg(_background("flat", 1024, 1024))
        cleaned, result = wm.clean_image_bytes(data)
        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "unknown-size")
        self.assertEqual((result.width, result.height), (1024, 1024))
        self.assertIs(cleaned, data)

    def test_pixels_outside_the_logo_barely_move(self):
        size = (1376, 768)
        spec = wm.KNOWN_SPECS[size]
        data = _jpeg(_stamp(_background("texture", *size), spec.gain))
        cleaned, result = wm.clean_image_bytes(data)
        self.assertTrue(result.applied)
        a = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float64)
        b = np.asarray(Image.open(io.BytesIO(cleaned)).convert("RGB"), dtype=np.float64)
        keep = np.ones(a.shape[:2], bool)
        keep[result.y - wm.OUTLINE_PAD:result.y + result.size + wm.OUTLINE_PAD,
             result.x - wm.OUTLINE_PAD:result.x + result.size + wm.OUTLINE_PAD] = False
        mse = ((a[keep] - b[keep]) ** 2).mean()
        self.assertGreater(10 * np.log10(255 ** 2 / max(mse, 1e-9)), 45)

    def test_provenance_metadata_is_carried_over(self):
        size = (768, 1376)
        spec = wm.KNOWN_SPECS[size]
        c2pa = _app(0xEB, b"JP\x00\x01" + b"c2pa-manifest" * 40)
        xmp = _app(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>trainedAlgorithmicMedia</x:xmpmeta>")
        iptc = _app(0xED, b"Photoshop 3.0\x008BIM Made with Google AI")
        data = _jpeg(_stamp(_background("flat", *size), spec.gain), (xmp, c2pa, iptc))

        cleaned, result = wm.clean_image_bytes(data)

        self.assertTrue(result.applied)
        for segment in (xmp, c2pa, iptc):
            self.assertIn(segment, cleaned)
        segs = [seg for seg, _ in Image.open(io.BytesIO(cleaned)).applist]
        self.assertEqual(segs[0], "APP0")
        self.assertEqual(Image.open(io.BytesIO(cleaned)).size, size)

    def test_weaker_stamp_than_calibrated_never_leaves_a_dark_star(self):
        # If Google weakens the stamp, a full-strength removal would draw a dark
        # logo; either the guard reverts it or what is left is faint.
        size = (1376, 768)
        clean = _background("gradient", *size)
        data = _jpeg(_stamp(clean, 0.35))
        cleaned, result = wm.clean_image_bytes(data)
        if result.applied:
            after, alpha = _logo_region(cleaned, size)
            self.assertLess(wm._edge_match(after, alpha), wm.DARK_GHOST_MIN_EDGE_MATCH)
        else:
            self.assertIs(cleaned, data)
            self.assertIn(result.reason, {"not-detected", "reverted-dark-ghost", "safety-near-black"})

    def test_garbage_input_never_raises(self):
        for data in (b"", b"not an image", b"\xff\xd8\xff\xe0broken"):
            with self.subTest(data=data[:12]):
                cleaned, result = wm.clean_image_bytes(data)
                self.assertIs(cleaned, data)
                self.assertFalse(result.applied)
                self.assertTrue(result.reason.startswith("error:"))

    def test_png_input_keeps_png(self):
        size = (1376, 768)
        spec = wm.KNOWN_SPECS[size]
        buf = io.BytesIO()
        Image.fromarray(_stamp(_background("flat", *size), spec.gain).round().astype(np.uint8)).save(buf, "PNG")
        cleaned, result = wm.clean_image_bytes(buf.getvalue())
        self.assertTrue(result.applied)
        self.assertEqual(Image.open(io.BytesIO(cleaned)).format, "PNG")


if __name__ == "__main__":
    unittest.main()
