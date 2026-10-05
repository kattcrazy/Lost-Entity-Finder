"""Unit tests for Lost Entity Find And Replace manager helpers."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

mock_config_flow = types.ModuleType("lost_entity_finder.config_flow")
mock_config_flow.get_enable_bulk_fix = lambda _hass, _entry: False
mock_config_flow.get_max_pending_changes = lambda _hass, _entry: 1000
sys.modules["lost_entity_finder.config_flow"] = mock_config_flow

mock_scanner = types.ModuleType("lost_entity_finder.scanner")
mock_scanner.async_scan_tracked_references = MagicMock(return_value={})
mock_scanner.resource_types_for_scopes = lambda scopes: frozenset()
mock_scanner.coerce_scan_result = lambda value: value
sys.modules["lost_entity_finder.scanner"] = mock_scanner

from lost_entity_finder.manager import EntityFinderManager  # noqa: E402
from lost_entity_finder.models import PendingEntityIdChange, ReferenceHit  # noqa: E402


class EntityFinderManagerTests(unittest.TestCase):
    """Tests for lost-entity counting logic."""

    def setUp(self) -> None:
        """Create a manager with a mocked store."""
        self.manager = EntityFinderManager.__new__(EntityFinderManager)
        self.manager.hass = MagicMock()
        self.manager.entry = MagicMock()
        self.manager.store = MagicMock()
        self.manager._active_issue_ids = set()
        self.manager._find_issue_ids = set()
        self.manager._current_hits = {}
        self.manager._unsubs = []
        self.manager._delayed_unsubs = []
        self.manager._listeners = []
        self.manager._awaiting_scan = set()
        self.manager._issue_snapshots = {}
        self.manager._has_completed_scan = False
        self.manager._auto_replace_all_running = False
        self.manager._scan_full = False
        self.manager._scan_scopes = set()
        self.manager._scan_lock = asyncio.Lock()
        self.manager._scan_idle = asyncio.Event()
        self.manager._scan_idle.set()
        self.manager.entity_platform = None
        self.manager._debouncer = MagicMock()

    def test_get_lost_entity_ids_includes_scan_hits(self) -> None:
        """Lost count should include old IDs with active references."""
        self.manager.store.get_pending_changes.return_value = {
            "sensor.old": PendingEntityIdChange(
                old_entity_id="sensor.old",
                new_entity_id="sensor.new",
                changed_at="2026-01-01T00:00:00+00:00",
            )
        }
        self.manager.store.is_ignored.return_value = False
        self.manager._current_hits = {
            "sensor.old": [
                ReferenceHit(
                    resource_type="automation",
                    label="Test",
                    edit_url="/config/automation/edit/test",
                    resource_id="test",
                )
            ]
        }

        self.assertEqual(self.manager.get_lost_entity_ids(), ["sensor.old"])
        self.assertEqual(self.manager.get_lost_count(), 1)

    def test_get_lost_entity_ids_includes_nothing_found(self) -> None:
        """Ignore All walks this list, so a nothing-found repair must be on it."""
        self.manager.store.get_pending_changes.return_value = {
            "sensor.old": PendingEntityIdChange(
                old_entity_id="sensor.old",
                new_entity_id="sensor.new",
                changed_at="2026-01-01T00:00:00+00:00",
                keep_without_references=True,
            )
        }
        self.manager.store.is_ignored.return_value = False
        self.manager._current_hits = {}

        self.assertEqual(self.manager.get_lost_entity_ids(), ["sensor.old"])

    def test_get_lost_entity_ids_includes_awaiting_scan(self) -> None:
        """Lost count should include changes recorded before the first scan."""
        self.manager.store.get_pending_changes.return_value = {
            "sensor.old": PendingEntityIdChange(
                old_entity_id="sensor.old",
                new_entity_id="sensor.new",
                changed_at="2026-01-01T00:00:00+00:00",
            )
        }
        self.manager.store.is_ignored.return_value = False
        self.manager._current_hits = {}
        self.manager._awaiting_scan = {"sensor.old"}

        self.assertEqual(self.manager.get_lost_entity_ids(), ["sensor.old"])

    def test_get_lost_entity_ids_excludes_ignored(self) -> None:
        """Ignored entity ID changes should not appear in the lost count."""
        self.manager.store.get_pending_changes.return_value = {
            "sensor.old": PendingEntityIdChange(
                old_entity_id="sensor.old",
                new_entity_id="sensor.new",
                changed_at="2026-01-01T00:00:00+00:00",
            )
        }
        self.manager.store.is_ignored.return_value = True
        self.manager._current_hits = {
            "sensor.old": [
                ReferenceHit(
                    resource_type="automation",
                    label="Test",
                    edit_url="/config/automation/edit/test",
                    resource_id="test",
                )
            ]
        }
        self.manager._awaiting_scan = {"sensor.old"}

        self.assertEqual(self.manager.get_lost_entity_ids(), [])

    def test_unload_cancels_startup_timer_and_debouncer(self) -> None:
        """Unload should cancel the startup scan and shut the debouncer down."""
        startup = MagicMock()
        self.manager._startup_scan_unsub = startup
        self.manager._unloaded = False
        self.manager._debouncer.async_shutdown = AsyncMock()

        asyncio.run(self.manager.async_unload())

        startup.assert_called_once_with()
        self.manager._debouncer.async_shutdown.assert_awaited()
        self.assertTrue(self.manager._unloaded)

    def test_failed_scope_keeps_previous_hits(self) -> None:
        """A crashed scanner must not wipe the repair for that area."""
        import lost_entity_finder.manager as manager_module

        kept = ReferenceHit(
            resource_type="dashboard",
            label="Overview",
            edit_url="/lovelace/home?edit=1",
            resource_id="lovelace",
        )
        original = manager_module.resource_types_for_scopes
        manager_module.resource_types_for_scopes = lambda scopes: (
            frozenset({"dashboard"}) if "lovelace" in scopes else frozenset()
        )
        try:
            self.manager._current_hits = {"sensor.old": [kept]}
            self.manager._merge_scoped_hits(
                {},
                frozenset({"lovelace"}),
                frozenset({"lovelace"}),
            )
            self.assertEqual(self.manager._current_hits["sensor.old"], [kept])

            self.manager._current_hits = {"sensor.old": [kept]}
            self.manager._merge_scoped_hits({}, frozenset({"lovelace"}), frozenset())
            # Successful empty scoped scans keep prior hits (reload races).
            self.assertEqual(self.manager._current_hits["sensor.old"], [kept])

            fresh = ReferenceHit(
                resource_type="dashboard",
                label="Energy",
                edit_url="/lovelace/energy?edit=1",
                resource_id="energy",
            )
            self.manager._current_hits = {"sensor.old": [kept]}
            self.manager._merge_scoped_hits(
                {"sensor.old": [fresh]},
                frozenset({"lovelace"}),
                frozenset(),
            )
            self.assertEqual(self.manager._current_hits["sensor.old"], [fresh])
        finally:
            manager_module.resource_types_for_scopes = original

    def test_incomplete_empty_scan_keeps_existing_issue(self) -> None:
        """A scan with no hits must not drop a tracked rename or its repair."""
        self.manager.store.get_pending_changes.return_value = {
            "sensor.old": PendingEntityIdChange(
                old_entity_id="sensor.old",
                new_entity_id="sensor.new",
                changed_at="2026-01-01T00:00:00+00:00",
            )
        }
        self.manager.store.is_ignored.return_value = False
        self.manager._active_issue_ids = {"lost_sensor__old"}
        self.manager._awaiting_scan = set()
        self.manager._issue_snapshots = {}
        self.manager._unloaded = False

        asyncio.run(
            self.manager._async_sync_repairs(
                {}, searched={"sensor.old"}, forget_clean=False
            )
        )

        self.assertIn("sensor.old", self.manager._awaiting_scan)
        self.assertIn("lost_sensor__old", self.manager._active_issue_ids)

    def test_auto_replace_all_logs_failure_summary(self) -> None:
        """Bulk Auto-Replace should log one summary when some items fail."""
        self.manager.entry = MagicMock()
        self.manager.store.get_pending_changes.return_value = {
            "sensor.bad": PendingEntityIdChange(
                old_entity_id="sensor.bad",
                new_entity_id="sensor.good",
                changed_at="2026-01-01T00:00:00+00:00",
            )
        }
        self.manager.store.is_ignored.return_value = False
        self.manager._current_hits = {
            "sensor.bad": [
                ReferenceHit(
                    resource_type="automation",
                    label="Hall",
                    edit_url="/config/automation/edit/hall",
                    resource_id="hall",
                    auto_replaceable=True,
                )
            ]
        }
        self.manager._unloaded = False
        self.manager.async_trigger_rescan = AsyncMock()
        self.manager.async_mark_completed = AsyncMock()
        self.manager.drop_hits_for_keys = MagicMock()

        result = MagicMock()
        result.updated = []
        result.updated_keys = []
        result.failed = ["Hall (automation)"]
        result.skipped = []

        with (
            patch(
                "lost_entity_finder.manager.get_enable_bulk_fix",
                return_value=True,
            ),
            patch(
                "lost_entity_finder.replacer.async_apply_replace",
                AsyncMock(return_value=result),
            ),
            self.assertLogs("lost_entity_finder.manager", level="WARNING") as logs,
        ):
            asyncio.run(self.manager.async_auto_replace_all())

        self.assertTrue(
            any("Auto-Replace All summary" in line for line in logs.output)
        )
        self.assertTrue(any("failed=1" in line for line in logs.output))
        self.assertFalse(self.manager.is_auto_replace_all_running)

    def test_unload_deletes_find_issues(self) -> None:
        """Find-references repairs must be removed with the integration."""
        self.manager._startup_scan_unsub = None
        self.manager._unloaded = False
        self.manager._debouncer.async_shutdown = AsyncMock()
        self.manager._find_issue_ids = {"find_sensor__old"}
        self.manager._active_issue_ids = set()

        with patch("lost_entity_finder.manager.ir.async_delete_issue") as delete_issue:
            asyncio.run(self.manager.async_unload())

        delete_issue.assert_called_once_with(
            self.manager.hass, "lost_entity_finder", "find_sensor__old"
        )
        self.assertEqual(self.manager._find_issue_ids, set())

    def test_wait_for_scan_rechecks_under_lock(self) -> None:
        """Waiters must not return between idle and a newly queued scan."""
        self.manager._scan_full = True
        self.manager._scan_idle.clear()

        async def _run() -> None:
            waiter = asyncio.create_task(self.manager.async_wait_for_scan())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.manager._scan_full = False
            self.manager._scan_idle.set()
            await waiter

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
