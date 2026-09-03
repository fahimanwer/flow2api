# Shared Agent Status

Last updated: 2026-09-03 (Asia/Kolkata)

## Active

- Working tree is on local `main`, fast-forwarded to `fork/main` at `3a45480`. `feat/async-submit-status` is not merged into `main`; do not merge it as part of this investigation.

## Findings

- 2026-09-03 read-only production audit: virtually all successful image requests since the Flow2API UTC daily reset carried Content Factory's exact deterministic `featured-image-2026-07-v2` prompt scaffold. The only non-Factory successes were two operator smoke tests using prompt `apple` and one request correlated by timestamp and prompt to the owner's n8n `Interior/Exterior Redesign` workflow. No unexplained or abusive prompts found.
- Attribution caveat: Traefik access logging is disabled and Flow2API stores model/prompt/status but not client IP, user agent, or an API-key principal. Prompt and cross-system timestamp evidence is strong, but historical network-origin attribution is unavailable with the current shared key/log schema.
- Investigation was read-only in production. No code, config, credential, deployment, traffic, or database changes were made.

## Coordination

- 2026-09-03: owner authorized committing and pushing this `WORK.md` incident record to `fork/main`; documentation-only change, with no Flow2API service rebuild or deployment expected.
- Before any future commit, push, or deploy: reread and update this file, prune stale entries, and coordinate with other active sessions through the owner.
- Never deploy from a tree that does not contain `origin/main`.
