# Flow Characters (consistent people / objects) — flow2api

A character is a named person or thing built from 1–3 photos. In the prompt you write `@Name`
and Flow keeps that face/object consistent. flow2api creates the character on the account that
serves the request, caches it, and passes it to Google as a reference entity.

## API

Add `characters` to a normal request (OpenAI `/v1/chat/completions` or Gemini `/v1beta/...:generateContent`):

```json
{
  "model": "nano-banana-2-lite-landscape",
  "messages": [{"role": "user", "content": "@Maya sits in a sunny café, smiling at the camera"}],
  "characters": [
    {"name": "Maya", "images": ["data:image/jpeg;base64,...", "https://example.com/maya-2.jpg"]}
  ]
}
```

- `name`: 1–40 characters, letters/digits/space/`_`/`-`, unique per request (case-insensitive).
- `images`: 1–3 photos (data URL or http(s) URL), up to 12 MB each. Order matters (it is part of the cache key).
- Mention with `@Name`; `@Maya Rose` beats `@Maya` (longest name wins); `@Unknown` stays plain text.
  A character that is never mentioned is still attached as a reference.
- Limits: images up to 10 characters; videos up to 3 and only on ingredients models
  (`omni-r2v`, `omni`, `omni-flash`, `veo-r2v`, `veo-r2v-lite`, `veo_3_1_r2v_*`). Frame models
  (start/end frame, text-to-video, extend) answer 400. `omni` / `omni-flash` with characters always take
  the ingredients route (a first/last-frame video cannot carry characters).
- Ordinary reference images (`image_url` parts) still work alongside characters.

## What happens inside

1. Validation (names, counts, model type) → 400 before anything is uploaded.
2. Per character: cache lookup in `flow_characters` (account + project + name + photo digest).
   Miss → create the entity, upload each photo, attach it to slot 0/1/2, read the entity back.
   Any failure → 400/502 to the caller, no generation submitted, no account strike.
3. Generation: `referenceEntities:[{entityId}]` + `structuredPrompt.parts` with the `@Name` mentions as
   entity references (video: `textInput.structuredPrompt`).

Cache rows are written only after the read-back shows the photos; a cached entity that Flow reports as
not found is recreated. Flow has no delete route for entities (checked 2026-09-23), so characters stay in
the account's project; rows are removed with the account/project.

## Flow endpoints used (CONFIRMED 2026-09-23 on aisandbox-pa.googleapis.com)

| Step | Call |
|---|---|
| create | `POST /v1/flow/entities` `{"entity":{"projectId","entityInfo":{"entityType":"CHARACTER","displayName","characterInfo":{}}}}` |
| photo | `POST /v1/flow/uploadImage`, then `POST /v1/flow:copyProjectMedia` `{"mediaId","destinationProjectId","destinationMediaContext":{"entityContext":{"entityId","characterSlot":{"imageReferenceIndex":n}}}}` |
| read | `GET /v1/flow/entities:batchGet?entityIds=…` |
| image | `flowMedia:batchGenerateImages` `requests[i].referenceEntities`, `structuredPrompt.parts[].reference.entity{entityId,handle}` |
| video | `video:batchAsyncGenerateVideoReferenceImages` `requests[i].referenceEntities`, `textInput.structuredPrompt.parts` |

Limits come from `GET /v1/flow/models` (`inputSpec.maxCharacters`). The web app counts 3 image slots per character.
