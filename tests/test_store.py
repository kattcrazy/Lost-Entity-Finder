"""Unit tests for Lost Entity Find And Replace storage."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.const import DEFAULT_MAX_PENDING_CHANGES
from lost_entity_finder.store import EntityFinderStore


class EntityFinderStoreTests(unittest.IsolatedAsyncioTestCase):
    """Tests for pending entity ID change storage."""

    async def asyncSetUp(self) -> None:
        """Create a store with mocked persistence."""
        self.store = EntityFinderStore.__new__(EntityFinderStore)
        self.store.hass = MagicMock()
        self.store._pending = {}
        self.store._ignored = {}
        self.store._max_pending_changes = DEFAULT_MAX_PENDING_CHANGES
        self.store._pending_store = MagicMock()
        self.store._pending_store.async_save = AsyncMock()
        self.store._ignored_store = MagicMock()
        self.store._ignored_store.async_save = AsyncMock()

    async def test_record_entity_id_change_persists_entry(self) -> None:
        """Recording a change should write to the pending store."""
        await self.store.async_record_entity_id_change(
            "sensor.old", "sensor.new", unique_id="abc"
        )

        pending = self.store.get_pending_changes()
        self.assertIn("sensor.old", pending)
        self.assertEqual(pending["sensor.old"].new_entity_id, "sensor.new")
        self.assertEqual(pending["sensor.old"].unique_id, "abc")
        self.assertTrue(pending["sensor.old"].changed_at)
        self.store._pending_store.async_save.assert_awaited()

    async def test_chain_collapse_updates_intermediate_target(self) -> None:
        """A->B followed by B->C should collapse the original old ID to C."""
        await self.store.async_record_entity_id_change("sensor.a", "sensor.b")
        await self.store.async_record_entity_id_change("sensor.b", "sensor.c")

        pending = self.store.get_pending_changes()
        self.assertEqual(pending["sensor.a"].new_entity_id, "sensor.c")
        self.assertEqual(pending["sensor.b"].new_entity_id, "sensor.c")

    async def test_chain_collapse_when_later_step_is_stored_first(self) -> None:
        """{B: C, A: B} must point A at C, not leave it on B."""
        await self.store.async_record_entity_id_change("sensor.b", "sensor.c")
        await self.store.async_record_entity_id_change("sensor.a", "sensor.b")

        pending = self.store.get_pending_changes()
        self.assertEqual(pending["sensor.a"].new_entity_id, "sensor.c")
        self.assertEqual(pending["sensor.b"].new_entity_id, "sensor.c")

    async def test_rename_cycle_is_dropped(self) -> None:
        """A->B then B->A has no stable final id, so both rows are removed."""
        await self.store.async_record_entity_id_change("sensor.a", "sensor.b")
        await self.store.async_record_entity_id_change("sensor.b", "sensor.a")

        pending = self.store.get_pending_changes()
        self.assertNotIn("sensor.a", pending)
        self.assertNotIn("sensor.b", pending)

    async def test_ignore_cap_keeps_the_id_just_ignored(self) -> None:
        """The ignore cap drops the oldest id, not the one just ignored."""
        from lost_entity_finder.const import MAX_IGNORED

        for index in range(MAX_IGNORED):
            entity_id = f"sensor.zzz_{index:02d}"
            self.store._ignored[entity_id] = f"2020-01-01T00:00:{index:02d}+00:00"

        await self.store.async_ignore("sensor.aaa")

        ignored = self.store.get_ignored_entity_ids()
        self.assertIn("sensor.aaa", ignored)
        self.assertEqual(len(ignored), MAX_IGNORED)
        self.assertNotIn("sensor.zzz_00", ignored)

    async def test_pending_cap_drops_oldest(self) -> None:
        """Pending changes should stay under the configured cap."""
        for index in range(DEFAULT_MAX_PENDING_CHANGES + 3):
            await self.store.async_record_entity_id_change(
                f"sensor.old_{index:02d}",
                f"sensor.new_{index:02d}",
            )
            self.store._pending[f"sensor.old_{index:02d}"]["changed_at"] = (
                f"2026-01-01T00:00:{index:02d}+00:00"
            )

        self.store._enforce_pending_cap()
        self.assertEqual(len(self.store._pending), DEFAULT_MAX_PENDING_CHANGES)
        self.assertNotIn("sensor.old_00", self.store._pending)
        self.assertIn(
            f"sensor.old_{DEFAULT_MAX_PENDING_CHANGES + 2:02d}", self.store._pending
        )

    async def test_async_load_reads_changes_key(self) -> None:
        """Storage should use the changes key for pending entity ID changes."""
        self.store._pending_store.async_load = AsyncMock(
            return_value={
                "changes": {
                    "sensor.old": {
                        "new_entity_id": "sensor.new",
                        "changed_at": "2026-01-01T00:00:00+00:00",
                    }
                }
            }
        )
        self.store._ignored_store.async_load = AsyncMock(return_value={"entity_ids": []})

        await self.store.async_load()

        pending = self.store.get_pending_changes()
        self.assertEqual(pending["sensor.old"].new_entity_id, "sensor.new")

    def test_parse_entity_id_change_from_old_entity_id(self) -> None:
        """Registry events should expose old and new entity IDs."""
        parsed = EntityFinderStore.parse_entity_id_change(
            {
                "action": "update",
                "entity_id": "sensor.kitchen",
                "old_entity_id": "sensor.old_kitchen",
            }
        )
        self.assertEqual(parsed, ("sensor.old_kitchen", "sensor.kitchen"))

    def test_parse_entity_id_change_from_changes(self) -> None:
        """Reset/regenerate events may only include the old ID in changes."""
        parsed = EntityFinderStore.parse_entity_id_change(
            {
                "action": "update",
                "entity_id": "light.living_room",
                "changes": {"entity_id": "light.old_living_room"},
            }
        )
        self.assertEqual(parsed, ("light.old_living_room", "light.living_room"))

    def test_parse_entity_id_change_ignores_unchanged(self) -> None:
        """Unchanged entity IDs should not be treated as changes."""
        parsed = EntityFinderStore.parse_entity_id_change(
            {
                "action": "update",
                "entity_id": "sensor.same",
                "old_entity_id": "sensor.same",
            }
        )
        self.assertIsNone(parsed)

    async def test_apply_registry_event_records_change(self) -> None:
        """Registry update events should create pending changes."""
        registry = MagicMock()
        registry.async_get.return_value = MagicMock(unique_id="uid-1")

        with patch(
            "lost_entity_finder.store.er.async_get",
            return_value=registry,
        ):
            await self.store.async_apply_registry_event(
                {
                    "action": "update",
                    "entity_id": "sensor.new",
                    "old_entity_id": "sensor.old",
                }
            )

        pending = self.store.get_pending_changes()
        self.assertEqual(pending["sensor.old"].new_entity_id, "sensor.new")

    async def test_record_normalizes_entity_id_case(self) -> None:
        """Registry and service paths must share one lowercase key."""
        await self.store.async_record_entity_id_change(
            "Sensor.Old", "Sensor.New", unique_id="abc"
        )
        pending = self.store.get_pending_changes()
        self.assertIn("sensor.old", pending)
        self.assertEqual(pending["sensor.old"].new_entity_id, "sensor.new")

    async def test_keep_without_references_clears_on_later_record(self) -> None:
        """Re-import without the flag must drop sticky nothing-found behavior."""
        await self.store.async_record_entity_id_change(
            "sensor.old",
            "sensor.new",
            keep_without_references=True,
        )
        await self.store.async_record_entity_id_change(
            "sensor.old",
            "sensor.newer",
            keep_without_references=False,
        )
        pending = self.store.get_pending_changes()
        self.assertFalse(pending["sensor.old"].keep_without_references)

    async def test_record_many_saves_once(self) -> None:
        """Bulk import should persist pending changes with one write."""
        imported = await self.store.async_record_entity_id_changes(
            [
                ("sensor.a", "sensor.a2", None),
                ("sensor.b", "sensor.b2", "uid-b"),
            ]
        )
        self.assertEqual(imported, 2)
        self.assertEqual(self.store._pending_store.async_save.await_count, 1)
        pending = self.store.get_pending_changes()
        self.assertEqual(pending["sensor.a"].new_entity_id, "sensor.a2")
        self.assertEqual(pending["sensor.b"].unique_id, "uid-b")

    async def test_ignore_many_saves_once(self) -> None:
        """Bulk ignore should persist ignored ids with one write."""
        await self.store.async_ignore_many(["sensor.a", "sensor.b"])
        self.assertEqual(self.store._ignored_store.async_save.await_count, 1)
        ignored = self.store.get_ignored_entity_ids()
        self.assertEqual(ignored, {"sensor.a", "sensor.b"})

    def test_parse_entity_id_change_normalizes_case(self) -> None:
        """Registry events should store lowercase ids."""
        parsed = EntityFinderStore.parse_entity_id_change(
            {
                "action": "update",
                "entity_id": "Sensor.Kitchen",
                "old_entity_id": "Sensor.Old_Kitchen",
            }
        )
        self.assertEqual(parsed, ("sensor.old_kitchen", "sensor.kitchen"))


if __name__ == "__main__":
    unittest.main()
