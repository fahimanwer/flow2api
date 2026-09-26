#!/usr/bin/env python3
"""Register /opt/ext in a NEW Chrome profile (container started with FU_DEVTOOLS=1). Run inside the container:
   docker exec flow-ultra-01 python3 /opt/register-ext.py
Uses only the standard library: a minimal WebSocket client for the browser's CDP endpoint."""
import base64, json, os, socket, sys, urllib.request

ver = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=10))
path = "/" + ver["webSocketDebuggerUrl"].split("/", 3)[3]
s = socket.create_connection(("127.0.0.1", 9222), timeout=20)
key = base64.b64encode(os.urandom(16)).decode()
s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:9222\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
resp = b""
while b"\r\n\r\n" not in resp: resp += s.recv(4096)
assert b" 101 " in resp.split(b"\r\n")[0], resp[:200]

def send(obj):
    data = json.dumps(obj).encode(); mask = os.urandom(4)
    hdr = bytes([0x81]) + (bytes([0x80 | len(data)]) if len(data) < 126 else bytes([0x80 | 126]) + len(data).to_bytes(2, "big"))
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def recv():
    def rd(n):
        b = b""
        while len(b) < n:
            chunk = s.recv(n - len(b))
            if not chunk: raise EOFError("CDP socket closed")
            b += chunk
        return b
    h = rd(2); n = h[1] & 0x7F
    if n == 126: n = int.from_bytes(rd(2), "big")
    elif n == 127: n = int.from_bytes(rd(8), "big")
    return json.loads(rd(n))

send({"id": 1, "method": "Extensions.loadUnpacked", "params": {"path": "/opt/ext"}})
while True:
    m = recv()
    if m.get("id") == 1:
        print(json.dumps(m))
        # success means Chrome returned an extension id; anything else is a failure (exit 1)
        sys.exit(0 if (m.get("result") or {}).get("id") else 1)
