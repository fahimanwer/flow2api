"""Ultra browsers managed from the dashboard (tmp/ultra_browsers_plan.md, Slices A + B).

Each Ultra account runs in its own always-on Chrome container on the browser host (cf-worker-01). A small
host agent (docker/flow-ultra/agent/ultra_agent.py) polls flow2api for work over HTTPS; flow2api never calls
into the host. This module is the flow2api side: the records (ports, browsers, jobs, sign-in attempts, alert
outbox), the ownership rules a session push must pass, the onboarding hold and its release, the drain before
a restart/stop/update, the derived health state and the Telegram sender.

Slice A (records, status, observe-only browsers, screenshots, alerts) is always on. Slice B (adding an
account, assisted sign-in, start/stop/restart/update) is OFF unless ULTRA_BROWSERS_ENABLED=1. Nothing here
logs or stores a password, a challenge code or a connection token (codes are sealed at rest and blanked on
delivery).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..core import vault
from ..core.account_tiers import normalize_user_paygate_tier
from ..core.database import Database, proxy_port, with_proxy_port
from ..core.logger import debug_logger

ULTRA_TIER = "PAYGATE_TIER_TWO"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Timings (seconds). The settle wait covers the gap between a pick and its request_logs row (R3-5).
DRAIN_SETTLE_S = 20
DRAIN_MAX_S = 120
HOST_SILENT_S = 600
VERIFIED_FRESH_S = 600
LOGOUT_CONFIRM_S = 600
AUTH_BAD_S = 1800
LEASE_S = {"login": 180, "create": 300, "update": 300, "bootstrap": 120, "start": 150, "restart": 150,
           "stop": 90, "status": 90, "screenshot": 60}
CODE_TTL_S = 600
NUMBER_TTL_S = 300
ALERT_MAX_TRIES = 20
SCREENSHOT_TTL_S = 300

# Jobs the agent knows. Read-only ones may run beside a mutating job (a screenshot during a sign-in).
READ_ONLY_JOBS = ("status", "screenshot")
MUTATING_JOBS = ("create", "bootstrap", "login", "start", "stop", "restart", "update")
DRAINED_JOBS = ("stop", "restart", "update")   # take the account out of serving first
FINAL_JOB_STATES = ("done", "failed", "uncertain", "deferred", "cancelled")

# Browser states. Lifecycle states are set by jobs and people; health states are derived from observations.
LIFECYCLE_STATES = ("creating", "bootstrapping", "signing_in", "needs_you", "onboarding", "stopped",
                    "starting", "error", "observing")
HEALTH_STATES = ("ok", "degraded", "logged_out", "labs_dead")
ALERT_STATES = ("logged_out", "needs_you", "error")
AUTH_DISABLE_REASONS = ("auto_st_expired", "auto_at_stale")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def slice_b_enabled() -> bool:
    return _env_flag("ULTRA_BROWSERS_ENABLED")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


class UltraError(Exception):
    """A refused request: `status` is the HTTP status, the message is safe to show."""

    def __init__(self, message: str, status: int = 409, blockers: Optional[List[str]] = None):
        super().__init__(message)
        self.status = status
        self.blockers = blockers or []


# Result keys an agent may never persist (defence in depth: the agent does not send them).
_SECRET_KEYS = re.compile(r"pass(word)?|secret|token|code|cookie|png|screenshot", re.I)


def _scrub(result: Any) -> Any:
    if isinstance(result, dict):
        return {k: ("[removed]" if _SECRET_KEYS.search(str(k)) else _scrub(v)) for k, v in result.items()}
    if isinstance(result, list):
        return [_scrub(v) for v in result[:50]]
    if isinstance(result, str):
        return result[:500]
    return result


def derive_health(browser: Dict[str, Any], token: Optional[Dict[str, Any]], obs: Dict[str, Any],
                  ext_connected: Optional[bool], now: datetime) -> Tuple[str, str, Dict[str, Optional[str]]]:
    """Health of a live browser from separate observations (Codex round 1 #4). Returns
    (state, detail, marks) where marks are the updated cookies_absent_since / auth_bad_since.
    Network, CDP or proxy trouble is 'degraded' (unknown), never 'logged_out'; cookies alone never make 'ok'."""
    marks = {"cookies_absent_since": browser.get("cookies_absent_since"), "auth_bad_since": browser.get("auth_bad_since")}
    if obs.get("container_up") is False:
        return "degraded", "container is not running", marks
    expected_ip = (browser.get("expected_egress_ip") or "").strip()
    egress = (obs.get("egress_ip") or "").strip()
    egress_ok = bool(egress) and (not expected_ip or egress == expected_ip)
    cookies = obs.get("google_cookies")  # True / False / None (unknown)
    cookies_at = _parse(obs.get("cookies_at"))

    # Google cookies absent on two reads >= 10 min apart, egress healthy → logged out.
    if cookies is False and egress_ok and cookies_at:
        first = _parse(marks["cookies_absent_since"])
        if first is None:
            marks["cookies_absent_since"] = _iso(cookies_at)
        elif (cookies_at - first).total_seconds() >= LOGOUT_CONFIRM_S:
            return "logged_out", "Google cookies gone on two reads 10 min apart", marks
    elif cookies is True:
        marks["cookies_absent_since"] = None

    auth_bad = bool(token) and not token.get("is_active") and (token.get("ban_reason") or "") in AUTH_DISABLE_REASONS
    if auth_bad:
        since = _parse(marks["auth_bad_since"])
        if since is None:
            marks["auth_bad_since"] = _iso(now)
        elif (now - since).total_seconds() >= AUTH_BAD_S:
            if cookies is True:
                return "labs_dead", "Google signed in, Labs credential dead (cookie healer owns it)", marks
            if cookies is False and ext_connected:
                return "logged_out", "credential dead and Google cookies gone", marks
    else:
        marks["auth_bad_since"] = None

    if not egress_ok:
        return "degraded", ("egress IP unknown" if not egress else f"egress {egress} is not the expected {expected_ip}"), marks
    if not token:
        return "degraded", "no account bound yet", marks
    if ext_connected is False:
        return "degraded", "extension not connected", marks
    if not token.get("is_active"):
        return "degraded", f"account disabled ({token.get('ban_reason') or 'manual'})", marks
    if cookies is None:
        return "degraded", "Google sign-in not checked yet", marks
    if cookies is False:
        return "degraded", "Google cookies missing (confirming)", marks
    return "ok", "", marks


class UltraService:
    def __init__(self, db: Database, *, now: Callable[[], datetime] = _utcnow,
                 ext_connected: Optional[Callable[[str], Optional[bool]]] = None,
                 published_version: Optional[Callable[[], Optional[str]]] = None,
                 vault_key: Optional[str] = None):
        self.db = db
        self.now = now
        self._ext_connected = ext_connected
        self._published_version = published_version
        self._vault_key = vault_key  # None = read ULTRA_VAULT_KEY
        self._screenshots: Dict[str, Tuple[float, bytes]] = {}
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------ config

    @property
    def host_id(self) -> str:
        return os.environ.get("ULTRA_HOST_ID", "cf-worker-01").strip() or "cf-worker-01"

    def agent_token_ok(self, authorization: Optional[str]) -> bool:
        expected = os.environ.get("ULTRA_AGENT_TOKEN", "").strip()
        if not expected:
            raise UltraError("ULTRA_AGENT_TOKEN is not configured on flow2api", 503)
        provided = (authorization or "")[7:] if (authorization or "").startswith("Bearer ") else (authorization or "")
        return hmac.compare_digest(provided.strip().encode(), expected.encode())

    def vault_ok(self) -> bool:
        return vault.available(self._vault_key)

    def _require_b(self):
        if not slice_b_enabled():
            raise UltraError("Ultra browser management is switched off (set ULTRA_BROWSERS_ENABLED=1)", 403)
        if not self.vault_ok():
            raise UltraError("ULTRA_VAULT_KEY is missing or invalid: passwords cannot be stored", 503)

    # ------------------------------------------------------------------ ports (A2, R3-2)

    async def _pool(self, conn) -> Dict[str, Any]:
        row = await (await conn.execute("SELECT ext_proxy_pool FROM plugin_config WHERE id = 1")).fetchone()
        try:
            data = json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def _port_blockers(self, conn, port: int, allow_token: Optional[int] = None) -> List[str]:
        """Why `port` cannot become an Ultra port: any device assignment, any port move from/to it, any account
        (active or not — a disabled one can come back) redeeming or logging in through it."""
        out = []
        for (rk,) in await (await conn.execute(
                "SELECT route_key FROM device_port_assignments WHERE port = ?", (port,))).fetchall():
            owner = await (await conn.execute("SELECT token_id FROM device_identity WHERE route_key = ?", (rk,))).fetchone()
            if allow_token is not None and owner and int(owner[0]) == int(allow_token):
                continue
            out.append(f"device {rk[:24]} is assigned this port")
        for rk, tid, fp, tp in await (await conn.execute(
                "SELECT route_key, token_id, from_port, to_port FROM port_migrations WHERE from_port = ? OR to_port = ?",
                (port, port))).fetchall():
            if allow_token is not None and int(tid) == int(allow_token):
                continue
            out.append(f"port move of token {tid} ({fp}→{tp})")
        for tid, email, redeem, login_proxy in await (await conn.execute(
                "SELECT id, email, redeem_proxy_url, proxy_url FROM tokens")).fetchall():
            if allow_token is not None and int(tid) == int(allow_token):
                continue
            if proxy_port(redeem) == port or proxy_port(login_proxy) == port:
                out.append(f"token {tid} ({email}) uses this port")
        if await (await conn.execute("SELECT 1 FROM ultra_ports WHERE port = ?", (port,))).fetchone():
            out.append("already an Ultra port")
        return out

    async def check_port(self, port: int) -> Dict[str, Any]:
        async with self.db.connect() as conn:
            blockers = await self._port_blockers(conn, int(port))
            in_pool = int(port) in [int(p) for p in (await self._pool(conn)).get("ports") or []]
        return {"port": int(port), "in_pool": in_pool, "blockers": blockers}

    async def reserve_port(self, port: int, *, proxy_host: str = "", expected_egress_ip: str = "",
                           city: str = "", tz: str = "", note: str = "") -> Dict[str, Any]:
        """Put `port` on the Ultra list and take it out of the shared pool, in ONE transaction. The pool row is
        rewritten in place (connection token and auto-enable untouched)."""
        port = int(port)
        if not 1 <= port <= 65535:
            raise UltraError("port out of range", 400)
        now = _iso(self.now())
        async with self.db.connect(write=True) as conn:
            blockers = await self._port_blockers(conn, port)
            if blockers:
                raise UltraError(f"port {port} is in use", 409, blockers)
            pool = await self._pool(conn)
            host = (proxy_host or pool.get("host") or "").strip()
            ports = [int(p) for p in pool.get("ports") or []]
            removed = port in ports
            if removed:
                pool["ports"] = [p for p in ports if p != port]
                await conn.execute("UPDATE plugin_config SET ext_proxy_pool = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                                   (json.dumps(pool),))
            await conn.execute(
                "INSERT INTO ultra_ports (port, proxy_host, expected_egress_ip, city, timezone, state, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'free', ?, ?, ?)",
                (port, host, (expected_egress_ip or "").strip() or None, (city or "").strip() or None,
                 (tz or "").strip() or None, (note or "").strip()[:200] or None, now, now),
            )
            await conn.commit()
        debug_logger.op_warning(f"[ULTRA] port {port} kept for Ultra browsers (removed from pool={removed})")
        return {"port": port, "removed_from_pool": removed, "proxy_host": host}

    # ------------------------------------------------------------------ browsers

    async def _next_name(self, conn) -> str:
        names = {r[0] for r in await (await conn.execute("SELECT name FROM ultra_browsers")).fetchall()}
        n = 3  # 01 and 02 were made by hand (Slice D adopts them under their own names)
        while f"flow-ultra-{n:02d}" in names:
            n += 1
        return f"flow-ultra-{n:02d}"

    async def register_observed(self, *, name: str, container: str, port: int, token_id: Optional[int],
                                proxy_host: str = "", expected_egress_ip: str = "", city: str = "", tz: str = "",
                                route_key: str = "") -> Dict[str, Any]:
        """Slice A: record a hand-made browser (flow-ultra-01/02) as observe_only. No ownership guard, no hold,
        no updater, no lifecycle job — status and screenshots only. Its port goes on the Ultra list; the only
        user allowed on that port is the browser's own account."""
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise UltraError("name must be lowercase letters, digits and dashes", 400)
        port = int(port)
        now = _iso(self.now())
        async with self.db.connect(write=True) as conn:
            email = ""
            if token_id is not None:
                row = await (await conn.execute("SELECT email FROM tokens WHERE id = ?", (int(token_id),))).fetchone()
                if not row:
                    raise UltraError(f"token {token_id} not found", 404)
                email = row[0] or ""
            pool = await self._pool(conn)
            if port in [int(p) for p in pool.get("ports") or []]:
                raise UltraError(f"port {port} is in the shared pool; take it out first", 409)
            existing = await (await conn.execute("SELECT state, browser_name FROM ultra_ports WHERE port = ?", (port,))).fetchone()
            if existing and existing[1]:
                raise UltraError(f"port {port} already belongs to {existing[1]}", 409)
            if not existing:
                blockers = [b for b in await self._port_blockers(conn, port, allow_token=token_id) if b != "already an Ultra port"]
                if blockers:
                    raise UltraError(f"port {port} is in use", 409, blockers)
                await conn.execute(
                    "INSERT INTO ultra_ports (port, proxy_host, expected_egress_ip, city, timezone, state, browser_name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?)",
                    (port, (proxy_host or pool.get("host") or "").strip(), expected_egress_ip or None, city or None, tz or None, name, now, now))
            else:
                await conn.execute("UPDATE ultra_ports SET state = 'reserved', browser_name = ?, updated_at = ? WHERE port = ?",
                                   (name, now, port))
            try:
                await conn.execute(
                    "INSERT INTO ultra_browsers (name, host_id, mode, container, port, email, email_norm, token_id, route_key, "
                    "route_acked, desired_state, state, onboarding_hold, created_at, updated_at) "
                    "VALUES (?, ?, 'observe_only', ?, ?, ?, ?, ?, ?, 1, 'running', 'observing', 0, ?, ?)",
                    (name, self.host_id, (container or name).strip(), port, email, norm_email(email),
                     int(token_id) if token_id is not None else None, (route_key or "").strip() or None, now, now))
            except Exception as e:
                raise UltraError(f"could not record {name}: {e}", 409)
            await conn.commit()
        return {"name": name, "mode": "observe_only", "port": port, "token_id": token_id}

    async def add_account(self, *, email: str, password: str, port: int, reserved_client: str = "") -> Dict[str, Any]:
        """Slice B: a NEW managed browser. The row (with the caller restriction and the onboarding hold) exists
        before the browser starts; the password is sealed; the first job is `create`."""
        self._require_b()
        email_n = norm_email(email)
        if not EMAIL_RE.match(email_n):
            raise UltraError("a valid email is required", 400)
        if not isinstance(password, str) or not password or len(password) > 200:
            raise UltraError("a password is required", 400)
        client = (reserved_client or "").strip().lower()
        if client and not re.fullmatch(r"[a-z0-9._-]{1,40}", client):
            raise UltraError("'Only for app' must be a client name (letters, digits, . _ -)", 400)
        port = int(port)
        now = _iso(self.now())
        async with self.db.connect(write=True) as conn:
            prow = await (await conn.execute("SELECT state, browser_name FROM ultra_ports WHERE port = ?", (port,))).fetchone()
            if not prow:
                raise UltraError(f"port {port} is not an Ultra port (reserve it first)", 409)
            if prow[0] != "free" or prow[1]:
                raise UltraError(f"port {port} is not free ({prow[0]}{', ' + prow[1] if prow[1] else ''})", 409)
            if await (await conn.execute("SELECT name FROM ultra_browsers WHERE email_norm = ?", (email_n,))).fetchone():
                raise UltraError(f"{email_n} already has an Ultra browser", 409)
            # A port may have gained a user since it was reserved (R3-2 makes that hard, check anyway).
            blockers = [b for b in await self._port_blockers(conn, port) if b != "already an Ultra port"]
            if blockers:
                raise UltraError(f"port {port} is in use", 409, blockers)
            name = await self._next_name(conn)
            route_key = f"ultra-{name}-{uuid.uuid4().hex[:12]}"
            sealed = vault.seal(password, context=f"ultra-browser:{name}", raw_key=self._vault_key)
            tok = await (await conn.execute(
                "SELECT id FROM tokens WHERE lower(trim(email)) = ? ORDER BY id LIMIT 1", (email_n,))).fetchone()
            token_id = int(tok[0]) if tok else None
            await conn.execute(
                "INSERT INTO ultra_browsers (name, host_id, mode, container, port, email, email_norm, sealed_password, "
                "reserved_client, token_id, route_key, route_acked, desired_state, state, state_detail, onboarding_hold, "
                "created_at, updated_at) VALUES (?, ?, 'managed', ?, ?, ?, ?, ?, ?, ?, ?, 0, 'running', 'creating', ?, 1, ?, ?)",
                (name, self.host_id, name, port, email.strip(), email_n, sealed, client, token_id, route_key,
                 "creating the browser", now, now))
            await conn.execute("UPDATE ultra_ports SET state = 'reserved', browser_name = ?, updated_at = ? WHERE port = ?",
                               (name, now, port))
            if token_id is not None:
                # The account already exists (e.g. it ran on a laptop): it stops serving until this browser
                # owns it and the release conditions hold. Its laptop pushes are refused from now on.
                await conn.execute("UPDATE tokens SET ultra_hold = 'onboarding', reserved_client = ? WHERE id = ?",
                                   (client, token_id))
            await self._insert_job(conn, name, "create", {}, now)
            await conn.commit()
        debug_logger.op_warning(f"[ULTRA] {name}: new Ultra browser for {email_n} on port {port} (existing token={token_id})")
        return {"name": name, "port": port, "route_key": route_key, "token_id": token_id}

    async def get_browser(self, name: str, conn=None) -> Optional[Dict[str, Any]]:
        async def _q(c):
            c.row_factory = None
            cur = await c.execute(
                "SELECT b.*, p.proxy_host, p.expected_egress_ip, p.city, p.timezone FROM ultra_browsers b "
                "LEFT JOIN ultra_ports p ON p.port = b.port WHERE b.name = ?", (name,))
            row = await cur.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in cur.description], row))
        if conn is not None:
            return await _q(conn)
        async with self.db.connect() as c:
            return await _q(c)

    async def list_browsers(self, host_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with self.db.connect() as c:
            sql = ("SELECT b.*, p.proxy_host, p.expected_egress_ip, p.city, p.timezone FROM ultra_browsers b "
                   "LEFT JOIN ultra_ports p ON p.port = b.port")
            args: tuple = ()
            if host_id:
                sql += " WHERE b.host_id = ?"
                args = (host_id,)
            cur = await c.execute(sql + " ORDER BY b.name", args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ state + alerts (A5)

    async def _set_state(self, conn, name: str, state: str, detail: str = "", alert_text: Optional[str] = None) -> bool:
        """Change a browser's state; an alert row is written in the SAME transaction when the change enters an
        alert state or returns to ok from one (never repeated while the state stays)."""
        row = await (await conn.execute("SELECT state, mode FROM ultra_browsers WHERE name = ?", (name,))).fetchone()
        if not row:
            return False
        old = row[0]
        now = _iso(self.now())
        if old == state:
            await conn.execute("UPDATE ultra_browsers SET state_detail = ?, updated_at = ? WHERE name = ?",
                               (detail[:300], now, name))
            return False
        await conn.execute("UPDATE ultra_browsers SET state = ?, state_detail = ?, updated_at = ? WHERE name = ?",
                           (state, detail[:300], now, name))
        text = None
        if state in ALERT_STATES:
            text = alert_text or f"Ultra browser {name}: {state.replace('_', ' ')}" + (f" — {detail}" if detail else "")
        elif state == "ok" and old in ALERT_STATES + ("onboarding",):
            text = f"Ultra browser {name}: back OK" if old != "onboarding" else f"Ultra browser {name}: ready and serving"
        if text:
            await conn.execute(
                "INSERT INTO ultra_alerts (browser, kind, text, created_at, next_try_at) VALUES (?, ?, ?, ?, ?)",
                (name, state, text[:900], now, now))
        return True

    async def set_state(self, name: str, state: str, detail: str = "", alert_text: Optional[str] = None) -> bool:
        async with self.db.connect(write=True) as conn:
            changed = await self._set_state(conn, name, state, detail, alert_text)
            await conn.commit()
        return changed

    async def send_pending_alerts(self, sender: Optional[Callable[[str], Awaitable[None]]] = None) -> int:
        """Deliver unsent alert rows (at-least-once). Without Telegram settings they wait in the outbox."""
        if sender is None:
            if not (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip()):
                return 0
            sender = telegram_send
        now = self.now()
        async with self.db.connect() as c:
            rows = await (await c.execute(
                "SELECT id, text, tries FROM ultra_alerts WHERE sent_at IS NULL AND tries < ? "
                "AND (next_try_at IS NULL OR next_try_at <= ?) ORDER BY id LIMIT 20",
                (ALERT_MAX_TRIES, _iso(now)))).fetchall()
        sent = 0
        for aid, text, tries in rows:
            err = None
            try:
                await sender(text)
            except Exception as e:  # never log the bot token: only the error class and a short text
                err = f"{type(e).__name__}: {str(e)[:120]}"
            async with self.db.connect(write=True) as c:
                if err is None:
                    await c.execute("UPDATE ultra_alerts SET sent_at = ?, tries = ? WHERE id = ?", (_iso(self.now()), tries + 1, aid))
                    sent += 1
                else:
                    backoff = min(1800, 15 * (2 ** min(tries, 7)))
                    await c.execute("UPDATE ultra_alerts SET tries = ?, last_error = ?, next_try_at = ? WHERE id = ?",
                                    (tries + 1, err, _iso(self.now() + timedelta(seconds=backoff)), aid))
                await c.commit()
        return sent

    # ------------------------------------------------------------------ jobs

    async def _insert_job(self, conn, browser: str, kind: str, args: Dict[str, Any], now: str,
                          state: str = "queued", attempt_id: Optional[str] = None) -> str:
        job_id = uuid.uuid4().hex
        mutating = 0 if kind in READ_ONLY_JOBS else 1
        try:
            await conn.execute(
                "INSERT INTO ultra_jobs (id, browser, kind, mutating, args, state, attempt_id, hold_started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, browser, kind, mutating, json.dumps(args or {}), state, attempt_id,
                 now if state == "draining" else None, now, now))
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise UltraError(f"{browser} already has a job running; wait for it to finish", 409)
            raise
        return job_id

    async def request_job(self, name: str, kind: str, *, by: str = "admin") -> Dict[str, Any]:
        """A person (or the coordinator) asks for an operation on a browser."""
        if kind not in READ_ONLY_JOBS + MUTATING_JOBS:
            raise UltraError(f"unknown operation {kind}", 400)
        if kind not in READ_ONLY_JOBS:
            self._require_b()
        now_dt = self.now()
        now = _iso(now_dt)
        async with self.db.connect(write=True) as conn:
            b = await self.get_browser(name, conn)
            if not b:
                raise UltraError(f"no Ultra browser {name}", 404)
            if kind not in READ_ONLY_JOBS and b["mode"] != "managed":
                raise UltraError(f"{name} is observe-only: no lifecycle changes until it is adopted", 409)
            attempt_id = None
            if kind == "login":
                if not b.get("route_acked"):
                    raise UltraError(f"{name}: the extension is not bootstrapped yet", 409)
                busy = await (await conn.execute(
                    "SELECT 1 FROM ultra_login_attempts WHERE browser = ? AND state IN ('reserved', 'running', 'challenge')",
                    (name,))).fetchone()
                if busy:
                    raise UltraError(f"{name}: a sign-in is already in progress", 409)
                attempt_id = uuid.uuid4().hex[:16]
            state = "queued"
            if kind in DRAINED_JOBS and b.get("token_id") is not None:
                hold = await (await conn.execute("SELECT COALESCE(ultra_hold, '') FROM tokens WHERE id = ?",
                                                 (int(b["token_id"]),))).fetchone()
                if hold is not None and not hold[0]:
                    # Acquire the hold now; the job becomes leasable once the account has drained.
                    await conn.execute("UPDATE tokens SET ultra_hold = ? WHERE id = ?", (kind, int(b["token_id"])))
                    state = "draining"
            job_id = await self._insert_job(conn, name, kind, {"by": by}, now, state=state, attempt_id=attempt_id)
            if attempt_id:
                await conn.execute(
                    "INSERT INTO ultra_login_attempts (attempt_id, browser, job_id, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'reserved', ?, ?)", (attempt_id, name, job_id, now, now))
                await self._set_state(conn, name, "signing_in", "sign-in queued")
            if kind == "stop":
                await conn.execute("UPDATE ultra_browsers SET desired_state = 'stopped', updated_at = ? WHERE name = ?", (now, name))
            if kind == "start":
                await conn.execute("UPDATE ultra_browsers SET desired_state = 'running', updated_at = ? WHERE name = ?", (now, name))
            await conn.commit()
        return {"job_id": job_id, "state": state, "attempt_id": attempt_id}

    async def process_drains(self) -> None:
        """draining → queued once the account has had no outstanding request for the settle time; at
        DRAIN_MAX_S the operation is deferred (hold released, nothing done), never forced (R3-5)."""
        async with self.db.connect() as c:
            rows = await (await c.execute(
                "SELECT j.id, j.browser, j.kind, j.hold_started_at, b.token_id FROM ultra_jobs j "
                "JOIN ultra_browsers b ON b.name = j.browser WHERE j.state = 'draining'")).fetchall()
        for job_id, browser, kind, started, token_id in rows:
            started_dt = _parse(started) or self.now()
            elapsed = (self.now() - started_dt).total_seconds()
            if elapsed < DRAIN_SETTLE_S:
                continue
            outstanding = await self.db.count_outstanding_requests(int(token_id)) if token_id is not None else 0
            async with self.db.connect(write=True) as c:
                if outstanding == 0:
                    await c.execute("UPDATE ultra_jobs SET state = 'queued', updated_at = ? WHERE id = ? AND state = 'draining'",
                                    (_iso(self.now()), job_id))
                elif elapsed >= DRAIN_MAX_S:
                    await c.execute("UPDATE ultra_jobs SET state = 'deferred', error = ?, updated_at = ? WHERE id = ? AND state = 'draining'",
                                    (f"{outstanding} request(s) still running after {DRAIN_MAX_S}s; not forced", _iso(self.now()), job_id))
                    if token_id is not None:
                        await c.execute("UPDATE tokens SET ultra_hold = '' WHERE id = ? AND ultra_hold = ?", (int(token_id), kind))
                    if kind == "stop":
                        await c.execute("UPDATE ultra_browsers SET desired_state = 'running' WHERE name = ?", (browser,))
                    debug_logger.op_warning(f"[ULTRA] {browser}: {kind} deferred, {outstanding} request(s) still running")
                await c.commit()

    async def lease(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Hand the agent the oldest queued job of its browsers. A mutating job waits while another mutating
        job of that browser is leased. Secrets (password for a login, connection token and proxy for a
        bootstrap/create/update) are built here, once, and never stored."""
        now_dt = self.now()
        now = _iso(now_dt)
        async with self.db.connect(write=True) as conn:
            rows = await (await conn.execute(
                "SELECT j.id, j.browser, j.kind, j.mutating, j.args, j.attempt_id, j.secret_delivered FROM ultra_jobs j "
                "JOIN ultra_browsers b ON b.name = j.browser WHERE j.state = 'queued' AND b.host_id = ? "
                "ORDER BY j.created_at, j.id", (host_id,))).fetchall()
            for job_id, browser, kind, mutating, args, attempt_id, delivered in rows:
                if mutating:
                    busy = await (await conn.execute(
                        "SELECT 1 FROM ultra_jobs WHERE browser = ? AND mutating = 1 AND state = 'leased'", (browser,))).fetchone()
                    if busy:
                        continue
                lease_until = _iso(now_dt + timedelta(seconds=LEASE_S.get(kind, 120)))
                cur = await conn.execute(
                    "UPDATE ultra_jobs SET state = 'leased', leased_at = ?, lease_until = ?, secret_delivered = 1, updated_at = ? "
                    "WHERE id = ? AND state = 'queued'", (now, lease_until, now, job_id))
                if cur.rowcount != 1:
                    continue
                b = await self.get_browser(browser, conn)
                job = {"id": job_id, "browser": browser, "kind": kind, "args": json.loads(args or "{}"),
                       "attempt_id": attempt_id, "lease_until": lease_until}
                secrets: Dict[str, Any] = {}
                if kind == "login":
                    try:
                        if delivered:
                            raise ValueError("login secret already delivered once")  # never twice
                        secrets["password"] = vault.open_sealed(b["sealed_password"] or "", context=f"ultra-browser:{browser}",
                                                               raw_key=self._vault_key)
                    except Exception as e:
                        await conn.execute("UPDATE ultra_jobs SET state = 'failed', error = ?, updated_at = ? WHERE id = ?",
                                           (f"password not available: {type(e).__name__}", now, job_id))
                        await conn.execute("UPDATE ultra_login_attempts SET state = 'failed', updated_at = ? WHERE attempt_id = ?",
                                           (now, attempt_id))
                        await self._set_state(conn, browser, "needs_you", "the stored password cannot be opened (vault key changed?)")
                        await conn.commit()
                        return None
                    secrets["email"] = b["email"]
                    await conn.execute("UPDATE ultra_login_attempts SET state = 'running', updated_at = ? WHERE attempt_id = ?",
                                       (now, attempt_id))
                if kind in ("create", "bootstrap", "update"):
                    plugin = await (await conn.execute("SELECT connection_token FROM plugin_config WHERE id = 1")).fetchone()
                    secrets["connection_token"] = (plugin[0] if plugin else "") or ""
                    secrets["route_key"] = b.get("route_key")
                if kind in ("create", "update"):
                    pool = await self._pool(conn)
                    if not pool.get("user") or not b.get("proxy_host"):
                        await conn.execute("UPDATE ultra_jobs SET state = 'failed', error = ? WHERE id = ?",
                                           ("no proxy credentials in the extension pool settings", job_id))
                        await conn.commit()
                        return None
                    secrets["site"] = {
                        "proxyUrl": f"http://{pool['user']}:{pool.get('pass', '')}@{b['proxy_host']}:{int(b['port'])}",
                        "clientLabel": browser,
                        "proxyAllHosts": True,
                    }
                await conn.commit()
                if secrets:
                    job["secrets"] = secrets
                return job
            await conn.commit()
        return None

    async def renew(self, job_ids: List[str]) -> None:
        if not job_ids:
            return
        now_dt = self.now()
        async with self.db.connect(write=True) as conn:
            for jid in job_ids[:20]:
                row = await (await conn.execute("SELECT kind FROM ultra_jobs WHERE id = ? AND state = 'leased'", (jid,))).fetchone()
                if row:
                    await conn.execute("UPDATE ultra_jobs SET lease_until = ? WHERE id = ?",
                                       (_iso(now_dt + timedelta(seconds=LEASE_S.get(row[0], 120))), jid))
            await conn.commit()

    async def expire_leases(self) -> int:
        """A leased job whose agent went silent: a read-only one simply failed; a mutating one is UNCERTAIN
        (the container may or may not have changed) — the hold stays and a person looks. A login is never
        replayed: its attempt becomes uncertain and the browser needs_you."""
        now = _iso(self.now())
        n = 0
        async with self.db.connect(write=True) as conn:
            rows = await (await conn.execute(
                "SELECT id, browser, kind, mutating, attempt_id FROM ultra_jobs WHERE state = 'leased' AND lease_until < ?",
                (now,))).fetchall()
            for job_id, browser, kind, mutating, attempt_id in rows:
                n += 1
                if not mutating:
                    await conn.execute("UPDATE ultra_jobs SET state = 'failed', error = 'lease expired', updated_at = ? WHERE id = ?",
                                       (now, job_id))
                    continue
                await conn.execute("UPDATE ultra_jobs SET state = 'uncertain', error = 'agent went silent', updated_at = ? WHERE id = ?",
                                   (now, job_id))
                if attempt_id:
                    await conn.execute("UPDATE ultra_login_attempts SET state = 'uncertain', updated_at = ? WHERE attempt_id = ?",
                                       (now, attempt_id))
                    await self._set_state(conn, browser, "needs_you",
                                          "the agent stopped answering during sign-in (the password may have been typed): check the screen, then Retry sign-in")
                else:
                    await self._set_state(conn, browser, "error", f"{kind} did not finish (agent went silent); account stays held")
            await conn.commit()
        return n

    # ------------------------------------------------------------------ agent results

    async def handle_result(self, host_id: str, job_id: str, outcome: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """outcome: progress (login steps, keeps the lease) | done | failed | uncertain."""
        if outcome not in ("progress", "done", "failed", "uncertain"):
            raise UltraError("outcome must be progress|done|failed|uncertain", 400)
        result = result if isinstance(result, dict) else {}
        png = result.pop("png_b64", None) if outcome == "done" else None
        now_dt = self.now()
        now = _iso(now_dt)
        reply: Dict[str, Any] = {"ok": True}
        async with self.db.connect(write=True) as conn:
            row = await (await conn.execute(
                "SELECT j.browser, j.kind, j.state, j.attempt_id, b.host_id FROM ultra_jobs j "
                "JOIN ultra_browsers b ON b.name = j.browser WHERE j.id = ?", (job_id,))).fetchone()
            if not row or row[4] != host_id:
                raise UltraError("unknown job", 404)
            browser, kind, state, attempt_id, _ = row
            if state != "leased":
                # late answer for a job already given up on: record nothing, tell the agent to stop
                await conn.commit()
                return {"ok": False, "stop": True, "state": state}
            b = await self.get_browser(browser, conn)
            if outcome == "progress":
                reply.update(await self._login_progress(conn, b, attempt_id, result, now_dt))
                await conn.execute("UPDATE ultra_jobs SET lease_until = ? WHERE id = ?",
                                   (_iso(now_dt + timedelta(seconds=LEASE_S.get(kind, 120))), job_id))
                await conn.commit()
                return reply
            await conn.execute("UPDATE ultra_jobs SET state = ?, result = ?, error = ?, updated_at = ? WHERE id = ?",
                               (outcome, json.dumps(_scrub(result))[:4000], (str(result.get("error") or "")[:300] or None),
                                now, job_id))
            await self._after_job(conn, b, kind, outcome, result, attempt_id, now)
            await conn.commit()
        if png and kind == "screenshot":
            self._store_screenshot(job_id, png)
        return reply

    async def _login_progress(self, conn, b, attempt_id: Optional[str], result: Dict[str, Any], now_dt: datetime) -> Dict[str, Any]:
        step = str(result.get("step") or "")[:40]
        now = _iso(now_dt)
        if not attempt_id:
            return {}
        await conn.execute("UPDATE ultra_login_attempts SET step = ?, updated_at = ? WHERE attempt_id = ?", (step, now, attempt_id))
        if step == "password_sent":
            await conn.execute("UPDATE ultra_login_attempts SET password_sent = 1 WHERE attempt_id = ?", (attempt_id,))
            return {}
        ch = result.get("challenge") if isinstance(result.get("challenge"), dict) else None
        if not ch:
            return {}
        kind = str(ch.get("kind") or "other")[:20]
        if kind == "number":
            text = f"Tap {str(ch.get('number') or '?')[:4]} in the Gmail app on {str(ch.get('device') or 'your phone')[:60]}"
            ttl = NUMBER_TTL_S
        elif kind == "code":
            text = f"Type the code Google sent ({str(ch.get('hint') or 'SMS / app')[:80]})"
            ttl = CODE_TTL_S
        else:
            text = str(ch.get("text") or "Google asks something the agent cannot answer")[:200]
            ttl = CODE_TTL_S
        cur = await (await conn.execute("SELECT challenge_id, challenge_text FROM ultra_login_attempts WHERE attempt_id = ?",
                                        (attempt_id,))).fetchone()
        if cur and cur[0] and cur[1] == text and not ch.get("new"):
            return {"challenge_id": cur[0]}   # the same challenge reported again
        challenge_id = uuid.uuid4().hex[:12]
        await conn.execute(
            "UPDATE ultra_login_attempts SET state = 'challenge', challenge_id = ?, challenge_kind = ?, challenge_text = ?, "
            "challenge_expires_at = ?, updated_at = ? WHERE attempt_id = ?",
            (challenge_id, kind, text, _iso(now_dt + timedelta(seconds=ttl)), now, attempt_id))
        await self._set_state(conn, b["name"], "needs_you", text, alert_text=f"Ultra browser {b['name']} ({b['email']}): {text}")
        return {"challenge_id": challenge_id}

    async def _after_job(self, conn, b, kind: str, outcome: str, result: Dict[str, Any], attempt_id: Optional[str], now: str):
        name = b["name"]
        token_id = b.get("token_id")
        ok = outcome == "done"
        if kind in READ_ONLY_JOBS:
            return
        if kind == "create":
            if ok:
                await self._set_state(conn, name, "bootstrapping", "browser up; configuring the extension")
                await self._insert_job(conn, name, "bootstrap", {}, now)
            else:
                await self._set_state(conn, name, "error", f"create {outcome}: {str(result.get('error') or '')[:200]}")
        elif kind == "bootstrap":
            seen = str(result.get("route_key") or "")
            if ok and seen and seen == (b.get("route_key") or ""):
                await conn.execute("UPDATE ultra_browsers SET route_acked = 1, updated_at = ? WHERE name = ?", (now, name))
                await self._set_state(conn, name, "signing_in", "extension configured; sign-in queued")
                attempt_id = uuid.uuid4().hex[:16]
                job_id = await self._insert_job(conn, name, "login", {"by": "onboarding"}, now, attempt_id=attempt_id)
                await conn.execute(
                    "INSERT INTO ultra_login_attempts (attempt_id, browser, job_id, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'reserved', ?, ?)", (attempt_id, name, job_id, now, now))
            else:
                await self._set_state(conn, name, "error",
                                      f"bootstrap {outcome}: route key read back {'mismatch' if seen else 'missing'}")
        elif kind == "login":
            final = str(result.get("step") or ("signed_in" if ok else outcome))
            astate = "signed_in" if ok and final == "signed_in" else ("uncertain" if outcome == "uncertain" else "needs_you")
            if attempt_id:
                await conn.execute("UPDATE ultra_login_attempts SET state = ?, step = ?, updated_at = ? WHERE attempt_id = ?",
                                   (astate, final[:40], now, attempt_id))
                await conn.execute("UPDATE ultra_messages SET state = 'expired', payload = NULL WHERE attempt_id = ? AND state = 'pending'",
                                   (attempt_id,))
            if astate == "signed_in":
                bits = []
                if result.get("ultra") is False:
                    bits.append("no ULTRA badge seen")
                if not result.get("project_opened"):
                    bits.append("no project tab open")
                if bits:
                    await self._set_state(conn, name, "needs_you", "signed in, but " + ", ".join(bits))
                elif b.get("onboarding_hold"):
                    await self._set_state(conn, name, "onboarding", "signed in; waiting for the extension to register the account")
                else:
                    await self._set_state(conn, name, "degraded", "signed in again; checking health")
            else:
                await self._set_state(conn, name, "needs_you",
                                      str(result.get("detail") or result.get("error") or f"sign-in stopped at {final}")[:250])
        elif kind in ("stop", "start", "restart", "update"):
            hold = await (await conn.execute("SELECT COALESCE(ultra_hold, '') FROM tokens WHERE id = ?",
                                             (int(token_id),))).fetchone() if token_id is not None else None
            if kind == "stop":
                if ok:
                    await self._set_state(conn, name, "stopped", "stopped by a person")
                    if token_id is not None and hold is not None and hold[0] in ("stop", ""):
                        await conn.execute("UPDATE tokens SET ultra_hold = 'stopped' WHERE id = ?", (int(token_id),))
                else:
                    await self._set_state(conn, name, "error", f"stop {outcome}: {str(result.get('error') or '')[:200]}")
                return
            ready = ok and bool(result.get("ready"))
            if ready:
                if token_id is not None and hold is not None and hold[0] in (kind, "stopped"):
                    # verified ready: the account may serve again (onboarding keeps its own hold)
                    await conn.execute("UPDATE tokens SET ultra_hold = '' WHERE id = ? AND ultra_hold IN (?, 'stopped')",
                                       (int(token_id), kind))
                if b["state"] in ("stopped", "error", "starting"):
                    await self._set_state(conn, name, "onboarding" if b.get("onboarding_hold") else "degraded",
                                          f"{kind} done; checking health")
            else:
                # failed / uncertain / not verified: the hold STAYS (R3-5)
                await self._set_state(conn, name, "error", f"{kind} {outcome}: {str(result.get('error') or 'not verified ready')[:200]}")

    def _store_screenshot(self, job_id: str, png_b64: str) -> None:
        import base64
        try:
            data = base64.b64decode(png_b64)
        except Exception:
            return
        if not data.startswith(b"\x89PNG") or len(data) > 6 * 1024 * 1024:
            return
        cutoff = time.monotonic() - SCREENSHOT_TTL_S
        for k in [k for k, (t, _) in self._screenshots.items() if t < cutoff]:
            self._screenshots.pop(k, None)
        self._screenshots[job_id] = (time.monotonic(), data)

    def get_screenshot(self, job_id: str) -> Optional[bytes]:
        item = self._screenshots.get(job_id)
        if not item or item[0] < time.monotonic() - SCREENSHOT_TTL_S:
            return None
        return item[1]

    # ------------------------------------------------------------------ challenge replies (messages)

    async def reply_challenge(self, name: str, attempt_id: str, challenge_id: str, kind: str, code: str = "") -> Dict[str, Any]:
        """A person's answer to the CURRENT challenge of the running attempt: single use, expires with it."""
        self._require_b()
        if kind not in ("code", "tapped", "resend"):
            raise UltraError("kind must be code|tapped|resend", 400)
        now_dt = self.now()
        async with self.db.connect(write=True) as conn:
            a = await (await conn.execute(
                "SELECT state, challenge_id, challenge_kind, challenge_expires_at FROM ultra_login_attempts "
                "WHERE attempt_id = ? AND browser = ?", (attempt_id, name))).fetchone()
            if not a or a[0] != "challenge" or a[1] != challenge_id:
                raise UltraError("that challenge is no longer current (refresh the page)", 409)
            if (_parse(a[3]) or now_dt) < now_dt:
                raise UltraError("that challenge has expired; press Retry sign-in", 409)
            if kind == "code":
                if a[2] != "code":
                    raise UltraError("this challenge does not take a code", 409)
                code = (code or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9-]{4,12}", code):
                    raise UltraError("the code looks wrong (4-12 letters/digits)", 400)
            used = await (await conn.execute(
                "SELECT 1 FROM ultra_messages WHERE attempt_id = ? AND challenge_id = ? AND kind = ? "
                "AND (kind != 'tapped' OR state = 'pending')", (attempt_id, challenge_id, kind))).fetchone()
            if used:
                raise UltraError(f"'{kind}' was already sent for this challenge", 409)
            payload = vault.seal(code, context=f"ultra-code:{attempt_id}", raw_key=self._vault_key) if kind == "code" else None
            await conn.execute(
                "INSERT INTO ultra_messages (browser, attempt_id, challenge_id, kind, payload, state, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (name, attempt_id, challenge_id, kind, payload, a[3], _iso(now_dt)))
            await conn.commit()
        return {"ok": True}

    async def take_messages(self, host_id: str) -> List[Dict[str, Any]]:
        now = _iso(self.now())
        out = []
        async with self.db.connect(write=True) as conn:
            await conn.execute("UPDATE ultra_messages SET state = 'expired', payload = NULL WHERE state = 'pending' AND expires_at < ?", (now,))
            rows = await (await conn.execute(
                "SELECT m.id, m.browser, m.attempt_id, m.challenge_id, m.kind, m.payload FROM ultra_messages m "
                "JOIN ultra_browsers b ON b.name = m.browser WHERE m.state = 'pending' AND b.host_id = ? ORDER BY m.id",
                (host_id,))).fetchall()
            for mid, browser, attempt_id, challenge_id, kind, payload in rows:
                msg = {"browser": browser, "attempt_id": attempt_id, "challenge_id": challenge_id, "kind": kind}
                if kind == "code" and payload:
                    try:
                        msg["code"] = vault.open_sealed(payload, context=f"ultra-code:{attempt_id}", raw_key=self._vault_key)
                    except Exception:
                        msg = None
                await conn.execute("UPDATE ultra_messages SET state = 'delivered', payload = NULL, delivered_at = ? WHERE id = ?",
                                   (now, mid))
                if msg:
                    out.append(msg)
            await conn.commit()
        return out

    # ------------------------------------------------------------------ the session push (R3-1, B2)

    async def push_guard(self, email: str, route_key: Optional[str], proxy_url: Optional[str],
                         token_id: Optional[int]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Ownership of a verified push, BEFORE any credential or routing write. Returns (managed_row, refusal).
        managed email → only its browser's route key and proxy endpoint; managed route key → only its email and
        bound token. observe_only rows are not enforced (Slice D)."""
        email_n = norm_email(email)
        rk = (route_key or "").strip()
        async with self.db.connect() as conn:
            by_email = await self._managed_row(conn, "email_norm", email_n) if email_n else None
            by_route = await self._managed_row(conn, "route_key", rk) if rk else None
        row = by_email or by_route
        if row is None:
            return None, None
        if by_email and by_route and by_email["name"] != by_route["name"]:
            return row, f"route key belongs to Ultra browser {by_route['name']}, account to {by_email['name']}"
        if (row.get("route_key") or "") != rk:
            return row, f"{email_n} is owned by Ultra browser {row['name']}; pushes from another browser are refused"
        if row["email_norm"] != email_n:
            return row, f"Ultra browser {row['name']} is for {row['email_norm']}, not {email_n}"
        if row.get("token_id") is not None and token_id is not None and int(row["token_id"]) != int(token_id):
            return row, f"Ultra browser {row['name']} is bound to token {row['token_id']}"
        if not Database._endpoint_matches(proxy_url, row.get("proxy_host") or "", int(row["port"])):
            return row, f"Ultra browser {row['name']} must use its own proxy port {row['port']}"
        return row, None

    async def _managed_row(self, conn, col: str, value: str) -> Optional[Dict[str, Any]]:
        cur = await conn.execute(
            f"SELECT b.*, p.proxy_host FROM ultra_browsers b LEFT JOIN ultra_ports p ON p.port = b.port "
            f"WHERE b.{col} = ? AND b.mode = 'managed'", (value,))
        r = await cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else None

    async def bind_token(self, name: str, token_id: int) -> None:
        async with self.db.connect(write=True) as conn:
            await conn.execute("UPDATE ultra_browsers SET token_id = ?, updated_at = ? WHERE name = ? AND token_id IS NULL",
                               (int(token_id), _iso(self.now()), name))
            await conn.commit()

    async def adopt_token(self, name: str, token_id: int) -> None:
        """The managed browser pushed an account that already had a row (created after the browser was added):
        bind it and put it on the onboarding hold with the browser's caller restriction, in one transaction."""
        async with self.db.connect(write=True) as conn:
            cur = await conn.execute("UPDATE ultra_browsers SET token_id = ?, updated_at = ? WHERE name = ? AND token_id IS NULL "
                                     "AND onboarding_hold = 1", (int(token_id), _iso(self.now()), name))
            if cur.rowcount:
                await conn.execute(
                    "UPDATE tokens SET ultra_hold = 'onboarding', reserved_client = "
                    "(SELECT reserved_client FROM ultra_browsers WHERE name = ?) WHERE id = ?", (name, int(token_id)))
            await conn.commit()

    async def note_verified(self, token_id: int) -> None:
        """A push for this account was verified by Google's API just now (release condition, R3-3)."""
        async with self.db.connect(write=True) as conn:
            cur = await conn.execute("UPDATE ultra_browsers SET last_verified_at = ? WHERE token_id = ? AND mode = 'managed'",
                                     (_iso(self.now()), int(token_id)))
            await conn.commit()
            hit = cur.rowcount
        if hit:
            row = None
            async with self.db.connect() as c:
                row = await (await c.execute("SELECT name FROM ultra_browsers WHERE token_id = ?", (int(token_id),))).fetchone()
            if row:
                await self.finalize_onboarding(row[0])

    async def finalize_onboarding(self, name: str) -> Tuple[bool, List[str]]:
        """Release the onboarding hold — only here, in one transaction, and only when every condition holds:
        email, Ultra tier, project, route, redeem proxy, caller restriction, and a credential verified within
        the last 10 minutes."""
        now_dt = self.now()
        async with self.db.connect(write=True) as conn:
            b = await self.get_browser(name, conn)
            if not b or b["mode"] != "managed" or not b["onboarding_hold"]:
                await conn.commit()
                return False, ["not onboarding"]
            missing = []
            t = None
            if b.get("token_id") is not None:
                cur = await conn.execute(
                    "SELECT email, user_paygate_tier, current_project_id, extension_route_key, redeem_proxy_url, "
                    "reserved_client, ultra_hold, ban_reason FROM tokens WHERE id = ?", (int(b["token_id"]),))
                t = await cur.fetchone()
            if not t:
                missing.append("no account registered yet")
            else:
                if norm_email(t[0]) != b["email_norm"]:
                    missing.append("email")
                if normalize_user_paygate_tier(t[1]) != ULTRA_TIER:
                    missing.append("Ultra tier")
                if not (t[2] or "").strip():
                    missing.append("project")
                if (t[3] or "") != (b.get("route_key") or "") or not b.get("route_acked"):
                    missing.append("route key")
                if not Database._endpoint_matches(t[4], b.get("proxy_host") or "", int(b["port"])):
                    missing.append("redeem proxy")
                if (t[5] or "") != (b.get("reserved_client") or ""):
                    missing.append("caller restriction")
                verified = _parse(b.get("last_verified_at"))
                if not verified or (now_dt - verified).total_seconds() > VERIFIED_FRESH_S:
                    missing.append("fresh verified credential")
            if missing:
                await conn.commit()
                return False, missing
            tid = int(b["token_id"])
            await conn.execute(
                "UPDATE tokens SET ultra_hold = '', is_active = CASE WHEN ban_reason = 'ultra_onboarding' THEN 1 ELSE is_active END, "
                "banned_at = CASE WHEN ban_reason = 'ultra_onboarding' THEN NULL ELSE banned_at END, "
                "ban_reason = CASE WHEN ban_reason = 'ultra_onboarding' THEN NULL ELSE ban_reason END "
                "WHERE id = ? AND ultra_hold = 'onboarding'", (tid,))
            await conn.execute("UPDATE ultra_browsers SET onboarding_hold = 0, updated_at = ? WHERE name = ?", (_iso(now_dt), name))
            await self._set_state(conn, name, "ok", "")
            await conn.commit()
        debug_logger.op_warning(f"[ULTRA] {name}: onboarding complete, token {tid} released")
        return True, []

    # ------------------------------------------------------------------ agent poll (A4)

    async def agent_poll(self, host_id: str, agent_version: str, observations: Dict[str, Any],
                         running_jobs: List[str], accept_jobs: bool = True) -> Dict[str, Any]:
        now_dt = self.now()
        now = _iso(now_dt)
        async with self.db.connect(write=True) as conn:
            h = await (await conn.execute("SELECT state FROM ultra_hosts WHERE host_id = ?", (host_id,))).fetchone()
            if h is None:
                await conn.execute("INSERT INTO ultra_hosts (host_id, agent_version, state, last_poll_at, updated_at) VALUES (?, ?, 'ok', ?, ?)",
                                   (host_id, agent_version[:40], now, now))
            else:
                await conn.execute("UPDATE ultra_hosts SET agent_version = ?, state = 'ok', last_poll_at = ?, updated_at = ? WHERE host_id = ?",
                                   (agent_version[:40], now, now, host_id))
                if h[0] == "silent":
                    await conn.execute("INSERT INTO ultra_alerts (browser, kind, text, created_at, next_try_at) VALUES (NULL, 'host_ok', ?, ?, ?)",
                                       (f"Ultra host {host_id}: agent is answering again", now, now))
            await conn.commit()
        await self.renew([str(j) for j in (running_jobs or [])])
        for name, obs in (observations or {}).items():
            if isinstance(obs, dict):
                await self.record_observation(str(name), host_id, obs)
        browsers = []
        for b in await self.list_browsers(host_id):
            browsers.append({
                "name": b["name"], "container": b["container"], "mode": b["mode"], "port": b["port"],
                "timezone": b.get("timezone") or "", "desired_state": b["desired_state"],
                "route_key": b.get("route_key") if b["mode"] == "managed" else None,
            })
        if not accept_jobs:   # agent --dry-run: observe only
            return {"ok": True, "browsers": browsers, "job": None, "messages": []}
        job = await self.lease(host_id)
        messages = await self.take_messages(host_id)
        return {"ok": True, "browsers": browsers, "job": job, "messages": messages}

    _OBS_KEYS = ("container_up", "fu_ready", "egress_ip", "egress_at", "google_cookies", "cookies_at",
                 "ext_version", "ext_route_key", "error", "at")

    async def record_observation(self, name: str, host_id: str, obs: Dict[str, Any]) -> None:
        now_dt = self.now()
        async with self.db.connect() as c:
            cur = await c.execute("SELECT obs, host_id FROM ultra_browsers WHERE name = ?", (name,))
            row = await cur.fetchone()
        if not row or row[1] != host_id:
            return
        try:
            merged = json.loads(row[0]) if row[0] else {}
        except ValueError:
            merged = {}
        for k in self._OBS_KEYS:
            if k in obs:
                v = obs[k]
                merged[k] = v if isinstance(v, (bool, int, float)) or v is None else str(v)[:200]
        async with self.db.connect(write=True) as c:
            await c.execute("UPDATE ultra_browsers SET obs = ?, last_seen = ? WHERE name = ?",
                            (json.dumps(merged), _iso(now_dt), name))
            await c.commit()

    async def refresh_health(self) -> None:
        """Derive health for live browsers (not in a lifecycle state) and write state changes + alerts."""
        now_dt = self.now()
        for b in await self.list_browsers():
            if b["state"] not in HEALTH_STATES + ("observing",):
                continue
            if b["desired_state"] != "running":
                continue
            try:
                obs = json.loads(b.get("obs") or "{}")
            except ValueError:
                obs = {}
            token = None
            if b.get("token_id") is not None:
                async with self.db.connect() as c:
                    cur = await c.execute("SELECT is_active, ban_reason FROM tokens WHERE id = ?", (int(b["token_id"]),))
                    r = await cur.fetchone()
                token = {"is_active": bool(r[0]), "ban_reason": r[1]} if r else None
            connected = None
            rk = b.get("route_key")
            if rk is None and token is not None:
                async with self.db.connect() as c:
                    r = await (await c.execute("SELECT extension_route_key FROM tokens WHERE id = ?", (int(b["token_id"]),))).fetchone()
                rk = r[0] if r else None
            if self._ext_connected and rk:
                try:
                    connected = self._ext_connected(rk)
                except Exception:
                    connected = None
            state, detail, marks = derive_health(b, token, obs, connected, now_dt)
            async with self.db.connect(write=True) as c:
                await c.execute("UPDATE ultra_browsers SET cookies_absent_since = ?, auth_bad_since = ? WHERE name = ?",
                                (marks["cookies_absent_since"], marks["auth_bad_since"], b["name"]))
                await self._set_state(c, b["name"], state, detail)
                await c.commit()

    async def check_hosts(self) -> None:
        cutoff = _iso(self.now() - timedelta(seconds=HOST_SILENT_S))
        now = _iso(self.now())
        async with self.db.connect(write=True) as conn:
            rows = await (await conn.execute(
                "SELECT host_id FROM ultra_hosts WHERE state = 'ok' AND last_poll_at < ?", (cutoff,))).fetchall()
            for (host_id,) in rows:
                await conn.execute("UPDATE ultra_hosts SET state = 'silent', updated_at = ? WHERE host_id = ?", (now, host_id))
                await conn.execute("INSERT INTO ultra_alerts (browser, kind, text, created_at, next_try_at) VALUES (NULL, 'host_silent', ?, ?, ?)",
                                   (f"Ultra host {host_id}: agent silent for over {HOST_SILENT_S // 60} min", now, now))
            await conn.commit()

    async def schedule_updates(self) -> None:
        """Managed, running, healthy browsers on an older extension get an `update` job (drained first)."""
        if not slice_b_enabled() or not self._published_version:
            return
        latest = self._published_version()
        if not latest:
            return
        for b in await self.list_browsers():
            if b["mode"] != "managed" or b["desired_state"] != "running" or b["state"] not in ("ok", "degraded"):
                continue
            try:
                seen = json.loads(b.get("obs") or "{}").get("ext_version")
            except ValueError:
                seen = None
            if not seen or seen == latest:
                continue
            async with self.db.connect() as c:
                busy = await (await c.execute(
                    "SELECT 1 FROM ultra_jobs WHERE browser = ? AND (state IN ('draining', 'queued', 'leased') "
                    "OR (kind = 'update' AND updated_at > ?))",
                    (b["name"], _iso(self.now() - timedelta(minutes=30))))).fetchone()
            if busy:
                continue
            try:
                await self.request_job(b["name"], "update", by="updater")
                debug_logger.op_warning(f"[ULTRA] {b['name']}: extension {seen} → {latest} update requested")
            except UltraError:
                pass

    async def tick(self) -> None:
        for step in (self.expire_leases, self.process_drains, self.check_hosts, self.refresh_health,
                     self._finalize_all, self.schedule_updates):
            try:
                await step()
            except Exception as e:
                debug_logger.op_warning(f"[ULTRA] coordinator step {step.__name__} failed: {type(e).__name__}: {e}")

    async def _finalize_all(self) -> None:
        async with self.db.connect() as c:
            rows = await (await c.execute(
                "SELECT name FROM ultra_browsers WHERE mode = 'managed' AND onboarding_hold = 1 AND state = 'onboarding'")).fetchall()
        for (name,) in rows:
            await self.finalize_onboarding(name)

    # ------------------------------------------------------------------ status for the dashboard

    async def status(self) -> Dict[str, Any]:
        browsers = []
        async with self.db.connect() as c:
            for b in await self.list_browsers():
                b.pop("sealed_password", None)
                try:
                    b["obs"] = json.loads(b.get("obs") or "{}")
                except ValueError:
                    b["obs"] = {}
                if b.get("token_id") is not None:
                    cur = await c.execute(
                        "SELECT t.is_active, t.ban_reason, t.user_paygate_tier, t.ultra_hold, t.reserved_client, "
                        "COALESCE(s.today_image_count, 0), COALESCE(s.today_error_count, 0) FROM tokens t "
                        "LEFT JOIN token_stats s ON s.token_id = t.id WHERE t.id = ?", (int(b["token_id"]),))
                    r = await cur.fetchone()
                    if r:
                        b["token"] = {"is_active": bool(r[0]), "ban_reason": r[1], "tier": r[2], "hold": r[3] or "",
                                      "reserved_client": r[4] or "", "today_images": r[5], "today_errors": r[6]}
                a = await (await c.execute(
                    "SELECT attempt_id, state, step, challenge_id, challenge_kind, challenge_text, challenge_expires_at "
                    "FROM ultra_login_attempts WHERE browser = ? ORDER BY created_at DESC LIMIT 1", (b["name"],))).fetchone()
                if a:
                    b["attempt"] = dict(zip(("attempt_id", "state", "step", "challenge_id", "challenge_kind",
                                             "challenge_text", "challenge_expires_at"), a))
                jobs = await (await c.execute(
                    "SELECT id, kind, state, error, updated_at FROM ultra_jobs WHERE browser = ? ORDER BY created_at DESC LIMIT 5",
                    (b["name"],))).fetchall()
                b["jobs"] = [dict(zip(("id", "kind", "state", "error", "updated_at"), j)) for j in jobs]
                browsers.append(b)
            cur = await c.execute("SELECT * FROM ultra_ports ORDER BY port")
            ports = [dict(zip([d[0] for d in cur.description], r)) for r in await cur.fetchall()]
            cur = await c.execute("SELECT * FROM ultra_hosts ORDER BY host_id")
            hosts = [dict(zip([d[0] for d in cur.description], r)) for r in await cur.fetchall()]
            unsent = (await (await c.execute("SELECT COUNT(*) FROM ultra_alerts WHERE sent_at IS NULL")).fetchone())[0]
        return {
            "slice_b_enabled": slice_b_enabled(),
            "vault_ready": self.vault_ok(),
            "agent_token_set": bool(os.environ.get("ULTRA_AGENT_TOKEN", "").strip()),
            "telegram_set": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip()),
            "unsent_alerts": unsent,
            "hosts": hosts, "ports": ports, "browsers": browsers,
        }

    # ------------------------------------------------------------------ background loops

    def start(self) -> None:
        async def _loop(fn, every):
            await asyncio.sleep(5)
            while True:
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    debug_logger.op_warning(f"[ULTRA] {fn.__name__} loop error: {type(e).__name__}: {e}")
                await asyncio.sleep(every)
        self._tasks = [asyncio.create_task(_loop(self.tick, 10)), asyncio.create_task(_loop(self.send_pending_alerts, 15))]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []


async def telegram_send(text: str) -> None:
    import httpx
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text, "disable_web_page_preview": True})
    if r.status_code != 200:
        raise RuntimeError(f"telegram answered {r.status_code}")
