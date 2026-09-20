"""Unit tests for Lost Entity Find And Replace helpers."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.models import ReferenceHit
from lost_entity_finder.util import (
    deep_replace_entity_ids,
    extract_entities_from_value,
    format_references_for_repair,
    merge_reference_hits,
    slugify_find_issue_id,
    slugify_issue_id,
)


class EntityFinderUtilTests(unittest.TestCase):
    """Tests for util helpers."""

    def test_slugify_issue_id(self) -> None:
        """Issue IDs should be stable and safe."""
        self.assertEqual(slugify_issue_id("sensor.door"), "lost_sensor_door")

    def test_slugify_find_issue_id(self) -> None:
        """Find-reference issue IDs should use a distinct prefix."""
        self.assertEqual(
            slugify_find_issue_id("input_datetime.s_wake_up_time"),
            "find_input_datetime_s_wake_up_time",
        )

    def test_merge_reference_hits_deduplicates(self) -> None:
        """Merged hits should deduplicate by resource."""
        hit = ReferenceHit(
            resource_type="automation",
            label="Test",
            edit_url="/config/automation/edit/test",
            resource_id="test",
        )
        merged = merge_reference_hits(
            [{"sensor.old": [hit]}, {"sensor.old": [hit]}]
        )
        self.assertEqual(len(merged["sensor.old"]), 1)

    def test_deep_replace_entity_id_in_dict(self) -> None:
        """Structured replace should update entity_id fields."""
        hass = MagicMock()
        config = {"entity_id": "sensor.door", "nested": {"entity": "sensor.door"}}
        new_config, count = asyncio.run(
            deep_replace_entity_ids(hass, config, "sensor.door", "sensor.window")
        )
        self.assertEqual(count, 2)
        self.assertEqual(new_config["entity_id"], "sensor.window")
        self.assertEqual(new_config["nested"]["entity"], "sensor.window")

    def test_deep_replace_avoids_partial_match(self) -> None:
        """Replace should not touch sensor.doorbell when replacing sensor.door."""
        hass = MagicMock()
        config = {"entity_id": "sensor.doorbell"}
        new_config, count = asyncio.run(
            deep_replace_entity_ids(hass, config, "sensor.door", "sensor.window")
        )
        self.assertEqual(count, 0)
        self.assertEqual(new_config["entity_id"], "sensor.doorbell")

    def test_deep_replace_does_not_mutate_original(self) -> None:
        """Auto-Replace works on copies and leaves the source config unchanged."""
        hass = MagicMock()
        config = {"entity_id": "sensor.door"}
        new_config, count = asyncio.run(
            deep_replace_entity_ids(hass, config, "sensor.door", "sensor.window")
        )
        self.assertEqual(count, 1)
        self.assertEqual(config["entity_id"], "sensor.door")
        self.assertEqual(new_config["entity_id"], "sensor.window")

    def test_format_references_marks_manual_items(self) -> None:
        """Repairs should flag helpers and YAML configs before auto-replace runs."""
        hits = [
            ReferenceHit(
                resource_type="automation",
                label="UI Automation",
                edit_url="/config/automation/edit/ui",
                resource_id="ui",
                auto_replaceable=True,
            ),
            ReferenceHit(
                resource_type="automation",
                label="YAML Automation",
                edit_url="/config/automation/edit/yaml",
                resource_id="yaml",
                auto_replaceable=False,
            ),
            ReferenceHit(
                resource_type="helper",
                label="Trend Helper",
                edit_url="/config/helpers",
                resource_id="sensor.trend",
                auto_replaceable=False,
            ),
        ]
        references, manual_note = format_references_for_repair(hits)
        self.assertIn("UI Automation", references)
        self.assertIn("manual update required", references)
        self.assertIn("YAML Automation", references)
        self.assertIn("Trend Helper", references)
        self.assertIn("2 reference(s) cannot be auto-replaced", manual_note)

    def test_extract_finds_entity_id_dict_keys(self) -> None:
        """Dict keys that are entity IDs should be detected."""
        hass = MagicMock()
        config = {
            "google_assistant": {
                "entity_config": {
                    "event.summer_s_light_switch_action": {"expose": True},
                    "climate.upstairs": {"expose": True},
                }
            }
        }

        async def _run() -> set[str]:
            return await extract_entities_from_value(
                hass,
                config,
                {
                    "event.summer_s_light_switch_action",
                    "climate.upstairs",
                },
            )

        found = asyncio.run(_run())
        self.assertEqual(
            found,
            {"event.summer_s_light_switch_action", "climate.upstairs"},
        )

    def test_extract_finds_entity_id_in_jinja_string_comparison(self) -> None:
        """Entity IDs compared as Jinja strings should be detected."""
        hass = MagicMock()
        config = {
            "variables": {
                "notify_target": (
                    "{% if trigger.entity_id == 'device_tracker.example_phone' %}\n"
                    "  notify.mobile_app_user_one\n"
                    "{% else %}\n"
                    "  notify.mobile_app_user_two\n"
                    "{% endif %}"
                )
            }
        }

        async def _run() -> set[str]:
            return await extract_entities_from_value(
                hass,
                config,
                {"device_tracker.example_phone"},
            )

        found = asyncio.run(_run())
        self.assertEqual(found, {"device_tracker.example_phone"})

    def test_extract_finds_notify_entity_in_action(self) -> None:
        """Notify entities used as action targets should be detected."""
        hass = MagicMock()
        config = {
            "actions": [
                {
                    "action": "notify.mobile_app_user_one",
                    "data": {"message": "hello"},
                }
            ]
        }

        async def _run() -> set[str]:
            return await extract_entities_from_value(
                hass,
                config,
                {"notify.mobile_app_user_one"},
            )

        found = asyncio.run(_run())
        self.assertEqual(found, {"notify.mobile_app_user_one"})

    def test_extract_finds_notify_entity_in_template_variable(self) -> None:
        """Notify entities referenced in template variables should be detected."""
        hass = MagicMock()
        config = {
            "variables": {
                "notify_target": (
                    "{% if trigger.entity_id == 'person.example_user' %}\n"
                    "  notify.mobile_app_user_two\n"
                    "{% else %}\n"
                    "  notify.mobile_app_user_one\n"
                    "{% endif %}"
                )
            }
        }

        async def _run() -> set[str]:
            return await extract_entities_from_value(
                hass,
                config,
                {"notify.mobile_app_user_one", "notify.mobile_app_user_two"},
            )

        found = asyncio.run(_run())
        self.assertEqual(
            found,
            {"notify.mobile_app_user_one", "notify.mobile_app_user_two"},
        )

    def test_extract_uses_async_render_to_info_for_templates(self) -> None:
        """Template entity detection should call async_render_to_info without hass."""
        hass = MagicMock()
        template_value = "{{ states('sensor.example_target') | float }}"

        class _RenderInfo:
            entities = ("sensor.example_target",)

        with patch("lost_entity_finder.util.template.Template") as mock_template_cls:
            mock_tmpl = MagicMock()
            mock_tmpl.async_render_to_info.return_value = _RenderInfo()
            mock_template_cls.return_value = mock_tmpl

            async def _run() -> set[str]:
                return await extract_entities_from_value(
                    hass,
                    template_value,
                    {"sensor.example_target"},
                )

            found = asyncio.run(_run())

        mock_tmpl.async_render_to_info.assert_called_once_with({})
        self.assertEqual(found, {"sensor.example_target"})

    def test_extract_finds_template_entity_without_literal_substring(self) -> None:
        """Entities resolved only via render_to_info should still be detected."""
        hass = MagicMock()
        template_value = "{{ states(entity_id) }}"

        class _RenderInfo:
            entities = ("sensor.hidden_target",)

        with patch("lost_entity_finder.util.template.Template") as mock_template_cls:
            mock_tmpl = MagicMock()
            mock_tmpl.async_render_to_info.return_value = _RenderInfo()
            mock_template_cls.return_value = mock_tmpl

            async def _run() -> set[str]:
                return await extract_entities_from_value(
                    hass,
                    template_value,
                    {"sensor.hidden_target"},
                )

            found = asyncio.run(_run())

        self.assertEqual(found, {"sensor.hidden_target"})


if __name__ == "__main__":
    unittest.main()
