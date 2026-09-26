#!/usr/bin/env python3
"""Local authenticating proxy for the server browser: Chrome -> 127.0.0.1:3128 -> the box's fixed upstream proxy.

Chrome cannot put proxy credentials in --proxy-server, and the extension only applies its proxy AFTER it loads —
until then (every start) Chrome and Chrome sync talked to Google DIRECTLY from the datacenter IP (Codex review
2026-09-26). With this forwarder and --proxy-server, every byte leaves through the upstream from the first request.
Upstream comes from /opt/ext/site.json (proxyUrl, box-only). Standard library only."""
import asyncio, base64, json, sys
from urllib.parse import unquote

cfg = json.load(open("/opt/ext/site.json"))
url = cfg["proxyUrl"]                                   # http://USER:PASS@HOST:PORT
scheme, rest = url.split("://", 1)
creds, hostport = rest.rsplit("@", 1)
UP_HOST, UP_PORT = hostport.rsplit(":", 1); UP_PORT = int(UP_PORT)
user, pw = creds.split(":", 1)
AUTH = b"Proxy-Authorization: Basic " + base64.b64encode(f"{unquote(user)}:{pw}".encode()) + b"\r\n"

async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data: break
            w.write(data); await w.drain()
    except Exception:
        pass
    finally:
        try: w.close()
        except Exception: pass

async def handle(cr, cw):
    try:
        head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 30)
    except Exception:
        cw.close(); return
    first, _, rest = head.partition(b"\r\n")
    try:
        ur, uw = await asyncio.wait_for(asyncio.open_connection(UP_HOST, UP_PORT), 20)
    except Exception:
        cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n"); await cw.drain(); cw.close(); return
    # forward the request line + our auth + the client's headers (minus any client Proxy-Authorization)
    kept = b"".join(l + b"\r\n" for l in rest.split(b"\r\n") if l and not l.lower().startswith(b"proxy-authorization:"))
    uw.write(first + b"\r\n" + AUTH + kept + b"\r\n"); await uw.drain()
    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))

async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", 3128)
    print(f"forwarding 127.0.0.1:3128 -> {UP_HOST}:{UP_PORT}", flush=True)
    async with srv: await srv.serve_forever()

asyncio.run(main())
