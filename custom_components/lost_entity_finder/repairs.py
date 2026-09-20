"""Repairs flow for Lost Entity Find And Replace."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .config_flow import get_enable_bulk_fix
from .const import DOMAIN, ISSUE_KIND_FIND
from .entry_helpers import get_loaded_manager, iter_loaded_entries
from .manager import EntityFinderManager
from .models import ReferenceHit
from .replacer import async_apply_replace, async_preview_replace
from .scanner import async_scan_tracked_references
from .util import (
    format_references_for_repair,
    get_manual_hits,
    has_auto_replaceable_hits,
)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow."""
    return LostEntityReferencesRepairFlow(hass, issue_id, data or {})


class LostEntityReferencesRepairFlow(RepairsFlow):
    """Repair flow for lost entity references after an entity ID change."""

    def __init__(
        self,
        hass: HomeAssistant,
        issue_id: str,
        data: dict[str, str | int | float | None],
    ) -> None:
        """Initialize repair flow."""
        self._hass = hass
        self._issue_id = issue_id
        self._data = data
        self._kind = str(data.get("kind", "lost"))
        self._entity_id = str(
            data.get("entity_id") or data.get("old_entity_id") or ""
        )
        self._old_entity_id = str(data.get("old_entity_id") or self._entity_id)
        self._new_entity_id = str(data.get("new_entity_id", ""))
        self._preview = ""
        self._result_summary = ""
        self._hits: list[ReferenceHit] = []

    def _get_manager(self) -> EntityFinderManager | None:
        """Return the active manager."""
        return get_loaded_manager(self._hass)

    def _bulk_fix_enabled(self) -> bool:
        """Return whether Auto-Replace is enabled in integration settings."""
        for entry in iter_loaded_entries(self._hass):
            return get_enable_bulk_fix(self._hass, entry)
        return False

    def _can_offer_auto_replace(self, hits: list[ReferenceHit]) -> bool:
        """Return True when Auto-Replace can change at least one reference."""
        return self._bulk_fix_enabled() and has_auto_replaceable_hits(hits)

    async def _async_get_hits(self) -> list[ReferenceHit]:
        """Return current reference hits for this repair."""
        manager = self._get_manager()
        hits = manager.get_hits_for_old_entity(self._old_entity_id) if manager else []
        if not hits:
            hits = (
                await async_scan_tracked_references(self._hass, {self._old_entity_id})
            ).get(self._old_entity_id, [])
        return hits

    def _placeholders(
        self,
        hits: list[ReferenceHit] | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build translation placeholders."""
        if hits is None:
            manager = self._get_manager()
            hits = (
                manager.get_hits_for_old_entity(self._old_entity_id) if manager else []
            )
        references, manual_note = format_references_for_repair(hits)
        bulk_fix_hint = ""
        if has_auto_replaceable_hits(hits) and not self._bulk_fix_enabled():
            bulk_fix_hint = "_(Auto-Replace is disabled in integration settings.)_\n\n"
        placeholders = {
            "old_entity_id": self._old_entity_id,
            "new_entity_id": self._new_entity_id,
            "references": references,
            "manual_note": f"{manual_note}\n\n" if manual_note else "",
            "bulk_fix_hint": bulk_fix_hint,
            "result_summary": "",
        }
        if extra:
            placeholders.update(extra)
        return placeholders

    def _manual_completion_schema(self) -> vol.Schema:
        """Return schema for manual completion actions."""
        return vol.Schema(
            {
                vol.Required("action", default="fix_later"): vol.In(
                    {
                        "fix_later": "Fix later",
                        "mark_completed": "Mark as completed",
                        "ignore": "Ignore",
                    }
                )
            }
        )

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Choose Ignore or Auto-Replace."""
        if self._kind == ISSUE_KIND_FIND:
            return await self.async_step_find_references(user_input)
        self._hits = await self._async_get_hits()
        if not self._can_offer_auto_replace(self._hits):
            return await self.async_step_manual_only(user_input)
        return await self.async_step_choose_action(user_input)

    async def async_step_find_references(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Show on-demand find results and allow dismiss."""
        if user_input is not None:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})

        self._hits = await self._async_get_hits()
        references, manual_note = format_references_for_repair(self._hits)
        return self.async_show_form(
            step_id="find_references",
            data_schema=vol.Schema({}),
            description_placeholders={
                "entity_id": self._entity_id or self._old_entity_id,
                "references": references,
                "manual_note": f"{manual_note}\n\n" if manual_note else "",
            },
        )

    async def async_step_choose_action(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Present repair actions."""
        self._hits = await self._async_get_hits()

        if user_input is not None:
            action = user_input.get("action")
            if action == "ignore":
                return await self.async_step_ignore()
            if action == "auto_replace":
                return await self.async_step_preview()

        return self.async_show_form(
            step_id="choose_action",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="auto_replace"): vol.In(
                        {
                            "auto_replace": "Auto-Replace",
                            "ignore": "Ignore",
                        }
                    )
                }
            ),
            description_placeholders=self._placeholders(self._hits),
        )

    async def async_step_manual_only(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Show manual-only guidance when nothing can be auto-replaced."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "ignore":
                return await self.async_step_ignore()
            if action == "mark_completed":
                return await self.async_step_mark_completed()
            if action == "fix_later":
                return await self.async_step_fix_later()

        if not self._hits:
            self._hits = await self._async_get_hits()

        return self.async_show_form(
            step_id="manual_only",
            data_schema=self._manual_completion_schema(),
            description_placeholders=self._placeholders(self._hits),
        )

    async def async_step_manual_remaining(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle remaining manual references after Auto-Replace."""
        if user_input is not None:
            action = user_input.get("action")
            if action == "ignore":
                return await self.async_step_ignore()
            if action == "mark_completed":
                return await self.async_step_mark_completed()
            if action == "fix_later":
                return await self.async_step_fix_later()

        if not self._hits:
            self._hits = await self._async_get_hits()

        manual_hits = get_manual_hits(self._hits)
        references, manual_note = format_references_for_repair(manual_hits)
        result_summary = (
            f"{self._result_summary}\n\n" if self._result_summary else ""
        )
        return self.async_show_form(
            step_id="manual_remaining",
            data_schema=self._manual_completion_schema(),
            description_placeholders={
                "old_entity_id": self._old_entity_id,
                "new_entity_id": self._new_entity_id,
                "references": references,
                "manual_note": f"{manual_note}\n\n" if manual_note else "",
                "result_summary": result_summary,
            },
        )

    async def async_step_ignore(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ignore this lost entity ID change."""
        manager = self._get_manager()
        if manager is not None:
            await manager.async_ignore(self._old_entity_id, issue_id=self._issue_id)
        else:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        return self.async_create_entry(title="", data={})

    async def async_step_mark_completed(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Mark manual reference updates as completed."""
        manager = self._get_manager()
        if manager is not None:
            await manager.async_mark_completed(
                self._old_entity_id, issue_id=self._issue_id
            )
        else:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        return self.async_create_entry(title="", data={})

    async def async_step_fix_later(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Close the repair flow and keep the issue open for later."""
        return self.async_create_entry(title="", data={})

    async def async_step_preview(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Preview Auto-Replace changes."""
        if not self._hits:
            self._hits = await self._async_get_hits()

        if not self._can_offer_auto_replace(self._hits):
            return await self.async_step_manual_only()

        preview, _unique = await async_preview_replace(
            self._hass, self._hits, self._old_entity_id, self._new_entity_id
        )
        self._preview = preview

        if not self._hits:
            return self.async_create_entry(title="", data={})

        if user_input is not None:
            return await self.async_step_apply()

        return self.async_show_form(
            step_id="preview",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders(
                self._hits, {"preview": preview}
            ),
        )

    async def async_step_apply(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Apply Auto-Replace."""
        if not self._hits:
            return self.async_create_entry(title="", data={})

        result = await async_apply_replace(
            self._hass,
            self._hits,
            self._old_entity_id,
            self._new_entity_id,
        )
        self._result_summary = result.summary()
        manager = self._get_manager()
        if manager is not None:
            await manager.async_trigger_rescan()
            self._hits = await self._async_get_hits()

        if get_manual_hits(self._hits):
            return await self.async_step_manual_remaining()

        return self.async_create_entry(title="", data={})
