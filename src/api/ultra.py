"""Ultra browsers: dashboard (admin session) and host-agent (ULTRA_AGENT_TOKEN) endpoints.

Logic lives in src/services/ultra_browsers.py. Slice A endpoints (status, ports, observe-only registration,
screenshots) are always available; Slice B ones (add account, lifecycle, challenge replies) refuse unless
ULTRA_BROWSERS_ENABLED=1 and ULTRA_VAULT_KEY is valid. No response ever carries a stored password.
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..core.logger import debug_logger
from ..services.ultra_browsers import UltraError, UltraService

router = APIRouter()

service: Optional[UltraService] = None
_verify_admin_token = None


def set_dependencies(ultra: UltraService, verify_admin_token) -> None:
    global service, _verify_admin_token
    service = ultra
    _verify_admin_token = verify_admin_token


async def _admin(request: Request, authorization: Optional[str] = Header(None)):
    return await _verify_admin_token(request, authorization)


def _svc() -> UltraService:
    if service is None:
        raise HTTPException(status_code=503, detail="Ultra browsers are not initialised")
    return service


def _fail(e: UltraError):
    return JSONResponse(status_code=e.status, content={"success": False, "detail": str(e), "blockers": e.blockers})


def _agent(authorization: Optional[str]) -> UltraService:
    svc = _svc()
    try:
        if not svc.agent_token_ok(authorization):
            raise HTTPException(status_code=401, detail="invalid agent token")
    except UltraError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    return svc


# ---------------------------------------------------------------- dashboard

@router.get("/api/ultra/status")
async def ultra_status(_: Any = Depends(_admin)):
    return {"success": True, **(await _svc().status())}


@router.post("/api/ultra/ports/check")
async def ultra_port_check(body: Dict[str, Any], _: Any = Depends(_admin)):
    try:
        return {"success": True, **(await _svc().check_port(int(body.get("port"))))}
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="port required")


@router.post("/api/ultra/ports")
async def ultra_port_reserve(body: Dict[str, Any], _: Any = Depends(_admin)):
    try:
        out = await _svc().reserve_port(
            int(body.get("port")), proxy_host=str(body.get("proxy_host") or ""),
            expected_egress_ip=str(body.get("expected_egress_ip") or ""), city=str(body.get("city") or ""),
            tz=str(body.get("timezone") or ""), note=str(body.get("note") or ""))
    except UltraError as e:
        return _fail(e)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="port required")
    return {"success": True, **out}


@router.post("/api/ultra/observe")
async def ultra_observe(body: Dict[str, Any], _: Any = Depends(_admin)):
    """Slice A: record a hand-made browser (flow-ultra-01/02) read-only."""
    try:
        tid = body.get("token_id")
        out = await _svc().register_observed(
            name=str(body.get("name") or ""), container=str(body.get("container") or ""), port=int(body.get("port")),
            token_id=int(tid) if tid not in (None, "") else None, proxy_host=str(body.get("proxy_host") or ""),
            expected_egress_ip=str(body.get("expected_egress_ip") or ""), city=str(body.get("city") or ""),
            tz=str(body.get("timezone") or ""), route_key=str(body.get("route_key") or ""))
    except UltraError as e:
        return _fail(e)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="name, container and port required")
    return {"success": True, **out}


@router.post("/api/ultra/browsers")
async def ultra_add_account(body: Dict[str, Any], _: Any = Depends(_admin)):
    """Slice B: add a new Ultra account (email + password + a free Ultra port + optional 'Only for app')."""
    try:
        out = await _svc().add_account(email=str(body.get("email") or ""), password=body.get("password") or "",
                                       port=int(body.get("port") or 0), reserved_client=str(body.get("reserved_client") or ""))
    except UltraError as e:
        return _fail(e)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="email, password and port required")
    return {"success": True, **out}


_ACTIONS = {"restart": "restart", "stop": "stop", "start": "start", "retry_login": "login",
            "update": "update", "status": "status", "screenshot": "screenshot"}


@router.post("/api/ultra/browsers/{name}/action")
async def ultra_action(name: str, body: Dict[str, Any], _: Any = Depends(_admin)):
    kind = _ACTIONS.get(str(body.get("action") or ""))
    if not kind:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(_ACTIONS)}")
    try:
        out = await _svc().request_job(name, kind, by="admin")
    except UltraError as e:
        return _fail(e)
    debug_logger.op_warning(f"[ULTRA] {name}: {kind} requested from the dashboard ({out['state']})")
    return {"success": True, **out}


@router.post("/api/ultra/browsers/{name}/challenge")
async def ultra_challenge(name: str, body: Dict[str, Any], _: Any = Depends(_admin)):
    try:
        out = await _svc().reply_challenge(name, str(body.get("attempt_id") or ""), str(body.get("challenge_id") or ""),
                                           str(body.get("kind") or ""), str(body.get("code") or ""))
    except UltraError as e:
        return _fail(e)
    return {"success": True, **out}


@router.get("/api/ultra/screenshots/{job_id}")
async def ultra_screenshot(job_id: str, _: Any = Depends(_admin)):
    png = _svc().get_screenshot(job_id)
    if png is None:
        raise HTTPException(status_code=404, detail="no screenshot (not taken yet, or older than 5 min)")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- host agent

@router.post("/api/ultra/agent/poll")
async def ultra_agent_poll(body: Dict[str, Any], authorization: Optional[str] = Header(None)):
    svc = _agent(authorization)
    host_id = str(body.get("host_id") or "").strip()
    if not host_id:
        raise HTTPException(status_code=400, detail="host_id required")
    obs = body.get("observations") if isinstance(body.get("observations"), dict) else {}
    running = body.get("running_jobs") if isinstance(body.get("running_jobs"), list) else []
    try:
        return await svc.agent_poll(host_id, str(body.get("agent_version") or ""), obs, running,
                                    accept_jobs=body.get("accept_jobs") is not False)
    except UltraError as e:
        return _fail(e)


@router.post("/api/ultra/agent/result")
async def ultra_agent_result(body: Dict[str, Any], authorization: Optional[str] = Header(None)):
    svc = _agent(authorization)
    try:
        return await svc.handle_result(str(body.get("host_id") or ""), str(body.get("job_id") or ""),
                                       str(body.get("outcome") or ""), body.get("result") if isinstance(body.get("result"), dict) else {})
    except UltraError as e:
        return _fail(e)
