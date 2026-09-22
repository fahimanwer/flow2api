"""2026-09-22: registering a NEW account after Flow moved to flow.google.com. Labs' project.createProject
answers 404 for a fresh account, so (a) a project id reported by the worker is used as-is, (b) the pooled
extra projects never fail the registration, (c) without a reported project the error tells staff what to do."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.services.token_manager import TokenManager


def _tm(create_project_error=Exception("HTTP Error 404")):
    tm = TokenManager.__new__(TokenManager)
    tm.db = MagicMock()
    tm.db.get_token_by_st = AsyncMock(return_value=None)
    tm.db.add_token = AsyncMock(return_value=7)
    tm.db.add_project = AsyncMock(return_value=1)
    tm.flow_client = MagicMock()
    tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "at", "expires": None, "user": {"email": "n@x", "name": "N"}})
    tm.flow_client.get_credits = AsyncMock(return_value={"credits": 24195, "userPaygateTier": "PAYGATE_TIER_TWO"})
    tm.flow_client.create_project = AsyncMock(side_effect=create_project_error)
    tm._get_project_pool_size = lambda: 3
    tm._create_project_for_token = AsyncMock(side_effect=create_project_error)
    return tm


class AddTokenNewFlow(unittest.TestCase):
    def test_reported_project_is_used_and_pool_failure_does_not_abort(self):
        tm = _tm()
        tok = asyncio.run(tm.add_token(st="st", project_id="b157461c-fa6f-486f-ba90-5bec305f4959", project_name="Sep 22 - 13:55"))
        self.assertEqual(tok.id, 7)
        self.assertEqual(tok.current_project_id, "b157461c-fa6f-486f-ba90-5bec305f4959")
        self.assertEqual(tok.user_paygate_tier, "PAYGATE_TIER_TWO")
        tm.flow_client.create_project.assert_not_called()
        tm.db.add_project.assert_awaited_once()

    def test_without_reported_project_the_error_says_what_to_do(self):
        tm = _tm()
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(tm.add_token(st="st"))
        self.assertIn("flow.google.com", str(ctx.exception))
        self.assertIn("Reconnect", str(ctx.exception))
        tm.db.add_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
