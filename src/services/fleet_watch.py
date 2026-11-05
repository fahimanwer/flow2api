"""Fleet watch: page Telegram when generation stops working fleet-wide (2026-10-23).

Why: on 2026-10-21 Google retired the Labs login and every account died; nothing alerted, and the
service sat at 0 successful requests for ~40 hours until the owner noticed. /health stayed 200.

What it watches, every CHECK_EVERY_SECONDS, from the database only (no Google calls):
- "down": at least MIN_REQUESTS requests in the last WINDOW_MINUTES and not one succeeded;
- "no accounts": not a single active account;
- "degraded" (2026-11-05): at least DEGRADED_MIN_REQUESTS finished requests in the last
  DEGRADED_WINDOW_MINUTES and fewer than DEGRADED_BELOW of them succeeded. Why: on 2026-11-04 the fleet
  ran at 13-56 % success for 9 hours and nothing paged, because some requests still succeeded.
  It counts as recovered only at RECOVERED_AT again, so a rate hovering near 50 % does not flap.
A problem pages once when it starts, again every REMIND_SECONDS while it lasts, and once more when
it ends. Delivery reuses the Ultra browsers Telegram outbox (`ultra_alerts`, sent by
UltraService.send_pending_alerts; rows wait there when Telegram is not configured). The same rows
wake the flow2api T3 thread through `docker/alert-wake/` on the host.
"""
import asyncio
import time
from typing import Optional, Tuple

from ..core.logger import debug_logger

CHECK_EVERY_SECONDS = 60
WINDOW_MINUTES = 15
MIN_REQUESTS = 5
REMIND_SECONDS = 2 * 3600
DEGRADED_WINDOW_MINUTES = 60
DEGRADED_MIN_REQUESTS = 30
DEGRADED_BELOW = 0.50
RECOVERED_AT = 0.70


class FleetWatch:
    def __init__(self, db):
        self.db = db
        self._problem: Optional[str] = None
        self._kind: Optional[str] = None
        self._degraded_open = False  # a degraded page is open (also through a later "down"): recovery needs RECOVERED_AT
        self._last_page_at = 0.0
        self._task: Optional[asyncio.Task] = None

    async def _numbers(self) -> Tuple[int, int, int, int, int]:
        """(finished, ok) in the last WINDOW_MINUTES, active accounts, (finished, ok) in the last DEGRADED_WINDOW_MINUTES."""
        async with self.db.connect() as conn:
            # FINISHED requests by completion time (102 = still running: a burst of fresh video jobs is
            # not an outage, and a job that started earlier but finished now is evidence).
            total, ok, hour_total, hour_ok = await (await conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN t > datetime('now', ?) THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN t > datetime('now', ?) AND status_code = 200 THEN 1 ELSE 0 END), 0), "
                "COUNT(*), COALESCE(SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END), 0) "
                "FROM (SELECT status_code, COALESCE(updated_at, created_at) AS t FROM request_logs "
                "WHERE status_code != 102 AND COALESCE(updated_at, created_at) > datetime('now', ?))",
                (f"-{WINDOW_MINUTES} minutes", f"-{WINDOW_MINUTES} minutes", f"-{DEGRADED_WINDOW_MINUTES} minutes"),
            )).fetchone()
            active = (await (await conn.execute("SELECT COUNT(*) FROM tokens WHERE is_active = 1")).fetchone())[0]
        return int(total or 0), int(ok or 0), int(active or 0), int(hour_total or 0), int(hour_ok or 0)

    @staticmethod
    def diagnose(total: int, ok: int, active: int, hour_total: int = 0, hour_ok: int = 0,
                 degraded: bool = False) -> Optional[Tuple[str, Optional[str]]]:
        """(kind, text) of the current problem, or None. `degraded` = a degraded page is open (recovery needs
        RECOVERED_AT). Text None = stay degraded without paging: too few requests this hour to judge."""
        if active == 0:
            return "fleet_down", (f"flow2api: NO active account (all disabled). {total} requests in the last {WINDOW_MINUTES} min, "
                                  f"{ok} succeeded. Check the admin page (Token Management) and the worker browsers.")
        if total >= MIN_REQUESTS and ok == 0:
            return "fleet_down", (f"flow2api: 0 of {total} requests succeeded in the last {WINDOW_MINUTES} min "
                                  f"({active} active accounts). Google may have changed Flow again; check the logs.")
        if hour_total >= DEGRADED_MIN_REQUESTS:
            rate = hour_ok / hour_total
            if rate < (RECOVERED_AT if degraded else DEGRADED_BELOW):
                return "fleet_degraded", (f"flow2api degraded: only {hour_ok} of {hour_total} finished requests succeeded "
                                          f"in the last {DEGRADED_WINDOW_MINUTES} min ({rate:.0%}; {active} active accounts). "
                                          "Check the logs for the main failure reason.")
        elif degraded:
            return "fleet_degraded", None
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
        total, ok, active, hour_total, hour_ok = await self._numbers()
        found = self.diagnose(total, ok, active, hour_total, hour_ok, degraded=self._degraded_open)
        now = time.monotonic()
        if found:
            kind, problem = found
            if problem is None:  # degraded, but too quiet to judge: keep it open, silently
                return self._problem
            if self._problem is None or kind != self._kind or now - self._last_page_at >= REMIND_SECONDS:
                still = self._problem is not None and kind == self._kind
                prefix = ("STILL DOWN: " if kind == "fleet_down" else "STILL DEGRADED: ") if still else ""
                await self._page(kind, prefix + problem)
                self._last_page_at = now
            self._problem, self._kind = problem, kind
            self._degraded_open = self._degraded_open or kind == "fleet_degraded"
        elif self._problem is not None and active > 0 and (ok if self._kind == "fleet_down" else hour_ok) > 0:
            # Recovery needs evidence (a real success); quiet traffic keeps the outage open, silently.
            if self._kind == "fleet_down":
                text = (f"flow2api recovered: {ok} of {total} finished requests OK in the last "
                        f"{WINDOW_MINUTES} min, {active} active accounts.")
            else:
                text = (f"flow2api recovered: {hour_ok} of {hour_total} finished requests OK in the last "
                        f"{DEGRADED_WINDOW_MINUTES} min, {active} active accounts.")
            await self._page("fleet_ok", text)
            self._problem = self._kind = None
            self._degraded_open = False
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
