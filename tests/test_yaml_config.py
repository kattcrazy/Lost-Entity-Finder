"""Unit tests for configuration.yaml scanning."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.scanners import yaml_config  # noqa: E402
from lost_entity_finder.scanners.yaml_config import LoadedCoreConfig  # noqa: E402
from lost_entity_finder.util import manual_reference_reason  # noqa: E402
from lost_entity_finder.models import ReferenceHit  # noqa: E402


class YamlConfigHelperTests(unittest.TestCase):
    """Tests for YAML config scanner helpers."""

    def test_section_label(self) -> None:
        """Section labels should name the configuration area."""
        self.assertEqual(
            yaml_config.section_label("sensor"),
            "configuration.yaml (sensor)",
        )

    def test_manual_reason_for_yaml_config(self) -> None:
        """YAML configuration hits should explain manual updates."""
        hit = ReferenceHit(
            resource_type="yaml_config",
            label="configuration.yaml (template)",
            edit_url="/config/configuration",
            resource_id="configuration.yaml:template",
            auto_replaceable=False,
        )
        self.assertEqual(manual_reference_reason(hit), "YAML config")


class YamlConfigTreeTests(unittest.IsolatedAsyncioTestCase):
    """Tests for config tree extraction."""

    async def asyncSetUp(self) -> None:
        """Create a mock Home Assistant instance."""
        self.hass = MagicMock()

    async def test_extract_skips_automation_subtree(self) -> None:
        """Automation YAML should not be scanned here (dedicated scanner handles it)."""
        config = {
            "automation": [
                {
                    "alias": "Test",
                    "trigger": {"platform": "state", "entity_id": "sensor.old"},
                }
            ],
            "template": [
                {
                    "sensor": {
                        "temp": {
                            "value_template": "{{ states('sensor.old') }}",
                        }
                    }
                },
            ],
        }
        found = await yaml_config._async_extract_from_config_tree(
            self.hass, config, {"sensor.old"}
        )
        self.assertEqual(found, {"sensor.old"})

    async def test_extract_skips_nested_domain_keys(self) -> None:
        """Package-style nested automation/script blocks should be skipped."""
        config = {
            "homeassistant": {
                "packages": {
                    "garage": {
                        "automation": [
                            {
                                "alias": "Garage",
                                "trigger": {
                                    "platform": "state",
                                    "entity_id": "binary_sensor.old",
                                },
                            }
                        ],
                        "notify": [{"name": "alert", "platform": "telegram"}],
                    }
                },
                "customize": {
                    "sensor.old": {"friendly_name": "Old"},
                },
            }
        }
        found = await yaml_config._async_extract_from_config_tree(
            self.hass, config["homeassistant"]["packages"]["garage"], {"binary_sensor.old"}
        )
        self.assertEqual(found, set())

    async def test_extract_finds_entity_id_dict_keys(self) -> None:
        """Entity IDs used as YAML mapping keys should be detected."""
        config = {
            "google_assistant": {
                "project_id": "example",
                "entity_config": {
                    "light.spare_bulb_4": {
                        "name": "Summer's Main Light",
                        "room": "Summer's Room",
                        "expose": True,
                    }
                },
            }
        }
        found = await yaml_config._async_extract_from_config_tree(
            self.hass, config, {"light.spare_bulb_4"}
        )
        self.assertEqual(found, {"light.spare_bulb_4"})

    async def test_extract_finds_customize_entity_keys(self) -> None:
        """homeassistant.customize entity keys should be detected."""
        config = {
            "homeassistant": {
                "customize": {
                    "sensor.old": {"friendly_name": "Old"},
                }
            }
        }
        found = await yaml_config._async_extract_from_config_tree(
            self.hass, config, {"sensor.old"}
        )
        self.assertEqual(found, {"sensor.old"})

    async def test_scan_finds_google_assistant_entity_config(self) -> None:
        """Merged configuration should detect entity IDs used as mapping keys."""
        merged_config = {
            "google_assistant": {
                "entity_config": {
                    "light.spare_bulb_4": {
                        "name": "Summer's Main Light",
                        "expose": True,
                    }
                }
            }
        }
        self.hass = MagicMock()

        with patch.object(
            yaml_config,
            "async_load_core_config",
            AsyncMock(return_value=LoadedCoreConfig(merged_config, True)),
        ):
            hits = await yaml_config.async_scan(self.hass, {"light.spare_bulb_4"})

        self.assertIn("light.spare_bulb_4", hits)
        self.assertEqual(hits["light.spare_bulb_4"][0].label, "configuration.yaml (google_assistant)")

    async def test_scan_finds_template_sensor_reference(self) -> None:
        """Merged configuration should surface YAML-only references."""
        merged_config = {
            "template": [
                {
                    "sensor": {
                        "temp": {
                            "value_template": "{{ states('sensor.old_light') }}",
                        }
                    }
                }
            ],
            "automation": [
                {
                    "alias": "Skip me",
                    "trigger": {"platform": "state", "entity_id": "sensor.old_light"},
                }
            ],
        }
        self.hass = MagicMock()

        with patch.object(
            yaml_config,
            "async_load_core_config",
            AsyncMock(return_value=LoadedCoreConfig(merged_config, True)),
        ):
            hits = await yaml_config.async_scan(self.hass, {"sensor.old_light"})

        self.assertIn("sensor.old_light", hits)
        self.assertEqual(len(hits["sensor.old_light"]), 1)
        self.assertEqual(hits["sensor.old_light"][0].resource_type, "yaml_config")
        self.assertEqual(
            hits["sensor.old_light"][0].label,
            "configuration.yaml (template)",
        )
        self.assertFalse(hits["sensor.old_light"][0].auto_replaceable)

    async def test_scan_includes_packages_and_themes_but_not_ui_files(self) -> None:
        """Merged packages and themes count. automations.yaml content does not."""
        merged_config = {
            "automation": [
                {
                    "id": "ui_only",
                    "trigger": {"platform": "state", "entity_id": "sensor.from_automations_yaml"},
                }
            ],
            "binary_sensor": [
                {
                    "platform": "template",
                    "sensors": {
                        "garage": {
                            "value_template": "{{ states('binary_sensor.from_package') }}",
                        }
                    },
                }
            ],
            "frontend": {
                "themes": {
                    "night": {"card-mod": "binary_sensor.from_theme"},
                },
                "extra_module_url": ["/local/binary_sensor.from_frontend"],
            },
            "homeassistant": {
                "packages": {
                    "already_merged": {
                        "sensor": [
                            {
                                "platform": "template",
                                "sensors": {
                                    "dup": {
                                        "value_template": "{{ states('sensor.only_inside_packages_key') }}",
                                    }
                                },
                            }
                        ]
                    }
                }
            },
        }
        self.hass = MagicMock()

        with patch.object(
            yaml_config,
            "async_load_core_config",
            AsyncMock(return_value=LoadedCoreConfig(merged_config, True)),
        ):
            hits = await yaml_config.async_scan(
                self.hass,
                {
                    "sensor.from_automations_yaml",
                    "binary_sensor.from_package",
                    "binary_sensor.from_theme",
                    "binary_sensor.from_frontend",
                    "sensor.only_inside_packages_key",
                },
            )

        self.assertNotIn("sensor.from_automations_yaml", hits)
        self.assertNotIn("sensor.only_inside_packages_key", hits)
        self.assertEqual(
            hits["binary_sensor.from_package"][0].label,
            "configuration.yaml (binary_sensor)",
        )
        self.assertEqual(
            hits["binary_sensor.from_theme"][0].label,
            "configuration.yaml (themes)",
        )
        self.assertNotIn("binary_sensor.from_frontend", hits)
        self.assertFalse(hits["binary_sensor.from_package"][0].auto_replaceable)

    async def test_file_fallback_scans_packages_when_merge_fails(self) -> None:
        """If yaml merge is gone, package files should still be scanned."""
        import tempfile
        from homeassistant.exceptions import HomeAssistantError

        async def _boom(_hass: object) -> dict:
            raise HomeAssistantError("merge unavailable")

        self.hass = MagicMock()
        temp = Path(tempfile.mkdtemp())
        packages = temp / "packages"
        packages.mkdir(exist_ok=True)
        (packages / "garage.yaml").write_text("placeholder: 1\n", encoding="utf-8")
        (temp / "configuration.yaml").write_text("placeholder: 1\n", encoding="utf-8")
        self.hass.config.path = lambda rel="": str(temp / rel if rel else temp)

        async def _run_job(func, *args):
            return func(*args)

        self.hass.async_add_executor_job = _run_job

        def _load(path: str):
            path_s = str(path)
            if path_s.endswith("configuration.yaml"):
                return {
                    "binary_sensor": [
                        {
                            "value_template": "{{ states('binary_sensor.from_file') }}"
                        }
                    ]
                }
            if path_s.endswith("garage.yaml"):
                return {
                    "sensor": [
                        {
                            "value_template": "{{ states('sensor.from_package_file') }}"
                        }
                    ]
                }
            return {}

        try:
            with (
                patch(
                    "homeassistant.config.async_hass_config_yaml",
                    _boom,
                ),
                patch.object(yaml_config, "_load_yaml_file", _load),
            ):
                hits = await yaml_config.async_scan(
                    self.hass,
                    {"binary_sensor.from_file", "sensor.from_package_file"},
                )
        finally:
            (packages / "garage.yaml").unlink()
            (temp / "configuration.yaml").unlink()
            packages.rmdir()
            temp.rmdir()

        self.assertIn("binary_sensor.from_file", hits)
        self.assertIn("sensor.from_package_file", hits)
        self.assertFalse(hits["binary_sensor.from_file"][0].auto_replaceable)
        self.assertFalse(hits["sensor.from_package_file"][0].auto_replaceable)


if __name__ == "__main__":
    unittest.main()
