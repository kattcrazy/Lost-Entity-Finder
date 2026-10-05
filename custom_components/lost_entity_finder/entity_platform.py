"""Entity platform coordinator for Lost Entity Find And Replace entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .config_flow import get_enable_bulk_fix
from .manager import EntityFinderManager

if TYPE_CHECKING:
    from .button import AutoReplaceAllButton


class EntityFinderEntityPlatform:
    """Coordinate Lost Entity Find And Replace standalone entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        manager: EntityFinderManager,
    ) -> None:
        """Initialize platform coordinator."""
        self.hass = hass
        self.entry = entry
        self.manager = manager
        self._async_add_button_entities = None
        self._auto_replace_button: AutoReplaceAllButton | None = None

    def async_setup_buttons(self, async_add_entities) -> None:
        """Set up Lost Entity Find And Replace buttons."""
        from .button import IgnoreAllButton, RescanLostEntitiesButton, RestoreIgnoredButton

        self._async_add_button_entities = async_add_entities
        async_add_entities(
            [
                RescanLostEntitiesButton(self.manager, self.entry),
                IgnoreAllButton(self.manager, self.entry),
                RestoreIgnoredButton(self.manager, self.entry),
            ]
        )
        if get_enable_bulk_fix(self.hass, self.entry):
            self._async_add_auto_replace()

    def _async_add_auto_replace(self) -> None:
        """Add the Auto-Replace All button if missing."""
        from .button import AutoReplaceAllButton

        if (
            self._auto_replace_button is not None
            or self._async_add_button_entities is None
        ):
            return
        self._auto_replace_button = AutoReplaceAllButton(self.manager, self.entry)
        self._async_add_button_entities([self._auto_replace_button])

    async def async_refresh_auto_replace(self) -> None:
        """Add or remove the Auto-Replace All button based on settings."""
        if get_enable_bulk_fix(self.hass, self.entry) or (
            self.manager is not None and self.manager.is_auto_replace_all_running
        ):
            self._async_add_auto_replace()
            return
        if self._auto_replace_button is not None:
            button = self._auto_replace_button
            self._auto_replace_button = None
            remove = getattr(button, "async_remove", None)
            if callable(remove):
                await remove()
