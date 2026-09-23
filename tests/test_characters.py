"""Flow Characters (2026-09-23): validation, @Name prompt parts, cache/setup service."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.services.characters import (
    CharacterService,
    CharacterSetupError,
    LoadedCharacter,
    build_prompt_parts,
    plain_prompt,
    validate_characters,
)


def _c(name, n=1):
    return LoadedCharacter(name=name, images=[f"img{i}-{name}".encode() for i in range(n)])


class ValidationTests(unittest.TestCase):
    def test_good_and_bad_names(self):
        self.assertIsNone(validate_characters([_c("Maya"), _c("Leo 2"), _c("Zed_9-x")], 10))
        self.assertIn("invalid", validate_characters([_c("@Maya")], 10))
        self.assertIn("invalid", validate_characters([_c(" Maya")], 10))
        self.assertIn("invalid", validate_characters([_c("")], 10))
        self.assertIn("invalid", validate_characters([_c("a" * 41)], 10))
        self.assertIn("twice", validate_characters([_c("Maya"), _c("maya")], 10))

    def test_counts_and_photos(self):
        self.assertIn("At most 3", validate_characters([_c("A"), _c("B"), _c("C"), _c("D")], 3))
        self.assertIn("at least one photo", validate_characters([LoadedCharacter("A", [])], 10))
        self.assertIn("at most 3", validate_characters([_c("A", 4)], 10))
        self.assertIn("empty", validate_characters([LoadedCharacter("A", [b""])], 10))

    def test_digest_depends_on_order(self):
        a = LoadedCharacter("A", [b"1", b"2"]); b = LoadedCharacter("A", [b"2", b"1"])
        self.assertNotEqual(a.digest, b.digest)
        self.assertEqual(a.digest, LoadedCharacter("A", [b"1", b"2"]).digest)
        # length prefix: "12"+"" must differ from "1"+"2"
        self.assertNotEqual(LoadedCharacter("A", [b"12"]).digest, LoadedCharacter("A", [b"1", b"2"]).digest)


class PromptPartsTests(unittest.TestCase):
    def test_mentions_become_entity_references(self):
        parts, ids = build_prompt_parts("A photo of @Maya and @Leo at the beach.", {"Maya": "e1", "Leo": "e2"})
        self.assertEqual(parts, [
            {"text": "A photo of "},
            {"reference": {"entity": {"entityId": "e1", "handle": "Maya"}}},
            {"text": " and "},
            {"reference": {"entity": {"entityId": "e2", "handle": "Leo"}}},
            {"text": " at the beach."},
        ])
        self.assertEqual(ids, ["e1", "e2"])

    def test_longest_name_wins_and_word_boundary(self):
        parts, _ = build_prompt_parts("@Maya Rose waves; @Maya2 is not her; email a@Maya.com", {"Maya": "e1", "Maya Rose": "e3"})
        refs = [p["reference"]["entity"]["handle"] for p in parts if "reference" in p]
        self.assertEqual(refs, ["Maya Rose", "Maya"])  # "@Maya2" untouched, "a@Maya.com" -> Maya matched (boundary is '.')
        self.assertIn({"text": " waves; @Maya2 is not her; email a"}, parts)

    def test_unknown_mention_and_unmentioned_character(self):
        parts, ids = build_prompt_parts("Hello @Nobody", {"Maya": "e1"})
        self.assertEqual(parts, [{"text": "Hello @Nobody"}])
        self.assertEqual(ids, ["e1"])  # still attached as a reference
        self.assertEqual(plain_prompt("@Maya and @Nobody", ["Maya"]), "Maya and @Nobody")

    def test_empty_prompt_gives_one_text_part(self):
        self.assertEqual(build_prompt_parts("", {"Maya": "e1"})[0], [{"text": ""}])


class _FakeDB:
    def __init__(self):
        self.rows = {}
        self.touched = 0

    async def get_flow_character(self, token_id, project_id, name, digest):
        return self.rows.get((token_id, project_id, name, digest))

    async def upsert_flow_character(self, token_id, project_id, name, digest, entity_id):
        self.rows[(token_id, project_id, name, digest)] = {"entity_id": entity_id}

    async def delete_flow_character(self, token_id, project_id, name, digest):
        self.rows.pop((token_id, project_id, name, digest), None)

    async def touch_flow_character(self, *a):
        self.touched += 1


def _flow(photos_seen=None, missing=False):
    fc = MagicMock()
    fc._n = 0

    async def create(at, project_id, name):
        fc._n += 1
        return f"ent-{fc._n}"

    fc.create_character_entity = AsyncMock(side_effect=create)
    fc.upload_image = AsyncMock(side_effect=lambda at, img, ar, project_id=None: "media-" + img.decode()[:6])
    fc.copy_project_media_to_character_slot = AsyncMock(return_value={})
    refs = photos_seen

    async def batch_get(at, ids):
        if missing:
            return [{"error": {"code": 5, "message": "Entity not found: " + ids[0]}}]
        n = refs if refs is not None else len(fc.copy_project_media_to_character_slot.await_args_list)
        return [{"entity": {"entityId": ids[0], "entityInfo": {"characterInfo": {"imageReferences": [{"workflowId": "w"}] * n}}}}]

    fc.batch_get_entities = AsyncMock(side_effect=batch_get)
    return fc


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.svc = CharacterService()
        self.token = MagicMock(id=7, at="at")

    async def test_creates_uploads_attaches_and_caches(self):
        fc = _flow(); db = _FakeDB()
        out = await self.svc.ensure(fc, db, self.token, "proj", [_c("Maya", 2)])
        self.assertEqual(out, {"Maya": "ent-1"})
        self.assertEqual(fc.upload_image.await_count, 2)
        slots = [call.kwargs["image_reference_index"] for call in fc.copy_project_media_to_character_slot.await_args_list]
        self.assertEqual(slots, [0, 1])
        self.assertEqual(len(db.rows), 1)
        # second call: cache hit, no new entity
        out2 = await self.svc.ensure(fc, db, self.token, "proj", [_c("Maya", 2)])
        self.assertEqual(out2, {"Maya": "ent-1"})
        self.assertEqual(fc.create_character_entity.await_count, 1)
        self.assertEqual(db.touched, 1)

    async def test_missing_cached_entity_is_recreated(self):
        fc = _flow(); db = _FakeDB()
        db.rows[(7, "proj", "Maya", _c("Maya").digest)] = {"entity_id": "gone"}
        calls = {"n": 0}
        real = fc.batch_get_entities.side_effect

        async def batch_get(at, ids):
            calls["n"] += 1
            if ids == ["gone"]:
                return [{"error": {"code": 5, "message": "Entity not found: gone"}}]
            return await real(at, ids)

        fc.batch_get_entities = AsyncMock(side_effect=batch_get)
        out = await self.svc.ensure(fc, db, self.token, "proj", [_c("Maya")])
        self.assertEqual(out, {"Maya": "ent-1"})
        self.assertEqual(db.rows[(7, "proj", "Maya", _c("Maya").digest)]["entity_id"], "ent-1")

    async def test_other_read_errors_fail_closed_and_keep_the_row(self):
        fc = _flow(); db = _FakeDB()
        db.rows[(7, "proj", "Maya", _c("Maya").digest)] = {"entity_id": "keep"}
        fc.batch_get_entities = AsyncMock(return_value=[{"error": {"code": 7, "message": "denied"}}])
        with self.assertRaises(CharacterSetupError) as ctx:
            await self.svc.ensure(fc, db, self.token, "proj", [_c("Maya")])
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("keep", db.rows[(7, "proj", "Maya", _c("Maya").digest)]["entity_id"])
        fc.create_character_entity.assert_not_awaited()

    async def test_attach_failure_publishes_no_row(self):
        fc = _flow(); db = _FakeDB()
        fc.copy_project_media_to_character_slot = AsyncMock(side_effect=RuntimeError("HTTP 500"))
        with self.assertRaises(CharacterSetupError) as ctx:
            await self.svc.ensure(fc, db, self.token, "proj", [_c("Maya")])
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(db.rows, {})

    async def test_incomplete_read_back_publishes_no_row(self):
        fc = _flow(photos_seen=0); db = _FakeDB()
        with self.assertRaises(CharacterSetupError):
            await self.svc.ensure(fc, db, self.token, "proj", [_c("Maya", 2)])
        self.assertEqual(db.rows, {})

    async def test_concurrent_requests_share_one_entity(self):
        fc = _flow(); db = _FakeDB()
        results = await asyncio.gather(*(self.svc.ensure(fc, db, self.token, "proj", [_c("Maya")]) for _ in range(4)))
        self.assertEqual({tuple(r.items()) for r in results}, {(("Maya", "ent-1"),)})
        self.assertEqual(fc.create_character_entity.await_count, 1)

    async def test_cache_is_scoped_per_account_and_project(self):
        fc = _flow(); db = _FakeDB()
        await self.svc.ensure(fc, db, self.token, "proj-a", [_c("Maya")])
        await self.svc.ensure(fc, db, self.token, "proj-b", [_c("Maya")])
        await self.svc.ensure(fc, db, MagicMock(id=8, at="at2"), "proj-a", [_c("Maya")])
        self.assertEqual(fc.create_character_entity.await_count, 3)


if __name__ == "__main__":
    unittest.main()
