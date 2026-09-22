# Flow model limits (reference images, audio, characters)

Source of truth: Flow's own model list, read 2026-09-22 with an account access token:

```
GET https://aisandbox-pa.googleapis.com/v1/flow/models
Authorization: Bearer <token row's `at`>
```

It returns `modelConfig.{imageModels,videoModels,audioModels}`; each model has `usages[]`
with `key` (the upstream model key), `inputSpec.maxImageReferences`, `maxAudioReferences`,
`maxCharacters`, `creditMapping` (cost per tier: 1 = free, 2 = Pro, 3 = Ultra) and
`videoLengthSeconds`. The Flow web app builds its "Maximum image ingredients reached"
message from these same fields (web bundle, `cVa`/`sVa`). Re-read it when Google changes
models; no captcha is needed for this GET.

## Limits on 2026-09-22 (CONFIRMED from that list)

| Flow model | Upstream key(s) | Max images | Audio | Characters |
|---|---|---|---|---|
| Nano Banana Pro / 2 / 2 Lite | `GEM_PIX_2`, `NARWHAL`, `HARBOR_SEAL` | 10 | – | 10 |
| Omni 1.1 Flash ingredients | `abra_r2v_{4,6,8,10}s` (+ `_360p`) | 7 | 5 | 3 |
| Omni 1.1 Flash video edit | `abra_edit` (+ `_360p`) | 5 | 3 | 3 |
| Veo 3.1 Fast ingredients | `veo_3_1_r2v_fast_{landscape,portrait}` (+ `_ultra`) | 3 | 1 | 3 |
| Veo 3.1 Lite ingredients | `veo_3_1_r2v_lite` (+ `_low_priority`, Ultra 0 credits) | 3 | 1 | 3 |
| Veo 3.1 Quality | no ingredients mode | – | – | – |

In the web app one character uses 3 of the image slots. Veo ingredients are 8 s only.

## What flow2api supports (2026-09-22, branch `feat/flow-reference-limits`)

- Images: up to 10 reference images; an 11th is refused with HTTP 400. Google itself took
  20 and 30 images without error in a live test that day, but Flow only offers 10 and
  nothing shows the extra ones are used.
- Omni: `omni*` models take 1 image = first frame, 2 = first + last frame, 3-7 = ingredients.
  `omni_r2v` (`omni-r2v`, durations `omni_r2v_{4,6,8,10}s`, `_portrait`) sends every image
  as an ingredient, 1-7.
- Veo Lite ingredients: `veo_3_1_r2v_lite` (`veo-r2v-lite`), 1-3 images; "Veo 3.1 - Lite"
  with 3 images now picks it.
- Veo Fast ingredients: unchanged, 1-3.
- Reference images upload 3 at a time (one by one took ~6.6 s each: 20 images = 132 s).
- NOT supported: audio references, characters, Omni video edit (`abra_edit`), Veo Lite
  low-priority keys.
- Follow-up: every `*_relaxed` Veo key we still expose (`veo-r2v-relaxed`, `veo-relaxed`,
  `veo-i2v-relaxed`, e.g. `veo_3_1_r2v_fast_landscape_ultra_relaxed`) is listed under
  `deprecatedModels`; Flow's replacement is "Veo 3.1 - Lite [Lower Priority]" (`*_lite_low_priority`,
  Ultra only, 0 credits). Not changed yet.
