# Live Creaa tests — 2026-09-08 IST

Account: 64287. Existing Flow2API key and Chrome worker. All generation requests used unlimited_only. Tests ran through curl against https://flow.ashuthefire.com .

## Baseline

- GPT Image 2, 1K medium: job cj_fc6b6582328f4c5b8761, succeeded; provider task 759a3e40-9d9f-4a1d-b7e1-044e4d1e3d45. PNG HTTP 200, image/png, 1,521,068 bytes.
- Seedance 2.5, requested 4 seconds at 1280x720: job cj_84fabed77fe34b6a95cc, succeeded; provider task c68da60d-1b5e-4669-9dba-768591a12c57. MP4 HTTP 200, video/mp4, 3,564,269 bytes. ffprobe: H.264 video, AAC audio, 1280x720, 4.063991 seconds.
- Video timing: 102.342 seconds in local queue, 697.179 seconds after provider acceptance, 799.521 seconds total. Provider acceptance-to-completion includes any Creaa-side queue delay, not just rendering.
- GET /v1/creaa/jobs/{id} returned progress updates, including 32, 34, 43, 47 and finally 100. This is polling, not an SSE stream.

## Concurrent probes

1. I1 cj_089831453fa441f7a35d: GPT Image 2 1K medium, blue cup; provider accepted 20:01:57.463 UTC, completed 20:04:02.051 (124.588 seconds).
2. I2 cj_79cf1b191beb4e858b9c: GPT Image 2 1K medium, green cup; provider accepted 20:01:59.743 UTC, completed 20:04:00.790 (121.047 seconds). Both were simultaneously reported at 50/60% and finished ~1.3 seconds apart.
3. V1 cj_3b0d3be8fec84af5975f: Seedance 2.5 4-second video accepted 20:02:08.563 UTC while both images were running; provider task d83a428d-c893-431a-bde1-5c53d1b8a633. Still processing at the end of the limit checks; backend continues tracking it. This confirms overlapping accepted workflows, not a claim about simultaneous GPU execution.
4. I3 cj_c4c45c1008c1472897c7: third image rejected with no provider task. Creaa explicitly stated unlimited mode allows at most 2 simultaneous image tasks; higher concurrency requires credit mode. No credit-mode fallback was attempted.
5. V2 cj_fd9c9e413dc541059817: second video rejected with no provider task. Exact message: "Seedance 2.5 unlimited mode allows only 1 submitted or queued task at a time, regardless of duration. Please wait for the current task to finish and try again."

Rejected I3/V2 initially entered conservative needs_review states and were explicitly resolved to failed after the rejection responses established that no upstream task existed. This cleared the account hold. They were not resubmitted.

## Operating setting

Owner requested testing. Codex temporarily changed account 64287 local image/video/total caps from 1/1/1 to 3/2/5 to probe one above advertised limits. After the server rejections, caps were set to the verified operating configuration 2/1/3. The Flow provider throttle was unchanged. New Creaa accounts retain the default until configured.

The measured unlimited limits apply to the tested GPT Image 2 / Seedance 2.5 workflows on this account. Other image/video model-specific allowances were not load-tested. Website eligibility separately reports current supported models, resolutions and clip ceilings. Actual credit-ledger deductions were not independently audited.

Backend change: PR #15, production commit c3068389ca8a684962e3daa9d5bb26c699e33b3c, Coolify deployment qagfmenha2rixqi0gpaahdki (finished 20:01:10 UTC). Tests: 25 targeted Python and 20 Node checks passed before rollout. No extension source/reload was required for these scheduler changes.
