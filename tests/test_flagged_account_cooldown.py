"""An account whose prompts Google refuses several times in a row is paused (2026-09-22:
one Pro account refused 16 harmless prompts in a row and Pro-first ordering kept picking it)."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.services.token_manager import TokenManager

UNSAFE = ("Generation failed: PUBLIC_ERROR_UNSAFE_GENERATION: com.google.apps.framework.request."
          "StatusException: <eye3 title='INVALID_ARGUMENT'/> generic::INVALID_ARGUMENT")


def _make_tm():
    tm = TokenManager.__new__(TokenManager)
    tm.db = MagicMock()
    tm.db.touch_token_last_error = AsyncMock()
    tm.db.reset_error_count = AsyncMock()
    tm.db.upsert_token_health_cooldown = AsyncMock()
    tm.db.delete_token_health_cooldown = AsyncMock()
    tm._health_cd = {}
    tm.clear_recaptcha_cooldown = AsyncMock()
    tm._note_verified_at = AsyncMock()
    return tm


def run(coro):
    return asyncio.run(coro)


class FlaggedAccountCooldownTests(unittest.TestCase):
    def test_six_refusals_in_a_row_pause_the_account(self):
        tm = _make_tm()
        for _ in range(5):
            run(tm.record_error(59, UNSAFE, model="gemini-3.1-flash-image-landscape"))
        self.assertFalse(tm.is_health_cooldown(59))
        run(tm.record_error(59, UNSAFE, model="gemini-3.1-flash-image-landscape"))
        self.assertTrue(tm.is_health_cooldown(59, "safety_flagged"))
        self.assertIn("safety flag", tm.health_cooldown_reason(59))
        tm.db.upsert_token_health_cooldown.assert_awaited_once()
        self.assertEqual(tm.db.upsert_token_health_cooldown.await_args.args[1], "safety_flagged")
        # never a disable strike: the account itself is not "erroring"
        self.assertFalse(hasattr(tm.db, "increment_token_stats") and tm.db.increment_token_stats.called)

    def test_a_success_in_between_resets_the_count(self):
        tm = _make_tm()
        for _ in range(5):
            run(tm.record_error(59, UNSAFE))
        run(tm.record_success(59))
        for _ in range(5):
            run(tm.record_error(59, UNSAFE))
        self.assertFalse(tm.is_health_cooldown(59))

    def test_success_clears_an_active_pause(self):
        tm = _make_tm()
        for _ in range(6):
            run(tm.record_error(59, UNSAFE))
        self.assertTrue(tm.is_health_cooldown(59))
        run(tm.record_success(59))
        self.assertFalse(tm.is_health_cooldown(59))
        tm.db.delete_token_health_cooldown.assert_awaited_with(59, "safety_flagged")

    def test_other_accounts_are_not_affected(self):
        tm = _make_tm()
        for tid in (1, 2, 3):
            for _ in range(2):
                run(tm.record_error(tid, UNSAFE))
        self.assertFalse(any(tm.is_health_cooldown(t) for t in (1, 2, 3)))


if __name__ == "__main__":
    unittest.main()
