#!/usr/bin/env bash
# Keep the server browser's worker extension equal to the package published in the Flow2API admin.
# Runs from flow-ultra-ext-sync.timer (every 10 min). Layout on the box:
#   /srv/flow-ultra/releases/<version>/   one folder per installed release (kept until a newer one is verified)
#   /srv/flow-ultra/releases/current      the version the container must load (read by entrypoint.sh at start)
#   /srv/flow-ultra/releases/pending      set while an activation is in flight; cleared only after the container
#                                         is verified running that version (so a failed restart is retried, never
#                                         silently treated as "up to date")
# Config: /etc/flow-ultra/sync.env (FLOW_BASE, CONNECTION_TOKEN, CONTAINER), /etc/flow-ultra/site.json (proxy,
# label, proxyAllHosts — box-only; copied INTO each release, never in git or the zip). `--force` reinstalls.
set -euo pipefail
exec 9>/run/flow-ultra-ext-sync.lock; flock -n 9 || { echo "another sync is running"; exit 0; }
# shellcheck disable=SC1091
. /etc/flow-ultra/sync.env
: "${FLOW_BASE:?}" "${CONNECTION_TOKEN:?}"; CONTAINER="${CONTAINER:-flow-ultra-01}"
REL=/srv/flow-ultra/releases; SITE=/etc/flow-ultra/site.json; FORCE="${1:-}"
mkdir -p "$REL"
ver_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$1" 2>/dev/null || echo none; }

activate() {  # $1 = version dir name: point current at it, restart, verify, clear pending
  local v="$1"
  printf '%s' "$v" > "$REL/pending"
  printf '%s' "$v" > "$REL/current.tmp" && mv -f "$REL/current.tmp" "$REL/current"
  echo "activating $v: restarting $CONTAINER (graceful, 30 s)"
  docker restart -t 30 "$CONTAINER" >/dev/null || { echo "restart failed; pending stays set"; return 1; }
  for i in $(seq 1 30); do
    sleep 2
    running=$(docker exec "$CONTAINER" sh -c 'cat /opt/ext/manifest.json 2>/dev/null' | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)
    if [ "$running" = "$v" ] && docker exec "$CONTAINER" pgrep -x chromium >/dev/null 2>&1; then
      rm -f "$REL/pending"; echo "verified: container runs $v"
      # keep this and the previous release only
      ls -1d "$REL"/*/ 2>/dev/null | sed 's#/$##' | grep -v "/$v$" | sort -V | head -n -1 | xargs -r rm -rf
      return 0
    fi
  done
  echo "container did not come up on $v within 60 s; pending stays set for the next run"; return 1
}

# 0. an earlier activation that never got verified → finish it before anything else
if [ -f "$REL/pending" ]; then
  p=$(cat "$REL/pending"); echo "pending activation of $p found"; activate "$p" || exit 1
fi

latest=$(curl -fsS --max-time 20 -H "Authorization: Bearer $CONNECTION_TOKEN" "$FLOW_BASE/api/plugin/ext-version" \
         | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))')
[ -n "$latest" ] || { echo "server did not report a version"; exit 1; }
current=$(cat "$REL/current" 2>/dev/null || echo none)
site_same=yes; cmp -s "$SITE" "$REL/$current/site.json" 2>/dev/null || site_same=no
if [ "$FORCE" != "--force" ] && [ "$latest" = "$current" ] && [ "$site_same" = yes ]; then
  echo "up to date: $current"; exit 0
fi

# 1. download + validate BEFORE touching the running container
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
curl -fsS --max-time 60 -o "$tmp/ext.zip" "$FLOW_BASE/download/worker-latest.zip?token=$CONNECTION_TOKEN"
mkdir "$tmp/ext" && python3 -c 'import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$tmp/ext.zip" "$tmp/ext"
if [ -f "$tmp/ext/manifest.json" ]; then inner="$tmp/ext"; else inner=$(dirname "$(find "$tmp/ext" -maxdepth 2 -name manifest.json | head -1)"); fi
got=$(ver_of "$inner/manifest.json")
[ "$got" = "$latest" ] || { echo "downloaded $got but server advertises $latest; not installing"; exit 1; }
for f in manifest.json background.js options.html options.js; do [ -f "$inner/$f" ] || { echo "package lacks $f; not installing"; exit 1; }; done
{ [ -f "$inner/site.json" ] || [ -f "$inner/site.js" ]; } && { echo "published package contains site.json/site.js — refusing (credentials would spread)"; exit 1; }
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SITE" || { echo "$SITE is not valid JSON"; exit 1; }
install -m 0644 "$SITE" "$inner/site.json"
# the worker loads its overrides as a script (importScripts), generated from the box json
printf 'globalThis.FlowSite = %s;\n' "$(cat "$SITE")" > "$inner/site.js"

# 2. install as a new release folder (the old one stays until the new one is verified running)
dest="$REL/$got"; [ "$got" = "$current" ] && dest="$REL/$got-$(date +%s)"   # --force / site.json change: fresh folder
rm -rf "$dest.new"; cp -a "$inner" "$dest.new"; chown -R 1000:1000 "$dest.new"; mv "$dest.new" "$dest"
echo "installed $got into $dest (was $current, site.json same=$site_same)"
activate "$(basename "$dest")"
