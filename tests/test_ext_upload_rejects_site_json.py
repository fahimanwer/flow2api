"""2026-09-24: the published worker package must never contain site.json (box-only proxy credentials)."""
import io
import json
import unittest
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api import ext_update


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, json.dumps({"version": "9.9.9"}) if n.endswith("manifest.json") else "x")
    return buf.getvalue()


class ExtUploadRejectsSiteJson(unittest.TestCase):
    def setUp(self):
        async def ok(request, auth): return None
        self._saved = ext_update._verify_admin_token
        ext_update._verify_admin_token = ok
        app = FastAPI(); app.include_router(ext_update.router)
        self.client = TestClient(app)

    def tearDown(self):
        ext_update._verify_admin_token = self._saved

    def test_site_json_anywhere_is_refused(self):
        for names in (["manifest.json", "site.json"], ["ext/manifest.json", "ext/site.json"]):
            r = self.client.post("/api/ext/upload", content=_zip(names))
            self.assertEqual(r.status_code, 400, names)
            self.assertIn("site.json", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
