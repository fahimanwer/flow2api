"""Token manager for Flow2API with AT auto-refresh"""
import asyncio
import hashlib
import re
from dataclasses import dataclass
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List
from ..core.database import Database
from ..core.config import config
from ..core.models import Token, Project
from ..core.logger import debug_logger
from ..core.monitoring import record_token_refresh
from .flow_client import FlowClient, FlowAPIError
from .proxy_manager import ProxyManager


@dataclass
class RefreshOutcome:
    """Result of an AT refresh attempt.

    ``reason`` is one of:
      - ``ok``                    success
      - ``st_expired``            ST/credential is invalid (401 / UNAUTHENTICATED)
      - ``at_stale``              ST still valid (session returns the user) but the
                                  access token inside it is dead and Google is not
                                  renewing it (API 401). Only a human re-login on the
                                  worker device fixes this — never treat as st_expired.
      - ``network``               transport/proxy/timeout failure (token is fine)
      - ``st_refresh_unavailable``ST refresh not possible in the current mode
      - ``unknown``               unclassified failure

    Truthiness mirrors ``success`` so any legacy ``if outcome:`` check that we
    might have missed still behaves correctly.
    """

    success: bool
    reason: str = "ok"
    # verified: the AT was accepted by the API (get_credits OK). A transport blip during
    # verification still PROMOTES the credential (legacy behavior) but is NOT verified —
    # nothing that requires proof (validation cache, health clear, re-enable, the
    # device's "healthy" signal) may key off an unverified success.
    verified: bool = True
    # fingerprint of the CANDIDATE access token the API rejected (at_stale only).
    candidate_fp: str = ""

    def __bool__(self) -> bool:
        return self.success


# Error fragments that indicate an *environmental* failure (reCAPTCHA / captcha /
# upstream anti-abuse), NOT a problem with the token itself. These must never
# count toward auto-disable: an otherwise-valid (often paid) token would be
# killed by a burst of transient IP/fingerprint reCAPTCHA rejections.
_ENVIRONMENTAL_ERROR_MARKERS = (
    "unusual_activity",
    "recaptcha",
    "captcha",
    "evaluation failed",
)


def _is_environmental_token_error(error_message: Optional[str]) -> bool:
    """Return True if the error reflects environment/anti-bot, not token health."""
    if not error_message:
        return False
    lowered = error_message.lower()
    return any(marker in lowered for marker in _ENVIRONMENTAL_ERROR_MARKERS)


# Upstream capacity / captcha-SOLVER outages: not the account's fault AND not its
# IP reputation either — so unlike environmental errors these get NO reCAPTCHA
# cooldown (pausing an account for Flow being busy or a solver outage is wrong)
# and NO auto-disable count. Absorbed from upstream's _should_count_token_error.
_CAPACITY_ERROR_MARKERS = (
    "too much traffic",
    "error_no_slot_available",
    "打码服务资源不足",
    "打码服务资源阻塞",
    "没有可用的token进行",
    "yescaptcha",
    "capsolver",
    "capmonster",
    "ezcaptcha",
)


def _is_capacity_or_solver_error(error_message: Optional[str]) -> bool:
    """True if the error is upstream capacity / captcha-provider trouble."""
    if not error_message:
        return False
    lowered = error_message.lower()
    return any(marker in lowered for marker in _CAPACITY_ERROR_MARKERS)


# Error fragments that mean the account hit a usage quota (daily/per-model). Not a
# token-health problem — the token is simply out of allowance for now, so it is
# cooled down (skipped by the load balancer) and auto-recovered later, NOT counted
# toward the auto-disable threshold.
_QUOTA_ERROR_MARKERS = (
    "per_model_daily_quota",
    "daily_quota_reached",
    "quota_reached",
    "resource has been exhausted",
    "resource_exhausted",
)


def _is_quota_error(error_message: Optional[str]) -> bool:
    """Return True if the error is a usage-quota exhaustion (not token health)."""
    if not error_message:
        return False
    lowered = error_message.lower()
    return any(marker in lowered for marker in _QUOTA_ERROR_MARKERS)


# Fragments that specifically mean the DAILY per-model allowance is spent (resets at
# midnight Pacific). These get cooled until the PT reset — not just a few minutes — so
# we stop re-probing an exhausted model all day (repeated 429s look like abuse). Generic
# resource_exhausted (which can also be a short rate-limit) keeps the shorter cooldown.
_DAILY_QUOTA_MARKERS = (
    "per_model_daily_quota",
    "daily_quota_reached",
    "daily_quota",
)


def _is_daily_quota_error(error_message: Optional[str]) -> bool:
    if not error_message:
        return False
    lowered = error_message.lower()
    return any(marker in lowered for marker in _DAILY_QUOTA_MARKERS)


# Aspect-ratio / resolution suffixes stripped to get a model's quota family.
_MODEL_VARIANT_SUFFIXES = (
    "-4k", "-2k", "_4k", "_2k", "_1080p",
    "-landscape", "-portrait", "-square", "-four-three", "-three-four",
)


def model_quota_key(model: Optional[str]) -> str:
    """Reduce an API model id to its quota family (the underlying Flow model).

    Flow quota is PER MODEL: every aspect-ratio / resolution variant of one model
    (e.g. all gemini-3.0-pro-image-*) shares a single daily quota. So the per-model
    cooldown is keyed by the base family, not the full variant id.
    """
    key = (model or "").strip().lower()
    changed = True
    while changed and key:
        changed = False
        for suf in _MODEL_VARIANT_SUFFIXES:
            if key.endswith(suf):
                key = key[: -len(suf)]
                changed = True
    return key


class TokenManager:
    """Token lifecycle manager with AT auto-refresh"""

    def __init__(self, db: Database, flow_client: FlowClient):
        self.db = db
        self.flow_client = flow_client
        self._refresh_lock_guard = asyncio.Lock()
        self._project_lock_guard = asyncio.Lock()
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._project_locks: dict[int, asyncio.Lock] = {}
        self._refresh_futures: dict[int, asyncio.Task] = {}
        # Per-(token, model-family) quota cooldown. Quota is per model, so a token
        # that exhausted one model stays usable for the others — only the specific
        # model is paused here (in memory; cleared on restart). See [[per-model quota]].
        self._model_quota_until: dict[tuple, datetime] = {}
        # Upstream protocol-login: AT-validation cache + background ST refresher handle.
        self._at_validation_cache: dict[int, float] = {}
        self._protocol_refresher_task: Optional[asyncio.Task] = None
        # Per-account reCAPTCHA/anti-bot progressive cooldown: token_id -> (until, strikes).
        # Retained after expiry (NOT popped) so strikes escalate on a repeat failure;
        # cleared only on success, or decayed inside mark_recaptcha_failure.
        self._recaptcha_cd: dict[int, tuple] = {}
        # Account-health cooldowns keyed by (token_id, kind) — see AT_STALE_* below.
        # Entry: {until, strikes, first_failure_at, last_failure_at, fingerprint}.
        self._health_cd: dict[tuple, dict] = {}
        # One-time lazy load of persisted cooldowns (survives restarts). See #3.
        self._quota_loaded = False

    # at_stale: the session cookie is alive but the access token Google embeds in it is
    # dead and NOT being renewed (API 401). Escalating pause instead of a disable —
    # each strike also asks the worker device to reload Labs (Google may renew on a
    # real navigation). Disable (auto_at_stale) only after AT_STALE_DISABLE_STRIKES
    # failures inside AT_STALE_DISABLE_WINDOW with NO verified success in between; any
    # verified success clears the entry, so a healthy account can never be starved.
    AT_STALE_BACKOFF_SECONDS = (600, 1800, 3600)   # strike 1, 2, 3+
    AT_STALE_DISABLE_STRIKES = 3
    AT_STALE_DISABLE_WINDOW = timedelta(hours=2)
    # Only these bans may be lifted by a worker session push — and only after the pushed
    # credential has been VERIFIED against the API. auto_error / 429 are not proven
    # healed by a working login, so a push never clears them.
    AUTH_DISABLE_REASONS = ("auto_st_expired", "auto_at_stale")

    # Short cooldown for a generic/ambiguous rate-limit (could be transient).
    MODEL_QUOTA_COOLDOWN_MINUTES = 30

    # Per-ACCOUNT progressive cooldown for reCAPTCHA / "unusual activity" failures.
    # Unlike quota (per-model), an anti-bot rejection reflects the account's IP/session
    # REPUTATION, so the WHOLE token is paused across every model. Each consecutive strike
    # escalates the pause (seconds, capped at the last value); a success resets it. Backing
    # off — instead of re-picking a flagged account every few seconds and racking up more
    # rejections — is what lets Google's reCAPTCHA score recover and stops the spiral.
    RECAPTCHA_BACKOFF_SECONDS = (60, 180, 300, 600, 900, 1800, 3600, 7200)  # strike 1..8+, capped at 2h
    # If the previous cooldown ended more than this ago, the next failure is treated as a
    # fresh strike-1 (an occasional blip) rather than a continuation of an old burst.
    RECAPTCHA_STRIKE_DECAY = timedelta(minutes=30)

    def _next_pt_daily_reset(self) -> datetime:
        """Next Google-Flow daily-quota reset = next midnight America/Los_Angeles (PT),
        returned as UTC with a small margin so we retry AFTER the reset lands."""
        try:
            from zoneinfo import ZoneInfo
            pt = ZoneInfo("America/Los_Angeles")
            now_pt = datetime.now(pt)
            next_midnight_pt = (now_pt + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return next_midnight_pt.astimezone(timezone.utc) + timedelta(minutes=2)
        except Exception:
            # tz data unavailable — fall back to a long cooldown (worst case ~24h to PT
            # midnight; 12h avoids all-day re-probing without over-holding a false positive).
            return datetime.now(timezone.utc) + timedelta(hours=12)

    async def _ensure_quota_loaded(self):
        """Lazily load ALL persisted cooldowns into memory once (survives restart):
        per-model quota cooldowns AND per-account reCAPTCHA cooldowns."""
        if self._quota_loaded:
            return
        self._quota_loaded = True  # set first so a load error doesn't retry every call
        try:
            rows = await self.db.get_active_model_quota_cooldowns()
            for token_id, model_key, until_iso in rows:
                try:
                    until = datetime.fromisoformat(until_iso)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    self._model_quota_until[(token_id, model_key)] = until
                except Exception:
                    continue
            if rows:
                debug_logger.event(f"[MODEL_QUOTA] loaded {len(rows)} persisted cooldown(s)")
        except Exception as e:
            debug_logger.op_warning(f"[MODEL_QUOTA] could not load persisted cooldowns: {e}")
        try:
            rc_rows = await self.db.get_active_recaptcha_cooldowns()
            for token_id, until_iso, strikes in rc_rows:
                try:
                    until = datetime.fromisoformat(until_iso)
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    self._recaptcha_cd[token_id] = (until, int(strikes))
                except Exception:
                    continue
            if rc_rows:
                debug_logger.event(f"[RECAPTCHA_CD] loaded {len(rc_rows)} persisted cooldown(s)")
        except Exception as e:
            debug_logger.op_warning(f"[RECAPTCHA_CD] could not load persisted cooldowns: {e}")
        try:
            h_rows = await self.db.get_recent_token_health_cooldowns()
            for token_id, kind, until_iso, strikes, first_iso, last_iso, fp in h_rows:
                try:
                    self._health_cd[(int(token_id), str(kind))] = {
                        "until": self._parse_iso_utc(until_iso),
                        "strikes": int(strikes or 1),
                        "first_failure_at": self._parse_iso_utc(first_iso),
                        "last_failure_at": self._parse_iso_utc(last_iso),
                        "fingerprint": fp,
                    }
                except Exception:
                    continue
            if h_rows:
                debug_logger.event(f"[HEALTH_CD] loaded {len(h_rows)} persisted cooldown(s)")
        except Exception as e:
            debug_logger.op_warning(f"[HEALTH_CD] could not load persisted cooldowns: {e}")

    @staticmethod
    def _parse_iso_utc(value) -> Optional[datetime]:
        if not value:
            return None
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    async def mark_model_quota_exhausted(
        self, token_id: int, model: Optional[str], error_message: Optional[str] = None
    ):
        """Pause ONLY this model for this token after a per-model quota error.

        A DAILY quota hit is paused until the next Pacific-time reset (so we don't keep
        probing an exhausted model every 30 min all day and rack up 429s that look like
        abusive traffic). Anything ambiguous keeps the short cooldown. Persisted so the
        pause survives a redeploy/restart.
        """
        key = model_quota_key(model)
        if not key:
            return
        if _is_daily_quota_error(error_message):
            until = self._next_pt_daily_reset()
            hrs = max(0.0, (until - datetime.now(timezone.utc)).total_seconds() / 3600)
            scope = f"until daily reset (~{hrs:.1f}h, midnight PT)"
        else:
            until = datetime.now(timezone.utc) + timedelta(minutes=self.MODEL_QUOTA_COOLDOWN_MINUTES)
            scope = f"{self.MODEL_QUOTA_COOLDOWN_MINUTES}m"
        self._model_quota_until[(token_id, key)] = until
        try:
            await self.db.upsert_model_quota_cooldown(token_id, key, until)
        except Exception as e:
            debug_logger.op_warning(f"[MODEL_QUOTA] could not persist cooldown: {e}")
        debug_logger.event(
            f"[MODEL_QUOTA] token={token_id} model='{key}' quota exhausted; paused {scope} "
            f"(other models stay active)"
        )

    def is_model_quota_exhausted(self, token_id: int, model: Optional[str]) -> bool:
        """True if this token's quota for the given model's family is on cooldown."""
        key = model_quota_key(model)
        if not key:
            return False
        until = self._model_quota_until.get((token_id, key))
        if not until:
            return False
        if datetime.now(timezone.utc) >= until:
            self._model_quota_until.pop((token_id, key), None)
            return False
        return True

    async def mark_recaptcha_failure(self, token_id: int) -> None:
        """Progressive per-account cooldown after a reCAPTCHA / unusual-activity failure.

        One strike per FAILURE ROUND, not per failed request. Escalates the pause with
        each consecutive round (RECAPTCHA_BACKOFF_SECONDS) and pauses the WHOLE token
        across every model (anti-bot reputation is account/IP-level, not per-model).

        Concurrency-safe: N requests that land on one account at once and all fail together
        are the SAME reputation event — they'd otherwise stack strikes 1→N and jump straight
        to the cap. Any failure that arrives while the account is ALREADY cooling (now <
        prev_until) is treated as an in-flight straggler of the current round and does NOT
        escalate. Escalation happens only on a genuine RETRY that fails again after the
        cooldown lapsed (still within RECAPTCHA_STRIKE_DECAY); a long quiet gap resets to
        strike 1. The read→write below has no await, so asyncio can't interleave it — the
        first failure's strike is committed before any concurrent sibling is scheduled.
        Persisted so a redeploy doesn't forget and re-hammer a flagged account. Cleared on
        success.
        """
        now = datetime.now(timezone.utc)
        prev = self._recaptcha_cd.get(token_id)
        if prev:
            prev_until, prev_strikes = prev
            if now < prev_until:
                # Still cooling from a strike set moments ago → concurrent/in-flight
                # straggler for the SAME round. Count the burst as one strike: no escalate,
                # no shortening of the window.
                debug_logger.log_info(
                    f"[RECAPTCHA_CD] token={token_id} concurrent failure inside active "
                    f"cooldown; strike stays {prev_strikes} (not escalating)"
                )
                return
            # Cooldown already lapsed: escalate only if it retried and failed again within
            # the recent-burst window; a long quiet gap resets to strike 1.
            strikes = prev_strikes if now < prev_until + self.RECAPTCHA_STRIKE_DECAY else 0
        else:
            strikes = 0
        strikes += 1
        seconds = self.RECAPTCHA_BACKOFF_SECONDS[
            min(strikes, len(self.RECAPTCHA_BACKOFF_SECONDS)) - 1
        ]
        until = now + timedelta(seconds=seconds)
        self._recaptcha_cd[token_id] = (until, strikes)
        try:
            await self.db.upsert_recaptcha_cooldown(token_id, until, strikes)
        except Exception as e:
            debug_logger.op_warning(f"[RECAPTCHA_CD] could not persist cooldown: {e}")
        debug_logger.event(
            f"[RECAPTCHA_CD] token={token_id} strike={strikes} paused {seconds}s "
            f"(reCAPTCHA/unusual_activity — whole account cooling down)"
        )

    def is_recaptcha_cooldown(self, token_id: int) -> bool:
        """True while this account is inside its reCAPTCHA cooldown window.

        Does NOT pop an expired entry (unlike quota): the strike count is retained so a
        repeat failure escalates instead of restarting at strike 1. It is cleared only by
        clear_recaptcha_cooldown (on success) or decayed inside mark_recaptcha_failure.
        """
        entry = self._recaptcha_cd.get(token_id)
        if not entry:
            return False
        return datetime.now(timezone.utc) < entry[0]

    async def clear_recaptcha_cooldown(self, token_id: int) -> None:
        """Reset a token's reCAPTCHA strike/cooldown (called on a successful generation)."""
        if self._recaptcha_cd.pop(token_id, None) is not None:
            try:
                await self.db.delete_recaptcha_cooldown(token_id)
            except Exception as e:
                debug_logger.op_warning(f"[RECAPTCHA_CD] could not clear cooldown: {e}")

    # ---------------- account-health cooldowns (at_stale) ----------------

    @staticmethod
    def _credential_fingerprint(at: Optional[str]) -> str:
        """Short stable id of an access token — used to tell 'same dead credential' from
        'a newer credential landed' without ever logging the token itself."""
        return hashlib.sha256((at or "").encode("utf-8")).hexdigest()[:16] if at else ""

    def is_health_cooldown(self, token_id: int, kind: Optional[str] = None) -> bool:
        """True while the account is paused for `kind` (any kind if None). Expired entries
        are kept (strike memory), they just stop gating."""
        now = datetime.now(timezone.utc)
        for (tid, k), entry in self._health_cd.items():
            if tid != token_id or (kind is not None and k != kind):
                continue
            until = entry.get("until")
            if until and now < until:
                return True
        return False

    def health_cooldown_reason(self, token_id: int) -> Optional[str]:
        if self.is_health_cooldown(token_id, "at_stale"):
            return "Google login needs attention (access token dead) — device must sign out/in"
        if self.is_health_cooldown(token_id, "throttle"):
            return "Google throttle cooldown"
        return None

    async def _note_verified_at(self, token_id: int, at: Optional[str]) -> None:
        """Called whenever an AT is PROVEN to work (credits OK / generation OK). Clears an
        at_stale entry only if the verified credential is a DIFFERENT one from the one
        that failed — an older in-flight success on the same dead AT can't mask a newer
        failure. Pass at=None for a generation success (the AT in use must be live)."""
        key = (int(token_id), "at_stale")
        entry = self._health_cd.get(key)
        if not entry:
            return
        if at is not None and entry.get("fingerprint") and self._credential_fingerprint(at) == entry["fingerprint"]:
            return
        self._health_cd.pop(key, None)
        try:
            await self.db.delete_token_health_cooldown(token_id, "at_stale")
        except Exception as e:
            debug_logger.op_warning(f"[HEALTH_CD] could not clear at_stale: {e}")
        debug_logger.event(f"[HEALTH_CD] token={token_id} at_stale cleared (credential verified)")

    async def _record_at_stale(self, token_id: int, candidate_fp: str) -> dict:
        """Leader-only: one strike for this failure round. Escalating pause; returns the
        entry plus `disable_due` = True when strikes reached the threshold inside the
        rolling window with no verified success in between (the window restarts whenever
        the entry was cleared by a success, so a healthy account cannot accumulate)."""
        now = datetime.now(timezone.utc)
        key = (int(token_id), "at_stale")
        prev = self._health_cd.get(key)
        fp = candidate_fp or ""
        if prev and prev.get("until") and now < prev["until"]:
            # Still inside the current pause → same round (concurrent straggler); no escalation.
            return {**prev, "disable_due": False, "escalated": False}
        first = (prev or {}).get("first_failure_at") or now
        strikes = int((prev or {}).get("strikes", 0)) + 1
        if now - first > self.AT_STALE_DISABLE_WINDOW:
            # Old burst aged out → fresh window.
            first, strikes = now, 1
        seconds = self.AT_STALE_BACKOFF_SECONDS[min(strikes, len(self.AT_STALE_BACKOFF_SECONDS)) - 1]
        entry = {
            "until": now + timedelta(seconds=seconds),
            "strikes": strikes,
            "first_failure_at": first,
            "last_failure_at": now,
            "fingerprint": fp,
        }
        self._health_cd[key] = entry
        try:
            await self.db.upsert_token_health_cooldown(
                token_id, "at_stale", entry["until"], strikes, first, now, fp
            )
        except Exception as e:
            debug_logger.op_warning(f"[HEALTH_CD] could not persist at_stale: {e}")
        disable_due = strikes >= self.AT_STALE_DISABLE_STRIKES and (now - first) <= self.AT_STALE_DISABLE_WINDOW
        debug_logger.event(
            f"[HEALTH_CD] token={token_id} at_stale strike={strikes} paused {seconds}s "
            f"(session alive, access token dead — needs device re-login){' → DISABLE' if disable_due else ''}"
        )
        return {**entry, "disable_due": disable_due, "escalated": True}

    async def _get_token_lock(
        self,
        lock_map: dict[int, asyncio.Lock],
        guard: asyncio.Lock,
        token_id: int,
    ) -> asyncio.Lock:
        """按 token 维度获取锁，避免不同 token 之间串行阻塞。"""
        async with guard:
            lock = lock_map.get(token_id)
            if lock is None:
                lock = asyncio.Lock()
                lock_map[token_id] = lock
            return lock

    def _get_project_pool_size(self) -> int:
        """读取当前生效的单 Token 项目池大小配置。"""
        try:
            return max(1, min(50, int(config.personal_project_pool_size or 4)))
        except Exception:
            return 4

    def _sort_projects(self, projects: List[Project]) -> List[Project]:
        """Sort projects in a stable order for round-robin selection."""
        return sorted(projects, key=lambda project: (project.id or 0, project.project_id))

    def _normalize_project_name_base(self, project_name: Optional[str] = None) -> str:
        """Normalize a project base name for pooled creation."""
        raw_name = (project_name or "").strip()
        if raw_name:
            parts = raw_name.rsplit(" ", 1)
            if len(parts) == 2 and parts[1].startswith("P") and parts[1][1:].isdigit():
                return parts[0]
            return raw_name
        return datetime.now().strftime("%b %d - %H:%M")

    def _build_project_name(self, pool_index: int, base_name: Optional[str] = None) -> str:
        """Build a project name for the pool."""
        normalized_base = self._normalize_project_name_base(base_name)
        return f"{normalized_base} P{pool_index}"

    def _normalize_protocol_mode(self, value: Optional[str]) -> str:
        mode = (value or "session").strip().lower()
        return "protocol" if mode == "protocol" else "session"

    def _normalize_refresh_interval(self, value: Optional[int]) -> int:
        try:
            return max(1, int(value if value is not None else 120))
        except Exception:
            return 120

    def _parse_at_expires(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _as_utc(self, value: Optional[datetime]) -> Optional[datetime]:
        if not value:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _normalize_token_proxy_url(self, proxy_url: Optional[str]) -> str:
        raw = (proxy_url or "").strip()
        if not raw:
            return ""
        proxy_manager = getattr(self.flow_client, "proxy_manager", None)
        if proxy_manager and hasattr(proxy_manager, "normalize_proxy_url"):
            normalized = proxy_manager.normalize_proxy_url(raw)
            return normalized or ""
        return raw

    def _clear_at_validation_cache(self, token_id: int) -> None:
        self._at_validation_cache.pop(int(token_id), None)

    def _mark_at_valid(self, token_id: int, ttl_seconds: int = 300) -> None:
        self._at_validation_cache[int(token_id)] = time.monotonic() + max(30, int(ttl_seconds or 300))

    def _has_recent_at_validation(self, token_id: int) -> bool:
        expires_at = float(self._at_validation_cache.get(int(token_id), 0.0) or 0.0)
        if expires_at <= time.monotonic():
            self._clear_at_validation_cache(token_id)
            return False
        return True

    async def _flow_call_for_token(self, token: Token, call):
        previous_fingerprint = self.flow_client.get_request_fingerprint()
        proxy_url = (getattr(token, "proxy_url", "") or "").strip()
        if proxy_url:
            next_fingerprint = dict(previous_fingerprint or {})
            next_fingerprint["proxy_url"] = proxy_url
            self.flow_client._set_request_fingerprint(next_fingerprint)
        try:
            return await call()
        finally:
            self.flow_client._set_request_fingerprint(previous_fingerprint)

    async def _st_to_at_for_token(self, token: Token, st: str) -> Dict[str, Any]:
        return await self._flow_call_for_token(token, lambda: self.flow_client.st_to_at(st))

    async def _get_credits_for_token(self, token: Token, at: str) -> Dict[str, Any]:
        return await self._flow_call_for_token(token, lambda: self.flow_client.get_credits(at))

    async def get_personal_warmup_project_ids(
        self,
        tokens: Optional[List[Token]] = None,
        limit: Optional[int] = None,
    ) -> List[str]:
        """返回 personal 模式启动时建议预热的项目 ID 列表。"""
        token_list = tokens if tokens is not None else await self.get_all_tokens()
        pool_size = self._get_project_pool_size()
        warmup_ids: List[str] = []
        seen_projects: set[str] = set()

        try:
            warmup_limit = None if limit is None else max(1, int(limit))
        except Exception:
            warmup_limit = None

        for token in token_list:
            if not token or not token.is_active:
                continue

            candidate_ids: List[str] = []
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                candidate_ids.append(current_project_id)

            projects = [project for project in await self.db.get_projects_by_token(token.id) if project.is_active]
            for project in self._sort_projects(projects):
                project_id = str(project.project_id or "").strip()
                if project_id and project_id not in candidate_ids:
                    candidate_ids.append(project_id)

            for project_id in candidate_ids[:pool_size]:
                if project_id in seen_projects:
                    continue
                seen_projects.add(project_id)
                warmup_ids.append(project_id)
                if warmup_limit is not None and len(warmup_ids) >= warmup_limit:
                    return warmup_ids

        return warmup_ids

    async def _create_project_for_token(self, token: Token, pool_index: int, base_name: Optional[str] = None) -> Project:
        """Create a new pooled project for a token and persist it."""
        project_name = self._build_project_name(pool_index, base_name)
        project_id = await self.flow_client.create_project(token.st, project_name)
        debug_logger.log_info(
            f"[PROJECT] Created pooled project for token {token.id}: {project_name} ({project_id})"
        )
        project = Project(
            project_id=project_id,
            token_id=token.id,
            project_name=project_name,
        )
        project.id = await self.db.add_project(project)
        return project

    def _select_next_project(self, token: Token, projects: List[Project]) -> Project:
        """Select the next project from the pool in round-robin order."""
        ordered_projects = self._sort_projects(projects)
        if not ordered_projects:
            raise ValueError("No available projects for token")

        if len(ordered_projects) == 1:
            return ordered_projects[0]

        if token.current_project_id:
            for index, project in enumerate(ordered_projects):
                if project.project_id == token.current_project_id:
                    return ordered_projects[(index + 1) % len(ordered_projects)]

        return ordered_projects[0]

    # ========== Token CRUD ==========

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        return await self.db.get_all_tokens()

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        return await self.db.get_active_tokens()

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        return await self.db.get_token(token_id)

    async def delete_token(self, token_id: int):
        """Delete token"""
        token = await self.db.get_token(token_id)
        project_ids: List[str] = []
        if token:
            current_project_id = str(token.current_project_id or "").strip()
            if current_project_id:
                project_ids.append(current_project_id)

        for project in await self.db.get_projects_by_token(token_id):
            project_id = str(project.project_id or "").strip()
            if project_id and project_id not in project_ids:
                project_ids.append(project_id)

        await self.db.delete_token(token_id)

        refresh_task = self._refresh_futures.pop(token_id, None)
        if refresh_task and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._refresh_locks.pop(token_id, None)
        self._project_locks.pop(token_id, None)

        if config.captcha_method == "personal" and project_ids:
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                for project_id in project_ids:
                    await service.stop_resident_mode(project_id)
            except Exception as e:
                debug_logger.log_warning(f"[DELETE_TOKEN] 清理 personal 浏览器状态失败: {e}")

    async def enable_token(self, token_id: int):
        """Enable a token and reset error count"""
        # Enable the token
        await self.db.update_token(token_id, is_active=True, ban_reason=None, banned_at=None)
        # Reset error count when enabling (only reset total error_count, keep today_error_count)
        await self.db.reset_error_count(token_id)
        # An explicit enable (admin or verified push) wipes health pauses + strike memory.
        self._health_cd.pop((int(token_id), "at_stale"), None)
        try:
            await self.db.delete_token_health_cooldown(token_id, "at_stale")
        except Exception as e:
            debug_logger.op_warning(f"[HEALTH_CD] could not clear on enable: {e}")

    # Disable reasons the system set automatically. A token disabled for one of these
    # is eligible to be auto-re-enabled when a fresh session is pushed (the credential
    # is healthy again). A MANUAL disable leaves ban_reason NULL and is never auto-revived.
    AUTO_DISABLE_REASONS = ("auto_st_expired", "auto_error", "429_rate_limit")

    async def disable_token(self, token_id: int, reason: Optional[str] = None):
        """Disable a token. `reason` records WHY: pass an auto_* reason for automatic
        disables (so a later session push can auto-recover it); leave None for a manual
        disable (ban_reason stays NULL → never auto-revived)."""
        fields = {"is_active": False}
        if reason is not None:
            fields["ban_reason"] = reason
            fields["banned_at"] = datetime.now(timezone.utc)
        await self.db.update_token(token_id, **fields)

    # ========== Token添加 (支持Project创建) ==========

    async def add_token(
        self,
        st: str,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: bool = True,
        video_enabled: bool = True,
        image_concurrency: int = -1,
        video_concurrency: int = -1,
        captcha_proxy_url: Optional[str] = None,
        extension_route_key: Optional[str] = None,
        protocol_mode: str = "session",
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: bool = True,
        refresh_interval_minutes: int = 120,
        is_active: bool = True,
        ban_reason: Optional[str] = None,
        session_result: Optional[dict] = None,
    ) -> Token:
        """Add a new token and prepare its pooled projects. is_active=False creates the
        row disabled from the start (caller verified the credential is dead) so a dead
        access token never has an active window."""
        existing_token = await self.db.get_token_by_st(st)
        if existing_token:
            raise ValueError(f"Token ??????: {existing_token.email}?")

        debug_logger.log_info(f"[ADD_TOKEN] Converting ST to AT...")
        try:
            # A caller that already fetched (and verified) the session passes it in, so the
            # AT we store is exactly the one it probed — never a second, unprobed mint.
            result = session_result if session_result else await self.flow_client.st_to_at(st)
            at = result["access_token"]
            expires = result.get("expires")
            user_info = result.get("user", {})
            email = user_info.get("email", "")
            name = user_info.get("name", email.split("@")[0] if email else "")
            at_expires = None
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                except Exception:
                    pass
        except Exception as e:
            raise ValueError(f"ST?AT??: {str(e)}")

        try:
            credits_result = await self.flow_client.get_credits(at)
            credits = credits_result.get("credits", 0)
            user_paygate_tier = credits_result.get("userPaygateTier")
        except Exception:
            credits = 0
            user_paygate_tier = None

        base_project_name = self._normalize_project_name_base(project_name)
        project_pool_size = self._get_project_pool_size()
        pooled_projects: List[Project] = []

        if project_id:
            first_project_name = self._build_project_name(1, base_project_name)
            debug_logger.log_info(f"[ADD_TOKEN] Using provided project_id as pooled project #1: {project_id}")
            pooled_projects.append(Project(
                project_id=project_id,
                token_id=0,
                project_name=first_project_name,
                tool_name="PINHOLE"
            ))
        else:
            try:
                first_project_name = self._build_project_name(1, base_project_name)
                first_project_id = await self.flow_client.create_project(st, first_project_name)
                debug_logger.log_info(f"[ADD_TOKEN] Created pooled project #1: {first_project_name} (ID: {first_project_id})")
                pooled_projects.append(Project(
                    project_id=first_project_id,
                    token_id=0,
                    project_name=first_project_name,
                    tool_name="PINHOLE"
                ))
            except Exception as e:
                raise ValueError(f"??????: {str(e)}")

        token = Token(
            st=st,
            at=at,
            at_expires=at_expires,
            email=email,
            name=name,
            remark=remark,
            is_active=is_active,
            ban_reason=ban_reason,
            banned_at=(datetime.now(timezone.utc) if (not is_active and ban_reason) else None),
            credits=credits,
            user_paygate_tier=user_paygate_tier,
            current_project_id=pooled_projects[0].project_id,
            current_project_name=pooled_projects[0].project_name,
            image_enabled=image_enabled,
            video_enabled=video_enabled,
            image_concurrency=image_concurrency,
            video_concurrency=video_concurrency,
            captcha_proxy_url=captcha_proxy_url,
            extension_route_key=extension_route_key,
            protocol_mode=self._normalize_protocol_mode(protocol_mode),
            google_cookies=(google_cookies or "").strip(),
            login_account=(login_account or "").strip(),
            login_password=login_password or "",
            proxy_url=self._normalize_token_proxy_url(proxy_url),
            auto_refresh_enabled=bool(auto_refresh_enabled),
            refresh_interval_minutes=self._normalize_refresh_interval(refresh_interval_minutes),
        )

        token_id = await self.db.add_token(token)
        token.id = token_id

        pooled_projects[0].token_id = token_id
        pooled_projects[0].id = await self.db.add_project(pooled_projects[0])

        while len(pooled_projects) < project_pool_size:
            new_project = await self._create_project_for_token(token, len(pooled_projects) + 1, base_project_name)
            pooled_projects.append(new_project)

        debug_logger.log_info(
            f"[ADD_TOKEN] Token added successfully (ID: {token_id}, Email: {email}, pooled_projects={len(pooled_projects)})"
        )
        return token
    async def update_token(
        self,
        token_id: int,
        st: Optional[str] = None,
        at: Optional[str] = None,
        at_expires: Optional[datetime] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        remark: Optional[str] = None,
        image_enabled: Optional[bool] = None,
        video_enabled: Optional[bool] = None,
        image_concurrency: Optional[int] = None,
        video_concurrency: Optional[int] = None,
        captcha_proxy_url: Optional[str] = None,
        extension_route_key: Optional[str] = None,
        protocol_mode: Optional[str] = None,
        google_cookies: Optional[str] = None,
        login_account: Optional[str] = None,
        login_password: Optional[str] = None,
        proxy_url: Optional[str] = None,
        auto_refresh_enabled: Optional[bool] = None,
        refresh_interval_minutes: Optional[int] = None,
    ):
        """Update token (支持修改project_id和project_name)

        当用户编辑保存token时，如果token未过期，自动清空429禁用状态
        """
        update_fields = {}
        credential_updated = any(value is not None for value in (st, at, at_expires))

        if st is not None:
            update_fields["st"] = st
        if at is not None:
            update_fields["at"] = at
        if at_expires is not None:
            update_fields["at_expires"] = at_expires
        if project_id is not None:
            update_fields["current_project_id"] = project_id
        if project_name is not None:
            update_fields["current_project_name"] = project_name
        if remark is not None:
            update_fields["remark"] = remark
        if image_enabled is not None:
            update_fields["image_enabled"] = image_enabled
        if video_enabled is not None:
            update_fields["video_enabled"] = video_enabled
        if image_concurrency is not None:
            update_fields["image_concurrency"] = image_concurrency
        if video_concurrency is not None:
            update_fields["video_concurrency"] = video_concurrency
        if captcha_proxy_url is not None:
            update_fields["captcha_proxy_url"] = captcha_proxy_url
        if extension_route_key is not None:
            update_fields["extension_route_key"] = extension_route_key
        if protocol_mode is not None:
            update_fields["protocol_mode"] = self._normalize_protocol_mode(protocol_mode)
        if google_cookies is not None:
            update_fields["google_cookies"] = google_cookies.strip()
        if login_account is not None:
            update_fields["login_account"] = login_account.strip()
        if login_password is not None:
            update_fields["login_password"] = login_password
        if proxy_url is not None:
            update_fields["proxy_url"] = self._normalize_token_proxy_url(proxy_url)
        if auto_refresh_enabled is not None:
            update_fields["auto_refresh_enabled"] = bool(auto_refresh_enabled)
        if refresh_interval_minutes is not None:
            update_fields["refresh_interval_minutes"] = self._normalize_refresh_interval(refresh_interval_minutes)

        # 检查token是否因429被禁用，如果是且未过期，则清空429状态
        # NOTE: no unconditional revive here. Re-enabling a disabled account is the
        # CALLER's policy decision — the plugin session-push path (admin.py) revives
        # auto-disabled accounts and honors auto_enable_on_update for manual ones.
        # An unconditional is_active=True here (upstream behavior) would resurrect
        # manually disabled accounts on every worker credential push.
        token = await self.db.get_token(token_id)

        # Legacy 429 clearing applies only to an EXPLICIT credential edit (admin saved a new
        # ST). Metadata-only updates (plugin protocol fields) must not turn a 429 ban into a
        # manual-looking one that a later push could lift.
        if credential_updated and token and token.ban_reason == "429_rate_limit":
            # 检查token是否过期
            is_expired = False
            if token.at_expires:
                now = datetime.now(timezone.utc)
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires
                is_expired = at_expires_aware <= now

            # 如果未过期，清空429禁用状态
            if not is_expired:
                debug_logger.log_info(f"[UPDATE_TOKEN] Token {token_id} 编辑保存，清空429禁用状态")
                update_fields["ban_reason"] = None
                update_fields["banned_at"] = None

        if update_fields:
            if credential_updated:
                self._clear_at_validation_cache(token_id)
            await self.db.update_token(token_id, **update_fields)

    # ========== AT自动刷新逻辑 (核心) ==========

    def _should_refresh_at(self, token: Token) -> bool:
        """根据当前 token 快照判断是否需要刷新 AT。"""
        if not token.at:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT不存在,需要刷新")
            return True

        if not token.at_expires:
            debug_logger.log_info(f"[AT_CHECK] Token {token.id}: AT过期时间未知,尝试刷新")
            return True

        now = datetime.now(timezone.utc)
        if token.at_expires.tzinfo is None:
            at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
        else:
            at_expires_aware = token.at_expires

        time_until_expiry = at_expires_aware - now
        if time_until_expiry.total_seconds() < 3600:
            debug_logger.log_info(
                f"[AT_CHECK] Token {token.id}: AT即将过期 "
                f"(剩余 {time_until_expiry.total_seconds():.0f} 秒),需要刷新"
            )
            return True

        return False

    def needs_at_refresh(self, token: Optional[Token]) -> bool:
        """供调度层快速判断当前 token 是否大概率会触发 AT 刷新。"""
        if not token:
            return True
        return self._should_refresh_at(token)

    async def ensure_valid_token(
        self,
        token: Optional[Token],
        disable_on_failure: bool = True,
    ) -> Optional[Token]:
        """确保 token 的 AT 可用，并在必要时返回刷新后的最新对象。

        Args:
            token: 待校验的 token。
            disable_on_failure: 自动取流路径（负载均衡 / 生成）默认 True：
                凭证失效（ST 过期）时禁用该 token，使其退出可用池。
                网络/未知错误不会禁用（避免误杀正常账号）。
                手动管理动作（如刷新余额）应传入 False，永不禁用。
        """
        if not token:
            return None

        # Paused for a dead access token (at_stale): don't burn an API call, the load
        # balancer already skips it; direct callers get None until the pause lapses.
        if self.is_health_cooldown(token.id, "at_stale"):
            return None

        if not self._should_refresh_at(token):
            if self._has_recent_at_validation(token.id):
                return token
            try:
                credits_result = await self._get_credits_for_token(token, token.at)
                await self.db.update_token(
                    token.id,
                    credits=credits_result.get("credits", 0),
                    user_paygate_tier=credits_result.get("userPaygateTier"),
                )
                self._mark_at_valid(token.id)
                await self._note_verified_at(token.id, token.at)
                return await self.db.get_token(token.id) or token
            except Exception as e:
                debug_logger.log_warning(
                    f"[AT_CHECK] Token {token.id}: 本地判定未过期，但上游校验失败，准备刷新 AT/ST - {e}"
                )

        outcome = await self._refresh_at(token.id, escalate=disable_on_failure)
        if not outcome.success:
            # Only a confirmed credential failure removes the token from the
            # pool. Transient network errors leave it enabled to be retried.
            # at_stale is handled by the refresh leader (pause → device reload →
            # threshold disable); never disabled here.
            if disable_on_failure and outcome.reason == "st_expired":
                debug_logger.op_warning(
                    f"[AT_REFRESH] token={token.id} ST expired → disabling (auto_st_expired; "
                    f"auto-recovers on next session push)"
                )
                await self.disable_token(token.id, reason="auto_st_expired")
            return None

        return await self.db.get_token(token.id)

    async def is_at_valid(self, token_id: int, token: Optional[Token] = None) -> bool:
        """检查AT是否有效,如果无效或即将过期则自动刷新

        Returns:
            True if AT is valid or refreshed successfully
            False if AT cannot be refreshed
        """
        token_obj = token if token and token.id == token_id else await self.db.get_token(token_id)
        if not token_obj:
            return False

        valid_token = await self.ensure_valid_token(token_obj)
        return valid_token is not None


    async def _locked_refresh(self, token_id: int, st: str, token: Optional[Token]) -> RefreshOutcome:
        """validate-then-promote under the per-token credential lock. The lock is held
        ONLY for this call — never across a device round-trip, because the device's
        push (/api/plugin/update-token → validate_and_promote) needs the same lock."""
        refresh_lock = await self._get_token_lock(self._refresh_locks, self._refresh_lock_guard, token_id)
        async with refresh_lock:
            return await self._do_refresh_at(token_id, st, token)

    async def _refresh_at_inner(self, token_id: int, escalate: bool = True) -> RefreshOutcome:
        """Perform exactly one real AT refresh attempt (single-flight per token via
        ``_refresh_at``).

        Side-effect-free with respect to enabling/disabling on st_expired/network:
        that decision belongs to the caller (``ensure_valid_token``). at_stale
        escalation (strike → device reload → threshold disable) happens here, in the
        leader, and ONLY when ``escalate`` is True (automatic pool path). Manual
        callers (admin refresh, disable_on_failure=False) get recovery attempts but
        never strikes or disables.
        """
        token = await self.db.get_token(token_id)
        if not token:
            return RefreshOutcome(False, "unknown")

        outcome = await self._locked_refresh(token_id, token.st, token)
        if outcome.success:
            return outcome

        # Only attempt a (browser/personal/protocol) ST refresh when the failure
        # is an expired/invalid ST. A network blip must NOT trigger a session
        # refresh nor be treated as a dead credential.
        if outcome.reason == "st_expired":
            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: ST expired, trying ST refresh...")
            new_st = await self._try_refresh_st(token_id, token)   # lock NOT held (device round-trip)
            if new_st:
                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: ST refreshed, retrying AT refresh...")
                latest_token = await self.db.get_token(token_id) or token
                retry = await self._locked_refresh(token_id, new_st, latest_token)
                if retry.success:
                    return retry
                outcome = retry

        if outcome.reason == "at_stale":
            outcome = await self._handle_at_stale(token_id, token, outcome, escalate=escalate)
            if outcome.success:
                return outcome

        debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: all refresh attempts failed ({outcome.reason})")
        self._clear_at_validation_cache(token_id)
        return outcome

    @staticmethod
    def _snapshot_fp(token: Optional[Token]) -> str:
        """Fingerprint of the serving (st, at) pair — the CAS key for a disable."""
        if not token:
            return ""
        return hashlib.sha256(f"{token.st or ''}|{token.at or ''}".encode("utf-8")).hexdigest()[:16]

    async def _handle_at_stale(
        self, token_id: int, token: Token, failed: RefreshOutcome, escalate: bool = True
    ) -> RefreshOutcome:
        """Leader-only (inside the coalesced refresh task) so ONE failure round yields ONE
        strike / ONE forced reload / at most ONE disable — concurrent waiters just
        receive the outcome. No lock is held across the device round-trip.

        Order: record the strike (pause) → try protocol refresh for protocol tokens →
        ask the bound worker device to reload Labs and push again (the push endpoint
        validates and promotes only a working credential) → re-verify. If still stale
        and the strike threshold is due, disable with ban_reason=auto_at_stale, guarded
        by a snapshot CAS so a valid push that landed meanwhile is never disabled.
        """
        serving_fp = self._snapshot_fp(token)
        candidate_fp = failed.candidate_fp or self._credential_fingerprint(token.at)
        strike = None
        if escalate:
            # Record the strike UNDER the lock after re-checking the serving snapshot: a
            # verified push may have promoted a fresh credential between our failed
            # attempt (lock released) and now — then this failure is stale and must not
            # pause the fresh credential. Holding the lock here means a push cannot
            # interleave between the check and the write.
            refresh_lock = await self._get_token_lock(self._refresh_locks, self._refresh_lock_guard, token_id)
            async with refresh_lock:
                latest = await self.db.get_token(token_id)
                if latest and self._snapshot_fp(latest) != serving_fp:
                    debug_logger.log_info(
                        f"[AT_REFRESH] token={token_id} stale failure discarded — credential changed meanwhile"
                    )
                    fresh = await self._do_refresh_at(token_id, latest.st, latest)
                    return fresh
                strike = await self._record_at_stale(token_id, candidate_fp)
            if not strike.get("escalated"):
                # Concurrent straggler inside an active pause: nothing more to do.
                return RefreshOutcome(False, "at_stale", verified=False, candidate_fp=candidate_fp)

        # Recovery attempt 1: protocol-mode ST refresh (no-op unless configured).
        try:
            protocol_st = await self._try_protocol_refresh_st(token_id, token)
        except Exception:
            protocol_st = None
        if protocol_st:
            latest = await self.db.get_token(token_id) or token
            retry = await self._locked_refresh(token_id, protocol_st, latest)
            if retry.success:
                return retry

        # Recovery attempt 2: bound worker device — forced Labs reload, then push. The
        # push goes through validate_and_promote (its own lock acquisition — ours is
        # NOT held here); a still-dead grant leaves st/at untouched and tells the
        # device to show "sign out / sign in".
        if config.captcha_method == "extension":
            new_st = await self._try_refresh_st_via_extension(token_id, token, reload=True)
            if new_st:
                latest = await self.db.get_token(token_id) or token
                retry = await self._locked_refresh(token_id, new_st, latest)
                if retry.success:
                    return retry

        if strike and strike.get("disable_due"):
            # CAS under the lock: disable only if the serving (st, at) pair is STILL the
            # one that failed — a verified push meanwhile changes it and wins.
            refresh_lock = await self._get_token_lock(self._refresh_locks, self._refresh_lock_guard, token_id)
            async with refresh_lock:
                latest = await self.db.get_token(token_id)
                if latest and latest.is_active and self._snapshot_fp(latest) == serving_fp:
                    debug_logger.op_warning(
                        f"[AT_REFRESH] token={token_id} access token dead for {strike.get('strikes')} "
                        f"rounds within {self.AT_STALE_DISABLE_WINDOW} → disabling (auto_at_stale; "
                        f"device must sign out/in to Google Labs)"
                    )
                    await self.disable_token(token_id, reason="auto_at_stale")
                else:
                    debug_logger.log_info(
                        f"[AT_REFRESH] token={token_id} at_stale disable skipped — credential changed or already inactive"
                    )
        return RefreshOutcome(False, "at_stale", verified=False, candidate_fp=candidate_fp)

    async def _refresh_at(self, token_id: int, escalate: bool = True) -> RefreshOutcome:
        """Coalesce concurrent AT refresh calls for the same token. The FIRST caller is
        the leader and its ``escalate`` flag governs the round; later callers only
        await the shared outcome (they never strike/reload/disable)."""
        existing_task = self._refresh_futures.get(token_id)
        if existing_task:
            return await existing_task

        async def runner() -> RefreshOutcome:
            try:
                return await self._refresh_at_inner(token_id, escalate=escalate)
            finally:
                current = self._refresh_futures.get(token_id)
                if current is task:
                    self._refresh_futures.pop(token_id, None)

        task = asyncio.create_task(runner())
        self._refresh_futures[token_id] = task
        return await task

    def _is_auth_error(self, error: Exception) -> bool:
        """True iff the error is an authentication failure (ST/AT invalid).

        Prefers the structured status code/reason from FlowAPIError. The legacy
        string fallback is deliberately NARROW ("HTTP ... 401" / UNAUTHENTICATED):
        a bare "401" substring also matches ports/byte counts inside transport
        errors, which would misclassify a network blip as an expired credential
        and (via refresh_and_check) disable a healthy token.
        """
        if isinstance(error, FlowAPIError):
            return error.status_code == 401 or error.reason == "UNAUTHENTICATED"
        error_msg = str(error)
        if "UNAUTHENTICATED" in error_msg:
            return True
        return bool(re.search(r"\bHTTP\b[^0-9]{0,20}\b401\b|\bstatus(?:\s+code)?[:= ]\s*401\b", error_msg, re.IGNORECASE))

    def _classify_refresh_error(self, error: Exception) -> str:
        """Map a refresh exception to a RefreshOutcome reason.

        Order matters: structured auth first (precise), then transport (so a
        timeout/proxy error can never fall through to the string-auth match),
        then the narrow legacy-string auth fallback.
        """
        if isinstance(error, FlowAPIError) and (error.status_code == 401 or error.reason == "UNAUTHENTICATED"):
            return "st_expired"
        if self.flow_client._is_timeout_error(error) or self.flow_client._is_proxy_connection_error(error):
            return "network"
        if self._is_auth_error(error):
            return "st_expired"
        return "unknown"

    async def validate_and_promote(self, token_id: int, st: str, source: str = "push") -> RefreshOutcome:
        """Public entry for credential PUSHES (worker extension / admin import): verify the
        session's access token against the API and only then commit st/at. Serialized
        with the refresh path via the per-token lock. ``at_stale`` ⇒ nothing stored —
        the caller must not re-enable and should tell the device to re-login."""
        refresh_lock = await self._get_token_lock(self._refresh_locks, self._refresh_lock_guard, token_id)
        async with refresh_lock:
            token = await self.db.get_token(token_id)
            outcome = await self._do_refresh_at(token_id, st, token)
            if outcome.success:
                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: credential promoted via {source}")
            return outcome

    async def _do_refresh_at(self, token_id: int, st: str, token: Optional[Token] = None) -> RefreshOutcome:
        """执行 AT 刷新的核心逻辑

        Args:
            token_id: Token ID
            st: Session Token

        Returns:
            RefreshOutcome(success, reason). success=True iff a VERIFIED AT was
            obtained and promoted. VALIDATE-THEN-PROMOTE: the ST/AT pair is written
            only after the API accepted the AT (or the verification merely hit a
            transport blip). A session whose embedded AT the API rejects (401)
            never overwrites the serving credentials — it is ``at_stale``, not
            ``st_expired``: the cookie is alive, Google just stopped renewing the
            access token inside it, and only a device re-login can fix that.
        """
        try:
            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: 开始刷新AT...")

            # 使用ST转AT
            token = token or await self.db.get_token(token_id)
            result = await (
                self._st_to_at_for_token(token, st)
                if token is not None
                else self.flow_client.st_to_at(st)
            )
            new_at = result["access_token"]
            expires = result.get("expires")

            # 解析过期时间
            new_at_expires = None
            if expires:
                try:
                    new_at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                except:
                    pass

            # 验证 AT 有效性：通过 get_credits 测试 — BEFORE writing anything.
            credits_fields: Dict[str, Any] = {}
            try:
                credits_result = await (
                    self._get_credits_for_token(token, new_at)
                    if token is not None
                    else self.flow_client.get_credits(new_at)
                )
                credits_fields = {
                    "credits": credits_result.get("credits", 0),
                    "user_paygate_tier": credits_result.get("userPaygateTier"),
                }
                debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: AT 验证成功（余额: {credits_result.get('credits', 0)}）")
                verified = True
            except Exception as verify_err:
                if self._is_auth_error(verify_err):
                    # Session alive (st_to_at returned a user) but the API rejects the AT
                    # embedded in it: Google is not renewing the grant. Do NOT overwrite
                    # the stored credentials; the caller applies the at_stale cooldown.
                    debug_logger.op_warning(
                        f"[AT_REFRESH] token={token_id} session alive but access token REJECTED "
                        f"(expires={expires}); not promoting — at_stale"
                    )
                    record_token_refresh("at", "failure")
                    return RefreshOutcome(False, "at_stale", verified=False,
                                          candidate_fp=self._credential_fingerprint(new_at))
                # Transport blip during verification only: the AT is almost certainly fine.
                # Keep long-standing behavior — promote it — but report it UNVERIFIED.
                verified = False
                debug_logger.log_warning(f"[AT_REFRESH] Token {token_id}: AT 验证时发生非认证错误: {str(verify_err)}")

            # Promote: ST + AT + expiry (+ credits when verified) in one write.
            await self.db.update_token(
                token_id,
                st=st,
                at=new_at,
                at_expires=new_at_expires,
                **credits_fields,
            )
            if verified:
                self._mark_at_valid(token_id)
                await self._note_verified_at(token_id, new_at)
            debug_logger.log_info(f"[AT_REFRESH] Token {token_id}: AT刷新成功 (expires {new_at_expires}, verified={verified})")
            record_token_refresh("at", "success")
            return RefreshOutcome(True, "ok", verified=verified)

        except Exception as e:
            reason = self._classify_refresh_error(e)
            debug_logger.log_error(f"[AT_REFRESH] Token {token_id}: AT刷新失败 ({reason}) - {str(e)}")
            record_token_refresh("at", "failure")
            return RefreshOutcome(False, reason)

    async def _try_protocol_refresh_st(self, token_id: int, token: Token) -> Optional[str]:
        if self._normalize_protocol_mode(getattr(token, "protocol_mode", "session")) != "protocol":
            return None
        if not (getattr(token, "google_cookies", "") or "").strip():
            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 未配置 Google Cookies，跳过协议刷新")
            return None

        try:
            from .protocol_login import protocol_loginer

            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 尝试协议刷新 ST...")
            login_result = await protocol_loginer.login(
                token.google_cookies,
                proxy=(getattr(token, "proxy_url", "") or None),
                email=(getattr(token, "login_account", "") or token.email or None),
            )
            if login_result.get("success") and login_result.get("session_token"):
                new_st = str(login_result["session_token"]).strip()
                await self.db.update_token(
                    token_id,
                    st=new_st,
                    last_st_refresh_at=datetime.now(timezone.utc),
                    last_st_refresh_result="success",
                )
                debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 协议刷新 ST 成功")
                record_token_refresh("st", "success")
                return new_st

            error = str(login_result.get("error") or "协议刷新失败")
            await self.db.update_token(
                token_id,
                last_st_refresh_at=datetime.now(timezone.utc),
                last_st_refresh_result=error,
            )
            debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 协议刷新 ST 失败 - {error}")
            record_token_refresh("st", "failure")
            return None
        except Exception as e:
            await self.db.update_token(
                token_id,
                last_st_refresh_at=datetime.now(timezone.utc),
                last_st_refresh_result=str(e),
            )
            debug_logger.log_error(f"[ST_REFRESH] Token {token_id}: 协议刷新 ST 异常 - {e}")
            record_token_refresh("st", "failure")
            return None

    async def _try_refresh_st(self, token_id: int, token) -> Optional[str]:
        """尝试通过浏览器刷新 Session Token

        使用常驻 tab 获取新的 __Secure-next-auth.session-token

        Args:
            token_id: Token ID
            token: Token 对象

        Returns:
            新的 ST 字符串，如果失败返回 None
        """
        try:
            from ..core.config import config

            # Upstream protocol-login refresh: no-ops unless this token is configured
            # protocol_mode='protocol' with Google cookies — safe to try first.
            protocol_st = await self._try_protocol_refresh_st(token_id, token)
            if protocol_st:
                return protocol_st

            # Extension mode: command the logged-in worker browser to push a fresh ST.
            if config.captcha_method == "extension":
                return await self._try_refresh_st_via_extension(token_id, token)

            # 仅在 personal 模式下支持 ST 自动刷新
            if config.captcha_method != "personal":
                debug_logger.log_info(f"[ST_REFRESH] 非 personal 模式，跳过 ST 自动刷新")
                return None

            if not token.current_project_id:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id} 没有 project_id，无法刷新 ST")
                return None

            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 尝试通过浏览器刷新 ST...")

            from .browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(self.db)

            refresh_timeout_seconds = 45.0
            try:
                new_st = await asyncio.wait_for(
                    service.refresh_session_token(token.current_project_id),
                    timeout=refresh_timeout_seconds,
                )
            except asyncio.TimeoutError:
                debug_logger.log_error(
                    f"[ST_REFRESH] Token {token_id}: 刷新 ST 超时 ({refresh_timeout_seconds:.0f}s)"
                )
                record_token_refresh("st", "failure")
                return None
            if new_st and new_st != token.st:
                # 更新数据库中的 ST
                await self.db.update_token(token_id, st=new_st)
                debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: ST 已自动更新")
                record_token_refresh("st", "success")
                return new_st
            elif new_st == token.st:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 获取到的 ST 与原 ST 相同，可能登录已失效")
                record_token_refresh("st", "failure")
                return None
            else:
                debug_logger.log_warning(f"[ST_REFRESH] Token {token_id}: 无法获取新 ST")
                record_token_refresh("st", "failure")
                return None

        except Exception as e:
            debug_logger.log_error(f"[ST_REFRESH] Token {token_id}: 刷新 ST 失败 - {str(e)}")
            record_token_refresh("st", "failure")
            return None

    async def _try_refresh_st_via_extension(self, token_id: int, token, reload: bool = False) -> Optional[str]:
        """Extension mode ST auto-refresh: command the logged-in worker browser to
        read its live Google Labs cookie and push a fresh ST to /api/plugin/update-token
        (which writes token.st). On success we re-read the token and return the fresh ST
        so ``_refresh_at_inner`` can retry the AT. Works with OR without a bound Route Key
        (the shared-pool fan-out is email-guarded, so it never touches the wrong account).
        """
        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            debug_logger.log_info(f"[ST_REFRESH] Token {token_id}: 通过扩展刷新 ST...")
            service = await ExtensionCaptchaService.get_instance(self.db)
            # reload=True forces a Labs navigation on the BOUND device before the push
            # (Google may renew a stale access token on a real page load). The push
            # endpoint validates+promotes, so a returned new ST is already verified.
            result = await service.request_session_refresh(token_id, timeout=75 if reload else 30, reload=reload)
            status = (result or {}).get("status")
            if status == "relogin_required":
                debug_logger.op_warning(
                    f"[ST_REFRESH] token={token_id} device reports Google grant dead even after reload — "
                    f"human sign-out/sign-in required on that machine"
                )
                record_token_refresh("st", "failure")
                return None

            if status == "refreshed":
                updated = await self.db.get_token(token_id)
                new_st = updated.st if updated else None
                if new_st and new_st != token.st:
                    debug_logger.event(f"[ST_REFRESH] token={token_id} ST refreshed via extension")
                    record_token_refresh("st", "success")
                    return new_st
                debug_logger.op_warning(
                    f"[ST_REFRESH] token={token_id} extension reported refreshed but ST unchanged"
                )
                record_token_refresh("st", "failure")
                return None

            debug_logger.op_warning(
                f"[ST_REFRESH] token={token_id} extension refresh not successful (status={status})"
            )
            record_token_refresh("st", "failure")
            return None
        except Exception as e:
            debug_logger.op_error(f"[ST_REFRESH] token={token_id} extension refresh error: {e}")
            record_token_refresh("st", "failure")
            return None

    async def _refresh_protocol_token(self, token: Token, now: datetime) -> None:
        """Protocol-login healer for one token. The protocol ST mint (Google login I/O)
        runs WITHOUT the credential lock; the result then goes through the same
        validate-then-promote path as every other credential (lock held only for
        st_to_at + get_credits + commit). Only a VERIFIED credential clears health or
        re-enables an auto_at_stale row."""
        token_id = int(token.id)
        latest = await self.db.get_token(token_id)
        if not latest or (not latest.is_active and (latest.ban_reason or "") != "auto_at_stale"):
            return
        if not latest.auto_refresh_enabled:
            return
        if self._normalize_protocol_mode(latest.protocol_mode) != "protocol":
            return
        if not (latest.google_cookies or "").strip():
            return

        new_st = await self._try_protocol_refresh_st(token_id, latest)
        if not new_st:
            return

        outcome = await self._locked_refresh(token_id, new_st, latest)
        if outcome.success and outcome.verified:
            await self.db.update_token(token_id, last_st_refresh_at=now, last_st_refresh_result="success")
            if not latest.is_active and (latest.ban_reason or "") == "auto_at_stale":
                await self.enable_token(token_id)
                debug_logger.event(f"[PROTOCOL_REFRESH] token={token_id} auto_at_stale healed via protocol login — re-enabled")
            debug_logger.log_info(f"[PROTOCOL_REFRESH] Token {token_id}: 协议刷新 ST/AT 成功")
        elif outcome.success:
            # Promoted but unverifiable (Google API unreachable): stored, nothing else.
            await self.db.update_token(token_id, last_st_refresh_at=now, last_st_refresh_result="stored (unverified)")
            record_token_refresh("at", "success")
        else:
            await self.db.update_token(token_id, last_st_refresh_at=now, last_st_refresh_result=f"failed: {outcome.reason}")
            record_token_refresh("at", "failure")
            debug_logger.log_error(f"[PROTOCOL_REFRESH] Token {token_id}: 协议 ST 转 AT 失败 - {outcome.reason}")

    async def run_protocol_refresh_once(self) -> None:
        """Refresh protocol-mode tokens whose ST refresh interval is due."""
        try:
            refresh_config = await self.db.get_token_refresh_config()
        except Exception as e:
            debug_logger.log_warning(f"[PROTOCOL_REFRESH] 读取刷新配置失败: {e}")
            return

        if not refresh_config or not refresh_config.enabled:
            return

        # Active tokens PLUS inactive ones disabled for a dead access token (auto_at_stale):
        # protocol login can mint a fresh grant, so those must not be stranded.
        all_tokens = await self.db.get_all_tokens()
        tokens = [t for t in all_tokens if t.is_active or (t.ban_reason or "") == "auto_at_stale"]
        now = datetime.now(timezone.utc)
        for token in tokens:
            try:
                if not token.auto_refresh_enabled:
                    continue
                if self._normalize_protocol_mode(token.protocol_mode) != "protocol":
                    continue
                if not (token.google_cookies or "").strip():
                    continue

                interval_minutes = token.refresh_interval_minutes or refresh_config.refresh_interval_minutes or 120
                interval_minutes = self._normalize_refresh_interval(interval_minutes)
                last_refresh = self._as_utc(token.last_st_refresh_at)
                if last_refresh and now - last_refresh < timedelta(minutes=interval_minutes):
                    continue

                await self._refresh_protocol_token(token, now)
            except Exception as e:
                debug_logger.log_error(f"[PROTOCOL_REFRESH] Token {getattr(token, 'id', '?')}: 后台刷新异常 - {e}")

    async def _protocol_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.run_protocol_refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                debug_logger.log_error(f"[PROTOCOL_REFRESH] 后台任务异常 - {e}")

    def start_protocol_refresher(self) -> None:
        if self._protocol_refresher_task and not self._protocol_refresher_task.done():
            return
        self._protocol_refresher_task = asyncio.create_task(self._protocol_refresh_loop())

    async def stop_protocol_refresher(self) -> None:
        task = self._protocol_refresher_task
        self._protocol_refresher_task = None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            debug_logger.log_warning(f"[PROTOCOL_REFRESH] 停止后台任务时出错: {e}")

    async def ensure_project_exists(self, token_id: int) -> str:
        """Ensure a token has a pooled set of projects and return one in round-robin order."""
        project_lock = await self._get_token_lock(
            self._project_locks,
            self._project_lock_guard,
            token_id,
        )
        async with project_lock:
            token = await self.db.get_token(token_id)
            if not token:
                raise ValueError("Token not found")

            projects = [project for project in await self.db.get_projects_by_token(token_id) if project.is_active]
            projects = self._sort_projects(projects)

            try:
                project_pool_size = self._get_project_pool_size()
                while len(projects) < project_pool_size:
                    new_project = await self._create_project_for_token(token, len(projects) + 1)
                    projects.append(new_project)
                    projects = self._sort_projects(projects)

                selectable_projects = projects[:project_pool_size]
                selected_project = self._select_next_project(token, selectable_projects)
                await self.db.update_token(
                    token_id,
                    current_project_id=selected_project.project_id,
                    current_project_name=selected_project.project_name,
                )
                return selected_project.project_id
            except Exception as e:
                raise ValueError(f"Failed to prepare project pool: {str(e)}")

    async def record_usage(self, token_id: int, is_video: bool = False):
        """Record token usage"""
        await self.db.update_token(token_id, use_count=1, last_used_at=datetime.now())

        if is_video:
            await self.db.increment_token_stats(token_id, "video")
        else:
            await self.db.increment_token_stats(token_id, "image")

    async def record_error(self, token_id: int, error_message: Optional[str] = None, model: Optional[str] = None):
        """Record token error and auto-disable if threshold reached.

        Environmental failures (reCAPTCHA / captcha / upstream anti-abuse) are
        NOT counted toward auto-disable: they reflect IP/fingerprint reputation,
        not token validity. Counting them would auto-disable an otherwise-valid
        (often paid) token after a burst of transient reCAPTCHA rejections.
        """
        if _is_capacity_or_solver_error(error_message):
            # Flow capacity / captcha-solver outage: not the account's fault and not its
            # IP reputation — no disable count, and no reCAPTCHA cooldown either.
            debug_logger.log_info(
                f"[TOKEN] Token {token_id} hit an upstream capacity/solver error; "
                f"not counting toward auto-disable: {str(error_message)[:120]}"
            )
            return

        if _is_environmental_token_error(error_message):
            # Anti-bot/reCAPTCHA rejection: don't count it toward auto-disable (it's IP/
            # fingerprint reputation, not token health) BUT apply a progressive per-account
            # cooldown so the load balancer stops re-picking this account and racking up
            # more "unusual activity" flags. See mark_recaptcha_failure.
            await self.mark_recaptcha_failure(token_id)
            debug_logger.log_info(
                f"[TOKEN] Token {token_id} hit an environmental/captcha error; cooling the "
                f"account (not counting toward auto-disable): {str(error_message)[:120]}"
            )
            return

        if _is_quota_error(error_message):
            # Quota is PER MODEL — pause ONLY this model for this token. The token
            # stays active and keeps serving every other model (which have their own
            # separate daily quotas). Do NOT disable the whole token. Daily quota is
            # paused until the PT reset (see mark_model_quota_exhausted).
            await self.mark_model_quota_exhausted(token_id, model, error_message)
            return

        await self.db.increment_token_stats(token_id, "error")

        # Check if should auto-disable token (based on consecutive errors)
        stats = await self.db.get_token_stats(token_id)
        admin_config = await self.db.get_admin_config()

        if stats and stats.consecutive_error_count >= admin_config.error_ban_threshold:
            debug_logger.log_warning(
                f"[TOKEN_BAN] Token {token_id} consecutive error count ({stats.consecutive_error_count}) "
                f"reached threshold ({admin_config.error_ban_threshold}), auto-disabling (will auto-recover)"
            )
            # Mark it recoverable (ban_reason + timestamp) so auto_recover_tokens
            # re-enables it after a cooldown, instead of disabling it forever.
            await self.db.update_token(
                token_id,
                is_active=False,
                ban_reason="auto_error",
                banned_at=datetime.now(timezone.utc),
            )

    async def record_success(self, token_id: int):
        """Record successful request (reset consecutive error count)

        This method resets error_count to 0, which is used for auto-disable threshold checking.
        Note: today_error_count and historical statistics are NOT reset.
        """
        await self.db.reset_error_count(token_id)
        # A success proves the account's reCAPTCHA/IP reputation is healthy again — reset
        # any progressive anti-bot cooldown so it isn't held back on the next request.
        await self.clear_recaptcha_cooldown(token_id)
        # A generation succeeded → the access token in use is live → clear at_stale.
        await self._note_verified_at(token_id, None)

    async def ban_token_for_429(self, token_id: int):
        """因429错误立即禁用token

        Args:
            token_id: Token ID
        """
        debug_logger.log_warning(f"[429_BAN] 禁用Token {token_id} (原因: 429 Rate Limit)")
        await self.db.update_token(
            token_id,
            is_active=False,
            ban_reason="429_rate_limit",
            banned_at=datetime.now(timezone.utc)
        )

    async def auto_unban_429_tokens(self):
        """自动解禁因429被禁用的token

        规则:
        - 距离禁用时间12小时后自动解禁
        - 仅解禁未过期的token
        - 仅解禁因429被禁用的token
        """
        all_tokens = await self.db.get_all_tokens()
        now = datetime.now(timezone.utc)

        for token in all_tokens:
            # 跳过非429禁用的token
            if token.ban_reason != "429_rate_limit":
                continue

            # 跳过未禁用的token
            if token.is_active:
                continue

            # 跳过没有禁用时间的token
            if not token.banned_at:
                continue

            # 检查token是否已过期
            if token.at_expires:
                # 确保时区一致
                if token.at_expires.tzinfo is None:
                    at_expires_aware = token.at_expires.replace(tzinfo=timezone.utc)
                else:
                    at_expires_aware = token.at_expires

                # 如果已过期，跳过
                if at_expires_aware <= now:
                    debug_logger.log_info(f"[AUTO_UNBAN] Token {token.id} 已过期，跳过解禁")
                    continue

            # 确保banned_at时区一致
            if token.banned_at.tzinfo is None:
                banned_at_aware = token.banned_at.replace(tzinfo=timezone.utc)
            else:
                banned_at_aware = token.banned_at

            # 检查是否已过12小时
            time_since_ban = now - banned_at_aware
            if time_since_ban.total_seconds() >= 12 * 3600:  # 12小时
                debug_logger.log_info(
                    f"[AUTO_UNBAN] 解禁Token {token.id} (禁用时间: {banned_at_aware}, "
                    f"已过 {time_since_ban.total_seconds() / 3600:.1f} 小时)"
                )
                await self.db.update_token(
                    token.id,
                    is_active=True,
                    ban_reason=None,
                    banned_at=None
                )
                # 重置错误计数
                await self.db.reset_error_count(token.id)

    # Per-reason cooldown (minutes) before an auto-disabled token is retried.
    RECOVERABLE_COOLDOWNS_MINUTES = {
        "auto_error": 10,      # repeated generic errors -> short retry
        "daily_quota": 30,     # usage quota -> retry occasionally (resets daily)
        "429_rate_limit": 720, # rate limit -> 12h, matches legacy behaviour
    }

    async def auto_recover_tokens(self):
        """Re-enable auto-disabled tokens after a per-reason cooldown.

        Lets transient failures (error bursts, usage quota, 429 rate limits)
        recover on their own without manual re-enabling. Only tokens disabled by
        the system (those carrying a recoverable ban_reason + banned_at) are
        touched — a manual admin disable (no ban_reason) is left alone. Tokens
        whose credentials are already expired are skipped (the refresh/extension
        path handles those).
        """
        all_tokens = await self.db.get_all_tokens()
        now = datetime.now(timezone.utc)

        for token in all_tokens:
            if token.is_active:
                continue
            cooldown = self.RECOVERABLE_COOLDOWNS_MINUTES.get(token.ban_reason)
            if cooldown is None:        # manual disable / unknown reason -> leave it
                continue
            if not token.banned_at:
                continue

            # Skip tokens whose access token is already expired.
            if token.at_expires:
                at_exp = token.at_expires if token.at_expires.tzinfo else token.at_expires.replace(tzinfo=timezone.utc)
                if at_exp <= now:
                    continue

            banned_at = token.banned_at if token.banned_at.tzinfo else token.banned_at.replace(tzinfo=timezone.utc)
            elapsed_min = (now - banned_at).total_seconds() / 60.0
            if elapsed_min >= cooldown:
                debug_logger.log_info(
                    f"[AUTO_RECOVER] Re-enabling Token {token.id} "
                    f"(reason={token.ban_reason}, cooled {elapsed_min:.0f}m >= {cooldown}m)"
                )
                await self.db.update_token(
                    token.id,
                    is_active=True,
                    ban_reason=None,
                    banned_at=None,
                )
                await self.db.reset_error_count(token.id)

    # ========== 余额刷新 ==========

    async def refresh_credits(self, token_id: int) -> int:
        """刷新Token余额

        Returns:
            credits
        """
        token = await self.db.get_token(token_id)
        if not token:
            return 0

        # 确保AT有效（手动刷新余额：失败时绝不禁用 token）
        token = await self.ensure_valid_token(token, disable_on_failure=False)
        if not token:
            return 0

        try:
            result = await self.flow_client.get_credits(token.at)
            credits = result.get("credits", 0)
            user_paygate_tier = result.get("userPaygateTier")

            # 更新数据库
            await self.db.update_token(
                token_id,
                credits=credits,
                user_paygate_tier=user_paygate_tier,
            )

            return credits
        except Exception as e:
            debug_logger.log_error(f"Failed to refresh credits for token {token_id}: {str(e)}")
            return 0
