import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import WebSocket

from ..core.logger import debug_logger


@dataclass
class ExtensionConnection:
    websocket: WebSocket
    route_key: str = ""
    client_label: str = ""
    connected_at: float = field(default_factory=time.time)


class ExtensionCaptchaService:
    _instance: Optional["ExtensionCaptchaService"] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        self.db = db
        self.active_connections: list[ExtensionConnection] = []
        self.pending_requests: dict[str, asyncio.Future] = {}
        self._rr_index = 0  # round-robin cursor for empty-route (shared pool) browsers

    @classmethod
    async def get_instance(cls, db=None) -> "ExtensionCaptchaService":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db=db)
        elif db is not None and cls._instance.db is None:
            cls._instance.db = db
        return cls._instance

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        conn = ExtensionConnection(
            websocket=websocket,
            route_key=(websocket.query_params.get("route_key") or "").strip(),
            client_label=(websocket.query_params.get("client_label") or "").strip(),
        )
        self.active_connections.append(conn)
        debug_logger.log_info(
            f"[Extension Captcha] Client connected. Total: {len(self.active_connections)}, "
            f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
        )

    def disconnect(self, websocket: WebSocket):
        for conn in list(self.active_connections):
            if conn.websocket is websocket:
                self.active_connections.remove(conn)
                debug_logger.log_info(
                    f"[Extension Captcha] Client disconnected. Total: {len(self.active_connections)}, "
                    f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
                )
                return

    def _find_connection(self, websocket: WebSocket) -> Optional[ExtensionConnection]:
        for conn in self.active_connections:
            if conn.websocket is websocket:
                return conn
        return None

    def _select_connection(
        self, route_key: str, strict: bool = False
    ) -> Optional[ExtensionConnection]:
        """Pick the browser to serve a request for `route_key`.

        Preferred: the browser bound to this account's route key (so captcha MINTING
        happens on the same device — hence same residential IP — that the backend REDEEMS
        through). If that device is offline:
          - strict=True  (session refresh): return None. We must NEVER read another
            account's cookie, so refuse rather than pick a different device.
          - strict=False (captcha minting): fall back to ANY online browser. reCAPTCHA
            tokens are site-level, so a valid token from another IP beats a hard failure
            (it just may score lower if it doesn't match the redeem IP).
        """
        normalized_key = (route_key or "").strip()
        if normalized_key:
            for conn in self.active_connections:
                if conn.route_key == normalized_key:
                    return conn
            if strict:
                return None
            # else fall through to the any-browser fallback below
        # Round-robin across ALL connected browsers (spreads minting load; each IP stays
        # under Google's per-IP rate limit).
        if not self.active_connections:
            return None
        self._rr_index = (self._rr_index + 1) % len(self.active_connections)
        return self.active_connections[self._rr_index]

    def _describe_routes(self) -> str:
        labels = []
        for conn in self.active_connections:
            label = conn.route_key or "(empty)"
            if conn.client_label:
                label = f"{label}:{conn.client_label}"
            labels.append(label)
        return ", ".join(labels)

    def describe_routes(self) -> str:
        return self._describe_routes()

    async def _send_ack(self, websocket: WebSocket, payload: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _resolve_route_key(self, token_id: Optional[int]) -> str:
        if not token_id or not self.db:
            return ""
        try:
            token = await self.db.get_token(token_id)
            if token and token.extension_route_key:
                return token.extension_route_key.strip()
        except Exception as e:
            debug_logger.log_warning(f"[Extension Captcha] Failed to resolve route key for token {token_id}: {e}")
        return ""

    def _has_connection_for_route_key(self, route_key: str) -> bool:
        return self._select_connection(route_key) is not None

    async def has_connection_for_token(self, token_id: Optional[int]) -> tuple[bool, str]:
        route_key = await self._resolve_route_key(token_id)
        return self._has_connection_for_route_key(route_key), route_key

    async def _apply_pool_mode(self, route_key: str, pool_mode) -> None:
        """Persist the device-reported pool for every account bound to `route_key`.

        The Failed-image switch is a DEVICE-level setting: a profile can have pushed
        more than one account (account switches keep the same route key), and all of
        them follow the device's pool. Absent/invalid pool_mode (pre-v3.1.0 extensions)
        changes nothing; unchanged values are not rewritten.
        """
        if pool_mode not in ("auto", "failed_image") or not route_key or not self.db:
            return
        try:
            tokens = await self.db.get_all_tokens()
            for token in tokens:
                bound_key = (token.extension_route_key or "").strip()
                current = getattr(token, "pool_mode", None) or "auto"
                if bound_key == route_key and current != pool_mode:
                    await self.db.update_token(token.id, pool_mode=pool_mode)
                    debug_logger.event(
                        f"[POOL] token={token.id} ({token.email}) {current} -> {pool_mode} "
                        f"(register, route_key={route_key})"
                    )
        except Exception as e:
            debug_logger.op_warning(f"[POOL] failed to apply pool_mode from register: {e}")

    async def handle_message(self, websocket: WebSocket, data: str):
        try:
            payload = json.loads(data)
            message_type = payload.get("type")

            if message_type == "register":
                conn = self._find_connection(websocket)
                if conn:
                    conn.route_key = (payload.get("route_key") or conn.route_key or "").strip()
                    conn.client_label = (payload.get("client_label") or conn.client_label or "").strip()
                    # Always-on so we can SEE what the extension actually reports on every
                    # register — distinguishes "sent pool=auto" (no-op) from "sent nothing"
                    # (old extension). Without this a no-op flip is invisible.
                    debug_logger.event(
                        f"[REGISTER] route_key={conn.route_key or '-'} "
                        f"label={conn.client_label or '-'} pool_mode={payload.get('pool_mode')!r} "
                        f"ext_version={payload.get('ext_version')!r}"
                    )
                    # Two-pool routing: register is the device's authoritative pool report.
                    # Persisting it here makes the Failed-image switch take effect on every
                    # tick/Reconnect (socket re-register), independent of the session push —
                    # which is fire-and-forget and has silently failed in the field.
                    await self._apply_pool_mode(conn.route_key, payload.get("pool_mode"))
                    await self._send_ack(
                        websocket,
                        {
                            "type": "register_ack",
                            "route_key": conn.route_key,
                            "client_label": conn.client_label,
                        },
                    )
                return

            # Type-agnostic correlation: ANY framed reply carrying a known req_id
            # resolves its future — get_token's token result AND the
            # session_refresh_result ack both route through here. Do NOT add a
            # `type` check; request_session_refresh relies on this.
            req_id = payload.get("req_id")
            if req_id and req_id in self.pending_requests:
                # Match the response by req_id alone (a unique uuid4). The socket
                # the answer returns on may differ from the one we dispatched to —
                # MV3 service workers get suspended/revived and reconnect on a NEW
                # socket between receiving a get_token and replying. Binding the
                # future to one websocket would drop those valid answers as
                # "non-owner" and force a 20s timeout. req_id is the real
                # correlation key, so accept it from whichever socket carries it.
                future = self.pending_requests[req_id]
                if not future.done():
                    future.set_result(payload)
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Error handling message: {e}")

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        timeout: int = 20,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        if not self.active_connections:
            debug_logger.log_warning("[Extension Captcha] No active extension connections available.")
            raise RuntimeError("Chrome Extension not connected or Google Labs tab not open.")

        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            available = self._describe_routes() or "none"
            raise RuntimeError(
                f"No Chrome Extension connection matches token_id={token_id} route_key='{route_key}'. "
                f"Available route keys: {available}"
            )

        req_id = f"req_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future

        request_data = {
            "type": "get_token",
            "req_id": req_id,
            "action": action,
            "project_id": project_id,
            "route_key": route_key,
        }

        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching token request via route_key={route_key or '-'}, "
                f"label={conn.client_label or '-'}, project_id={project_id}, action={action}"
            )
            await conn.websocket.send_text(json.dumps(request_data))
            result = await asyncio.wait_for(future, timeout=timeout)

            if result.get("status") == "success":
                return result.get("token")

            error_msg = result.get("error")
            debug_logger.log_error(f"[Extension Captcha] Error from extension: {error_msg}")
            return None

        except asyncio.TimeoutError:
            debug_logger.log_error(f"[Extension Captcha] Timeout waiting for token (req_id: {req_id})")
            return None
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Communication error: {e}")
            return None
        finally:
            self.pending_requests.pop(req_id, None)

    async def _dispatch_session_refresh_to(
        self, conn: "ExtensionConnection", token_id: Optional[int], timeout: int
    ) -> dict:
        """Send one refresh_session command to a specific browser and await its ack.

        The extension reads its LIVE Google Labs cookie and pushes it to
        /api/plugin/update-token WITH this token_id; that endpoint is email-guarded
        (409 => account_mismatch) so pushing to the wrong account is a safe no-op.
        """
        req_id = f"refresh_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future
        try:
            await conn.websocket.send_text(json.dumps({
                "type": "refresh_session",
                "req_id": req_id,
                "route_key": conn.route_key or "",
                "token_id": token_id,
            }))
            result = await asyncio.wait_for(future, timeout=timeout)
            return result if isinstance(result, dict) else {"status": "error", "error": "malformed ack"}
        except asyncio.TimeoutError:
            return {"status": "timeout", "req_id": req_id}
        except Exception as e:
            return {"status": "error", "error": str(e)}
        finally:
            self.pending_requests.pop(req_id, None)

    async def request_session_refresh(self, token_id: Optional[int], timeout: int = 30) -> dict:
        """Tell the worker browser holding this account's Google Labs session to
        refresh it NOW (read the live cookie, push a fresh ST), and return the result.

        Two modes:
        - BOUND (token has an extension_route_key): dispatch only to that browser.
        - SHARED POOL (empty route_key): the correct browser is identified by ACCOUNT,
          not route_key. We fan out SERIALLY over connected browsers; the email guard on
          /api/plugin/update-token makes a wrong-account push a safe no-op (account_mismatch),
          so we stop on the first ``refreshed`` and continue past mismatches. This lets the
          refresh work in a shared-pool deployment without per-account route keys.
        Only works while that browser is still logged in; a logged-out session surfaces as
        ``logged_out`` (no command can recover it — a human must re-login there).
        """
        if not self.active_connections:
            return {"status": "no_browser", "token_id": token_id}

        route_key = await self._resolve_route_key(token_id)
        if route_key:
            conn = self._select_connection(route_key, strict=True)
            if conn is None:
                return {
                    "status": "no_browser",
                    "route_key": route_key,
                    "available": self._describe_routes() or "none",
                }
            debug_logger.event(f"[EXT_REFRESH] token={token_id} route_key={route_key} (bound)")
            return await self._dispatch_session_refresh_to(conn, token_id, timeout)

        # Shared pool: bound each attempt so a stack of offline/busy browsers can't
        # blow the caller's overall timeout. Serialize to avoid multi-tab reload storms.
        conns = list(self.active_connections)
        per_conn_timeout = max(8, min(timeout, 15))
        debug_logger.event(
            f"[EXT_REFRESH] token={token_id} shared-pool fan-out over {len(conns)} browser(s)"
        )
        last: dict = {"status": "no_browser", "token_id": token_id}
        saw_mismatch = False
        saw_logged_out = False
        for conn in conns:
            result = await self._dispatch_session_refresh_to(conn, token_id, per_conn_timeout)
            status = (result or {}).get("status")
            if status == "refreshed":
                debug_logger.event(
                    f"[EXT_REFRESH] token={token_id} refreshed via label={conn.client_label or '-'}"
                )
                return result
            if status == "account_mismatch":
                saw_mismatch = True
            elif status == "logged_out":
                saw_logged_out = True
            last = result
        # Nobody refreshed. Prefer the most actionable reason for the operator.
        if saw_mismatch:
            debug_logger.op_warning(
                f"[EXT_REFRESH] token={token_id} no browser is logged into this account "
                f"(account_mismatch on all)"
            )
            return {"status": "account_mismatch", "token_id": token_id}
        if saw_logged_out:
            return {"status": "logged_out", "token_id": token_id}
        return last

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        _ = project_id, error_message
        debug_logger.log_warning(f"[Extension Captcha] Flow error reported (ignoring): {error_reason}")
