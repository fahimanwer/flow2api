#!/usr/bin/env bash
# flow2api-alert-wake — wake the flow2api T3 thread when flow2api raises a problem alert (owner order 2026-11-05:
# "if anything breaks in flow ... it can message you and wake you up so that you can fix it automatically").
#
# Runs every minute (systemd timer) as root on the flow2api host (78.46.131.169). Reads NEW rows of flow2api's alert
# outbox `ultra_alerts` (the same rows flow2api sends to Telegram), read-only. Problem rows become ONE message posted
# into the flow2api T3 thread on the owner's Mac through T3's own API (wakes it even when idle or settled).
# If the Mac's T3 cannot be reached for FALLBACK_AFTER seconds, the message goes to the CC box's fleet-guard watch
# thread instead. Recoveries never wake anyone. Telegram delivery is untouched (flow2api does that itself).
#
# Usage: flow2api-alert-wake            one pass (the timer)
#        flow2api-alert-wake --dry-run  print what the next pass would post; changes nothing
#        flow2api-alert-wake --test mac|box   post a harmless TEST message to that thread; changes no state
# Config: /etc/flow2api-alert-wake/env (MAC_ORIGIN, MAC_THREAD, BOX_ORIGIN, BOX_THREAD); tokens (root 0600) in
# /etc/flow2api-alert-wake/mac.token and box.token. Stop: systemctl disable --now flow2api-alert-wake.timer
set -uo pipefail
CONF_DIR=${CONF_DIR:-/etc/flow2api-alert-wake}
STATE=${STATE:-/var/lib/flow2api-alert-wake}
LOG=${LOG:-/var/log/flow2api-alert-wake.log}
DB=${DB:-/var/lib/docker/volumes/jc4044co4w8w0g8w0g4ks0ws-data/_data/flow.db}
POST=${POST:-/usr/local/lib/flow2api-alert-wake/t3-post.mjs}
WAKE_KINDS="'fleet_down','fleet_degraded','host_silent','logged_out','needs_you','error'"   # fleet_ok/host_ok/ok = recoveries
FALLBACK_AFTER=${FALLBACK_AFTER:-900}
MAX_PER_HOUR=${MAX_PER_HOUR:-4}
# shellcheck disable=SC1091
. "$CONF_DIR/env"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
post() {  # $1 = mac|box, message on stdin; prints the poster's last line
  local origin thread token
  if [ "$1" = mac ]; then origin=$MAC_ORIGIN thread=$MAC_THREAD token=$CONF_DIR/mac.token
  else origin=$BOX_ORIGIN thread=$BOX_THREAD token=$CONF_DIR/box.token; fi
  timeout 30 node "$POST" "$origin" "$token" "$thread" 2>&1 | tail -1
}
record_post() { echo "$now" >> "$STATE/posts"; tail -n 50 "$STATE/posts" > "$STATE/posts.tmp" && mv "$STATE/posts.tmp" "$STATE/posts"; }
FOOTER="Sent by flow2api-alert-wake on 78.46.131.169 (docker/alert-wake/ in the flow2api repo; stop: systemctl disable --now flow2api-alert-wake.timer)."

mkdir -p "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0

if [ "${1:-}" = "--test" ]; then
  target=${2:-mac}
  out=$(printf '[flow2api-alert TEST] Wake-up test of flow2api-alert-wake (no problem, nothing to fix). Reply with one line: "wake test received".\n%s\n' "$FOOTER" | post "$target")
  echo "$target: $out"; log "test $target: $out"
  case "$out" in SENT*) exit 0;; *) exit 1;; esac
fi

cursor=$(cat "$STATE/cursor" 2>/dev/null || true)
if ! [[ "$cursor" =~ ^[0-9]+$ ]]; then  # first run: start after what is already there, never replay history
  cursor=$(sqlite3 -readonly "$DB" "SELECT COALESCE(MAX(id), 0) FROM ultra_alerts") || { log "db read failed"; exit 1; }
  [ "${1:-}" = "--dry-run" ] && { echo "first run: would start after alert id $cursor"; exit 0; }
  echo "$cursor" > "$STATE/cursor"; log "started after alert id $cursor"; exit 0
fi

rows=$(sqlite3 -readonly -separator $'\t' "$DB" \
  "SELECT id, kind, created_at, replace(replace(replace(text, char(10), ' '), char(13), ' '), char(9), ' ')
   FROM ultra_alerts WHERE id > $cursor ORDER BY id LIMIT 50") || { log "db read failed"; exit 1; }
[ -z "$rows" ] && { rm -f "$STATE/fail-since"; exit 0; }
last=$(tail -1 <<<"$rows" | cut -f1)
problems=$(awk -F'\t' -v kinds="$WAKE_KINDS" 'index(kinds, "'"'"'" $2 "'"'"'") { printf "- %s %s: %s\n", $3, $2, $4 }' <<<"$rows")

if [ -z "$problems" ]; then  # only recoveries: nothing to wake
  [ "${1:-}" = "--dry-run" ] && { echo "only recoveries up to id $last: no wake"; exit 0; }
  echo "$last" > "$STATE/cursor"; rm -f "$STATE/fail-since"; exit 0
fi

msg=$(printf '%s\n%s\n%s\n%s\n' \
  "[flow2api-alert] flow2api raised a problem on the server (the same text went to Telegram):" \
  "$problems" \
  "You are the flow2api session (this thread). Diagnose and fix it: read WORK.md and the docs, use the diagnosing-bugs skill, check the server first (container logs, request_logs, accounts), fix only what you can prove, ship through the normal PR flow, verify live, then send the owner a short full report. If it needs a person (a Google login, a phone tap), say exactly what and stop." \
  "$FOOTER")

if [ "${1:-}" = "--dry-run" ]; then echo "would post (alerts up to id $last):"; echo "$msg"; exit 0; fi

now=$(date +%s)
recent=$(awk -v t=$((now - 3600)) '$1 > t' "$STATE/posts" 2>/dev/null | wc -l)
if [ "$recent" -ge "$MAX_PER_HOUR" ]; then  # storm guard: hold the rows (cursor unchanged); they go out together later
  log "storm guard: $recent wakes in the last hour, holding alerts up to id $last"
  exit 0
fi

out=$(printf '%s\n' "$msg" | post mac)
case "$out" in
  SENT*)
    echo "$last" > "$STATE/cursor"; rm -f "$STATE/fail-since"; record_post
    log "woke the Mac thread (alerts up to id $last): $out"; exit 0;;
esac
log "Mac post failed: $out"
since=$(cat "$STATE/fail-since" 2>/dev/null || true)
[[ "$since" =~ ^[0-9]+$ ]] || { since=$now; echo "$now" > "$STATE/fail-since"; }
[ $((now - since)) -lt "$FALLBACK_AFTER" ] && exit 1   # retry next minute; cursor unchanged

out=$(printf '%s\n%s\n' \
  "[flow2api-alert via the CC box] The flow2api thread on the owner's Mac could not be reached since $(date -u -d "@$since" +%FT%TZ). This is a flow2api app problem: handle it per your README (app bug -> delegate_task in ~/projects/flow2api with these facts)." \
  "$msg" | post box)
case "$out" in
  SENT*)
    echo "$last" > "$STATE/cursor"; rm -f "$STATE/fail-since"; record_post
    log "Mac unreachable for $((now - since)) s: woke the box watch thread instead (alerts up to id $last): $out"; exit 0;;
esac
log "box post failed too: $out"; exit 1
