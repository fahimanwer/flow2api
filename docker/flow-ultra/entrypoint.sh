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
# Chromium keeps the extension's service-worker SCRIPT in its service-worker cache and, for an unpacked
# extension, fires "installed" on a version change but keeps running the OLD cached script (seen twice on
# this box: 3.5.1 on 2026-09-22, 3.7.3 on 2026-09-24). Dropping the cache forces a fresh read of every
# worker script at start; sites' service workers simply re-register. Cookies/logins are untouched.
rm -rf "/profile/browser/Default/Service Worker"
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
# only OUR old release paths; Chrome itself lives in /opt/google and its built-ins must stay
gone = [k for k, v in s.items() if str(v.get("path", "")).startswith(("/opt/releases/", "/opt/ext/")) and v.get("path") != "/opt/ext"]
for k in gone: del s[k]
if gone:
    json.dump(p, open(f, "w")); print("pruned stale extension entries:", gone)
PRUNE
echo "extension release $ver"

rm -f /tmp/fu-ready   # set only after the extension is registered in THIS start (ext-sync.sh checks it)
# Every byte through the box's fixed upstream proxy from the first request (proxy-forward.py). No site.json = no
# forwarder (then Chrome goes direct, like a staff laptop).
PROXY_ARGS=()
FWD=""
if [ -f /opt/ext/site.json ]; then
  python3 /opt/proxy-forward.py >/profile/logs/proxy-forward.log 2>&1 &
  FWD=$!
  for i in $(seq 1 50); do (echo > /dev/tcp/127.0.0.1/3128) 2>/dev/null && break; sleep 0.1; done
  PROXY_ARGS=(--proxy-server=http://127.0.0.1:3128 --proxy-bypass-list="<-loopback>" --force-webrtc-ip-handling-policy=disable_non_proxied_udp)
fi
# `docker restart` keeps the container /tmp: a stale X lock from the previous run would make Xvfb exit at
# once ("Server is already active for display 99") and the container restart forever (2026-09-24).
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 "${WINDOW}x24" -nolisten tcp -ac +extension RANDR >/profile/logs/xvfb.log 2>&1 &
XVFB=$!
for i in $(seq 1 50); do xdpyinfo -display :99 >/dev/null 2>&1 && break; sleep 0.1; done
openbox >/profile/logs/openbox.log 2>&1 &
WM=$!
x11vnc -display :99 -listen "$(hostname -i | awk '{print $1}')" -rfbport 5900 -forever -shared -nopw -quiet >/profile/logs/x11vnc.log 2>&1 &
VNC=$!
# Branded Chrome ignores --load-extension (137+): the extension is registered ONCE in the profile through
# chrome://extensions → Load unpacked → /opt/ext (see README). Chrome then reloads it from /opt/ext at every start,
# so the updater's release swap + restart still delivers new versions. The flag stays for Chromium builds.
"${FU_BROWSER:-google-chrome-stable}" --user-data-dir=/profile/browser --load-extension=/opt/ext \
  --no-first-run --no-default-browser-check --disable-session-crashed-bubble --hide-crash-restore-bubble \
  --disable-features=TranslateUI --window-size="${WINDOW/x/,}" --window-position=0,0 --lang=en-US \
  --password-store=basic --no-sandbox --test-type \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --enable-unsafe-extension-debugging \
  "${PROXY_ARGS[@]}" \
  "${FU_START_URL:-about:blank}" >/profile/logs/browser.log 2>&1 &
BR=$!
# Branded Chrome ignores --load-extension, the Load unpacked dialog cannot open here, and an extension loaded through
# CDP Extensions.loadUnpacked lasts one browser session (verified 2026-09-26: gone from the profile after a restart).
# So register /opt/ext at EVERY start. Same path => same extension id => its storage (route key etc.) is kept.
# The debug port is bound to the container's loopback only (the pinterest fleet runs Chrome the same way).
( for i in $(seq 1 60); do
    curl -fs http://127.0.0.1:9222/json/version >/dev/null 2>&1 && break; sleep 1
  done
  for try in 1 2 3 4 5; do
    python3 /opt/register-ext.py >>/profile/logs/register-ext.log 2>&1 && { touch /tmp/fu-ready; exit 0; }
    sleep 3
  done
  echo "extension registration FAILED 5 times — stopping the container so Docker restarts it" >&2
  kill -TERM 1 ) &

stopping=0
cleanup() {
  [ "$stopping" = 1 ] && return; stopping=1
  # 1. Chromium first, and WAIT for it (up to 25 s; docker stop gives us 30) so the profile is flushed.
  kill -TERM "$BR" 2>/dev/null || true
  for i in $(seq 1 250); do kill -0 "$BR" 2>/dev/null || break; sleep 0.1; done
  kill -0 "$BR" 2>/dev/null && { echo "browser did not exit in 25 s; killing" >&2; kill -KILL "$BR" 2>/dev/null || true; }
  # 2. then the display stack (and wait for Xvfb so it removes its lock)
  kill -TERM "$VNC" "$WM" "$XVFB" $FWD 2>/dev/null || true
  for i in $(seq 1 30); do kill -0 "$XVFB" 2>/dev/null || break; sleep 0.1; done
}
trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT
while :; do
  for p in $XVFB $WM $VNC $BR $FWD; do
    kill -0 "$p" 2>/dev/null || { echo "child $p exited" >&2; exit 1; }
  done
  sleep 2 & wait $!   # interruptible sleep so a TERM is handled at once
done
