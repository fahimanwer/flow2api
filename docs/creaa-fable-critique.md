**Verdict: not ready to build, ready to spike.** The plan is directionally sound but about 80% of it designs a system whose two load-bearing facts, task identity and billing semantics, have never been observed. Shrink it to a manual spike plus a one-page design that waits for the spike report.

## What is proven vs unknown

Proven from the live observations: a credit balance exists, an Unlimited toggle exists for both media kinds with a displayed in-progress cap, the official API has zero credits, and the picker lists models. That is all.

Unknown, and each one is load-bearing:
- **Whether Unlimited means zero spend.** Agent is on for images while the FAQ says Agent costs credits. Nobody has run one image and re-read the balance.
- **Whether a task has a durable identity.** No task ID, no history view, no reload behavior captured. Without this, every timeout becomes needs_review and the queue is useless.
- **Whether video Unlimited executes at all**, and its duration ceiling.
- **Whether image and video share the in-progress cap.**
- **Whether the terms forbid this.** The payment terms restrict scripts. The plan treats this as a clarification to run in parallel. It is a gate.
- **What the result asset is** (blob, signed CDN URL, expiry), which decides the whole retrieval design.

## Correctness blockers

1. **Step 1 contradicts itself.** It says "UI automation" and "no live spending" in the same paragraph. You cannot resolve the billing question without letting one image possibly cost credits, and you cannot automate before the terms are clarified. Make the spike manual and authorize a small credit loss, stated as a number Fahim signs off on.

2. **The attempt marker is in the wrong process.** The click happens in the tab. The MV3 worker can die between "marker written" and "click sent". Persist the marker from the content script to the backend, wait for the ack, then click. Without the ack the click never happens.

3. **Reconciliation has no matching key.** Prompt plus timestamp matching fails if two users send similar prompts. Do not put a nonce in the prompt; it changes the output. The clean fix is the plan's own default: one submission at a time per account. Then any new task appearing after the marker timestamp is yours by construction. State that this is why the cap is one, not just caution. Raising it later reintroduces ambiguity, so it needs a task-ID contract first.

4. **Human co-use of the same account is missing.** If Fahim generates by hand in another tab, the in-progress cap fills and the extension's submit fails or queues in an unknown way. Read the site's displayed in-progress count right before submit and treat it as truth over the local lease. On reconnect, rebase the local count to the observed count.

5. **Long video tabs get throttled.** Chrome throttles timers in background tabs and Memory Saver discards them. A six-second Seedance job may take minutes. The plan covers worker restart but not tab discard. Keep the generation tab foregrounded in its own window, or poll from an alarm-driven worker that reopens the tab and reads state from the page, never from JavaScript memory.

6. **Cancellation is unspecified upstream.** Fine to say "unsupported", but a queued cancel racing a lease must be tested: cancel arriving after the marker ack but before the click must abort the click.

## Unnecessary complexity now

Cut from the first prototype: eleven job states, event stream, idempotency-key conflict semantics, users/devices/accounts split, per-user tokens, hosted TLS, artifact retention policy, chunked streaming with backpressure, the provider adapter abstraction, and the "narrow same-origin request adapter" idea. Two people on localhost need five states: queued, submitting, submitted, done, needs_review. Everything else is a paragraph in a later doc. Streaming design in particular cannot start until you know if the video is a blob or a URL.

## Missing acceptance tests

Each of these is a single manual run with the balance written down before and after:
- One image, Agent off. One image, Agent on. Resolves the FAQ conflict.
- Three images submitted by hand at once. What happens to the third.
- Video then image. Shared cap or not.
- Reload the page mid-generation. Does the task reappear with the same ID and result.
- Open the result URL one hour later and one day later.
- A prompt the site rejects for policy. Does it consume a credit, and what does the failure look like.
- Log out and back in. What the logged-out page looks like, so the extension can detect it.
- Longest video duration Unlimited accepts.

## Concurrency for two people

One active job per account with FIFO means one video blocks the other person's images for minutes. That is acceptable for a pilot but must be stated to Ankit. Add a per-user queued cap of two or three so one person cannot fill the queue. Do not build lanes until the shared-cap test answers whether lanes are even possible.

## Recommended first spike

Two to three hours, no code, no extension:
1. Send the terms question to Creaa support today. Do not automate anything until it is answered.
2. Open DevTools with network recording and preserve-log on.
3. Run the eight tests above by hand. Save a sanitized HAR and screenshots.
4. Write a one-page report: task ID format, history behavior, asset type and expiry, billing per test, cap behavior, timings.

Only if the report shows a durable task ID and a readable history, build a throwaway extension that does exactly one thing: fill prompt, click, read back the task ID from the page. No backend, no queue. If the report shows no task ID, stop. The queue design cannot be made correct and the plan should say so instead of falling back to needs_review as a feature.

## Concrete plan changes

- Move terms clarification from step 0 to a hard gate before any automation.
- Replace step 1 with the manual spike above and an explicit credit-loss budget.
- Move the attempt marker to content script with backend ack before click.
- Justify the one-per-account cap as the reconciliation mechanism.
- Add human co-use detection and observed-count rebase.
- Add tab discard handling for video.
- Delete the architecture, API contract, media handling, and steps 3 through 5 into a deferred appendix until the spike report exists.
