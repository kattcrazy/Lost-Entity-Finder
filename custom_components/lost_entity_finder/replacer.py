"""Apply structured entity ID replacements across HA configs."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .models import ReferenceHit
from .scanners import lovelace as lovelace_scanner
from .ui_config import async_replace_ui_item, ui_file_has_id
from .ui_text import ui
from .util import (
    dedupe_reference_hits,
    deep_replace_entity_ids,
    extract_entities_from_value,
    manual_reference_reason,
)

_LOGGER = logging.getLogger(__name__)


class ReplaceResult:
    """Summary of an Auto-Replace run."""

    def __init__(self) -> None:
        """Initialize result."""
        self.updated: list[str] = []
        self.updated_keys: list[tuple[str, str]] = []
        self.skipped: list[str] = []
        self.failed: list[str] = []

    def summary(self) -> str:
        """Return a human-readable summary."""
        lines: list[str] = []
        if self.updated:
            lines.append(
                ui("summary_updated")
                + "\n"
                + "\n".join(f"- {item}" for item in self.updated)
            )
        if self.skipped:
            lines.append(
                ui("summary_skipped")
                + "\n"
                + "\n".join(f"- {item}" for item in self.skipped)
            )
        if self.failed:
            lines.append(
                ui("summary_failed")
                + "\n"
                + "\n".join(f"- {item}" for item in self.failed)
            )
        return "\n\n".join(lines) if lines else ui("summary_none")


async def async_preview_replace(
    hass: HomeAssistant,
    hits: list[ReferenceHit],
    old_entity_id: str,
    new_entity_id: str,
) -> tuple[str, dict[tuple[str, str], ReferenceHit]]:
    """Build a preview string and deduplicated hit map."""
    unique = dedupe_reference_hits(hits)
    auto_hits = [hit for hit in unique.values() if hit.auto_replaceable]
    manual_hits = [hit for hit in unique.values() if not hit.auto_replaceable]

    sections: list[str] = []
    if auto_hits:
        sections.append(
            ui("preview_will_update")
            + "\n"
            + "\n".join(
                f"- [{hit.label}]({hit.edit_url}) ({hit.resource_type})"
                for hit in auto_hits
            )
        )
    if manual_hits:
        sections.append(
            ui("preview_manual_required")
            + "\n"
            + "\n".join(
                f"- [{hit.label}]({hit.edit_url}) "
                f"({manual_reference_reason(hit)})"
                for hit in manual_hits
            )
        )

    preview = "\n\n".join(sections) if sections else ui("preview_none")
    return preview, unique


async def async_apply_replace(
    hass: HomeAssistant,
    hits: list[ReferenceHit],
    old_entity_id: str,
    new_entity_id: str,
) -> ReplaceResult:
    """Apply replacements for all reference hits."""
    result = ReplaceResult()
    _, unique = await async_preview_replace(hass, hits, old_entity_id, new_entity_id)
    replaced_dashboards: set[str] = set()

    for hit in unique.values():
        if hit.resource_type == "dashboard":
            dashboard_url = str(
                hit.extra.get("dashboard_url") or hit.resource_id.split("/", 1)[0]
            )
            if dashboard_url in replaced_dashboards:
                continue
        else:
            dashboard_url = ""
        try:
            changed = await _replace_hit(hass, hit, old_entity_id, new_entity_id)
        except Exception as err:  # noqa: BLE001 - collect per-resource failures
            _LOGGER.exception("Auto-Replace failed for %s", hit.label)
            result.failed.append(f"{hit.label}: {err}")
            continue
        if changed:
            result.updated.append(hit.label)
            result.updated_keys.append((hit.resource_type, hit.resource_id))
            if dashboard_url:
                replaced_dashboards.add(dashboard_url)
        elif hit.auto_replaceable:
            result.failed.append(f"{hit.label} ({hit.resource_type})")
        else:
            reason = manual_reference_reason(hit)
            result.skipped.append(f"{hit.label} ({reason})")

    return result


async def _replace_hit(
    hass: HomeAssistant,
    hit: ReferenceHit,
    old_entity_id: str,
    new_entity_id: str,
) -> bool:
    """Replace references for a single hit."""
    if hit.resource_type in {"automation", "script", "scene"}:
        if not await ui_file_has_id(hass, hit.resource_type, hit.resource_id):
            return False
        return await async_replace_ui_item(
            hass, hit.resource_type, hit.resource_id, old_entity_id, new_entity_id
        )
    if hit.resource_type == "dashboard":
        url_path = str(
            hit.extra.get("dashboard_url") or hit.resource_id.split("/", 1)[0]
        )
        return await _replace_dashboard(
            hass, url_path, old_entity_id, new_entity_id
        )
    return False


async def _replace_dashboard(
    hass: HomeAssistant,
    url_path: str,
    old_entity_id: str,
    new_entity_id: str,
) -> bool:
    """Replace entity IDs in a storage-mode Lovelace dashboard."""
    dashboard = lovelace_scanner.find_dashboard(hass, url_path)
    if dashboard is None:
        _LOGGER.warning(
            "Auto-Replace skipped dashboard %s: dashboard store is missing or "
            "this dashboard was not found",
            url_path,
        )
        return False
    if lovelace_scanner.is_yaml_dashboard(dashboard):
        _LOGGER.warning(
            "Auto-Replace skipped dashboard %s: YAML dashboards are manual edits",
            url_path,
        )
        return False
    load = getattr(dashboard, "async_load", None)
    save = getattr(dashboard, "async_save", None)
    if not callable(load) or not callable(save):
        _LOGGER.warning(
            "Auto-Replace skipped dashboard %s: load/save is not available",
            url_path,
        )
        return False
    try:
        original = await load(force=False)
    except Exception:  # noqa: BLE001 - missing dashboard or storage rename
        _LOGGER.debug("Dashboard %s could not be loaded", url_path, exc_info=True)
        return False
    new_config, count, clean = await deep_replace_entity_ids(
        hass, original, old_entity_id, new_entity_id
    )
    if count == 0 or not clean:
        return False
    await save(new_config)
    try:
        loaded = await load(force=True)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Auto-Replace could not re-read dashboard %s; restoring previous config",
            url_path,
            exc_info=True,
        )
        try:
            await save(original)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Auto-Replace could not restore dashboard %s",
                url_path,
                exc_info=True,
            )
        return False
    remaining = await extract_entities_from_value(hass, loaded, {old_entity_id})
    updated = await extract_entities_from_value(hass, loaded, {new_entity_id})
    if remaining or not updated:
        _LOGGER.warning(
            "Auto-Replace restored dashboard %s: the saved config still had %s "
            "or was missing %s",
            url_path,
            old_entity_id,
            new_entity_id,
        )
        try:
            await save(original)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Auto-Replace could not restore dashboard %s",
                url_path,
                exc_info=True,
            )
        return False
    return True
