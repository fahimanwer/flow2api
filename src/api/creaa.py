"""REST + WebSocket surface for the browser-backed Creaa provider.

Wiring (done by ``src/main.py``)::

    service = CreaaBridge(db_path=Path(...))
    creaa.set_service(service)
    app.include_router(creaa.router)
    # lifespan: await service.start() ... await service.close()

Every REST route is authenticated with the existing API key through
``verify_api_key_flexible``; the worker WebSocket (``/creaa_ws``) accepts the same key
via ``?key=``, ``x-goog-api-key`` or a bearer header, mirroring ``/captcha_ws``.

Generation endpoints are ASYNCHRONOUS: they return 202 with a job that must be
polled at ``GET /v1/creaa/jobs/{id}``. This is not drop-in OpenAI Images compatibility.
"""
from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core.auth import AuthManager, verify_api_key_flexible
from ..core.creaa_models import ALL_STATES, CreaaValidationError
from ..core.logger import debug_logger
from ..services.creaa_bridge import CreaaBridge

router = APIRouter(tags=["creaa"])

_service: Optional[CreaaBridge] = None


def set_service(service: Optional[CreaaBridge]) -> None:
    global _service
    _service = service


def _svc() -> CreaaBridge:
    if _service is None:
        raise HTTPException(status_code=503, detail={"code": "creaa_unavailable", "message": "Creaa bridge is not configured"})
    return _service


def _http_error(exc: CreaaValidationError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.as_detail())


class ResolveRequest(BaseModel):
    """Narrow, explicit schema: exactly what an operator may assert about an uncertain job."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["resume", "fail"]
    provider_task_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    confirm_no_upstream_work: bool = False
    note: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------------- catalog / workers

@router.get("/v1/creaa/models")
async def creaa_models(_: str = Depends(verify_api_key_flexible)):
    service = _svc()
    return {"object": "list", "data": service.list_models()}


@router.get("/v1/creaa/accounts")
async def creaa_accounts(_: str = Depends(verify_api_key_flexible)):
    service = _svc()
    return {"object": "list", "data": service.list_accounts()}


# ---------------------------------------------------------------------- generations

async def _create_job(media_type: str, body: Dict[str, Any], idempotency_key: Optional[str]) -> JSONResponse:
    service = _svc()
    try:
        job, created = await service.submit(media_type, body, idempotency_key)
    except CreaaValidationError as exc:
        raise _http_error(exc)
    headers = {"Location": f"/v1/creaa/jobs/{job['id']}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(status_code=202, content=job, headers=headers)


@router.post("/v1/creaa/images/generations", status_code=202)
async def creaa_image_generation(
    body: Dict[str, Any] = Body(...),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(verify_api_key_flexible),
):
    return await _create_job("image", body, idempotency_key)


@router.post("/v1/creaa/videos/generations", status_code=202)
async def creaa_video_generation(
    body: Dict[str, Any] = Body(...),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    _: str = Depends(verify_api_key_flexible),
):
    return await _create_job("video", body, idempotency_key)


# ---------------------------------------------------------------------- jobs

@router.get("/v1/creaa/jobs")
async def creaa_list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    account_id: Optional[str] = Query(default=None, max_length=128),
    state: Optional[str] = Query(default=None),
    _: str = Depends(verify_api_key_flexible),
):
    service = _svc()
    if state is not None and state not in ALL_STATES:
        raise HTTPException(status_code=400, detail={"code": "invalid_state", "message": f"state must be one of {', '.join(ALL_STATES)}"})
    return {"object": "list", "data": service.list_jobs(limit=limit, account_id=account_id, state=state)}


@router.get("/v1/creaa/jobs/{job_id}")
async def creaa_get_job(job_id: str, _: str = Depends(verify_api_key_flexible)):
    service = _svc()
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "job not found"})
    return job


@router.post("/v1/creaa/jobs/{job_id}/cancel")
async def creaa_cancel_job(job_id: str, _: str = Depends(verify_api_key_flexible)):
    service = _svc()
    try:
        return await service.cancel(job_id)
    except CreaaValidationError as exc:
        raise _http_error(exc)


@router.post("/v1/creaa/jobs/{job_id}/resolve")
async def creaa_resolve_job(job_id: str, body: ResolveRequest, _: str = Depends(verify_api_key_flexible)):
    service = _svc()
    try:
        return await service.resolve(
            job_id,
            action=body.action,
            provider_task_id=body.provider_task_id,
            confirm_no_upstream_work=body.confirm_no_upstream_work,
            note=body.note,
        )
    except CreaaValidationError as exc:
        raise _http_error(exc)


# ---------------------------------------------------------------------- worker websocket

@router.websocket("/creaa_ws")
async def creaa_websocket_endpoint(websocket: WebSocket):
    api_key = (
        websocket.query_params.get("key")
        or websocket.query_params.get("api_key")
        or websocket.headers.get("x-goog-api-key")
        or ""
    ).strip()
    authorization = (websocket.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        api_key = authorization[7:].strip()
    if not api_key or not AuthManager.verify_api_key(api_key):
        await websocket.close(code=1008)
        return
    if _service is None:
        await websocket.close(code=1013)
        return

    service = _service
    await service.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await service.handle_message(websocket, data)
    except WebSocketDisconnect:
        await service.disconnect(websocket)
    except Exception as exc:
        debug_logger.log_error(f"[Creaa] websocket error: {type(exc).__name__}: {exc}")
        await service.disconnect(websocket)
