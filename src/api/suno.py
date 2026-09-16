"""Suno provider HTTP surface.

Two authentication tiers, matching the rest of this backend:

* generation and read endpoints take the shared Flow2API key;
* anything that imports, replaces, limits or deletes account credentials, or
  resolves a stuck job, requires an admin session.

Nothing here ever returns a cookie, a Clerk session id or a JWT.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..core.auth import verify_api_key_flexible
from ..core.suno_models import SunoConflictError, SunoValidationError, list_models
from ..services.suno_client import SunoAPIError
from ..services.suno_service import SunoService
from .admin import verify_admin_token

router = APIRouter(tags=["suno"])

_service: Optional[SunoService] = None


def set_service(service: Optional[SunoService]) -> None:
    global _service
    _service = service


def _svc() -> SunoService:
    if _service is None:
        raise HTTPException(status_code=503, detail={
            "error": {"code": "suno_unavailable", "message": "The Suno provider is not configured."}
        })
    return _service


def _http_error(exc: SunoValidationError) -> HTTPException:
    status = 409 if isinstance(exc, SunoConflictError) else 400
    status = int(exc.extra.get("http_status", status))
    body: Dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    extra = {k: v for k, v in exc.extra.items() if k != "http_status"}
    if extra:
        body["error"]["details"] = extra
    return HTTPException(status_code=status, detail=body)


def _upstream_error(exc: SunoAPIError) -> HTTPException:
    status = 502 if exc.status_code in (0, None) else int(exc.status_code)
    if status < 400:
        status = 502
    if status in (401, 403):
        # The caller's key was fine; it is our Suno session that was refused.
        status = 502
    return HTTPException(status_code=status, detail={
        "error": {"code": exc.code or "suno_upstream_error", "message": exc.message}
    })


# ------------------------------------------------------------ request bodies

class AccountImportRequest(BaseModel):
    cookie: str
    display_name: Optional[str] = None
    account_id: Optional[int] = None


class AccountLimitRequest(BaseModel):
    operator_limit: Optional[int] = None
    provider_cap: Optional[int] = None


class ResolveRequest(BaseModel):
    action: str
    clip_ids: Optional[list] = None
    confirm_no_upstream_work: bool = False
    note: str = ""


# ------------------------------------------------------------- caller routes

@router.get("/v1/suno/models")
async def suno_models(_: str = Depends(verify_api_key_flexible)):
    """Model catalogue. Capability discovery, not an entitlement claim: Suno
    gates newer models by plan and rejects the rest per generation."""
    return {"object": "list", "data": list_models()}


@router.post("/v1/suno/music/generations", status_code=202)
async def suno_generate(
    body: Dict[str, Any] = Body(...),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    _: str = Depends(verify_api_key_flexible),
):
    payload = dict(body or {})
    if idempotency_key and "idempotency_key" not in payload:
        payload["idempotency_key"] = idempotency_key
    try:
        job = await _svc().submit(payload)
    except SunoValidationError as exc:
        raise _http_error(exc)
    return JSONResponse(status_code=202, content=job)


@router.get("/v1/suno/jobs")
async def suno_list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    state: Optional[str] = Query(None),
    _: str = Depends(verify_api_key_flexible),
):
    try:
        return await _svc().list_jobs(limit=limit, offset=offset, state=state)
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.get("/v1/suno/jobs/{job_id}")
async def suno_get_job(job_id: str, _: str = Depends(verify_api_key_flexible)):
    job = await _svc().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "unknown_job", "message": f"No Suno job {job_id}."}
        })
    return job


@router.post("/v1/suno/jobs/{job_id}/cancel")
async def suno_cancel_job(job_id: str, _: str = Depends(verify_api_key_flexible)):
    try:
        return await _svc().cancel(job_id)
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.post("/v1/suno/jobs/{job_id}/retry")
async def suno_retry_job(job_id: str, _: str = Depends(verify_api_key_flexible)):
    """Re-queue a job that a captcha gate blocked before submission."""
    try:
        return await _svc().retry_blocked(job_id)
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.get("/v1/suno/jobs/{job_id}/audio/{clip_id}")
async def suno_job_audio(
    job_id: str,
    clip_id: str,
    request: Request,
    format: str = Query("mp3"),
    _: str = Depends(verify_api_key_flexible),
):
    """Stream one clip's audio through Flow2API.

    Suno's own ``audio_url`` is a forbidden stub and its media entries are
    encrypted, so the download endpoint is re-asked per request with the owning
    account's credentials. The upstream connection is closed if the caller goes
    away.
    """
    service = _svc()
    try:
        # Resolve eagerly so validation errors become a JSON status, not a
        # half-written stream body.
        stream = service.stream_audio(job_id, clip_id, format)
        first = await stream.__anext__()
    except StopAsyncIteration:
        first = b""
        stream = None
    except SunoValidationError as exc:
        raise _http_error(exc)
    except SunoAPIError as exc:
        raise _upstream_error(exc)

    media_type = "audio/mpeg" if format == "mp3" else "audio/mp4"

    async def body():
        try:
            if first:
                yield first
            if stream is not None:
                async for chunk in stream:
                    if await request.is_disconnected():
                        break
                    yield chunk
        finally:
            if stream is not None:
                await stream.aclose()

    return StreamingResponse(
        body(),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{clip_id}.{format}"'},
    )


@router.get("/v1/suno/accounts")
async def suno_accounts(_: str = Depends(verify_api_key_flexible)):
    """Connected accounts, projected. Credentials are never included."""
    return {"accounts": await _svc().list_accounts()}


# -------------------------------------------------------------- admin routes

@router.post("/api/suno/accounts")
async def suno_import_account(
    body: AccountImportRequest, _: str = Depends(verify_admin_token)
):
    """Import or replace a Suno cookie. Admin session required."""
    try:
        return await _svc().import_account(
            body.cookie, body.display_name or "", body.account_id
        )
    except SunoValidationError as exc:
        raise _http_error(exc)
    except SunoAPIError as exc:
        raise _upstream_error(exc)


@router.put("/api/suno/accounts/{account_id}/limits")
async def suno_set_limits(
    account_id: int, body: AccountLimitRequest, _: str = Depends(verify_admin_token)
):
    try:
        return await _svc().set_account_limit(
            account_id, body.operator_limit, body.provider_cap
        )
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.post("/api/suno/accounts/{account_id}/enable")
async def suno_enable_account(account_id: int, _: str = Depends(verify_admin_token)):
    try:
        return await _svc().set_account_enabled(account_id, True)
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.post("/api/suno/accounts/{account_id}/disable")
async def suno_disable_account(account_id: int, _: str = Depends(verify_admin_token)):
    try:
        return await _svc().set_account_enabled(account_id, False)
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.delete("/api/suno/accounts/{account_id}")
async def suno_delete_account(
    account_id: int,
    retire_audio: bool = Query(False),
    _: str = Depends(verify_admin_token),
):
    try:
        return await _svc().delete_account(account_id, retire_audio)
    except SunoValidationError as exc:
        raise _http_error(exc)


@router.post("/api/suno/accounts/{account_id}/refresh-billing")
async def suno_refresh_billing(account_id: int, _: str = Depends(verify_admin_token)):
    await _svc().refresh_account_billing(account_id)
    accounts = await _svc().list_accounts()
    for account in accounts:
        if account["id"] == account_id:
            return account
    raise HTTPException(status_code=404, detail={
        "error": {"code": "unknown_account", "message": f"No Suno account {account_id}."}
    })


@router.post("/api/suno/jobs/{job_id}/resolve")
async def suno_resolve_job(
    job_id: str, body: ResolveRequest, _: str = Depends(verify_admin_token)
):
    """Operator resolution for a job whose upstream state is unknown."""
    try:
        return await _svc().resolve(
            job_id, body.action, body.clip_ids,
            body.confirm_no_upstream_work, body.note,
        )
    except SunoValidationError as exc:
        raise _http_error(exc)
