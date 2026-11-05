"""Stage 2 step 4 (2026-11-05): accounts are created only by the worker push (cookie sessions,
docs/cookie-sync.md). The admin add-by-ST endpoint is gone, `st2at` is a read-only login probe for an
existing row key, an import never signs in for an account the server does not have, and an edit
cannot change a row's key."""
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.models import Token
from src.services.flow_client import FlowAPIError

HANDLE = "FLOWCK.abc"


async def _st_to_at(st):
    if st == "st-known":
        return {"access_token": HANDLE, "expires": None, "user": {"email": "known@x.com"}}
    raise FlowAPIError(401, "no Google cookies stored for this account", "UNAUTHENTICATED")


class AdminTokenEndpoints(unittest.TestCase):
    def setUp(self):
        self.row = Token(id=5, st="st-known", at=HANDLE, email="known@x.com")
        self.tm = MagicMock()
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=_st_to_at)
        self.tm.get_all_tokens = AsyncMock(return_value=[self.row])
        self.tm.get_token = AsyncMock(return_value=self.row)
        self.tm.update_token = AsyncMock()
        self.tm.add_token = AsyncMock()
        self.tm.disable_token = AsyncMock()
        self._patches = [patch.object(admin, "token_manager", self.tm), patch.object(admin, "concurrency_manager", None)]
        for p in self._patches:
            p.start()
        app = FastAPI()
        app.include_router(admin.router)
        app.dependency_overrides[admin.verify_admin_token] = lambda: "admin"
        self.client = TestClient(app)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_add_by_session_token_endpoint_is_gone(self):
        r = self.client.post("/api/tokens", json={"st": "st-new"})
        self.assertIn(r.status_code, (404, 405))
        self.assertFalse(hasattr(admin, "AddTokenRequest"))
        self.tm.flow_client.st_to_at.assert_not_awaited()
        self.tm.add_token.assert_not_awaited()

    def test_st2at_probes_a_known_row_key_and_refuses_an_unknown_one(self):
        ok = self.client.post("/api/tokens/st2at", json={"st": "st-known"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["access_token"].startswith("FLOWCK."))
        self.assertEqual(ok.json()["email"], "known@x.com")

        bad = self.client.post("/api/tokens/st2at", json={"st": "st-unknown"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("no Google cookies stored", bad.json()["detail"])
        self.tm.update_token.assert_not_awaited()  # a probe writes nothing

    def test_import_of_an_unknown_account_makes_no_google_call(self):
        r = self.client.post("/api/tokens/import", json={"tokens": [
            {"email": "new@x.com", "session_token": "st-new"},
            {"email": "known@x.com", "session_token": "st-known"},
        ]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["updated"], 1)
        self.assertEqual(len(body["errors"]), 1)
        self.assertIn("new@x.com is not in the database", body["errors"][0])
        self.assertIn("worker extension", body["errors"][0])
        # Only the known row signed in; the unknown item never reached Google.
        self.assertEqual(self.tm.flow_client.st_to_at.await_args_list, [call("st-known")])
        self.tm.add_token.assert_not_awaited()

    def test_edit_cannot_change_the_row_key(self):
        r = self.client.put("/api/tokens/5", json={"st": "st-other", "remark": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("row key cannot be changed", r.json()["detail"])
        self.tm.update_token.assert_not_awaited()

        same = self.client.put("/api/tokens/5", json={"st": "st-known", "remark": "x"})
        self.assertEqual(same.status_code, 200)
        kwargs = self.tm.update_token.await_args.kwargs
        self.assertEqual(kwargs["remark"], "x")
        self.assertNotIn("st", kwargs)
        self.tm.flow_client.st_to_at.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
