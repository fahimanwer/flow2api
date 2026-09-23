"""Load balancing module for Flow2API"""
import asyncio
import random
from typing import Any, Dict, Optional
from ..core.models import Token
from ..core.config import config
from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)

# Owner decision 2026-09-22 (tmp/tier_order_plan.md): save Ultra quota for what needs it.
IMAGE_TIER_RANK = {"PAYGATE_TIER_ONE": 0, "PAYGATE_TIER_NOT_PAID": 1, "PAYGATE_TIER_TWO": 2}
VIDEO_TIER_RANK = {"PAYGATE_TIER_TWO": 0, "PAYGATE_TIER_ONE": 1, "PAYGATE_TIER_NOT_PAID": 2}
from .concurrency_manager import ConcurrencyManager
from ..core.client_policy import client_block_reason, no_account_error, token_reserved_for
from ..core.logger import debug_logger


def _token_pool(token) -> str:
    return (getattr(token, "pool_mode", None) or "auto")


def select_pool(tokens, pool: str):
    """Restrict tokens to the requested pool (two-pool routing).

    - 'auto' (default, the automatic article pipeline): use ONLY 'auto' accounts, so
      'failed_image' accounts are RESERVED (never drained by the factory).
    - 'failed_image' (staff-driven failed-image regeneration): use 'failed_image' accounts;
      if none are active, fall back to 'auto' accounts so failed images still process when
      nobody has flipped their extension into Failed-image mode.
    """
    if pool == "failed_image":
        reserved = [t for t in tokens if _token_pool(t) == "failed_image"]
        return reserved if reserved else [t for t in tokens if _token_pool(t) == "auto"]
    return [t for t in tokens if _token_pool(t) == "auto"]


class LoadBalancer:
    """Token load balancer with load-aware selection"""

    def __init__(self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager
        self._image_pending: Dict[int, int] = {}
        self._video_pending: Dict[int, int] = {}
        self._pending_lock = asyncio.Lock()
        self._round_robin_state: Dict[str, Optional[int]] = {"image": None, "video": None, "default": None}
        self._rr_lock = asyncio.Lock()

    async def _get_pending_count(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> int:
        async with self._pending_lock:
            if for_image_generation:
                return max(0, int(self._image_pending.get(token_id, 0)))
            if for_video_generation:
                return max(0, int(self._video_pending.get(token_id, 0)))
            return 0

    async def _add_pending(self, token_id: int, for_image_generation: bool, for_video_generation: bool):
        async with self._pending_lock:
            if for_image_generation:
                self._image_pending[token_id] = max(0, int(self._image_pending.get(token_id, 0))) + 1
            elif for_video_generation:
                self._video_pending[token_id] = max(0, int(self._video_pending.get(token_id, 0))) + 1

    async def release_pending(self, token_id: int, for_image_generation: bool = False, for_video_generation: bool = False):
        async with self._pending_lock:
            if for_image_generation:
                current = max(0, int(self._image_pending.get(token_id, 0)))
                if current <= 1:
                    self._image_pending.pop(token_id, None)
                else:
                    self._image_pending[token_id] = current - 1
            elif for_video_generation:
                current = max(0, int(self._video_pending.get(token_id, 0)))
                if current <= 1:
                    self._video_pending.pop(token_id, None)
                else:
                    self._video_pending[token_id] = current - 1

    async def _get_token_load(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> tuple[int, Optional[int]]:
        """Get the token's current load.

        Returns:
            (inflight, remaining)
            remaining is None when there is no limit
        """
        if not self.concurrency_manager:
            return 0, None

        if for_image_generation:
            inflight = await self.concurrency_manager.get_image_inflight(token_id)
            remaining = await self.concurrency_manager.get_image_remaining(token_id)
            pending = await self._get_pending_count(token_id, True, False)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        if for_video_generation:
            inflight = await self.concurrency_manager.get_video_inflight(token_id)
            remaining = await self.concurrency_manager.get_video_remaining(token_id)
            pending = await self._get_pending_count(token_id, False, True)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        return 0, None

    async def _reserve_slot(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> bool:
        """Try to reserve a generation slot for this token."""
        if not self.concurrency_manager:
            return True

        if for_image_generation:
            return await self.concurrency_manager.acquire_image(token_id)

        if for_video_generation:
            return await self.concurrency_manager.acquire_video(token_id)

        return True

    async def _select_round_robin(self, tokens: list[dict], scenario: str) -> Optional[dict]:
        """Select candidate in round-robin order for the given scenario."""
        if not tokens:
            return None

        tokens_sorted = sorted(tokens, key=lambda item: item["token"].id or 0)
        async with self._rr_lock:
            last_id = self._round_robin_state.get(scenario)
            start_idx = 0
            if last_id is not None:
                for idx, item in enumerate(tokens_sorted):
                    if item["token"].id == last_id:
                        start_idx = (idx + 1) % len(tokens_sorted)
                        break
            selected = tokens_sorted[start_idx]
            self._round_robin_state[scenario] = selected["token"].id
        return selected

    async def _server_fallback_eligible(self, token: Token) -> bool:
        if not config.captcha_server_fallback_enabled:
            return False
        if not (getattr(token, "redeem_proxy_url", None) or "").strip():
            return False
        try:
            from .flow_page_captcha import FlowPageCaptchaService
            service = await FlowPageCaptchaService.get_instance()
            return bool(service.is_available())
        except Exception:
            return False

    async def _check_extension_route(self, token: Token) -> tuple[bool, str]:
        """Ensure extension captcha requests are routed to the selected account."""
        if config.captcha_method != "extension":
            return True, ""

        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            service = await ExtensionCaptchaService.get_instance(getattr(self.token_manager, "db", None))
            has_connection, route_key = await service.has_connection_for_token(token.id)
            if has_connection:
                return True, ""

            # 2026-09-23: a worker that is offline is no longer a dead end when the
            # server can mint for this account itself (flag on, account has its own
            # proxy, this deployment has Chromium). Health/quota/client filters above
            # are unchanged; extension-only paths (session refresh) stay strict.
            if await self._server_fallback_eligible(token):
                return True, ""

            available = service.describe_routes() or "none"
            if route_key:
                return False, f"Extension route {route_key} not connected (available routes: {available})"
            return False, f"Extension route not set or anonymous extension not connected (available routes: {available})"
        except Exception as exc:
            return False, f"Extension route check failed: {exc}"

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        reserve: bool = False,
        enforce_concurrency_filter: bool = True,
        track_pending: bool = False,
        pool: str = "auto",
        client: str = "",
    ) -> Optional[Token]:
        """
        Select a token using load-aware balancing

        Args:
            for_image_generation: If True, only select tokens with image_enabled=True
            for_video_generation: If True, only select tokens with video_enabled=True
            model: Model name (used to filter tokens for specific models)
            reserve: Whether to atomically reserve one concurrency slot for the selected token
            enforce_concurrency_filter:
                Whether to pre-filter tokens by current inflight/remaining capacity.
                For reserve=False generation paths, this should usually be False so
                requests can enter the downstream wait queue instead of failing fast.
            track_pending:
                Whether to count the selected token as a queued request immediately.
                This smooths burst distribution before the hard concurrency slot is acquired.

        Returns:
            Selected token or None if no available tokens
        """
        debug_logger.log_info(
            f"[LOAD_BALANCER] Selecting token (image={for_image_generation}, "
            f"video={for_video_generation}, model={model}, reserve={reserve})"
        )

        # Ensure persisted per-model quota cooldowns are loaded before we filter on them.
        await self.token_manager._ensure_quota_loaded()

        media = "video" if for_video_generation else "image"
        active_tokens = await self.token_manager.get_active_tokens()
        # Two-pool routing: keep failed_image accounts out of the auto pool (and vice versa).
        active_tokens = select_pool(active_tokens, pool)
        debug_logger.log_info(f"[LOAD_BALANCER] Found {len(active_tokens)} active tokens (pool={pool})")

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ No active tokens")
            return None

        available_tokens = []
        filtered_reasons = {}
        required_tier = get_required_paygate_tier_for_model(model)

        for token in active_tokens:
            # Per-account reCAPTCHA / anti-bot cooldown (whole token, ALL models): a
            # flagged account is rested so we don't re-trigger more "unusual activity"
            # failures. Progressive backoff lives in TokenManager.mark_recaptcha_failure.
            if self.token_manager.is_recaptcha_cooldown(token.id):
                filtered_reasons[token.id] = "reCAPTCHA cooldown (account level)"
                continue
            # Account-health pause (at_stale: Google stopped renewing this account's access
            # token — cookie alive, API 401). Skip it entirely; the refresh leader handles
            # device reload / threshold disable. See TokenManager._handle_at_stale.
            if self.token_manager.is_health_cooldown(token.id):
                filtered_reasons[token.id] = self.token_manager.health_cooldown_reason(token.id) or "Account health cooldown"
                continue
            # Per-caller routing (client_policy.py): a token reserved for another client, or
            # below the tier this client's policy demands, is out. Unidentified callers use
            # the 'default' policy (any tier) so their behaviour is unchanged.
            client_reason = client_block_reason(token, client, media)
            if client_reason:
                filtered_reasons[token.id] = client_reason
                continue
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            # Image generation is exempt from paygate-tier gating (free accounts
            # can generate images on Flow); only video enforces account tier.
            if model and not for_image_generation and not supports_model_for_tier(model, normalized_tier):
                filtered_reasons[token.id] = 'Account tier too low, needs ' + get_paygate_tier_label(required_tier)
                continue
            # Per-model quota: skip this token only for the specific model it has
            # exhausted; it stays available for every other model (separate quotas).
            if model and self.token_manager.is_model_quota_exhausted(token.id, model):
                filtered_reasons[token.id] = f"Model quota used up, cooling down: {model}"
                continue
            if for_image_generation:
                if not token.image_enabled:
                    filtered_reasons[token.id] = "Image generation disabled"
                    continue

                route_ok, route_reason = await self._check_extension_route(token)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_image(token.id)
                ):
                    filtered_reasons[token.id] = "Image concurrency full"
                    continue

            if for_video_generation:
                if not token.video_enabled:
                    filtered_reasons[token.id] = "Video generation disabled"
                    continue

                route_ok, route_reason = await self._check_extension_route(token)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_video(token.id)
                ):
                    filtered_reasons[token.id] = "Video concurrency full"
                    continue

            inflight, remaining = await self._get_token_load(
                token.id,
                for_image_generation=for_image_generation,
                for_video_generation=for_video_generation
            )
            available_tokens.append({
                "token": token,
                "inflight": inflight,
                "remaining": remaining,
                "needs_refresh": self.token_manager.needs_at_refresh(token),
                "random": random.random()
            })

        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] Filtered tokens:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")

        if not available_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ No usable token (image={for_image_generation}, video={for_video_generation})")
            return None

        # Lowest in-flight first; with a concurrency cap, tokens with more free slots first; then random shuffle
        call_mode = config.call_logic_mode
        if call_mode == "polling":
            scenario = "default"
            if for_image_generation:
                scenario = "image"
            elif for_video_generation:
                scenario = "video"

            ordered_candidates = []
            first_candidate = await self._select_round_robin(available_tokens, scenario)
            if first_candidate is not None:
                ordered_candidates.append(first_candidate)
                ordered_candidates.extend(
                    item for item in sorted(available_tokens, key=lambda item: item["token"].id or 0)
                    if item["token"].id != first_candidate["token"].id
                )
            available_tokens = ordered_candidates
        else:
            available_tokens.sort(
                key=lambda item: (
                    1 if item["needs_refresh"] else 0,
                    item["inflight"],
                    0 if item["remaining"] is None else 1,
                    -(item["remaining"] or 0),
                    item["random"]
                )
            )

        ready_candidates = [item for item in available_tokens if not item["needs_refresh"]]
        refresh_candidates = [item for item in available_tokens if item["needs_refresh"]]
        if ready_candidates and refresh_candidates:
            available_tokens = ready_candidates + refresh_candidates

        # Account order (admin "Save Ultra for last"): images try Pro, then Free, then Ultra;
        # videos try Ultra, then Pro, then Free. Accounts that are full, cooling or out of
        # quota were filtered out above, so an idle Ultra is still used when nothing cheaper
        # is left. Stable sort: rotation order is kept inside each tier.
        tier_rank = self._tier_rank_for(for_image_generation, for_video_generation)
        if tier_rank is not None:
            available_tokens.sort(
                key=lambda item: tier_rank.get(normalize_user_paygate_tier(item["token"].user_paygate_tier), 1)
            )

        # A client's own reserved accounts come first, in BOTH rotation modes (in polling mode the
        # round-robin cursor above would otherwise pick the reserved account only 1/N of the time
        # and spill onto shared accounts). Order inside each group is kept.
        if client:
            mine = [item for item in available_tokens if token_reserved_for(item["token"]) == client]
            if mine:
                available_tokens = mine + [item for item in available_tokens if token_reserved_for(item["token"]) != client]

        debug_logger.log_info("[LOAD_BALANCER] Candidate token load:")
        for item in available_tokens:
            token = item["token"]
            remaining = "unlimited" if item["remaining"] is None else item["remaining"]
            debug_logger.log_info(
                f"[LOAD_BALANCER]   - Token {token.id} ({token.email}) "
                f"inflight={item['inflight']}, remaining={remaining}, "
                f"needs_refresh={item['needs_refresh']}, credits={token.credits}"
            )

        # Only check AT for candidates actually tried, so each request does not scan every token
        for item in available_tokens:
            token = item["token"]
            token_id = token.id

            token = await self.token_manager.ensure_valid_token(token)
            if not token:
                debug_logger.log_info(f"[LOAD_BALANCER] Skipping token {token_id}: AT invalid or expired")
                continue

            if reserve and not await self._reserve_slot(token.id, for_image_generation, for_video_generation):
                debug_logger.log_info(f"[LOAD_BALANCER] Skipping token {token.id}: slot reservation failed")
                continue

            if track_pending:
                await self._add_pending(token.id, for_image_generation, for_video_generation)

            debug_logger.log_info(
                f"[LOAD_BALANCER] ✅ Selected token {token.id} ({token.email}) - "
                f"credits: {token.credits}, inflight={item['inflight']}"
            )
            return token

        debug_logger.log_info(f"[LOAD_BALANCER] ❌ No candidate token usable (image={for_image_generation}, video={for_video_generation})")
        return None

    @staticmethod
    def _tier_rank_for(for_image_generation: bool, for_video_generation: bool) -> Optional[Dict[str, int]]:
        """Tier -> try order for the current media type, or None when the picker is 'balanced'."""
        if config.tier_order != "save_ultra":
            return None
        if for_image_generation:
            return IMAGE_TIER_RANK
        if for_video_generation:
            return VIDEO_TIER_RANK
        return None

    async def get_unavailable_reason(
        self,
        *,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        pool: str = "auto",
        client: str = "",
    ) -> Optional[str]:
        """Give a clearer "no account available" reason, mainly for resolution/tier hints."""
        detail = await self.get_unavailable_detail(
            for_image_generation=for_image_generation,
            for_video_generation=for_video_generation,
            model=model,
            pool=pool,
            client=client,
        )
        return detail["message"] if detail else None

    async def get_unavailable_detail(
        self,
        *,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        pool: str = "auto",
        client: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Why no token could be selected: {"message": str, "extra": {...}|None}.

        `extra` is set only when the CLIENT POLICY (reserved / tier rule) is what removed the
        last usable account; callers put it into the 503 body so the app can react (code
        client_policy_no_account) instead of treating it as a Flow outage.
        """
        await self.token_manager._ensure_quota_loaded()
        active_tokens = select_pool(await self.token_manager.get_active_tokens(), pool)
        if not active_tokens:
            return {"message": (
                "No active account is available — every account is currently disabled, "
                "cooling down, or has an expired session. Re-enable at least one account "
                "(or refresh its session) in the admin UI."
            ), "extra": None}

        media = "video" if for_video_generation else "image"
        allowed_tokens = [t for t in active_tokens if client_block_reason(t, client, media) is None]
        if not allowed_tokens:
            extra = no_account_error(client, media)
            if extra["need"] == "off":
                message = f"{media} generation is switched off for client {extra['client']} (admin: Client routing)"
            else:
                message = (
                    f"No {extra['need']} account is available for client {extra['client']} ({media}): "
                    f"{len(active_tokens)} active account(s), none reserved for it or at the required tier"
                )
            return {"message": message, "extra": extra}
        active_tokens = allowed_tokens

        required_tier = get_required_paygate_tier_for_model(model)
        supported_tokens = []
        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not for_image_generation and not supports_model_for_tier(model, normalized_tier):
                continue
            supported_tokens.append(token)

        if model and not supported_tokens:
            tier_label = get_paygate_tier_label(required_tier)
            return {"extra": None, "message": f"This model requires a {tier_label} account, but no {tier_label} account is available: {model}"}

        # All otherwise-usable accounts are resting on a reCAPTCHA / anti-bot cooldown
        # (account-level, so it applies to every model). Report it clearly.
        if supported_tokens and all(
            self.token_manager.is_recaptcha_cooldown(t.id) for t in supported_tokens
        ):
            return {"extra": None, "message": (
                "All accounts are briefly cooling down after reCAPTCHA / unusual-activity "
                "checks; they auto-recover shortly (progressive backoff)."
            )}

        # All otherwise-usable accounts have NO online worker browser for their route
        # key (extension closed / disconnected). Say so — the generic message sends
        # people chasing the wrong thing.
        if supported_tokens:
            offline = 0
            for t in supported_tokens:
                ok, _ = await self._check_extension_route(t)
                if not ok:
                    offline += 1
            if offline == len(supported_tokens):
                return {"extra": None, "message": (
                    "No worker browser is online for any eligible account — every account's "
                    "Chrome extension is disconnected or closed. Open/reconnect the browsers "
                    "(the extension popup must show Connected for THAT account's route key)."
                )}

        # All otherwise-usable accounts are paused for a dead Google access token
        # (at_stale) — a human must sign out/in on those worker devices.
        if supported_tokens and all(
            self.token_manager.is_health_cooldown(t.id, "at_stale") for t in supported_tokens
        ):
            return {"extra": None, "message": (
                "All accounts are paused: Google stopped renewing their access tokens. "
                "Sign out of Google Labs and back in on the worker devices (the extension shows a red '!')."
            )}

        # All otherwise-usable tokens have exhausted THIS model's quota (other
        # models still work on them). Report it as a model-quota cooldown.
        if model and supported_tokens and all(
            self.token_manager.is_model_quota_exhausted(t.id, model) for t in supported_tokens
        ):
            return {"extra": None, "message": f"Model {model} has reached today's quota (cooling down); other models are still available."}

        capability_tokens = []
        for token in supported_tokens:
            if for_image_generation and not token.image_enabled:
                continue
            if for_video_generation and not token.video_enabled:
                continue
            capability_tokens.append(token)

        if supported_tokens and not capability_tokens:
            if for_image_generation:
                return {"extra": None, "message": "Eligible accounts exist, but image generation is disabled on all of them."}
            if for_video_generation:
                return {"extra": None, "message": "Eligible accounts exist, but video generation is disabled on all of them."}

        return None
