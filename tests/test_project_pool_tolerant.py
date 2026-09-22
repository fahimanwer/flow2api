"""2026-09-22: ensure_project_exists keeps generating with the projects a token already has when Labs'
project.createProject fails (404 for accounts born on flow.google.com); it only raises when there are none."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.core.models import Project, Token
from src.services.token_manager import TokenManager


def _tm(projects, create_error=Exception("HTTP Error 404")):
    tm = TokenManager.__new__(TokenManager)
    tm._project_locks = {}
    tm._project_lock_guard = asyncio.Lock()
    tm.db = MagicMock()
    tm.db.get_token = AsyncMock(return_value=Token(id=93, st="st", at="at", email="n@x", current_project_id=projects[0].project_id if projects else None))
    tm.db.get_projects_by_token = AsyncMock(return_value=projects)
    tm.db.update_token = AsyncMock()
    tm._get_project_pool_size = lambda: 3
    tm._create_project_for_token = AsyncMock(side_effect=create_error)
    return tm


class ProjectPoolTolerant(unittest.TestCase):
    def test_one_existing_project_is_enough(self):
        p = Project(id=1, project_id="b157461c-fa6f-486f-ba90-5bec305f4959", token_id=93, project_name="Sep 22")
        tm = _tm([p])
        self.assertEqual(asyncio.run(tm.ensure_project_exists(93)), p.project_id)
        tm.db.update_token.assert_awaited()

    def test_no_project_at_all_still_raises(self):
        tm = _tm([])
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(tm.ensure_project_exists(93))
        self.assertIn("Failed to prepare project pool", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
