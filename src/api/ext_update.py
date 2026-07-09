"""Worker-extension self-update distribution.

An extension loaded unpacked cannot silently auto-update its own code (a hard
Chrome rule), and this extension can't go on the Chrome Web Store (its reCAPTCHA
minting + residential proxying violate store policy). So instead of hand-sharing
zips, we host ONE canonical package here and let the extension *tell* staff when
they're behind:

  GET /api/plugin/ext-version   (connection-token authed) -> {version}
      the extension compares this to its own manifest version and, if behind,
      shows an "Update available" banner in the popup.
  GET /download/worker-latest.zip?token=<connection_token>
      the banner's Download button; serves the current package.
  POST /api/ext/upload          (admin authed, raw zip body)
      publish a new package from the admin "Publish extension" page — no SSH,
      no zip juggling. "Latest version" is read straight from the uploaded
      zip's manifest.json, so it can never drift from the actual bytes served.

The package lives under the persistent data volume (survives redeploys), next to
flow.db.
"""

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Optional

# A worker-extension zip is a few hundred KB; cap well above that but bounded so an
# accidental/huge upload can't balloon memory (the whole body is read in-process).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")  # Chrome manifest version: 1-4 dot-separated ints

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ..core.logger import debug_logger

# Persistent data volume (same dir that holds flow.db) so the package survives
# container redeploys — the code dir and static/ are replaced on every deploy.
DATA_DIR = Path(__file__).parent.parent.parent / "data"
EXT_DIR = DATA_DIR / "ext"
EXT_ZIP = EXT_DIR / "worker-latest.zip"

router = APIRouter()

# Wired from main.py (mirrors admin.set_dependencies) so this module is import-safe.
db = None
_verify_admin_token = None


def set_dependencies(database, verify_admin_token):
    global db, _verify_admin_token
    db = database
    _verify_admin_token = verify_admin_token


def _read_zip_version() -> Optional[str]:
    """Version straight from the uploaded package's manifest.json — never a
    separate field that could drift from the bytes we actually serve. Tolerates
    the manifest being at the zip root OR nested one level (folder-zipped)."""
    if not EXT_ZIP.exists():
        return None
    try:
        with zipfile.ZipFile(EXT_ZIP) as z:
            manifests = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == "manifest.json"]
            if not manifests:
                return None
            # Shallowest manifest = the extension root.
            manifests.sort(key=lambda n: n.count("/"))
            with z.open(manifests[0]) as m:
                version = str(json.load(m).get("version") or "").strip()
                return version or None
    except Exception as e:
        debug_logger.op_warning(f"[EXT_UPDATE] could not read package version: {e}")
        return None


async def _require_connection_token(authorization: Optional[str]) -> None:
    plugin_config = await db.get_plugin_config()
    provided = authorization[7:] if (authorization or "").startswith("Bearer ") else (authorization or "")
    if not plugin_config.connection_token or provided != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")


@router.get("/api/plugin/ext-version")
async def ext_version(authorization: Optional[str] = Header(None)):
    """Latest published extension version. The worker extension polls this and
    compares against its own manifest version to decide whether to nag."""
    await _require_connection_token(authorization)
    return {"success": True, "version": _read_zip_version()}


@router.get("/download/worker-latest.zip")
async def ext_download(token: str = Query(...)):
    """Serve the current package. Query-token authed (not a header) because this
    is opened as a plain browser download from the popup's Download button."""
    plugin_config = await db.get_plugin_config()
    if not plugin_config.connection_token or token != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    if not EXT_ZIP.exists():
        raise HTTPException(status_code=404, detail="No extension package has been published yet")
    from fastapi.responses import FileResponse
    version = _read_zip_version() or "latest"
    return FileResponse(
        str(EXT_ZIP),
        media_type="application/zip",
        filename=f"flow2api-worker-{version}.zip",
    )


@router.get("/api/ext/status")
async def ext_status(request: Request):
    """Current published package info for the admin 'Publish extension' page."""
    if _verify_admin_token is None:
        raise HTTPException(status_code=503, detail="Not ready")
    await _verify_admin_token(request, request.headers.get("authorization"))
    exists = EXT_ZIP.exists()
    return {
        "success": True,
        "version": _read_zip_version() if exists else None,
        "size": EXT_ZIP.stat().st_size if exists else 0,
        "published_at": int(EXT_ZIP.stat().st_mtime) if exists else None,
    }


@router.post("/api/ext/upload")
async def ext_upload(request: Request):
    """Publish a new package (admin only). Body is the raw .zip bytes. We validate
    it really is an extension zip (parseable manifest.json with a version) BEFORE
    replacing the live file, so a bad upload can never take distribution down."""
    # Admin gate — the real checker is wired via set_dependencies; call it with the
    # request's own Authorization header (reuses the console's admin session).
    if _verify_admin_token is None:
        raise HTTPException(status_code=503, detail="Not ready")
    await _verify_admin_token(request, request.headers.get("authorization"))

    contents = await request.body()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Package too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as z:
            manifests = [n for n in z.namelist() if n.rsplit("/", 1)[-1] == "manifest.json"]
            if not manifests:
                raise ValueError("no manifest.json in zip")
            manifests.sort(key=lambda n: n.count("/"))
            with z.open(manifests[0]) as m:
                version = str(json.load(m).get("version") or "").strip()
        if not version:
            raise ValueError("manifest.json has no version")
        if not _VERSION_RE.match(version):
            raise ValueError(f"manifest version {version!r} is not a valid dotted-numeric version")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Not a valid extension zip: {e}")

    EXT_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a concurrent download never sees a half-written file.
    tmp = EXT_ZIP.with_suffix(".zip.tmp")
    tmp.write_bytes(contents)
    tmp.replace(EXT_ZIP)
    debug_logger.event(f"[EXT_UPDATE] published extension package v{version} ({len(contents)} bytes)")
    return {"success": True, "version": version, "size": len(contents)}
