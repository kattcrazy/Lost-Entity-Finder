"""Scan scripts for tracked old entity IDs."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from ..models import ReferenceHit
from ..ui_config import async_scan_ui_domain


async def async_scan(
    hass: HomeAssistant, tracked: set[str]
) -> dict[str, list[ReferenceHit]]:
    """Scan scripts.yaml and package scripts."""
    return await async_scan_ui_domain(hass, "script", tracked)
