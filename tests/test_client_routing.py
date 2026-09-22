"""Per-caller account routing (2026-09-22): X-Flow-Client identity, client_policies tier rules,
tokens.reserved_client exclusivity/preference, and the machine-readable 503 (client_policy_no_account)."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.client_policy import (
    DEFAULT_CLIENT,
    NO_ACCOUNT_CODE,
    ClientPolicyStore,
    client_block_reason,
    no_account_error,
    normalize_client,
    resolve_client,
)
from src.core import client_policy as cp
from src.core.config import config
from src.core.models import Token
from src.services.load_balancer import LoadBalancer


def _token(tid, tier, reserved="", image_enabled=True, video_enabled=True):
    return Token(id=tid, st=f"st{tid}", at=f"at{tid}", email=f"t{tid}@x", user_paygate_tier=tier,
                 reserved_client=reserved, image_enabled=image_enabled, video_enabled=video_enabled)


FREE, PRO, ULT = "PAYGATE_TIER_NOT_PAID", "PAYGATE_TIER_ONE", "PAYGATE_TIER_TWO"


def _store(**policies):
    s = ClientPolicyStore()
    s.replace([{"client": c, "image_tier": v[0], "video_tier": v[1]} for c, v in policies.items()])
    return s


class FakeTokenManager:
    def __init__(self, tokens):
        self.tokens = tokens
        self.db = None
    async def _ensure_quota_loaded(self): pass
    async def get_active_tokens(self): return list(self.tokens)
    def is_recaptcha_cooldown(self, tid): return False
    def is_health_cooldown(self, tid, *a): return False
    def health_cooldown_reason(self, tid): return ""
    def is_model_quota_exhausted(self, tid, model): return False
    def needs_at_refresh(self, token): return False
    async def ensure_valid_token(self, token): return token


def _lb(tokens):
    return LoadBalancer(FakeTokenManager(tokens), concurrency_manager=None)


def run(coro):
    return asyncio.run(coro)


class Identity(unittest.TestCase):
    def test_header_precedence_and_normalisation(self):
        self.assertEqual(resolve_client({"x-flow-client": "Pinterest-Factory", "x-client": "other"}), "pinterest-factory")
        self.assertEqual(resolve_client({"x-client": "pinterest-factory"}), "pinterest-factory")
        self.assertEqual(resolve_client({}), "")
        self.assertEqual(resolve_client({"x-client": "bad name!"}), "")
        self.assertEqual(normalize_client("a" * 60), "a" * 40)
        self.assertEqual(normalize_client("  ai.reels_v2-x "), "ai.reels_v2-x")


class Policies(unittest.TestCase):
    def setUp(self):
        self.store = _store(**{"default": ("any", "any"), "pinterest-factory": ("ultra", "ultra"), "vid-off": ("any", "off")})

    def test_unknown_client_falls_back_to_default(self):
        self.assertEqual(self.store.get("nobody").client, DEFAULT_CLIENT)
        self.assertEqual(self.store.get("").client, DEFAULT_CLIENT)
        self.assertIsNone(client_block_reason(_token(1, FREE), "", "image", self.store))
        self.assertIsNone(client_block_reason(_token(1, FREE), "nobody", "video", self.store))

    def test_ultra_rule(self):
        for tier in (FREE, PRO):
            self.assertIn("needs a Ult account", client_block_reason(_token(1, tier), "pinterest-factory", "image", self.store))
        self.assertIsNone(client_block_reason(_token(1, ULT), "pinterest-factory", "image", self.store))

    def test_paid_rule_and_off(self):
        store = _store(**{"default": ("any", "any"), "cf": ("paid", "off")})
        self.assertIsNotNone(client_block_reason(_token(1, FREE), "cf", "image", store))
        self.assertIsNone(client_block_reason(_token(1, PRO), "cf", "image", store))
        self.assertIsNone(client_block_reason(_token(1, ULT), "cf", "image", store))
        self.assertIn("is off", client_block_reason(_token(1, ULT), "cf", "video", store))

    def test_reserved_token_is_exclusive(self):
        t = _token(1, ULT, reserved="pinterest-factory")
        self.assertIsNone(client_block_reason(t, "pinterest-factory", "image", self.store))
        self.assertEqual(client_block_reason(t, "", "image", self.store), "reserved for pinterest-factory")
        self.assertEqual(client_block_reason(t, "content-factory", "image", self.store), "reserved for pinterest-factory")

    def test_no_account_error_fields(self):
        e = no_account_error("pinterest-factory", "image", self.store)
        self.assertEqual(e, {"code": NO_ACCOUNT_CODE, "client": "pinterest-factory", "media": "image", "need": "ultra"})
        self.assertEqual(no_account_error("", "video", self.store)["client"], DEFAULT_CLIENT)
        self.assertEqual(no_account_error("vid-off", "video", self.store)["need"], "off")


class Selection(unittest.TestCase):
    """LoadBalancer.select_token with the real filter + ordering code and a fake token manager."""

    def setUp(self):
        self._saved_store = cp.client_policy_store._policies
        cp.client_policy_store.replace([
            {"client": "default", "image_tier": "any", "video_tier": "any"},
            {"client": "pinterest-factory", "image_tier": "ultra", "video_tier": "ultra"},
        ])
        self._saved_captcha, self._saved_mode = config.captcha_method, config.call_logic_mode
        config.set_captcha_method("yescaptcha")  # no extension route checks
        config.set_call_logic_mode("default")

    def tearDown(self):
        cp.client_policy_store._policies = self._saved_store
        config.set_captcha_method(self._saved_captcha)
        config.set_call_logic_mode(self._saved_mode)

    def _pick(self, lb, client, n=1, **kw):
        out = []
        for _ in range(n):
            t = run(lb.select_token(for_image_generation=True, model="gemini-3.0-pro-image-portrait-2k",
                                    reserve=False, enforce_concurrency_filter=False, client=client, **kw))
            out.append(t.id if t else None)
        return out

    def test_unidentified_caller_unchanged_when_nothing_is_reserved(self):
        lb = _lb([_token(1, FREE), _token(2, PRO), _token(3, ULT)])
        self.assertTrue(set(self._pick(lb, "", n=12)) <= {1, 2, 3})
        self.assertNotIn(None, self._pick(lb, "", n=12))

    def test_pinterest_only_gets_ultra(self):
        lb = _lb([_token(1, FREE), _token(2, PRO), _token(3, ULT)])
        self.assertEqual(set(self._pick(lb, "pinterest-factory", n=10)), {3})

    def test_pinterest_fails_fast_without_ultra(self):
        lb = _lb([_token(1, FREE), _token(2, PRO)])
        self.assertEqual(self._pick(lb, "pinterest-factory"), [None])
        detail = run(lb.get_unavailable_detail(for_image_generation=True, model="gemini-3.0-pro-image-portrait-2k",
                                               client="pinterest-factory"))
        self.assertEqual(detail["extra"]["code"], NO_ACCOUNT_CODE)
        self.assertEqual(detail["extra"]["need"], "ultra")
        self.assertIn("No ultra account is available for client pinterest-factory", detail["message"])
        # the same pool still serves an unidentified caller, with no policy extra
        self.assertNotIn(None, self._pick(lb, ""))
        self.assertIsNone(run(lb.get_unavailable_detail(for_image_generation=True, client="")))

    def test_reserved_token_hidden_from_others_and_preferred_by_owner(self):
        mine = _token(9, ULT, reserved="pinterest-factory")
        lb = _lb([_token(1, FREE), _token(3, ULT), mine, _token(4, ULT)])
        self.assertNotIn(9, self._pick(lb, "", n=15))
        self.assertNotIn(9, self._pick(lb, "content-factory", n=15))
        self.assertEqual(set(self._pick(lb, "pinterest-factory", n=10)), {9})

    def test_reserved_preferred_in_polling_mode_too(self):
        config.set_call_logic_mode("polling")
        mine = _token(9, ULT, reserved="pinterest-factory")
        lb = _lb([_token(3, ULT), mine, _token(4, ULT)])
        self.assertEqual(set(self._pick(lb, "pinterest-factory", n=6)), {9})
        # shared Ultras are what other callers rotate over; 9 never appears
        self.assertEqual(set(self._pick(lb, "", n=6)), {3, 4})

    def test_owner_spills_to_shared_ultra_when_reserved_is_unavailable(self):
        mine = _token(9, ULT, reserved="pinterest-factory", image_enabled=False)
        lb = _lb([_token(1, PRO), mine, _token(4, ULT)])
        self.assertEqual(set(self._pick(lb, "pinterest-factory", n=5)), {4})

    def test_video_rule_and_disabled_video(self):
        lb = _lb([_token(1, PRO), _token(3, ULT, video_enabled=False)])
        t = run(lb.select_token(for_video_generation=True, model=None, enforce_concurrency_filter=False, client="pinterest-factory"))
        self.assertIsNone(t)
        detail = run(lb.get_unavailable_detail(for_video_generation=True, client="pinterest-factory"))
        self.assertIsNone(detail["extra"])  # the Ultra exists but has video off: not a policy failure


class ErrorBody(unittest.TestCase):
    def test_error_response_keeps_shape_and_adds_code(self):
        from src.services.generation_handler import GenerationHandler
        body = json.loads(GenerationHandler._create_error_response(SimpleNamespace(), "boom", status_code=503,
                                                                    extra=no_account_error("pinterest-factory", "image", _store(**{"pinterest-factory": ("ultra", "any")}))))
        err = body["error"]
        self.assertEqual((err["message"], err["type"], err["status_code"]), ("boom", "server_error", 503))
        self.assertEqual(err["code"], NO_ACCOUNT_CODE)
        self.assertEqual((err["client"], err["need"], err["media"]), ("pinterest-factory", "ultra", "image"))
        plain = json.loads(GenerationHandler._create_error_response(SimpleNamespace(), "x", status_code=503))
        self.assertEqual(plain["error"]["code"], "generation_failed")


if __name__ == "__main__":
    unittest.main()
