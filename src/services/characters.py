"""Flow "Characters": a named person/thing built from 1-3 photos, referenced in
prompts as @Name, sent to Flow as an entity (reference) so the face/object stays
consistent across generations.

Flow's contract (CONFIRMED 2026-09-23 against aisandbox-pa, see docs/flow-characters.md):
- create:  POST /flow/entities {"entity":{"projectId","entityInfo":{"entityType":"CHARACTER",
           "displayName","characterInfo":{}}}}  -> {"entity":{"entityId",...}}
- photos:  POST /flow/uploadImage (existing upload_image) then POST /flow:copyProjectMedia into
           destinationMediaContext.entityContext.characterSlot.imageReferenceIndex = 0,1,2
- read:    GET  /flow/entities:batchGet?entityIds=... -> {"results":[{"entity":...}|{"error":{"code":5}}]}
- use:     requests[i].referenceEntities:[{"entityId"}] and structuredPrompt.parts
           [{"text"} | {"reference":{"entity":{"entityId","handle"}}}] (video: textInput.structuredPrompt)
Entities are project-scoped and there is no delete route, so the same name + photos on the same
account + project are reused through the flow_characters table.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.logger import debug_logger

MAX_CHARACTERS_IMAGE = 10   # Flow model list: inputSpec.maxCharacters for Nano Banana models
MAX_CHARACTERS_VIDEO = 3    # Omni abra_r2v_* and Veo r2v
MAX_PHOTOS_PER_CHARACTER = 3
MAX_PHOTO_BYTES = 12 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-]{0,39}$")
SETUP_DEADLINE_SECONDS = 120.0


@dataclass
class LoadedCharacter:
    name: str
    images: List[bytes] = field(default_factory=list)

    @property
    def digest(self) -> str:
        """sha256 over the name and the ORDERED photo bytes, each length-prefixed."""
        h = hashlib.sha256()
        h.update(self.name.encode("utf-8"))
        for img in self.images:
            h.update(len(img).to_bytes(8, "big"))
            h.update(img)
        return h.hexdigest()


class CharacterSetupError(Exception):
    """Character validation/setup failed BEFORE any generation was submitted.
    status_code is what the caller gets (400 = their input, 502 = Google)."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def validate_characters(characters: Sequence[LoadedCharacter], max_count: int) -> Optional[str]:
    """Return a caller-facing error text, or None when the list is acceptable."""
    if not characters:
        return None
    if len(characters) > max_count:
        return f"At most {max_count} characters are allowed for this model; {len(characters)} given"
    seen = set()
    for c in characters:
        name = (c.name or "").strip()
        if name != c.name or not NAME_RE.match(name):
            return (
                f"Character name {c.name!r} is invalid: 1-40 characters, letters/digits/space/_/-, "
                "no leading/trailing spaces, no '@'"
            )
        if name.lower() in seen:
            return f"Character name {name!r} is given twice"
        seen.add(name.lower())
        if not c.images:
            return f"Character {name!r} needs at least one photo"
        if len(c.images) > MAX_PHOTOS_PER_CHARACTER:
            return f"Character {name!r} has {len(c.images)} photos; at most {MAX_PHOTOS_PER_CHARACTER}"
        for img in c.images:
            if not img or len(img) > MAX_PHOTO_BYTES:
                return f"A photo of character {name!r} is empty or larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB"
    return None


def _mention_pattern(names: Sequence[str]) -> Optional[re.Pattern]:
    if not names:
        return None
    ordered = sorted(names, key=len, reverse=True)  # longest first: "@Maya Rose" before "@Maya"
    alternation = "|".join(re.escape(n) for n in ordered)
    # A mention ends where the name ends and the next character cannot continue a word.
    return re.compile(rf"@({alternation})(?![A-Za-z0-9_])")


def build_prompt_parts(prompt: str, entities: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Split the prompt at @Name mentions into Flow prompt parts.

    Returns (parts, referenced_entity_ids). Text runs become {"text"}; each mention becomes
    {"reference":{"entity":{"entityId","handle":Name}}}. Unknown @words stay text. A character
    that is never mentioned is still returned in referenced ids (it is attached as a reference).
    """
    pattern = _mention_pattern(list(entities.keys()))
    parts: List[Dict[str, Any]] = []
    mentioned: List[str] = []
    pos = 0
    if pattern:
        for m in pattern.finditer(prompt):
            if m.start() > pos:
                parts.append({"text": prompt[pos:m.start()]})
            name = m.group(1)
            parts.append({"reference": {"entity": {"entityId": entities[name], "handle": name}}})
            if entities[name] not in mentioned:
                mentioned.append(entities[name])
            pos = m.end()
    if pos < len(prompt) or not parts:
        parts.append({"text": prompt[pos:]})
    ordered_ids = mentioned + [eid for eid in entities.values() if eid not in mentioned]
    return parts, ordered_ids


def plain_prompt(prompt: str, names: Sequence[str]) -> str:
    """The prompt with each known @Name mention rendered as Name (for logs/fallbacks)."""
    pattern = _mention_pattern(list(names))
    return pattern.sub(lambda m: m.group(1), prompt) if pattern else prompt


class CharacterService:
    """Turns LoadedCharacters into Flow entity ids for ONE account + project, with a cache.

    A cache row is written only after the entity exists, every photo is attached and a
    batchGet read-back shows the photos. Creation for one cache key is serialized in-process,
    so two concurrent requests with the same character produce one entity."""

    _instance: Optional["CharacterService"] = None

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}

    @classmethod
    def get_instance(cls) -> "CharacterService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure(
        self,
        flow_client,
        db,
        token,
        project_id: str,
        characters: Sequence[LoadedCharacter],
        deadline_seconds: float = SETUP_DEADLINE_SECONDS,
    ) -> Dict[str, str]:
        """Return {name: entityId}. Raises CharacterSetupError; never submits a generation."""
        deadline = time.monotonic() + deadline_seconds
        out: Dict[str, str] = {}
        for character in characters:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CharacterSetupError("Character setup took too long", 504)
            try:
                out[character.name] = await asyncio.wait_for(
                    self._ensure_one(flow_client, db, token, project_id, character), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise CharacterSetupError(f"Character {character.name!r} setup timed out", 504)
        return out

    async def _ensure_one(self, flow_client, db, token, project_id: str, character: LoadedCharacter) -> str:
        key = f"{token.id}:{project_id}:{character.name.lower()}:{character.digest}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await db.get_flow_character(token.id, project_id, character.name, character.digest)
            if cached:
                entity_id = cached["entity_id"]
                state = await self._read_back(flow_client, token, entity_id, expected_photos=len(character.images))
                if state == "ok":
                    await db.touch_flow_character(token.id, project_id, character.name, character.digest)
                    debug_logger.event(f"[CHARACTER] token={token.id} reuse {character.name!r} entity={entity_id[:8]}")
                    return entity_id
                # Only an explicit "not found" is a miss; anything else fails closed (raised inside).
                await db.delete_flow_character(token.id, project_id, character.name, character.digest)
                debug_logger.event(f"[CHARACTER] token={token.id} cached entity for {character.name!r} is gone; recreating")
            started = time.monotonic()
            entity_id = await self._create(flow_client, token, project_id, character)
            await db.upsert_flow_character(token.id, project_id, character.name, character.digest, entity_id)
            debug_logger.event(
                f"[CHARACTER] token={token.id} created {character.name!r} entity={entity_id[:8]} "
                f"photos={len(character.images)} ms={int((time.monotonic() - started) * 1000)}"
            )
            return entity_id

    async def _create(self, flow_client, token, project_id: str, character: LoadedCharacter) -> str:
        try:
            entity_id = await flow_client.create_character_entity(token.at, project_id, character.name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CharacterSetupError(f"Flow could not create character {character.name!r}: {str(exc)[:200]}", 502)
        for index, image in enumerate(character.images):
            try:
                media_id = await flow_client.upload_image(token.at, image, "IMAGE_ASPECT_RATIO_LANDSCAPE", project_id=project_id)
                await flow_client.copy_project_media_to_character_slot(
                    token.at, project_id=project_id, media_id=media_id, entity_id=entity_id, image_reference_index=index
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise CharacterSetupError(
                    f"Flow could not attach photo {index + 1} of character {character.name!r}: {str(exc)[:200]}", 502
                )
        state = await self._read_back(flow_client, token, entity_id, expected_photos=len(character.images))
        if state != "ok":
            raise CharacterSetupError(f"Character {character.name!r} was created but Flow does not show its photos", 502)
        return entity_id

    async def _read_back(self, flow_client, token, entity_id: str, expected_photos: int) -> str:
        """'ok' when Flow returns the entity with >= expected photos, 'missing' on an explicit
        not-found, raises CharacterSetupError on anything else (fail closed)."""
        try:
            results = await flow_client.batch_get_entities(token.at, [entity_id])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CharacterSetupError(f"Flow could not read character {entity_id[:8]}: {str(exc)[:200]}", 502)
        result = (results or [None])[0] or {}
        error = result.get("error") if isinstance(result, dict) else None
        if error:
            if int(error.get("code", 0) or 0) == 5:  # NOT_FOUND
                return "missing"
            raise CharacterSetupError(f"Flow refused to read character {entity_id[:8]}: {error.get('message', '')[:120]}", 502)
        entity = result.get("entity") if isinstance(result, dict) else None
        if not isinstance(entity, dict) or entity.get("entityId") != entity_id:
            raise CharacterSetupError(f"Flow returned an unexpected record for character {entity_id[:8]}", 502)
        refs = (((entity.get("entityInfo") or {}).get("characterInfo") or {}).get("imageReferences") or [])
        if len(refs) < max(1, expected_photos):
            return "incomplete"
        return "ok"
