"""Omni 1080p (2026-10-11): Google's Veo 1080p upsampler accepts an Omni 1.1 clip (live:
omni_8s_portrait_1080p -> 1080x1920), so every omni* model has a `_1080p` twin."""
import unittest

from src.core.model_resolver import VIDEO_BASE_MODELS
from src.services.generation_handler import MODEL_CONFIG

UPSAMPLE = {"resolution": "VIDEO_RESOLUTION_1080P", "model_key": "veo_3_1_upsampler_1080p"}


class Omni1080pTests(unittest.TestCase):
    def test_every_omni_model_has_a_1080p_twin_that_only_adds_the_upsampler(self):
        bases = [k for k, v in MODEL_CONFIG.items() if v.get("video_type") == "omni" and not k.endswith("_1080p")]
        self.assertEqual(len(bases), 30)
        for key in bases:
            twin = MODEL_CONFIG[f"{key}_1080p"]
            self.assertEqual(twin["upsample"], UPSAMPLE, key)
            self.assertEqual({k: v for k, v in twin.items() if k != "upsample"}, MODEL_CONFIG[key], key)
            self.assertNotIn("upsample", MODEL_CONFIG[key])
        self.assertNotIn("omni_8s_portrait_1080p_1080p", MODEL_CONFIG)

    def test_gemini_aliases_reach_the_twins(self):
        self.assertEqual(VIDEO_BASE_MODELS["omni_r2v_8s_1080p"],
                         {"landscape": "omni_r2v_8s_1080p", "portrait": "omni_r2v_8s_portrait_1080p"})
        for key, pair in VIDEO_BASE_MODELS.items():
            for model in pair.values():
                self.assertIn(model, MODEL_CONFIG, key)


if __name__ == "__main__":
    unittest.main()
