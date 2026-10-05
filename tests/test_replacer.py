"""Unit tests for Auto-Replace write guards."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder import ui_config  # noqa: E402


class UiFileHasIdTests(unittest.IsolatedAsyncioTestCase):
    """Tests for failing closed when UI YAML cannot be read."""

    async def test_unread_file_is_not_writable(self) -> None:
        """A missing UI id list must not allow a write."""
        with patch.object(
            ui_config, "async_ui_managed_ids", AsyncMock(return_value=None)
        ):
            self.assertFalse(
                await ui_config.ui_file_has_id(MagicMock(), "automation", "abc")
            )

    async def test_missing_id_is_not_writable(self) -> None:
        """Ids that are not in the UI file must stay manual."""
        with patch.object(
            ui_config, "async_ui_managed_ids", AsyncMock(return_value=set())
        ):
            self.assertFalse(
                await ui_config.ui_file_has_id(MagicMock(), "automation", "abc")
            )

    async def test_present_id_is_writable(self) -> None:
        """Ids in the UI file may be written."""
        with patch.object(
            ui_config, "async_ui_managed_ids", AsyncMock(return_value={"abc"})
        ):
            self.assertTrue(
                await ui_config.ui_file_has_id(MagicMock(), "automation", "abc")
            )


class UiItemHelperTests(unittest.TestCase):
    """Tests for YAML item auto-replace rules."""

    def test_blueprint_is_not_auto_replaceable(self) -> None:
        """Blueprint instances stay manual."""
        item = {"id": "morning", "use_blueprint": {"path": "author/file.yaml"}}
        self.assertTrue(ui_config.is_blueprint_config(item))
        self.assertFalse(
            ui_config.is_auto_replaceable_ui_item("morning", item, {"morning"})
        )

    def test_unread_ids_are_not_auto_replaceable(self) -> None:
        """Fail closed when the UI file could not be read."""
        item = {"id": "morning", "alias": "Morning"}
        self.assertFalse(ui_config.is_auto_replaceable_ui_item("morning", item, None))


if __name__ == "__main__":
    unittest.main()
