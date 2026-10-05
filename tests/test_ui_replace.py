"""Unit tests for surgical Auto-Replace of UI yaml files."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder import ui_config  # noqa: E402


class _Hass:
    """Minimal hass for file-backed Auto-Replace tests."""

    def __init__(self, config_dir: Path) -> None:
        self.config = MagicMock()
        self.config.path = lambda rel="": str(config_dir / rel if rel else config_dir)
        self.services = MagicMock()
        self.services.async_call = AsyncMock()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class ReplaceEntityInUiYamlTests(unittest.TestCase):
    """Tests for in-text item edits."""

    def test_replaces_one_list_item_only(self) -> None:
        """Sibling automations must keep the old entity id."""
        original = (
            "- id: '1'\n"
            "  alias: One\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
            "- id: '2'\n"
            "  alias: Two\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
        )
        result = ui_config.replace_entity_in_ui_yaml(
            original, "automation", "1", "sensor.old", "sensor.new"
        )
        self.assertIsNotNone(result)
        new_text, count = result
        self.assertEqual(count, 1)
        self.assertIn("entity_id: sensor.new", new_text)
        self.assertEqual(new_text.count("sensor.old"), 1)
        self.assertIn("id: '2'", new_text)
        self.assertTrue(new_text.endswith("entity_id: sensor.old\n"))

    def test_replaces_script_mapping_item(self) -> None:
        """Script files are a mapping keyed by script id."""
        original = (
            "alpha:\n"
            "  alias: Alpha\n"
            "  sequence:\n"
            "    - entity_id: light.old\n"
            "beta:\n"
            "  alias: Beta\n"
            "  sequence:\n"
            "    - entity_id: light.old\n"
        )
        result = ui_config.replace_entity_in_ui_yaml(
            original, "script", "alpha", "light.old", "light.new"
        )
        self.assertIsNotNone(result)
        new_text, count = result
        self.assertEqual(count, 1)
        self.assertIn("alpha:\n", new_text)
        self.assertIn("entity_id: light.new", new_text.split("beta:")[0])
        self.assertIn("entity_id: light.old", new_text.split("beta:")[1])

    def test_skips_blueprint_item(self) -> None:
        """Blueprint instances stay manual."""
        original = (
            "- id: '1'\n"
            "  use_blueprint:\n"
            "    path: foo.yaml\n"
            "    input:\n"
            "      target: sensor.old\n"
        )
        self.assertIsNone(
            ui_config.replace_entity_in_ui_yaml(
                original, "automation", "1", "sensor.old", "sensor.new"
            )
        )

    def test_missing_id_aborts(self) -> None:
        """Unknown ids must not rewrite the file."""
        original = "- id: '1'\n  trigger:\n    entity_id: sensor.old\n"
        self.assertIsNone(
            ui_config.replace_entity_in_ui_yaml(
                original, "automation", "missing", "sensor.old", "sensor.new"
            )
        )

    def test_keeps_crlf_newlines(self) -> None:
        """An item edit must not rewrite the rest of the file's line endings."""
        original = (
            "- id: '1'\r\n"
            "  trigger:\r\n"
            "    entity_id: sensor.old\r\n"
            "- id: '2'\r\n"
            "  trigger:\r\n"
            "    entity_id: sensor.keep\r\n"
        )
        result = ui_config.replace_entity_in_ui_yaml(
            original, "automation", "1", "sensor.old", "sensor.new"
        )
        self.assertIsNotNone(result)
        new_text, _count = result
        self.assertIn("sensor.new", new_text)
        self.assertIn("sensor.keep", new_text)
        self.assertNotIn("\n", new_text.replace("\r\n", ""))

    def test_scene_with_both_ids_stays_manual(self) -> None:
        """Do not rename a scene key when the new id is already a key."""
        original = (
            "- id: '1'\n"
            "  name: Evening\n"
            "  entities:\n"
            "    light.old:\n"
            "      brightness: 100\n"
            "    light.new:\n"
            "      brightness: 200\n"
        )
        self.assertIsNone(
            ui_config.replace_entity_in_ui_yaml(
                original, "scene", "1", "light.old", "light.new"
            )
        )

    def test_scene_renames_member_key_when_target_is_free(self) -> None:
        """A scene that only has the old id may rename that key."""
        original = (
            "- id: '1'\n"
            "  name: Evening\n"
            "  entities:\n"
            "    light.old:\n"
            "      brightness: 100\n"
        )
        result = ui_config.replace_entity_in_ui_yaml(
            original, "scene", "1", "light.old", "light.new"
        )
        self.assertIsNotNone(result)
        new_text, count = result
        self.assertEqual(count, 1)
        self.assertIn("    light.new:\n", new_text)
        self.assertNotIn("light.old", new_text)


class AsyncReplaceUiItemTests(unittest.IsolatedAsyncioTestCase):
    """Tests for backup, mtime abort, and restore."""

    async def test_writes_sidecar_and_edits_original(self) -> None:
        """A successful replace copies the file then edits only the matching item."""
        temp = Path(tempfile.mkdtemp())
        path = temp / "automations.yaml"
        original = (
            "- id: '1'\n"
            "  alias: One\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
            "- id: '2'\n"
            "  alias: Two\n"
            "  trigger:\n"
            "    entity_id: sensor.keep\n"
        )
        path.write_text(original, encoding="utf-8")
        hass = _Hass(temp)
        try:
            changed = await ui_config.async_replace_ui_item(
                hass, "automation", "1", "sensor.old", "sensor.new"
            )
            self.assertTrue(changed)
            written = path.read_text(encoding="utf-8")
            backup_path = Path(str(path) + ui_config.BACKUP_SUFFIX)
            self.assertFalse(backup_path.exists())
            self.assertIn("sensor.new", written)
            self.assertIn("sensor.keep", written)
            self.assertNotIn("sensor.old", written)
            hass.services.async_call.assert_awaited()
        finally:
            for leftover in temp.iterdir():
                leftover.unlink()
            temp.rmdir()

    async def test_aborts_when_mtime_changes(self) -> None:
        """If the file changed since it was read, leave it alone."""
        temp = Path(tempfile.mkdtemp())
        path = temp / "automations.yaml"
        original = (
            "- id: '1'\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
        )
        path.write_text(original, encoding="utf-8")
        hass = _Hass(temp)
        calls = {"n": 0}

        def _mtime(_path: str) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                return 100
            return 101

        try:
            with patch.object(ui_config, "_mtime_ns", _mtime):
                changed = await ui_config.async_replace_ui_item(
                    hass, "automation", "1", "sensor.old", "sensor.new"
                )
            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(Path(str(path) + ui_config.BACKUP_SUFFIX).exists())
        finally:
            path.unlink()
            temp.rmdir()

    async def test_post_backup_mtime_abort_removes_sidecar(self) -> None:
        """A change after the sidecar copy must not leave an orphan backup."""
        temp = Path(tempfile.mkdtemp())
        path = temp / "automations.yaml"
        original = (
            "- id: '1'\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
        )
        path.write_text(original, encoding="utf-8")
        hass = _Hass(temp)
        calls = {"n": 0}

        def _mtime(_path: str) -> int:
            calls["n"] += 1
            # Read + early checks see a stable mtime; the post-backup check flips.
            if calls["n"] <= 3:
                return 100
            return 101

        try:
            with patch.object(ui_config, "_mtime_ns", _mtime):
                changed = await ui_config.async_replace_ui_item(
                    hass, "automation", "1", "sensor.old", "sensor.new"
                )
            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertFalse(Path(str(path) + ui_config.BACKUP_SUFFIX).exists())
        finally:
            for leftover in temp.iterdir():
                leftover.unlink()
            temp.rmdir()

    async def test_restores_backup_when_write_is_dirty(self) -> None:
        """If the written file is not the expected token edit, restore the copy."""
        temp = Path(tempfile.mkdtemp())
        path = temp / "automations.yaml"
        original = (
            "- id: '1'\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
        )
        path.write_text(original, encoding="utf-8")
        hass = _Hass(temp)

        def _dirty_write(target: str, contents: str) -> None:
            Path(target).write_text(contents + "# junk\n", encoding="utf-8")

        try:
            with patch.object(ui_config, "_write_text_atomic", _dirty_write):
                changed = await ui_config.async_replace_ui_item(
                    hass, "automation", "1", "sensor.old", "sensor.new"
                )
            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        finally:
            for leftover in temp.iterdir():
                leftover.unlink()
            temp.rmdir()

    async def test_keeps_backup_when_reload_fails(self) -> None:
        """File write still counts as success; backup stays if reload throws."""
        temp = Path(tempfile.mkdtemp())
        path = temp / "automations.yaml"
        original = (
            "- id: '1'\n"
            "  trigger:\n"
            "    entity_id: sensor.old\n"
        )
        path.write_text(original, encoding="utf-8")
        hass = _Hass(temp)
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("reload boom"))
        try:
            changed = await ui_config.async_replace_ui_item(
                hass, "automation", "1", "sensor.old", "sensor.new"
            )
            self.assertTrue(changed)
            self.assertIn("sensor.new", path.read_text(encoding="utf-8"))
            backup_path = Path(str(path) + ui_config.BACKUP_SUFFIX)
            self.assertTrue(backup_path.exists())
            self.assertEqual(backup_path.read_text(encoding="utf-8"), original)
        finally:
            for leftover in temp.iterdir():
                leftover.unlink()
            temp.rmdir()


if __name__ == "__main__":
    unittest.main()
