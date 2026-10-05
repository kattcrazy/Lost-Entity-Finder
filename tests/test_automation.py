"""Unit tests for automation YAML scanning."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.scanners import automation  # noqa: E402


class AutomationScanTests(unittest.IsolatedAsyncioTestCase):
    """Tests for scanning automations.yaml."""

    async def test_scan_finds_entity_in_ui_yaml(self) -> None:
        """Refs in automations.yaml should raise an auto-replaceable hit."""
        hass = MagicMock()
        ui_yaml = [
            {
                "id": "auto1",
                "alias": "Auto",
                "trigger": {"entity_id": "sensor.old"},
            }
        ]
        with (
            patch(
                "lost_entity_finder.ui_config.async_load_ui_config",
                AsyncMock(return_value=ui_yaml),
            ),
            patch(
                "lost_entity_finder.ui_config._async_merged_domain_section",
                AsyncMock(return_value=None),
            ),
        ):
            hits = await automation.async_scan(hass, {"sensor.old"})

        self.assertIn("sensor.old", hits)
        self.assertTrue(hits["sensor.old"][0].auto_replaceable)
        self.assertEqual(hits["sensor.old"][0].resource_id, "auto1")

    async def test_package_copy_is_manual(self) -> None:
        """An automation only in packages must not be written to automations.yaml."""
        hass = MagicMock()
        packaged = [
            {
                "id": "pkg1",
                "alias": "Package",
                "trigger": {"entity_id": "sensor.old"},
            }
        ]
        with (
            patch(
                "lost_entity_finder.ui_config.async_load_ui_config",
                AsyncMock(return_value=[]),
            ),
            patch(
                "lost_entity_finder.ui_config._async_merged_domain_section",
                AsyncMock(return_value=packaged),
            ),
        ):
            hits = await automation.async_scan(hass, {"sensor.old"})

        self.assertIn("sensor.old", hits)
        self.assertFalse(hits["sensor.old"][0].auto_replaceable)


if __name__ == "__main__":
    unittest.main()
