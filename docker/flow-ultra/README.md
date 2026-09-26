# flow-ultra — a Flow account in an always-on server browser

Runs on **cf-worker-01** (`root@178.63.65.20`) as container `flow-ultra-01`: Google Chrome + Xvfb/openbox + x11vnc,
the Flow2API worker extension, one Google account. Today: `nishatmini16@gmail.com` (Ultra), flow2api token 93,
`reserved_client = pinterest-factory`.

## How updates reach it (automatic)
1. Change `worker-extension/`, bump `manifest.json`, publish the zip in the Flow2API admin (Publish extension /
   `POST /api/ext/upload`) — the same step that shows staff the "Update available" banner.
2. `flow-ultra-ext-sync.timer` (every 10 min) asks `/api/plugin/ext-version`; if it differs from
   `/srv/flow-ultra/releases/current`, `ext-sync.sh` downloads the zip, validates it, puts this box's `site.json`
   in, installs it as `/srv/flow-ultra/releases/<version>/`, points `current` at it and restarts the container
   gracefully (`docker restart -t 30`). It marks the version `pending` until the container is verified running it
   (a failed restart is retried on the next run, never treated as done). The previous release is kept until then.
   Log: `journalctl -u flow-ultra-ext-sync`. Force now: `/opt/flow-ultra/ext-sync.sh --force`.
   The zip must not contain `site.json` (the server refuses such an upload; `zip … -x site.json`).

## All traffic through the fixed proxy (from the first byte)
`proxy-forward.py` listens on the container loopback (127.0.0.1:3128) and forwards to the upstream in
`site.json` with its credentials; Chrome starts with `--proxy-server=http://127.0.0.1:3128`, so Chrome, Chrome sync
and the extension never leave from the datacenter IP — not even before the extension loads (Codex review
2026-09-26). WebRTC is limited to proxied UDP.

## Box-only files (never in git, never in the zip)
- `/etc/flow-ultra/site.json` — `{"proxyUrl": "http://USER:PASS@disp.oxylabs.io:8005", "clientLabel": "flow-ultra-01", "proxyAllHosts": true}`
  (one fixed IP for EVERY host, so Google sees one steady location; values live only on the box).
- `/etc/flow-ultra/sync.env` — `FLOW_BASE`, `CONNECTION_TOKEN` (the plugin connection token), `CONTAINER`.

## Run / recreate (profile is on disk; never `docker rm -f` a live profile — a hard kill lost cookies once)
```
docker build -t flow-browser:4 /opt/flow-ultra
docker stop -t 30 flow-ultra-01; docker rm flow-ultra-01
docker run -d --name flow-ultra-01 --hostname flow-ultra-01 --restart=always --network bridge --shm-size=1g \
  --memory=3g -v /srv/flow-ultra/profile:/profile -v /srv/flow-ultra/releases:/opt/releases:ro flow-browser:4
# (entrypoint copies /opt/releases/$(cat current) to the real dir /opt/ext; the path — hence the extension id — never changes)
```

## How the extension is loaded (Chrome)
Branded Chrome ignores `--load-extension`, the Load unpacked file dialog does not open inside the container, and a
CDP-loaded extension lasts one browser session. So the entrypoint registers `/opt/ext` through CDP
(`register-ext.py`, debug port on the container loopback only) at EVERY start; same path ⇒ same id ⇒ storage kept.
Log: `/profile/logs/register-ext.log`. Nothing to do by hand.

## Signing in (a human does this)
On the box: `systemd-run --unit flow-ultra-novnc /usr/bin/websockify --web /usr/share/novnc 127.0.0.1:6081 <container-ip>:5900`.
On the Mac: `ssh -N -L 6081:127.0.0.1:6081 root@178.63.65.20`, open http://localhost:6081/vnc.html → Connect.
Sign in to Google, then Labs' own "Sign in with Google" on labs.google/fx; open flow.google.com once.
Stop the screen afterwards: `systemctl stop flow-ultra-novnc`.
