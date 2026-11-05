#!/usr/bin/env node
// t3-post.mjs — post a message into a T3 Code thread through T3's own WebSocket API, exactly as if the owner
// typed it. Works while the thread is idle, settled or reaped: T3 starts a new turn and restarts the session.
// Copied from the proven fleet-guard poster (/home/cc/fleet-guard/t3-post.mjs on the CC box, 2026-10-04);
// the only change is that origin and token come from arguments, so it can reach another machine's T3.
// Usage: t3-post.mjs <origin> <token-file> <threadId>   (message text on stdin)   -> prints SENT <seq> | FAILED <why>
// <token-file>: a bearer token from `t3 auth session issue --token-only` on the machine that runs that T3.
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

const [origin, tokenFile, threadId] = process.argv.slice(2);
const fail = (why) => { console.log(`FAILED ${why}`); process.exit(1); };
if (!origin || !tokenFile || !threadId) fail("usage: t3-post.mjs <origin> <token-file> <threadId> < text");
const text = readFileSync(0, "utf8").trim();
if (!text) fail("empty message");
setTimeout(() => fail("timeout"), 20000).unref();

const token = readFileSync(tokenFile, "utf8").trim();
const res = await fetch(`${origin}/api/auth/websocket-ticket`, {
  method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: "{}",
}).catch((e) => fail(`ticket: ${e.message}`));
if (!res.ok) fail(`ticket: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
const { ticket } = await res.json();

const ws = new WebSocket(`${origin.replace(/^http/, "ws")}/ws?wsTicket=${encodeURIComponent(ticket)}&orchestrationProtocol=2`);
ws.onerror = (e) => fail(`ws: ${e.message ?? "error"}`);
ws.onopen = () => ws.send(JSON.stringify({
  _tag: "Request", id: "1", tag: "orchestration.dispatchCommand", headers: [],
  payload: {
    type: "message.dispatch", commandId: randomUUID(), threadId, messageId: randomUUID(), text, attachments: [],
    // server resolves against live state: idle/reaped thread -> start a turn; active turn -> queue after it
    deliveryIntent: "auto", dispatchMode: { type: "queue_after_active" },
    createdBy: "user", creationSource: "web",
  },
}));
ws.onmessage = (ev) => {
  for (const m of [].concat(JSON.parse(ev.data))) {
    if (m._tag === "Ping") { ws.send(JSON.stringify({ _tag: "Pong" })); continue; }
    if (m._tag === "Defect") fail(`defect ${JSON.stringify(m.defect).slice(0, 300)}`);
    if (m._tag !== "Exit" || m.requestId !== "1") continue;
    if (m.exit?._tag === "Success") { console.log(`SENT ${m.exit.value?.sequence ?? ""}`); ws.close(); process.exit(0); }
    fail(JSON.stringify(m.exit).slice(0, 400));
  }
};
