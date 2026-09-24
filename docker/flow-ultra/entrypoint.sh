#!/usr/bin/env bash
# flow-ultra container entrypoint: virtual display → window manager → VNC (container IP only) → Chromium with the
# worker extension. The extension release is picked from the read-only mount /opt/releases/<version> named in
# /opt/releases/current, and COPIED to the fixed real path /opt/ext (Chromium derives the extension id from the
# resolved path; a symlink would change the id on every release and reset its storage/registration).
# Shutdown: Chromium is terminated FIRST and waited for (it flushes cookies/profile), then the rest.
set -euo pipefail
export DISPLAY=:99
WINDOW="${FU_WINDOW:-1400x1000}"
mkdir -p /profile/browser /profile/logs
rm -f /profile/browser/SingletonLock /profile/browser/SingletonSocket /profile/browser/SingletonCookie
ver="$(cat /opt/releases/current 2>/dev/null || true)"
[ -n "$ver" ] && [ -f "/opt/releases/$ver/manifest.json" ] || { echo "no usable release in /opt/releases (current='$ver')" >&2; exit 1; }
# A real directory, not a symlink: Chromium resolves symlinks when deriving the extension id, so a link would give
# every release a NEW id (fresh storage, new route key → the account's binding breaks). Copy (≈100 KB) instead.
rm -rf /opt/ext && cp -a "/opt/releases/$ver" /opt/ext
# Only /opt/ext may be an installed extension. An unpacked extension loaded once from any other path stays
# registered in the profile and keeps loading (a second worker with its own identity ran for an hour on
# 2026-09-24 after a symlink experiment). Prune such entries before Chromium starts.
python3 - <<'PRUNE' || true
import json
f = "/profile/browser/Default/Preferences"
try:
    p = json.load(open(f))
except Exception:
    raise SystemExit
s = p.get("extensions", {}).get("settings", {})
gone = [k for k, v in s.items() if str(v.get("path", "")).startswith("/opt/") and v.get("path") != "/opt/ext"]
for k in gone: del s[k]
if gone:
    json.dump(p, open(f, "w")); print("pruned stale extension entries:", gone)
PRUNE
echo "extension release $ver"

Xvfb :99 -screen 0 "${WINDOW}x24" -nolisten tcp -ac +extension RANDR >/profile/logs/xvfb.log 2>&1 &
XVFB=$!
for i in $(seq 1 50); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.1; done
openbox >/profile/logs/openbox.log 2>&1 &
WM=$!
x11vnc -display :99 -listen "$(hostname -i | awk '{print $1}')" -rfbport 5900 -forever -shared -nopw -quiet >/profile/logs/x11vnc.log 2>&1 &
VNC=$!
chromium --user-data-dir=/profile/browser --load-extension=/opt/ext \
  --no-first-run --no-default-browser-check --disable-session-crashed-bubble --hide-crash-restore-bubble \
  --disable-features=TranslateUI --window-size="${WINDOW/x/,}" --window-position=0,0 --lang=en-US \
  --password-store=basic --no-sandbox --test-type "${FU_START_URL:-about:blank}" >/profile/logs/browser.log 2>&1 &
BR=$!

stopping=0
cleanup() {
  [ "$stopping" = 1 ] && return; stopping=1
  # 1. Chromium first, and WAIT for it (up to 25 s; docker stop gives us 30) so the profile is flushed.
  kill -TERM "$BR" 2>/dev/null || true
  for i in $(seq 1 250); do kill -0 "$BR" 2>/dev/null || break; sleep 0.1; done
  kill -0 "$BR" 2>/dev/null && { echo "chromium did not exit in 25 s; killing" >&2; kill -KILL "$BR" 2>/dev/null || true; }
  # 2. then the display stack
  kill -TERM "$VNC" "$WM" "$XVFB" 2>/dev/null || true
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT
while :; do
  for p in $XVFB $WM $VNC $BR; do
    kill -0 "$p" 2>/dev/null || { echo "child $p exited" >&2; exit 1; }
  done
  sleep 2 & wait $!   # interruptible sleep so a TERM is handled at once
done
