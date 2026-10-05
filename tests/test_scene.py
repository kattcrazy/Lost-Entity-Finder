"""Unit tests for scene scanner."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.scanners import scene as scene_scanner


class SceneScannerTests(unittest.TestCase):
    """Tests for scene YAML scanning."""

    def test_async_scan_reads_entities_from_scenes_yaml(self) -> None:
        """Scene members live under entities in scenes.yaml."""
        hass = MagicMock()
        ui_yaml = [
            {
                "id": "evening_lights",
                "name": "Evening Lights",
                "entities": {"light.living_room": "on"},
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
            hits = asyncio.run(scene_scanner.async_scan(hass, {"light.living_room"}))

        self.assertIn("light.living_room", hits)
        self.assertEqual(hits["light.living_room"][0].resource_type, "scene")
        self.assertEqual(hits["light.living_room"][0].resource_id, "evening_lights")
        self.assertTrue(hits["light.living_room"][0].auto_replaceable)

    def test_async_scan_empty_file_has_no_hits(self) -> None:
        """No scenes.yaml items means no scene hits."""
        hass = MagicMock()
        with (
            patch(
                "lost_entity_finder.ui_config.async_load_ui_config",
                AsyncMock(return_value=[]),
            ),
            patch(
                "lost_entity_finder.ui_config._async_merged_domain_section",
                AsyncMock(return_value=None),
            ),
        ):
            hits = asyncio.run(scene_scanner.async_scan(hass, {"light.living_room"}))

        self.assertEqual(hits, {})


if __name__ == "__main__":
    unittest.main()
