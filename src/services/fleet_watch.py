"""Fleet watch: page Telegram when generation stops working fleet-wide (2026-10-23).

Why: on 2026-10-21 Google retired the Labs login and every account died; nothing alerted, and the
service sat at 0 successful requests for ~40 hours until the owner noticed. /health stayed 200.

What it watches, every CHECK_EVERY_SECONDS, from the database only (no Google calls):
- "down": at least MIN_REQUESTS requests in the last WINDOW_MINUTES and not one succeeded;
- "no accounts": not a single active account.
A problem pages once when it starts, again every REMIND_SECONDS while it lasts, and once more when
it ends. Delivery reuses the Ultra browsers Telegram outbox (`ultra_alerts`, sent by
UltraService.send_pending_alerts; rows wait there when Telegram is not configured).
"""
import asyncio
import time
from typing import Optional, Tuple

from ..core.logger import debug_logger

CHECK_EVERY_SECONDS = 60
WINDOW_MINUTES = 15
MIN_REQUESTS = 5
REMIND_SECONDS = 2 * 3600


class FleetWatch:
    def __init__(self, db):
        self.db = db
        self._problem: Optional[str] = None
        self._last_page_at = 0.0
        self._task: Optional[asyncio.Task] = None

    async def _numbers(self) -> Tuple[int, int, int]:
        async with self.db.connect() as conn:
            # FINISHED requests by completion time (102 = still running: a burst of fresh video jobs is
            # not an outage, and a job that started earlier but finished now is evidence).
            total, ok = await (await conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END), 0) "
                "FROM request_logs WHERE status_code != 102 AND COALESCE(updated_at, created_at) > datetime('now', ?)",
                (f"-{WINDOW_MINUTES} minutes",),
            )).fetchone()
            active = (await (await conn.execute("SELECT COUNT(*) FROM tokens WHERE is_active = 1")).fetchone())[0]
        return int(total or 0), int(ok or 0), int(active or 0)

    @staticmethod
    def diagnose(total: int, ok: int, active: int) -> Optional[str]:
        if active == 0:
            return (f"flow2api: NO active account (all disabled). {total} requests in the last {WINDOW_MINUTES} min, "
                    f"{ok} succeeded. Check the admin page (Token Management) and the worker browsers.")
        if total >= MIN_REQUESTS and ok == 0:
            return (f"flow2api: 0 of {total} requests succeeded in the last {WINDOW_MINUTES} min "
                    f"({active} active accounts). Google may have changed Flow again; check the logs.")
        return None

    async def _page(self, kind: str, text: str) -> None:
        # Same clock and format as the outbox (ultra_browsers._utcnow / _iso), or the sender never sees the row.
        from .ultra_browsers import _iso, _utcnow
        now = _iso(_utcnow())
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "INSERT INTO ultra_alerts (browser, kind, text, created_at, next_try_at) VALUES (NULL, ?, ?, ?, ?)",
                (kind, text[:900], now, now),
            )
            await conn.commit()
        debug_logger.op_warning(f"[FLEET_WATCH] {kind}: {text}")

    async def check_once(self) -> Optional[str]:
        total, ok, active = await self._numbers()
        problem = self.diagnose(total, ok, active)
        now = time.monotonic()
        if problem:
            if self._problem is None or now - self._last_page_at >= REMIND_SECONDS:
                await self._page("fleet_down", ("STILL DOWN: " if self._problem else "") + problem)
                self._last_page_at = now
            self._problem = problem
        elif self._problem is not None and ok > 0 and active > 0:
            # Recovery needs evidence (a real success); quiet traffic keeps the outage open, silently.
            await self._page("fleet_ok", f"flow2api recovered: {ok} of {total} finished requests OK in the last "
                                         f"{WINDOW_MINUTES} min, {active} active accounts.")
            self._problem = None
        return self._problem

    def start(self) -> None:
        async def _loop():
            await asyncio.sleep(CHECK_EVERY_SECONDS)  # let workers reconnect after a deploy first
            while True:
                try:
                    await self.check_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug_logger.op_warning(f"[FLEET_WATCH] check failed: {type(e).__name__}: {e}")
                await asyncio.sleep(CHECK_EVERY_SECONDS)

        self._task = asyncio.create_task(_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
