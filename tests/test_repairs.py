"""Unit tests for repair flow steps that do not need a Home Assistant instance."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

if "lost_entity_finder.config_flow" not in sys.modules:
    mock_config_flow = types.ModuleType("lost_entity_finder.config_flow")
    mock_config_flow.get_enable_bulk_fix = lambda *_args, **_kwargs: False
    sys.modules["lost_entity_finder.config_flow"] = mock_config_flow

if "lost_entity_finder.scanner" not in sys.modules:
    mock_scanner = types.ModuleType("lost_entity_finder.scanner")
    mock_scanner.async_scan_tracked_references = MagicMock(return_value={})
    mock_scanner.resource_types_for_scopes = lambda _scopes: frozenset()
    sys.modules["lost_entity_finder.scanner"] = mock_scanner

from lost_entity_finder.models import ReferenceHit  # noqa: E402
from lost_entity_finder.repairs import (  # noqa: E402
    LostEntityReferencesRepairFlow,
    is_find_references_submit,
)


def _hit() -> ReferenceHit:
    return ReferenceHit(
        resource_type="automation",
        label="Hall light",
        edit_url="/config/automation/edit/hall",
        resource_id="hall",
        auto_replaceable=True,
    )


class FindReferencesSubmitTests(unittest.TestCase):
    """Opening a find-references repair is not the same as confirming it."""

    def test_issue_id_payload_is_not_a_submit(self) -> None:
        """HA 2026.8 passes issue_id as the first step input."""
        self.assertFalse(is_find_references_submit(None))
        self.assertFalse(is_find_references_submit({"issue_id": "find_sensor_old"}))
        self.assertFalse(
            is_find_references_submit({"issue_id": "find_sensor_old", "extra": "1"})
        )
        self.assertTrue(is_find_references_submit({}))


class RepairFlowTests(unittest.IsolatedAsyncioTestCase):
    """Repair steps that can run against the flow object alone."""

    def _flow(self, kind: str = "find") -> LostEntityReferencesRepairFlow:
        return LostEntityReferencesRepairFlow(
            MagicMock(),
            "find_sensor_old",
            {
                "kind": kind,
                "entity_id": "sensor.old",
                "old_entity_id": "sensor.old",
                "new_entity_id": "sensor.new",
            },
        )

    async def test_find_references_waits_for_an_empty_confirm(self) -> None:
        """issue_id opens the form. An empty confirm dismisses the repair."""
        flow = self._flow()
        manager = MagicMock()
        manager.get_hits_for_old_entity.return_value = []
        manager.async_wait_for_scan = AsyncMock()
        manager.has_completed_scan = True
        manager.unregister_find_issue = MagicMock()

        async def _scan(*_args, **_kwargs):
            return {"sensor.old": [_hit()]}

        with (
            patch(
                "lost_entity_finder.repairs.get_loaded_manager", return_value=manager
            ),
            patch(
                "lost_entity_finder.repairs.async_scan_tracked_references", _scan
            ),
        ):
            opened = await flow.async_step_find_references(
                {"issue_id": "find_sensor_old"}
            )
            self.assertEqual(opened["type"], "form")
            self.assertEqual(opened["step_id"], "find_references")
            self.assertIn(
                "Hall light", opened["description_placeholders"]["references"]
            )

            confirmed = await flow.async_step_find_references({})
        self.assertEqual(confirmed["type"], "create_entry")
        manager.unregister_find_issue.assert_called_once_with("find_sensor_old")

    async def test_fix_later_aborts_without_closing_the_repair(self) -> None:
        """Fix later must not finish the flow, or Home Assistant deletes the issue."""
        flow = self._flow(kind="lost")
        result = await flow.async_step_fix_later()
        self.assertEqual(result["type"], "abort")
        self.assertEqual(result["reason"], "fix_later")

    async def test_apply_stays_open_when_replace_failed_or_hits_remain(self) -> None:
        """Auto-Replace stays on a result step until the hits are gone."""
        flow = self._flow(kind="lost")
        hit = _hit()
        flow._hits = [hit]
        result_obj = MagicMock()
        result_obj.failed = ["Hall light: not written"]
        result_obj.updated_keys = []
        result_obj.summary.return_value = "Failed:\n- Hall light: not written"
        manager = MagicMock()
        manager.async_trigger_rescan = AsyncMock()
        manager.async_wait_for_scan = AsyncMock()
        manager.async_mark_completed = AsyncMock()
        manager.drop_hits_for_keys = MagicMock()
        manager.has_completed_scan = True
        manager.get_hits_for_old_entity.return_value = [hit]

        async def _apply(*_args, **_kwargs):
            return result_obj

        with (
            patch("lost_entity_finder.repairs.async_apply_replace", _apply),
            patch("lost_entity_finder.repairs.get_loaded_manager", return_value=manager),
        ):
            stayed = await flow.async_step_apply()
        self.assertEqual(stayed["type"], "form")
        self.assertEqual(stayed["step_id"], "manual_remaining")
        manager.async_mark_completed.assert_not_awaited()

        result_obj.failed = []
        result_obj.updated_keys = [("automation", "hall")]
        manager.get_hits_for_old_entity.return_value = []

        async def _scan(*_args, **_kwargs):
            return {}

        flow._hits = [hit]
        with (
            patch("lost_entity_finder.repairs.async_apply_replace", _apply),
            patch("lost_entity_finder.repairs.get_loaded_manager", return_value=manager),
            patch("lost_entity_finder.repairs.async_scan_tracked_references", _scan),
        ):
            done = await flow.async_step_apply()
        self.assertEqual(done["type"], "create_entry")
        manager.drop_hits_for_keys.assert_called()
        manager.async_mark_completed.assert_awaited()


if __name__ == "__main__":
    unittest.main()
