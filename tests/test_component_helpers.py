"""Unit tests for component scanner helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap  # noqa: F401

from lost_entity_finder.ui_config import (
    is_auto_replaceable_ui_item,
)


class ComponentHelperTests(unittest.TestCase):
    """Tests for auto-replace eligibility on UI yaml items."""

    def test_item_with_id_is_auto_replaceable(self) -> None:
        """UI-created items should be auto-replaceable when they have an id."""
        item = {"id": "morning_routine", "alias": "Morning"}
        self.assertTrue(
            is_auto_replaceable_ui_item("morning_routine", item, {"morning_routine"})
        )

    def test_blueprint_items_are_not_auto_replaceable(self) -> None:
        """Blueprint-backed items must stay manual."""
        item = {
            "id": "morning_routine",
            "use_blueprint": {"path": "author/blueprint.yaml"},
        }
        self.assertFalse(
            is_auto_replaceable_ui_item("morning_routine", item, {"morning_routine"})
        )

    def test_items_without_id_are_not_auto_replaceable(self) -> None:
        """Items without a stable id cannot be updated in the UI yaml file."""
        item = {"alias": "Temporary"}
        self.assertFalse(is_auto_replaceable_ui_item(None, item, set()))

    def test_unread_ui_file_is_not_auto_replaceable(self) -> None:
        """A missing UI id list must not treat package items as writable."""
        item = {"id": "morning_routine", "alias": "Morning"}
        self.assertFalse(is_auto_replaceable_ui_item("morning_routine", item, None))

    def test_ui_file_membership_controls_auto_replace(self) -> None:
        """Only ids present in the UI yaml file are auto-replaceable."""
        item = {"id": "morning_routine", "alias": "Morning"}
        self.assertTrue(
            is_auto_replaceable_ui_item("morning_routine", item, {"morning_routine"})
        )
        self.assertFalse(is_auto_replaceable_ui_item("morning_routine", item, set()))


if __name__ == "__main__":
    unittest.main()
