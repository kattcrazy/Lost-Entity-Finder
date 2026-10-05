"""Repairs flow for Lost Entity Find And Replace."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .config_flow import get_enable_bulk_fix
from .const import DOMAIN, ISSUE_KIND_FIND
from .entry_helpers import get_loaded_manager
from .manager import EntityFinderManager
from .models import ReferenceHit
from .replacer import async_apply_replace, async_preview_replace
from .scanner import async_scan_tracked_references
from .ui_text import ui
from .util import (
    format_references_for_repair,
    has_auto_replaceable_hits,
)


def is_find_references_submit(user_input: dict[str, str] | None) -> bool:
    """Return True when find-references was confirmed, not merely opened.

    Home Assistant starts the fix flow with {issue_id: ...}. A later core
    version may add more keys to that opening payload. The form submit is an
    empty dict, so any payload that still carries issue_id is not a confirm.
    """
    if user_input is None:
        return False
    return "issue_id" not in user_input


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
        hass: HomeAssistant | None = None,
        issue_id: str = "",
        data: dict[str, str | int | float | None] | None = None,
    ) -> None:
        """Initialize repair flow.

        Arguments are optional so a future repairs manager can construct the
        flow with no custom arguments and set issue_id afterwards.
        """
        super().__init__()
        self._hass = hass
        self._issue_id = issue_id
        self._data = data or {}
        self._kind = str(self._data.get("kind", "lost"))
        self._entity_id = str(
            self._data.get("entity_id") or self._data.get("old_entity_id") or ""
        )
        self._old_entity_id = str(self._data.get("old_entity_id") or self._entity_id)
        self._new_entity_id = str(self._data.get("new_entity_id", ""))
        self._preview = ""
        self._result_summary = ""
        self._hits: list[ReferenceHit] = []

    def _get_manager(self) -> EntityFinderManager | None:
        """Return the active manager."""
        return get_loaded_manager(self._hass)

    def _bulk_fix_enabled(self) -> bool:
        """Return whether Auto-Replace is enabled in integration settings."""
        manager = self._get_manager()
        if manager is None:
            return False
        return get_enable_bulk_fix(self._hass, manager.entry)

    def _can_offer_auto_replace(self, hits: list[ReferenceHit]) -> bool:
        """Return True when Auto-Replace can change at least one reference."""
        return self._bulk_fix_enabled() and has_auto_replaceable_hits(hits)

    async def _async_scan_entity_hits(self) -> list[ReferenceHit]:
        """Scan supported targets for this repair's entity id."""
        result = await async_scan_tracked_references(
            self._hass, {self._old_entity_id}
        )
        return result.get(self._old_entity_id, [])

    async def _async_get_hits(self) -> list[ReferenceHit]:
        """Return current reference hits for this repair."""
        manager = self._get_manager()
        if manager is not None:
            await manager.async_wait_for_scan()

        # Find-references issues are not stored in manager hits. Always rescan
        # so reopening after the first manager scan still shows locations.
        if self._kind == ISSUE_KIND_FIND:
            return await self._async_scan_entity_hits()

        if manager is not None:
            hits = manager.get_hits_for_old_entity(self._old_entity_id)
            if hits or manager.has_completed_scan:
                return hits
        hits = manager.get_hits_for_old_entity(self._old_entity_id) if manager else []
        if not hits:
            hits = await self._async_scan_entity_hits()
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
            bulk_fix_hint = f"{ui('bulk_fix_disabled_hint')}\n\n"
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
                        "fix_later": ui("action_fix_later"),
                        "mark_completed": ui("action_mark_completed"),
                        "ignore": ui("action_ignore"),
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
        if is_find_references_submit(user_input):
            manager = self._get_manager()
            if manager is not None:
                manager.unregister_find_issue(self._issue_id)
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
                            "auto_replace": ui("action_auto_replace"),
                            "ignore": ui("action_ignore"),
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

        references, manual_note = format_references_for_repair(self._hits)
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
        """Close the dialog and leave the repair open."""
        return self.async_abort(reason="fix_later")

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
            manager.drop_hits_for_keys(self._old_entity_id, result.updated_keys)
            await manager.async_trigger_rescan()
            self._hits = await self._async_get_hits()

        if result.failed or self._hits:
            return await self.async_step_manual_remaining()

        if manager is not None:
            await manager.async_mark_completed(
                self._old_entity_id, issue_id=self._issue_id
            )
        return self.async_create_entry(title="", data={})
