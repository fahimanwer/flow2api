# Models: names, aliases and what each one does

Last checked 2026-10-11 against Google's live Flow model list (`GET aisandbox-pa /v1/flow/models`) and
Google's model docs. The full, always-current list is **`GET /v1/models`** (or the `/test` page): every
entry has a readable `name`, `family`, `google_model`, `mode`, `orientation`, `duration_seconds`,
`resolution`, `accounts`, `inputs` and `aliases`. This page explains the names; it does not repeat all
281 ids.

## Families (Google's names)

| Family | Google's model id | Our ids (all still work) | Readable aliases |
|---|---|---|---|
| **Nano Banana 2.1** (images, Flow's default on every tier, GA 2026-10-06) | `gemini-nano-banana-2.1` | `gemini-3.1-flash-image-{aspect}[-2k\|-4k]` | `nano-banana-2.1-{aspect}[-2k\|-4k]` |
| **Nano Banana Pro** (images) | `gemini-3-pro-image` | `gemini-3.0-pro-image-{aspect}[-2k\|-4k]` | `nano-banana-pro-{aspect}[-2k\|-4k]` |
| **Nano Banana 2 Lite** (images, 1K only) | `gemini-3.1-flash-lite-image` | `nano-banana-2-lite-{aspect}` | (the id is already readable) |
| **Omni 1.2 Flash** (video, since 2026-11-06; Flow key family `macaron_cookie`) | Flow: Omni 1.2 Flash | `omni*`, `omni_r2v*` (+ `_1080p`) | `omni-1.2-flash-…` (below; `omni-1.1-flash-…` still works) |
| **Veo 3.1 Fast / Quality / Lite** (video) | Veo 3.1 | `veo_3_1_…` | none; read `name` in `/v1/models` |

`{aspect}` = `landscape` (16:9), `portrait` (9:16), `square` (1:1), `four-three` (4:3), `three-four` (3:4).

**Why `gemini-3.1-flash-image-*` runs Nano Banana 2.1:** Google's own id `gemini-3.1-flash-image` is Nano
Banana 2, which Flow retired on 2026-10-06 (its Flow key NARWHAL answers 404). Flow's replacement is Nano
Banana 2.1 (Flow key BELUGA), and Flow lists "Nano Banana 2" as an alias of 2.1. We kept the old id so
no caller breaks; `nano-banana-2.1-*` is the honest name for the same thing. There is no "Gemini 3.2" image
model.

**Omni 1.2 (2026-11-06):** Google launched Omni 1.2 Flash in Flow (rolling out over a few days) and keeps
Omni 1.1, Veo Fast and Veo Quality for only ~30 more days. Every `omni*` id now runs Omni 1.2, so callers keep
their ids and get the newest model. The `omni-1.1-flash-…` and "Omni 1.1 Flash" names keep working and run 1.2
too. Omni 1.2 costs about twice the credits of 1.1 (table below). Keys come from Flow's own model list
(`HTrJv`) and two real Flow-app submits (`tmp/flow_models_2026-11-06_HTrJv.json`).

## Omni Flash (1.2): which id to use

| You want | Readable name | Old id (same model) |
|---|---|---|
| Text only, or 1-2 frames, or 3+ reference images (the model decides) | `omni-1.2-flash-8s-portrait` | `omni_8s_portrait` |
| Every image (or @character) is a reference ("Ingredients" in Flow) | `omni-1.2-flash-ingredients-8s-portrait` | `omni_r2v_8s_portrait` |
| Same, upscaled to 1080p (Pro or Ultra) | `omni-1.2-flash-ingredients-8s-portrait-1080p` | `omni_r2v_8s_portrait_1080p` |

Durations `4s`, `6s`, `8s`, `10s`; orientation `landscape` or `portrait`. "r2v" in the old ids means
"reference images to video", which Flow's UI calls **Ingredients**. `omni-flash*` ids are a legacy
duplicate of the same model (since Omni 1.2 there is no separate 720p pin: 720p is the key itself); prefer the
names above. Ids without a duration (`omni`, `omni_r2v`) are the 8 s ones. Omni 1.2 also offers 3-15 s
(11-15 s Pro/Ultra only), extend and edit; we expose 4/6/8/10 s for now.

## Modes (the `mode` field)

| Mode | Images | Characters (@Name) |
|---|---|---|
| Text to image, or edit / combine reference images | 0-10 | up to 10 |
| Text to video | 0 | no |
| First frame to video (Veo Lite i2v) | exactly 1 | no |
| First + last frame to video (Veo Lite interpolation) | exactly 2 | no |
| Frames to video (Veo `_fl`): 1 = first frame, 2 = first + last | 1-2 | no |
| Ingredients to video (reference images): Veo r2v | 0-3 | up to 3 |
| Ingredients to video (reference images): Omni ingredients | 1-7 (or characters only) | up to 3 |
| Auto (plain Omni): text only, 1-2 frames, or 3+ ingredients | 0-7 | up to 3 |
| Extend a video | 0; send `image_url` = `extend://<media id>` | no |

Characters take at most **2 photos each** (Flow refuses a third slot, 2026-10-11). See `docs/flow-characters.md`.

**Since 2026-10-21 (Google's Flow login change): characters are not available** (immediate 501), and an
extended video comes back as the extension clip only, without stitching. Everything else in these tables
was re-checked on the new Flow web API on 2026-10-23 (images incl. 2K and reference photos, a 4 s video);
Omni and the other video modes are mapped but not yet proven live.

## Accounts (the `accounts` field)

- **Images:** any account. 2K enlarge works on every tier; 4K enlarge needs Ultra, otherwise the image
  comes back 1K with a `flow_upscale` note (requested vs delivered).
- **Video:** any id with `_ultra` or `_4k` needs Ultra (that wins over `_1080p`, so `…_ultra_1080p` is Ultra
  only); other `_1080p` ids need Pro or Ultra; the rest run on any account.
- A caller's client policy (`X-Flow-Client`) can narrow this further (e.g. meta-pages = Ultra only).

## Ultra credit costs (Google's list, 2026-10-11; confirmed live where marked)

| What | Credits on Ultra |
|---|---|
| Nano Banana 2.1 / Pro / 2 Lite image, 2K and 4K enlarge | 0 |
| Omni 1.2 Flash 4 / 6 / 8 / 10 s (720p, every tier) | 12 / 18 / 24 / 30 (Google's list 2026-11-06; 12 / 15 s = 42 / 60, Pro/Ultra) |
| Omni 1.1 Flash 4 / 6 / 8 / 10 s (until Google drops it) | 7 / 10 / 12 / 15 |
| Veo 3.1 Fast clip (Ultra keys) | 10 (confirmed) |
| Veo 3.1 Lite clip | 5 |
| Veo 3.1 Quality clip | 100 |
| 1080p upscale (Veo or Omni) | 0 (confirmed for both) |
| 4K upscale | 50 (confirmed) |

Replies to `_1080p` / `_4k` video requests carry `flow_upscale: {requested, delivered[, reason]}`. If Google
refuses to start the upscale, you get the 720p clip with the reason; if an upscale job starts and then fails at
Google, the request answers with an error and the 720p clip is not returned (known gap).

## Retired by Google

All `*_relaxed` ids (Veo 3.1 Fast relaxed queue, 10 ids) use codes Google lists as deprecated. They are
still listed so callers get a clear answer, marked `retired_by_google: true` and "(retired by Google)" in
`name`; requests will likely fail.

## Not exposed yet (exist in Google's list)

Omni 1.2 360p variants (`macaron_cookie_*_360p`), Omni 1.2 lengths other than 4/6/8/10 s (3-15 s), Omni 1.2
extend (`macaron_cookie_extend_{3-10}s`) and edit (`macaron_cookie_edit`), Veo 3.1 Lite low-priority queue,
Gemini TTS. Omni 1.2 has its own first+last-frame key (`macaron_cookie_i2v_fl_{d}s`), which we now send for
2 frames (1.1 sent `abra_i2v_*` for both).

## Where the names come from (for maintainers)

`src/services/model_catalog.py` builds every field from `MODEL_CONFIG`; `READABLE_MODEL_ALIASES` in
`src/core/model_resolver.py` holds the readable names (resolved first, straight to the existing id, so
tier rules, quota, client policy and logs see the same id as before). Tests: `tests/test_model_catalog.py`.
When Google renames something, change the family table in `model_catalog.py` and this page together.
