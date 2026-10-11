"""Readable model names (2026-10-11): every id keeps working and stays primary in /v1/models;
the catalog adds a plain-words name, Google's official model, mode, orientation, duration,
resolution, accounts, inputs and readable aliases that resolve exactly to the old id."""
import asyncio
import types
import unittest

from src.api import routes
from src.core.model_resolver import (
    FRIENDLY_VIDEO_ALIASES,
    IMAGE_BASE_MODELS,
    READABLE_MODEL_ALIASES,
    VIDEO_BASE_MODELS,
    resolve_model_name,
)
from src.services.generation_handler import MODEL_CONFIG
from src.services.model_catalog import describe_model, model_catalog, readable_alias_entries


class ReadableAliasTests(unittest.TestCase):
    def test_aliases_point_at_real_ids_and_never_clash(self):
        self.assertEqual(len(READABLE_MODEL_ALIASES), 62)
        taken = set(MODEL_CONFIG) | set(IMAGE_BASE_MODELS) | set(VIDEO_BASE_MODELS) | set(FRIENDLY_VIDEO_ALIASES)
        for alias, target in READABLE_MODEL_ALIASES.items():
            self.assertIn(target, MODEL_CONFIG, alias)
            self.assertNotIn(alias, taken, alias)

    def test_examples(self):
        self.assertEqual(READABLE_MODEL_ALIASES["nano-banana-2.1-portrait-2k"], "gemini-3.1-flash-image-portrait-2k")
        self.assertEqual(READABLE_MODEL_ALIASES["nano-banana-pro-three-four-4k"], "gemini-3.0-pro-image-three-four-4k")
        self.assertEqual(READABLE_MODEL_ALIASES["omni-1.1-flash-ingredients-8s-portrait-1080p"], "omni_r2v_8s_portrait_1080p")
        self.assertEqual(READABLE_MODEL_ALIASES["omni-1.1-flash-10s-landscape"], "omni_10s")

    def test_exact_alias_is_not_re_routed_by_generation_settings(self):
        # omni_8s is also a VIDEO_BASE_MODELS family that portrait + 1080p settings would re-route.
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(imageConfig={"aspectRatio": "9:16", "imageSize": "1080p"})
        )
        self.assertEqual(resolve_model_name("omni_8s", request, MODEL_CONFIG), "omni_8s_portrait_1080p")  # settings are read
        self.assertEqual(resolve_model_name("omni-1.1-flash-8s-landscape", request, MODEL_CONFIG), "omni_8s")
        # the old ids keep their old behaviour
        self.assertEqual(resolve_model_name("omni_r2v_8s_portrait", None, MODEL_CONFIG), "omni_r2v_8s_portrait")
        self.assertEqual(resolve_model_name("gemini-3.1-flash-image-square", None, MODEL_CONFIG), "gemini-3.1-flash-image-square")


class DescribeTests(unittest.TestCase):
    def test_every_id_is_described(self):
        catalog = model_catalog()
        self.assertEqual([e["id"] for e in catalog], list(MODEL_CONFIG))
        for entry in catalog:
            self.assertTrue(entry["name"] and entry["family"] and entry["mode"] and entry["orientation"], entry["id"])
            self.assertEqual(entry["object"], "model")
            self.assertIsInstance(entry["description"], str)
            self.assertNotIn("BELUGA", entry["description"])
            self.assertNotIn("abra_", entry["description"])

    def test_expected_values(self):
        omni = describe_model("omni_r2v_8s_portrait_1080p")
        self.assertEqual(omni["name"], "Omni 1.1 Flash · Ingredients to video (reference images) · 8 s · Portrait 9:16 · 1080p (upscaled from 720p)")
        self.assertEqual(omni["google_model"], "gemini-omni-1.1-flash")
        self.assertEqual(omni["accounts"], "Pro or Ultra")
        self.assertEqual(omni["aliases"], ["omni-1.1-flash-ingredients-8s-portrait-1080p"])
        self.assertEqual(omni["inputs"]["min_images"], 1)
        self.assertTrue(omni["inputs"]["images_or_characters"])

        nb21 = describe_model("gemini-3.1-flash-image-portrait")
        self.assertEqual((nb21["family"], nb21["google_model"], nb21["type"]), ("Nano Banana 2.1", "gemini-nano-banana-2.1", "image"))
        self.assertEqual(nb21["aliases"], ["nano-banana-2.1-portrait"])

        lite = describe_model("nano-banana-2-lite-square")
        self.assertEqual((lite["family"], lite["type"], lite["resolution"]), ("Nano Banana 2 Lite", "image", "1K"))

        pro4k = describe_model("gemini-3.0-pro-image-three-four-4k")
        self.assertEqual(pro4k["orientation"], "Portrait 3:4")
        self.assertIn("4K enlarge needs Ultra", pro4k["accounts"])

        self.assertEqual(describe_model("veo_3_1_r2v_fast_portrait_ultra")["accounts"], "Ultra only")
        self.assertEqual(describe_model("veo_3_1_i2v_lite_portrait")["inputs"]["max_images"], 1)
        self.assertEqual(describe_model("veo_3_1_interpolation_lite_portrait")["inputs"]["min_images"], 2)
        self.assertEqual(describe_model("omni-flash_8s_portrait_1080p")["resolution"], "1080p (upscaled from pinned 720p)")
        self.assertEqual(describe_model("veo_3_1_t2v_lite_4s_portrait")["duration_seconds"], 4)
        extend = describe_model("veo_3_1_extend")
        self.assertIsNone(extend["duration_seconds"])
        self.assertTrue(extend["inputs"]["requires_video_id"])
        for model_id, duration in (("veo_3_1_t2v_portrait", 8), ("veo_3_1_t2v_portrait_4s", 4), ("veo_3_1_t2v_landscape_6s", 6)):
            quality = describe_model(model_id)
            self.assertEqual((quality["family"], quality["mode"], quality["duration_seconds"]),
                             ("Veo 3.1 Quality", "Text to video", duration), model_id)
        self.assertEqual(describe_model("veo_3_1_extend_portrait")["family"], "Veo 3.1 Quality")
        self.assertEqual(describe_model("veo_3_1_r2v_fast_portrait_ultra_1080p")["accounts"], "Ultra only")
        relaxed = describe_model("veo_3_1_t2v_fast_ultra_relaxed")
        self.assertTrue(relaxed["retired_by_google"])
        self.assertIn("retired by Google", relaxed["name"])


class ApiTests(unittest.TestCase):
    def test_v1_models_keeps_ids_and_adds_fields(self):
        data = asyncio.run(routes.list_models(api_key="k"))["data"]
        ids = [e["id"] for e in data]
        self.assertIn("gemini-3.0-pro-image-portrait-2k", ids)       # pinterest-factory health check
        self.assertIn("gemini-3.0-pro-image-three-four-2k", ids)
        self.assertNotIn("omni-1.1-flash-8s-portrait", ids)          # aliases are not extra entries
        entry = next(e for e in data if e["id"] == "omni_8s_portrait")
        for field in ("name", "family", "google_model", "mode", "orientation", "duration_seconds",
                      "resolution", "accounts", "inputs", "aliases", "description", "owned_by"):
            self.assertIn(field, entry)

    def test_aliases_endpoint_lists_readable_names_first(self):
        data = asyncio.run(routes.list_model_aliases(api_key="k"))["data"]
        readable = [e for e in data if e.get("alias_of")]
        self.assertEqual(len(readable), len(readable_alias_entries()))
        self.assertEqual(data[0]["id"], readable[0]["id"])
        self.assertIn("nano-banana-2.1", [e["id"] for e in data])     # short generationConfig alias still listed

    def test_gemini_list_and_get_show_display_names_and_aliases(self):
        models = asyncio.run(routes.list_gemini_models(api_key="k"))["models"]
        names = {m["name"]: m for m in models}
        self.assertTrue(names["models/omni_r2v_8s_portrait"]["displayName"].startswith("Omni 1.1 Flash · Ingredients"))
        self.assertIn("models/omni-1.1-flash-ingredients-8s-portrait", names)
        got = asyncio.run(routes.get_gemini_model("nano-banana-2.1-portrait", api_key="k"))
        self.assertEqual(got["displayName"], "Nano Banana 2.1 · Portrait 9:16 · 1K")
        self.assertIn("Alias of gemini-3.1-flash-image-portrait", got["description"])

    def test_gemini_generation_routes_resolve_readable_names(self):
        from src.core.models import GeminiGenerateContentRequest
        request = GeminiGenerateContentRequest(contents=[{"role": "user", "parts": [{"text": "a cat"}]}])
        for alias, target in (("omni-1.1-flash-ingredients-8s-portrait-1080p", "omni_r2v_8s_portrait_1080p"),
                              ("nano-banana-2.1-square-2k", "gemini-3.1-flash-image-square-2k")):
            normalized = asyncio.run(routes._normalize_gemini_request(alias, request))
            self.assertEqual(normalized.model, target)


if __name__ == "__main__":
    unittest.main()
