# alert-wake: a flow2api problem wakes the flow2api T3 thread (2026-11-05)

Owner order (2026-11-05): "if anything breaks in flow ... even if this session is settled, it can message you and
wake you up so that you can fix it automatically".

## How it works
1. flow2api already writes every alert into its outbox table `ultra_alerts` and sends it to Telegram
   (`src/services/fleet_watch.py`: `fleet_down`, `fleet_degraded` (new 2026-11-05: < 50 % of >= 30 finished requests OK
   in 60 min), `fleet_ok`; `src/services/ultra_browsers.py`: server-browser `logged_out` / `needs_you` / `error` /
   `host_silent`, and their `ok` / `host_ok` recoveries).
2. `flow2api-alert-wake` (systemd timer, every minute, root on the flow2api host 78.46.131.169) reads the NEW rows
   read-only. Problem rows become ONE message, posted into the flow2api T3 thread through T3's own WebSocket API
   (`t3-post.mjs`, the method fleet-guard has used on the CC box since 2026-10-04). That is the same as the owner
   typing: an idle, settled or reaped thread starts a new turn; a busy one queues it after the current turn.
3. The thread lives on the owner's Mac. If its T3 cannot be reached for 15 min (Mac asleep or offline), the message
   goes to the CC box's fleet-guard watch thread instead (`3e5c92d2-…`, "Set Up Safe Site Monitoring"), which handles
   app bugs per `/home/cc/fleet-guard/README.md`.
4. Recoveries never wake anyone. At most 4 wakes per hour (storm guard); Telegram still gets every row.

## On the host
| What | Where |
|---|---|
| script | `/usr/local/bin/flow2api-alert-wake` (= `flow2api-alert-wake.sh`) |
| poster | `/usr/local/lib/flow2api-alert-wake/t3-post.mjs` |
| units | `/etc/systemd/system/flow2api-alert-wake.{service,timer}` |
| config | `/etc/flow2api-alert-wake/env`: `MAC_ORIGIN`, `MAC_THREAD`, `BOX_ORIGIN`, `BOX_THREAD` |
| tokens (root 0600) | `/etc/flow2api-alert-wake/mac.token` (T3 bearer issued on the Mac, label `flow2api-alert-wake`, 365 d), `box.token` (issued on the CC box as `cc`, same label) |
| state | `/var/lib/flow2api-alert-wake/` (`cursor` = last alert id handled, `fail-since`, `posts`) |
| log | `/var/log/flow2api-alert-wake.log` |

Commands: `flow2api-alert-wake --dry-run` (what the next pass would post), `flow2api-alert-wake --test mac|box`
(a harmless TEST message), stop with `systemctl disable --now flow2api-alert-wake.timer`.
Tokens: list / revoke with `t3 auth session list|revoke` on the machine that issued them (on the Mac the CLI is the
app's server: `ELECTRON_RUN_AS_NODE=1 "/Applications/T3 Code (Nightly).app/Contents/MacOS/T3 Code (Nightly)"
"/Applications/T3 Code (Nightly).app/Contents/Resources/app.asar/apps/server/dist/bin.mjs" auth session list`).

## Install / update
```
scp docker/alert-wake/flow2api-alert-wake.sh root@78.46.131.169:/usr/local/bin/flow2api-alert-wake
scp docker/alert-wake/t3-post.mjs root@78.46.131.169:/usr/local/lib/flow2api-alert-wake/t3-post.mjs
scp docker/alert-wake/flow2api-alert-wake.{service,timer} root@78.46.131.169:/etc/systemd/system/
ssh root@78.46.131.169 'chmod 755 /usr/local/bin/flow2api-alert-wake && systemctl daemon-reload && systemctl enable --now flow2api-alert-wake.timer'
```
If the flow2api thread moves (a new Mac, a new thread), change `MAC_ORIGIN` / `MAC_THREAD` and issue a new
`mac.token` on that machine.
