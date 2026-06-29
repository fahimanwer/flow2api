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

    def _select_connection(self, route_key: str) -> Optional[ExtensionConnection]:
        normalized_key = (route_key or "").strip()
        if normalized_key:
            for conn in self.active_connections:
                if conn.route_key == normalized_key:
                    return conn
            return None
        # Empty token routes are only allowed to use an empty extension route.
        # A keyed route such as "9223" belongs to a specific browser/account
        # and must never be borrowed by another token just because it is the
        # only extension online.
        # Round-robin across ALL connected empty-route browsers so reCAPTCHA
        # minting load is spread across them (each browser/IP stays under
        # Google's rate limit) instead of hammering a single one.
        empty_conns = [c for c in self.active_connections if not c.route_key]
        if not empty_conns:
            return None
        self._rr_index = (self._rr_index + 1) % len(empty_conns)
        return empty_conns[self._rr_index % len(empty_conns)]

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

    async def handle_message(self, websocket: WebSocket, data: str):
        try:
            payload = json.loads(data)
            message_type = payload.get("type")

            if message_type == "register":
                conn = self._find_connection(websocket)
                if conn:
                    conn.route_key = (payload.get("route_key") or conn.route_key or "").strip()
                    conn.client_label = (payload.get("client_label") or conn.client_label or "").strip()
                    debug_logger.log_info(
                        f"[Extension Captcha] Client registered route_key={conn.route_key or '-'}, "
                        f"label={conn.client_label or '-'}"
                    )
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

    async def request_session_refresh(self, token_id: Optional[int], timeout: int = 30) -> dict:
        """Tell the worker browser BOUND to this token to refresh its Google Labs
        session NOW (read the live cookie, push a fresh ST), and return the result.

        Targets only the extension whose route_key matches the token's
        extension_route_key. Refuses to act on an empty route_key: an empty key
        would round-robin to a RANDOM shared-pool browser (_select_connection)
        and read the WRONG account's cookie — so we return ``not_bound`` instead.
        Only works while that browser is still logged in; a logged-out session
        surfaces as ``logged_out`` (no command can recover it).
        """
        route_key = await self._resolve_route_key(token_id)
        if not route_key:
            return {"status": "not_bound", "token_id": token_id}
        conn = self._select_connection(route_key)
        if conn is None:
            return {
                "status": "no_browser",
                "route_key": route_key,
                "available": self._describe_routes() or "none",
            }

        req_id = f"refresh_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future
        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching session refresh via route_key={route_key}, "
                f"label={conn.client_label or '-'}, token_id={token_id}, req_id={req_id}"
            )
            await conn.websocket.send_text(json.dumps({
                "type": "refresh_session",
                "req_id": req_id,
                "route_key": route_key,
                "token_id": token_id,
            }))
            result = await asyncio.wait_for(future, timeout=timeout)
            return result if isinstance(result, dict) else {"status": "error", "error": "malformed ack"}
        except asyncio.TimeoutError:
            debug_logger.log_error(f"[Extension Captcha] Timeout waiting for session refresh (req_id: {req_id})")
            return {"status": "timeout", "req_id": req_id, "route_key": route_key}
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Session refresh communication error: {e}")
            return {"status": "error", "error": str(e)}
        finally:
            self.pending_requests.pop(req_id, None)

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        _ = project_id, error_message
        debug_logger.log_warning(f"[Extension Captcha] Flow error reported (ignoring): {error_reason}")
