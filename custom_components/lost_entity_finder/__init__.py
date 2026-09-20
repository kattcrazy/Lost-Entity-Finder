"""Lost Entity Find And Replace integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from . import repairs  # noqa: F401
from .config_flow import get_max_pending_changes
from .const import DOMAIN, ISSUE_KIND_FIND, TRANSLATION_KEY_FOUND
from .entity_platform import EntityFinderEntityPlatform
from .entry_helpers import get_loaded_manager
from .manager import EntityFinderManager
from .scanner import async_scan_tracked_references
from .util import format_references_for_repair, slugify_find_issue_id

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_FIND_REFERENCES = "find_entity_references"
SERVICE_CHECK_ENTITY_ID_PAIR = "check_entity_id_pair"
SERVICE_SCHEMA_FIND_REFERENCES = vol.Schema({vol.Required("entity_id"): cv.entity_id})
SERVICE_SCHEMA_CHECK_ENTITY_ID_PAIR = vol.Schema(
    {
        vol.Required("renames"): vol.Schema({str: str}),
        vol.Optional("create_repair_without_references", default=False): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Lost Entity Find And Replace."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Lost Entity Find And Replace from a config entry."""
    manager = EntityFinderManager(hass, entry)
    await manager.async_setup()
    manager.entity_platform = EntityFinderEntityPlatform(hass, entry, manager)
    entry.runtime_data = manager
    hass.data[DOMAIN][entry.entry_id] = manager
    if not hass.services.has_service(DOMAIN, SERVICE_FIND_REFERENCES):
        async def _handle_service(call: ServiceCall) -> None:
            await _async_handle_find_references_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_FIND_REFERENCES,
            _handle_service,
            schema=SERVICE_SCHEMA_FIND_REFERENCES,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_CHECK_ENTITY_ID_PAIR):
        async def _handle_check_entity_id_pair(call: ServiceCall) -> None:
            await _async_handle_check_entity_id_pair_service(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_CHECK_ENTITY_ID_PAIR,
            _handle_check_entity_id_pair,
            schema=SERVICE_SCHEMA_CHECK_ENTITY_ID_PAIR,
        )
    await hass.config_entries.async_forward_entry_setups(entry, ["button"])
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates."""
    manager: EntityFinderManager = entry.runtime_data
    manager.store.set_max_pending_changes(get_max_pending_changes(hass, entry))
    await manager.store.async_enforce_pending_cap()
    await manager.entity_platform.async_refresh_auto_replace()
    await manager.async_trigger_rescan()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Lost Entity Find And Replace."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["button"])
    if not unload_ok:
        return False
    manager: EntityFinderManager = entry.runtime_data
    await manager.async_unload()
    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_FIND_REFERENCES)
        hass.services.async_remove(DOMAIN, SERVICE_CHECK_ENTITY_ID_PAIR)
    return True


async def _async_handle_find_references_service(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Scan and raise a repair listing locations that reference an entity ID."""
    from homeassistant.helpers import issue_registry as ir

    entity_id = str(call.data["entity_id"]).lower()
    hits_by_entity = await async_scan_tracked_references(hass, {entity_id})
    hits = hits_by_entity.get(entity_id, [])
    if not hits:
        raise HomeAssistantError(
            f"No references found for '{entity_id}' in supported scan targets."
        )

    references_md, manual_note = format_references_for_repair(hits)
    issue_id = slugify_find_issue_id(entity_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=TRANSLATION_KEY_FOUND,
        data={
            "kind": ISSUE_KIND_FIND,
            "entity_id": entity_id,
            "old_entity_id": entity_id,
        },
        translation_placeholders={
            "entity_id": entity_id,
            "references": references_md,
            "manual_note": f"{manual_note}\n\n" if manual_note else "",
        },
    )


async def _async_handle_check_entity_id_pair_service(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Check old/new entity ID pair(s) and sync repairs."""
    renames = call.data.get("renames")
    if not isinstance(renames, dict) or not renames:
        raise HomeAssistantError("renames must be a non-empty old-to-new entity ID map.")

    manager = get_loaded_manager(hass)
    if manager is None:
        raise HomeAssistantError("Lost Entity Finder is not loaded.")

    create_repair_without_references = bool(
        call.data.get("create_repair_without_references", False)
    )
    try:
        await manager.async_check_entity_renames(
            renames,
            create_repair_without_references=create_repair_without_references,
        )
    except Exception as err:
        _LOGGER.exception("check_entity_id_pair failed")
        raise HomeAssistantError(f"check_entity_id_pair failed: {err}") from err

