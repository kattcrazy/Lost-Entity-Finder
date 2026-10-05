"""Unit tests for Lovelace dashboard scanner."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.scanners import lovelace


class LovelaceScannerTests(unittest.TestCase):
    """Tests for dashboard entity extraction."""

    def test_extracts_helper_entities_from_sections_view(self) -> None:
        """Helpers on sections dashboards should be detected."""
        hass = MagicMock()
        config = {
            "views": [
                {
                    "type": "sections",
                    "sections": [
                        {
                            "type": "grid",
                            "cards": [
                                {"type": "tile", "entity": "input_boolean.my_helper"},
                                {
                                    "type": "entities",
                                    "entities": [
                                        "input_number.test",
                                        {"entity": "input_select.mode"},
                                    ],
                                },
                                {
                                    "type": "button",
                                    "tap_action": {
                                        "action": "call-service",
                                        "service": "input_boolean.turn_on",
                                        "target": {
                                            "entity_id": "input_boolean.other"
                                        },
                                    },
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        tracked = {
            "input_boolean.my_helper",
            "input_number.test",
            "input_select.mode",
            "input_boolean.other",
        }
        extracted = asyncio.run(
            lovelace._async_extract_entities(hass, config, tracked)
        )
        self.assertEqual(set(extracted.keys()), tracked)

    def test_extracts_entities_from_features_and_conditions(self) -> None:
        """Nested card features and conditions should be detected."""
        hass = MagicMock()
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "tile",
                            "entity": "light.kitchen",
                            "features": [
                                {
                                    "type": "area-controls",
                                    "controls": [
                                        "switch",
                                        {"entity_id": "input_boolean.area_toggle"},
                                    ],
                                }
                            ],
                        },
                        {
                            "type": "conditional",
                            "conditions": [
                                {"entity": "input_boolean.gate", "state": "on"}
                            ],
                            "card": {
                                "type": "entity",
                                "entity": "switch.outside",
                            },
                        },
                    ]
                }
            ]
        }
        tracked = {
            "input_boolean.area_toggle",
            "input_boolean.gate",
            "switch.outside",
        }
        extracted = asyncio.run(
            lovelace._async_extract_entities(hass, config, tracked)
        )
        self.assertEqual(set(extracted.keys()), tracked)

    async def _async_scan_dashboard(self, **dashboard_attrs: object) -> dict[str, list]:
        """Run async_scan with a mocked dashboard."""
        hass = MagicMock()
        dashboard = MagicMock()
        dashboard.url_path = "lovelace"
        dashboard.mode = "storage"
        dashboard.config = {"title": "Home"}
        dashboard.async_load = AsyncMock(
            return_value={
                "views": [
                    {
                        "cards": [
                            {"type": "tile", "entity": "input_boolean.dashboard_helper"}
                        ]
                    }
                ]
            }
        )
        for key, value in dashboard_attrs.items():
            setattr(dashboard, key, value)
        lovelace_data = MagicMock()
        lovelace_data.dashboards = {"lovelace": dashboard}
        hass.data = {"lovelace": lovelace_data}
        tracked = {"input_boolean.dashboard_helper"}
        with patch.object(
            lovelace, "_async_scan_yaml_dashboard_files", AsyncMock()
        ):
            return await lovelace.async_scan(hass, tracked)

    def test_async_scan_finds_helper_on_dashboard(self) -> None:
        """Dashboard scanner should report helper entity references."""
        hits = asyncio.run(self._async_scan_dashboard())
        self.assertIn("input_boolean.dashboard_helper", hits)
        self.assertEqual(hits["input_boolean.dashboard_helper"][0].resource_type, "dashboard")

    def test_extract_keeps_every_view(self) -> None:
        """An entity on two views should keep a link for each view."""
        hass = MagicMock()
        config = {
            "views": [
                {"title": "Kitchen", "path": "kitchen", "cards": [{"entity": "light.lamp"}]},
                {"title": "Bedroom", "path": "bedroom", "cards": [{"entity": "light.lamp"}]},
            ]
        }
        extracted = asyncio.run(
            lovelace._async_extract_entities(hass, config, {"light.lamp"})
        )
        self.assertEqual(
            extracted["light.lamp"],
            [("kitchen", "Kitchen"), ("bedroom", "Bedroom")],
        )

    def test_yaml_dashboard_is_manual(self) -> None:
        """YAML dashboards must not be Auto-Replaceable."""
        hits = asyncio.run(self._async_scan_dashboard(mode="yaml"))
        self.assertIn("input_boolean.dashboard_helper", hits)
        self.assertFalse(hits["input_boolean.dashboard_helper"][0].auto_replaceable)

    def test_missing_store_warns(self) -> None:
        """A missing dashboard store should warn instead of failing silently."""
        hass = MagicMock()
        hass.data = {}
        lovelace._store_missing_warned = False
        with (
            self.assertLogs(
                "lost_entity_finder.scanners.lovelace", level="WARNING"
            ) as logs,
            patch.object(
                lovelace, "_async_scan_yaml_dashboard_files", AsyncMock()
            ),
        ):
            hits = asyncio.run(lovelace.async_scan(hass, {"light.x"}))
        self.assertEqual(hits, {})
        self.assertTrue(
            any("Dashboard store is missing" in line for line in logs.output)
        )

    def test_unusable_store_warns(self) -> None:
        """Storage dashboards with no loadable API should warn once."""
        hass = MagicMock()
        dashboard = MagicMock()
        dashboard.url_path = "lovelace"
        dashboard.mode = "storage"
        dashboard.async_load = None
        dashboard.async_save = MagicMock()
        lovelace_data = MagicMock()
        lovelace_data.dashboards = {"lovelace": dashboard}
        hass.data = {"lovelace": lovelace_data}
        lovelace._store_unusable_warned = False
        with (
            self.assertLogs(
                "lost_entity_finder.scanners.lovelace", level="WARNING"
            ) as logs,
            patch.object(
                lovelace, "_async_scan_yaml_dashboard_files", AsyncMock()
            ),
        ):
            hits = asyncio.run(lovelace.async_scan(hass, {"light.x"}))
        self.assertEqual(hits, {})
        self.assertTrue(
            any("store shape looks unusable" in line for line in logs.output)
        )


if __name__ == "__main__":
    unittest.main()
