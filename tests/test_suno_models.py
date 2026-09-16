"""Pure-logic tests for the Suno provider: validation, models, state machine.

No network, no database. These encode the contract read from Suno's own web
bundle on 2026-09-16 (see docs/suno-provider-plan.md).
"""

import unittest

from src.core import suno_models as sm
from src.core.suno_models import SunoValidationError


class ModelResolutionTests(unittest.TestCase):
    def test_default_model_used_when_absent(self):
        public_id, mv = sm.resolve_model(None)
        self.assertEqual(public_id, sm.DEFAULT_MODEL)
        self.assertEqual(mv, "chirp-crow")

    def test_accepts_prefixed_bare_and_codename_forms(self):
        for value in ("suno/v5", "v5", "chirp-crow", "SUNO/V5"):
            with self.subTest(value=value):
                self.assertEqual(sm.resolve_model(value)[1], "chirp-crow")

    def test_unknown_model_rejected(self):
        with self.assertRaises(SunoValidationError) as ctx:
            sm.resolve_model("suno/v99")
        self.assertEqual(ctx.exception.code, "unknown_model")

    def test_catalogue_marks_exactly_one_default(self):
        models = sm.list_models()
        self.assertEqual(sum(1 for m in models if m["default"]), 1)
        self.assertTrue(all(m["id"].startswith("suno/") for m in models))

    def test_lyrics_budget_grows_with_newer_models(self):
        self.assertEqual(sm.lyrics_limit_for("chirp-v3-5"), 1250)
        self.assertEqual(sm.lyrics_limit_for("chirp-bluejay"), 3000)
        self.assertEqual(sm.lyrics_limit_for("chirp-hawk"), 5000)


class RequestValidationTests(unittest.TestCase):
    def test_description_mode_minimal_request(self):
        out = sm.normalize_generation_request({"prompt": "dreamy lo-fi for studying"})
        self.assertFalse(out["custom"])
        self.assertEqual(out["prompt"], "dreamy lo-fi for studying")
        self.assertEqual(out["mv"], "chirp-crow")

    def test_description_mode_requires_prompt(self):
        with self.assertRaises(SunoValidationError) as ctx:
            sm.normalize_generation_request({"prompt": "   "})
        self.assertEqual(ctx.exception.code, "missing_input")

    def test_description_mode_rejects_custom_only_fields(self):
        with self.assertRaises(SunoValidationError) as ctx:
            sm.normalize_generation_request({"prompt": "hi", "tags": "rock"})
        self.assertEqual(ctx.exception.code, "invalid_field")

    def test_custom_mode_accepts_lyrics_and_style(self):
        out = sm.normalize_generation_request({
            "custom": True, "prompt": "[Verse]\nhello", "tags": "indie rock",
            "title": "Hello", "model": "suno/v4",
        })
        self.assertTrue(out["custom"])
        self.assertEqual(out["mv"], "chirp-v4")
        self.assertEqual(out["title"], "Hello")

    def test_custom_mode_without_lyrics_must_be_instrumental(self):
        with self.assertRaises(SunoValidationError) as ctx:
            sm.normalize_generation_request({"custom": True, "tags": "ambient"})
        self.assertEqual(ctx.exception.code, "missing_input")

        ok = sm.normalize_generation_request({
            "custom": True, "tags": "ambient", "make_instrumental": True,
        })
        self.assertTrue(ok["make_instrumental"])

    def test_lyrics_length_is_model_specific(self):
        long_lyrics = "a" * 2000
        with self.assertRaises(SunoValidationError) as ctx:
            sm.normalize_generation_request({
                "custom": True, "prompt": long_lyrics, "tags": "pop", "model": "v3.5",
            })
        self.assertEqual(ctx.exception.code, "field_too_long")

        out = sm.normalize_generation_request({
            "custom": True, "prompt": long_lyrics, "tags": "pop", "model": "v5",
        })
        self.assertEqual(len(out["prompt"]), 2000)

    def test_title_and_tags_bounded(self):
        with self.assertRaises(SunoValidationError):
            sm.normalize_generation_request({
                "custom": True, "prompt": "x", "tags": "pop", "title": "t" * 81,
            })

    def test_unknown_fields_rejected(self):
        with self.assertRaises(SunoValidationError) as ctx:
            sm.normalize_generation_request({"prompt": "x", "wait_audio": True})
        self.assertEqual(ctx.exception.code, "unknown_field")

    def test_nul_bytes_stripped(self):
        out = sm.normalize_generation_request({"prompt": "clean\x00ish"})
        self.assertEqual(out["prompt"], "cleanish")

    def test_idempotency_key_charset_enforced(self):
        with self.assertRaises(SunoValidationError):
            sm.normalize_generation_request({"prompt": "x", "idempotency_key": "bad key!"})


class PayloadTests(unittest.TestCase):
    def test_description_payload_uses_gpt_description_prompt(self):
        request = sm.normalize_generation_request({"prompt": "sunset drive"})
        payload = sm.build_generate_payload(
            request, transaction_uuid="tx-1", captcha_token=None, captcha_version=None
        )
        self.assertEqual(payload["gpt_description_prompt"], "sunset drive")
        self.assertEqual(payload["prompt"], "")
        self.assertIsNone(payload["token"])
        self.assertIsNone(payload["token_provider"])
        self.assertEqual(payload["transaction_uuid"], "tx-1")
        self.assertEqual(payload["mv"], "chirp-crow")

    def test_custom_payload_carries_lyrics_and_style(self):
        request = sm.normalize_generation_request({
            "custom": True, "prompt": "[Verse]", "tags": "jazz", "title": "Blue",
            "negative_tags": "harsh",
        })
        payload = sm.build_generate_payload(
            request, transaction_uuid="tx-2", captcha_token="tok",
            captcha_version=sm.CAPTCHA_VERSION_HCAPTCHA,
        )
        self.assertEqual(payload["prompt"], "[Verse]")
        self.assertEqual(payload["tags"], "jazz")
        self.assertEqual(payload["title"], "Blue")
        self.assertEqual(payload["negative_tags"], "harsh")
        self.assertEqual(payload["token"], "tok")
        self.assertEqual(payload["token_provider"], sm.CAPTCHA_VERSION_HCAPTCHA)

    def test_token_provider_omitted_without_token(self):
        request = sm.normalize_generation_request({"prompt": "x"})
        payload = sm.build_generate_payload(
            request, transaction_uuid="tx", captcha_token=None,
            captcha_version=sm.CAPTCHA_VERSION_HCAPTCHA,
        )
        self.assertIsNone(payload["token_provider"])


class StateMachineTests(unittest.TestCase):
    def test_blocked_captcha_does_not_hold_a_slot(self):
        # Nothing was submitted, so the account must stay free. Two blocked
        # jobs would otherwise strand an account with two slots forever.
        self.assertNotIn(sm.BLOCKED_CAPTCHA, sm.ACTIVE_STATES)
        self.assertIn(sm.NEEDS_REVIEW, sm.ACTIVE_STATES)

    def test_blocked_captcha_is_recoverable_and_cancellable(self):
        self.assertTrue(sm.can_transition(sm.BLOCKED_CAPTCHA, sm.QUEUED))
        self.assertTrue(sm.can_transition(sm.BLOCKED_CAPTCHA, sm.CANCELLED))

    def test_submitting_cannot_be_cancelled(self):
        self.assertFalse(sm.can_transition(sm.SUBMITTING, sm.CANCELLED))

    def test_terminal_states_are_final(self):
        for state in sm.TERMINAL_STATES:
            self.assertEqual(sm.STATE_TRANSITIONS[state], frozenset())

    def test_every_state_has_a_transition_entry(self):
        self.assertEqual(set(sm.ALL_STATES), set(sm.STATE_TRANSITIONS))


class ClipEvaluationTests(unittest.TestCase):
    """One generation is an A/B pair; the two clips finish independently."""

    def test_streaming_is_not_terminal(self):
        clips = {"a": {"id": "a", "status": "streaming"}, "b": {"id": "b", "status": "submitted"}}
        verdict = sm.evaluate_clips(clips, ["a", "b"])
        self.assertFalse(verdict["all_terminal"])
        self.assertIsNone(verdict["settle"])
        # ...but it is already playable, which callers may use for early preview.
        self.assertEqual(verdict["playable_ids"], ["a"])

    def test_job_waits_for_the_sibling_clip(self):
        clips = {"a": {"id": "a", "status": "complete"}, "b": {"id": "b", "status": "queued"}}
        verdict = sm.evaluate_clips(clips, ["a", "b"])
        self.assertFalse(verdict["all_terminal"])
        self.assertEqual(verdict["pending_ids"], ["b"])

    def test_partial_success_when_one_clip_errors(self):
        clips = {"a": {"id": "a", "status": "complete"}, "b": {"id": "b", "status": "error"}}
        verdict = sm.evaluate_clips(clips, ["a", "b"])
        self.assertTrue(verdict["all_terminal"])
        self.assertEqual(verdict["settle"], sm.SUCCEEDED)
        self.assertEqual(verdict["complete_ids"], ["a"])
        self.assertEqual(verdict["failed_ids"], ["b"])

    def test_all_errors_fail_the_job(self):
        clips = {"a": {"id": "a", "status": "error"}, "b": {"id": "b", "status": "error"}}
        verdict = sm.evaluate_clips(clips, ["a", "b"])
        self.assertEqual(verdict["settle"], sm.FAILED)

    def test_unobserved_clip_is_pending_not_failed(self):
        verdict = sm.evaluate_clips({"a": {"id": "a", "status": "complete"}}, ["a", "b"])
        self.assertFalse(verdict["all_terminal"])
        self.assertEqual(verdict["pending_ids"], ["b"])


class ClipProjectionTests(unittest.TestCase):
    def test_summary_never_leaks_upstream_urls(self):
        clip = {
            "id": "c1", "title": "Song", "status": "complete",
            "audio_url": "https://studio-api.prod.suno.com/api/forbidden",
            "media_urls": [{"url": "https://cdn/enc", "encrypted": True}],
            "metadata": {"duration": 120, "tags": "pop", "prompt": "[Verse]"},
        }
        summary = sm.summarize_clip(clip)
        self.assertEqual(summary["clip_id"], "c1")
        self.assertEqual(summary["duration"], 120)
        serialized = repr(summary)
        self.assertNotIn("forbidden", serialized)
        self.assertNotIn("cdn/enc", serialized)

    def test_audio_format_validation(self):
        self.assertEqual(sm.validate_audio_format(None), "mp3")
        self.assertEqual(sm.validate_audio_format("m4a"), "m4a")
        with self.assertRaises(SunoValidationError):
            sm.validate_audio_format("wav")

    def test_clip_id_validation(self):
        self.assertEqual(sm.validate_clip_id("abc-123"), "abc-123")
        for bad in ("../etc", "a/b", "", None, "x" * 200):
            with self.subTest(bad=bad), self.assertRaises(SunoValidationError):
                sm.validate_clip_id(bad)


if __name__ == "__main__":
    unittest.main()
