"""2026-09-22: prompt rejections / mint failures stamp token_stats.last_error_at (tokens has no such column;
the old write raised sqlite3.OperationalError and turned those requests into HTTP 500s)."""
import asyncio
import os
import tempfile
import unittest

from src.core.database import Database
from src.core.models import Token


class TouchTokenLastError(unittest.TestCase):
    def test_stamps_stats_row_without_counting_an_error(self):
        async def main():
            path = tempfile.mktemp(suffix=".db")
            db = Database(path)
            await db.init_db()
            # branch 1: a tokens row with NO token_stats row (raw insert; add_token would create one)
            async with db._connect(write=True) as c:
                cur = await c.execute("INSERT INTO tokens (st, email) VALUES ('st1', 'a@x')")
                tid = cur.lastrowid
                await c.commit()
            # no stats row yet -> inserted
            await db.touch_token_last_error(tid)
            async with db._connect() as c:
                row = await (await c.execute("SELECT last_error_at, error_count FROM token_stats WHERE token_id = ?", (tid,))).fetchone()
            self.assertIsNotNone(row[0]); self.assertIn(row[1], (0, None))
            # existing row -> updated, still not counted
            await db.touch_token_last_error(tid)
            async with db._connect() as c:
                rows = await (await c.execute("SELECT COUNT(*), MAX(error_count) FROM token_stats WHERE token_id = ?", (tid,))).fetchone()
            self.assertEqual(rows[0], 1); self.assertIn(rows[1], (0, None))
            # branch 2: a token added the normal way (add_token creates its stats row) -> update, no count
            tid2 = await db.add_token(Token(st="st2", email="b@x"))
            await db.touch_token_last_error(tid2)
            async with db._connect() as c:
                r2 = await (await c.execute("SELECT COUNT(*), MAX(last_error_at), MAX(error_count) FROM token_stats WHERE token_id = ?", (tid2,))).fetchone()
            self.assertEqual(r2[0], 1); self.assertIsNotNone(r2[1]); self.assertIn(r2[2], (0, None))
            os.remove(path)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
