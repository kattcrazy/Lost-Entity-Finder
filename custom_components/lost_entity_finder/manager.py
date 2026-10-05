"""Lost Entity Find And Replace manager - entity ID change tracking, scanning, and repairs."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_COMPONENT_LOADED,
    EVENT_CORE_CONFIG_UPDATE,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later

from .config_flow import get_enable_bulk_fix, get_max_pending_changes
from .const import (
    DEBOUNCE_COOLDOWN,
    DOMAIN,
    REGISTRY_RESCAN_DOMAINS,
    RESCAN_COMPONENT_DOMAINS,
    TRANSLATION_KEY_LOST,
)
from .models import ReferenceHit
from .scanner import (
    async_scan_tracked_references,
    coerce_scan_result,
    resource_types_for_scopes,
)
from .store import EntityFinderStore
from .ui_text import ui
from .util import format_references_for_repair, has_auto_replaceable_hits, slugify_issue_id

_LOGGER = logging.getLogger(__name__)

_COMPONENT_SCAN_SCOPES = {
    "automation": "automation",
    "script": "script",
    "scene": "scene",
    "lovelace": "lovelace",
}


def _optional_event(module_name: str, attr: str) -> str | None:
    """Return an event name, or None when that constant is gone."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    value = getattr(module, attr, None)
    if isinstance(value, str) and value:
        return value
    return None


class EntityFinderManager:
    """Coordinate entity ID change tracking, scanning, and repair issues."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize manager."""
        self.hass = hass
        self.entry = entry
        self.store = EntityFinderStore(hass, get_max_pending_changes(hass, entry))
        self._active_issue_ids: set[str] = set()
        self._find_issue_ids: set[str] = set()
        self._current_hits: dict[str, list[ReferenceHit]] = {}
        self._unsubs: list[Any] = []
        self._listeners: list[Callable[[], None]] = []
        self._awaiting_scan: set[str] = set()
        self.entity_platform = None
        self._scan_lock = asyncio.Lock()
        self._scan_full = False
        self._scan_scopes: set[str] = set()
        self._has_completed_scan = False
        self._issue_snapshots: dict[str, tuple[tuple[str, str], ...]] = {}
        self._unloaded = False
        self._startup_scan_unsub: Callable[[], None] | None = None
        self._delayed_unsubs: list[Callable[[], None]] = []
        self._auto_replace_all_running = False
        self._scan_idle = asyncio.Event()
        self._scan_idle.set()
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=DEBOUNCE_COOLDOWN,
            immediate=False,
            function=self._async_run_scan,
        )

    async def async_setup(self) -> None:
        """Set up manager."""
        await self.store.async_load()
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_HOMEASSISTANT_STARTED, self._handle_full_rescan
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._handle_registry_event
            )
        )
        lovelace_event = _optional_event(
            "homeassistant.components.lovelace.const", "EVENT_LOVELACE_UPDATED"
        )
        if lovelace_event:
            self._unsubs.append(
                self.hass.bus.async_listen(
                    lovelace_event, self._handle_lovelace_updated
                )
            )
        else:
            _LOGGER.debug(
                "Lovelace updated event is unavailable; dashboard rescans wait for a full scan"
            )
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_COMPONENT_LOADED, self._handle_component_loaded
            )
        )
        automation_event = _optional_event(
            "homeassistant.components.automation", "EVENT_AUTOMATION_RELOADED"
        )
        if automation_event:
            self._unsubs.append(
                self.hass.bus.async_listen(
                    automation_event, self._handle_automation_reloaded
                )
            )
        else:
            _LOGGER.debug(
                "Automation reloaded event is unavailable; automation rescans wait for a full scan"
            )
        scene_event = _optional_event(
            "homeassistant.components.homeassistant.scene", "EVENT_SCENE_RELOADED"
        )
        if scene_event:
            self._unsubs.append(
                self.hass.bus.async_listen(scene_event, self._handle_scene_reloaded)
            )
        else:
            _LOGGER.debug(
                "Scene reloaded event is unavailable; scene rescans wait for a full scan"
            )
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE, self._handle_full_rescan
            )
        )
        # While Home Assistant is still starting, STARTED runs the one full scan.
        # The delayed scan is only for a reload after startup has already passed.
        if getattr(self.hass, "is_running", False) is True:
            self._startup_scan_unsub = async_call_later(
                self.hass, 5, self._schedule_scan
            )

    async def async_unload(self) -> None:
        """Tear down manager."""
        self._unloaded = True
        startup = self._startup_scan_unsub
        self._startup_scan_unsub = None
        if startup is not None:
            startup()
        for unsub in self._delayed_unsubs:
            unsub()
        self._delayed_unsubs.clear()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        shutdown = getattr(self._debouncer, "async_shutdown", None)
        if shutdown is not None:
            await shutdown()
        for issue_id in list(self._active_issue_ids | self._find_issue_ids):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._active_issue_ids.clear()
        self._find_issue_ids.clear()

    def register_find_issue(self, issue_id: str) -> None:
        """Track an on-demand find-references repair for unload cleanup."""
        self._find_issue_ids.add(issue_id)

    def unregister_find_issue(self, issue_id: str) -> None:
        """Stop tracking a dismissed find-references repair."""
        self._find_issue_ids.discard(issue_id)

    @property
    def is_auto_replace_all_running(self) -> bool:
        """Return True while Auto-Replace All is in progress."""
        return self._auto_replace_all_running

    @callback
    def _handle_full_rescan(self, _event: Event) -> None:
        """Schedule a full rescan."""
        self._schedule_scan()

    @callback
    def _handle_lovelace_updated(self, _event: Event) -> None:
        """Rescan dashboards only."""
        self._schedule_scan(scopes=frozenset({"lovelace"}))

    @callback
    def _handle_automation_reloaded(self, _event: Event) -> None:
        """Rescan automations only."""
        self._schedule_scan(scopes=frozenset({"automation"}))

    @callback
    def _handle_scene_reloaded(self, _event: Event) -> None:
        """Rescan scenes only."""
        self._schedule_scan(scopes=frozenset({"scene"}))

    @callback
    def _handle_component_loaded(self, event: Event) -> None:
        """Schedule a scoped rescan when a relevant component loads."""
        domain = event.data.get("domain")
        if domain not in RESCAN_COMPONENT_DOMAINS:
            return
        scope = _COMPONENT_SCAN_SCOPES.get(domain) if isinstance(domain, str) else None
        if scope:
            self._schedule_scan(scopes=frozenset({scope}))

    @callback
    def _handle_registry_event(self, event: Event) -> None:
        """Handle entity registry updates."""
        parsed = EntityFinderStore.parse_entity_id_change(event.data)
        if parsed is not None:
            old_entity_id, _new_entity_id = parsed

            async def _record_and_scan(_now: Any) -> None:
                if unsub in self._delayed_unsubs:
                    self._delayed_unsubs.remove(unsub)
                if self._unloaded:
                    return
                await self.store.async_apply_registry_event(event.data)
                if self._unloaded:
                    return
                self._awaiting_scan.add(old_entity_id)
                self._async_notify_listeners()
                self._schedule_scan()

            unsub = async_call_later(self.hass, 0, _record_and_scan)
            self._delayed_unsubs.append(unsub)
            return

        if event.data.get("action") not in {"create", "remove"}:
            return

        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return

        domain = entity_id.partition(".")[0]
        if domain in REGISTRY_RESCAN_DOMAINS:
            scope = _COMPONENT_SCAN_SCOPES.get(domain)
            if scope:
                self._schedule_scan(scopes=frozenset({scope}))

    @callback
    def _schedule_scan(
        self, *_args: Any, scopes: frozenset[str] | None = None
    ) -> None:
        """Enqueue a debounced scan. No scopes means a full scan."""
        if self._unloaded:
            return
        if scopes:
            self._scan_scopes.update(scopes)
        else:
            self._scan_full = True
        self._mark_scan_busy()
        self.hass.async_create_task(self._debouncer.async_call())

    @property
    def has_completed_scan(self) -> bool:
        """Return True after at least one scan has finished."""
        return self._has_completed_scan

    def _scan_idle_event(self) -> asyncio.Event:
        """Return the idle event, creating it if tests constructed via __new__."""
        event = getattr(self, "_scan_idle", None)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._scan_idle = event
        return event

    def _mark_scan_busy(self) -> None:
        """Mark that a scan is queued or running."""
        self._scan_idle_event().clear()

    def _mark_scan_idle_if_drained(self) -> None:
        """Mark idle when no follow-up scan is queued."""
        if not self._scan_full and not self._scan_scopes:
            self._scan_idle_event().set()

    async def async_wait_for_scan(self) -> None:
        """Wait until a queued or in-flight scan finishes."""
        while True:
            await self._scan_idle_event().wait()
            async with self._scan_lock:
                # Re-check under the lock so a schedule between idle and acquire
                # cannot let callers proceed on stale hits.
                if (
                    self._scan_idle_event().is_set()
                    and not self._scan_full
                    and not self._scan_scopes
                ):
                    return

    async def _async_run_scan(self) -> None:
        """Scan tracked entity ID changes and sync repairs."""
        async with self._scan_lock:
            try:
                while not self._unloaded:
                    full = self._scan_full or not self._scan_scopes
                    scopes = frozenset(self._scan_scopes)
                    self._scan_full = False
                    self._scan_scopes = set()

                    tracked = set(self.store.get_tracked_old_ids())
                    if not tracked:
                        if full:
                            self._current_hits = {}
                            await self._async_sync_repairs({}, searched=set())
                        self._has_completed_scan = True
                    else:
                        result = coerce_scan_result(
                            await async_scan_tracked_references(
                                self.hass, tracked, None if full else scopes
                            )
                        )
                        if self._unloaded:
                            return
                        if full:
                            self._apply_full_hits(result.hits, result.failed_scopes)
                        else:
                            self._merge_scoped_hits(
                                result.hits, scopes, result.failed_scopes
                            )
                        await self._async_sync_repairs(
                            self._current_hits,
                            searched=tracked,
                            forget_clean=full and not result.failed_scopes,
                        )
                        self._has_completed_scan = True

                    if self._unloaded or (not self._scan_full and not self._scan_scopes):
                        return
            finally:
                self._mark_scan_idle_if_drained()

    def _apply_full_hits(
        self,
        hits: dict[str, list[ReferenceHit]],
        failed_scopes: frozenset[str],
    ) -> None:
        """Replace hits from a full scan. Keep previous hits for scanners that threw."""
        if not failed_scopes:
            self._current_hits = hits
            return
        kept_types = resource_types_for_scopes(failed_scopes)
        merged: dict[str, list[ReferenceHit]] = {}
        for old_id, old_hits in self._current_hits.items():
            kept = [hit for hit in old_hits if hit.resource_type in kept_types]
            if kept:
                merged[old_id] = kept
        for old_id, new_hits in hits.items():
            merged.setdefault(old_id, []).extend(new_hits)
        self._current_hits = merged

    def _merge_scoped_hits(
        self,
        partial: dict[str, list[ReferenceHit]],
        scopes: frozenset[str],
        failed_scopes: frozenset[str] = frozenset(),
    ) -> None:
        """Replace hits for the scanners that just ran and keep the rest.

        A scanner that threw does not clear the previous hits for its type.
        A successful empty scoped scan also keeps previous hits for that scope
        (reload events can race before the domain is ready).
        """
        replaced = resource_types_for_scopes(scopes - failed_scopes)
        if replaced:
            has_replacement_hits = any(
                hit.resource_type in replaced
                for hits in partial.values()
                for hit in hits
            )
            if not has_replacement_hits:
                replaced = frozenset()
        merged: dict[str, list[ReferenceHit]] = {}
        for old_id, hits in self._current_hits.items():
            kept = [hit for hit in hits if hit.resource_type not in replaced]
            if kept:
                merged[old_id] = kept
        for old_id, hits in partial.items():
            merged.setdefault(old_id, []).extend(hits)
        self._current_hits = merged

    async def async_check_entity_renames(
        self,
        renames: dict[str, str],
        *,
        create_repair_without_references: bool = False,
    ) -> dict[str, int]:
        """Record old-to-new entity ID map and rescan for lost references."""
        registry = er.async_get(self.hass)
        prepared: list[tuple[str, str, str | None]] = []
        skipped = 0

        for old_entity_id, new_entity_id in renames.items():
            old_id = old_entity_id.lower().strip()
            new_id = new_entity_id.lower().strip()
            if not old_id or not new_id or old_id == new_id:
                skipped += 1
                continue
            entry = registry.async_get(new_id)
            unique_id = entry.unique_id if entry else None
            prepared.append((old_id, new_id, unique_id))

        imported = await self.store.async_record_entity_id_changes(
            prepared,
            keep_without_references=create_repair_without_references,
        )
        await self.async_trigger_rescan()
        return {"imported": imported, "skipped": skipped}

    def _issue_is_current(self, issue_id: str, placeholders: dict[str, str]) -> bool:
        """Return True when this repair card already shows the same text."""
        snapshot = tuple(sorted(placeholders.items()))
        if (
            issue_id in self._active_issue_ids
            and self._issue_snapshots.get(issue_id) == snapshot
        ):
            return True
        self._issue_snapshots[issue_id] = snapshot
        return False

    async def _async_sync_repairs(
        self,
        hits: dict[str, list[ReferenceHit]],
        searched: set[str] | None = None,
        forget_clean: bool = False,
    ) -> None:
        """Create, update, or delete repair issues.

        searched limits this pass to ids that were actually scanned. A rename
        recorded mid-scan stays awaiting until the follow-up scan. forget_clean
        drops a pending rename only after a full scan whose scanners all
        succeeded and found nothing.
        """
        enable_bulk_fix = get_enable_bulk_fix(self.hass, self.entry)
        seen_issue_ids: set[str] = set()

        for old_entity_id, pending in self.store.get_pending_changes().items():
            issue_id = slugify_issue_id(old_entity_id)
            if searched is not None and old_entity_id not in searched:
                if (
                    issue_id in self._active_issue_ids
                    or old_entity_id in self._awaiting_scan
                ):
                    seen_issue_ids.add(issue_id)
                continue
            references = hits.get(old_entity_id, [])
            self._awaiting_scan.discard(old_entity_id)

            if not references:
                if pending.keep_without_references:
                    references_md = ui("no_references_keep")
                    manual_note = ""
                    bulk_fix_hint = (
                        "" if enable_bulk_fix else ui("bulk_fix_disabled_hint")
                    )
                    placeholders = {
                        "old_entity_id": old_entity_id,
                        "new_entity_id": pending.new_entity_id,
                        "references": references_md,
                        "manual_note": manual_note,
                        "bulk_fix_hint": bulk_fix_hint,
                    }
                    seen_issue_ids.add(issue_id)
                    if not self._issue_is_current(issue_id, placeholders):
                        ir.async_create_issue(
                            self.hass,
                            DOMAIN,
                            issue_id,
                            is_fixable=True,
                            is_persistent=False,
                            severity=ir.IssueSeverity.WARNING,
                            translation_key=TRANSLATION_KEY_LOST,
                            data={
                                "old_entity_id": old_entity_id,
                                "new_entity_id": pending.new_entity_id,
                            },
                            translation_placeholders=placeholders,
                        )
                    if self.store.is_ignored(old_entity_id):
                        ir.async_ignore_issue(self.hass, DOMAIN, issue_id, True)
                elif forget_clean:
                    await self.store.async_remove_pending(old_entity_id)
                    ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                    self._issue_snapshots.pop(issue_id, None)
                else:
                    # Keep the pending rename and any existing repair until a
                    # later scan finds refs or a clean full scan forgets it.
                    self._awaiting_scan.add(old_entity_id)
                    if issue_id in self._active_issue_ids:
                        seen_issue_ids.add(issue_id)
                continue

            seen_issue_ids.add(issue_id)

            references_md, manual_note = format_references_for_repair(references)
            bulk_fix_hint = (
                "" if enable_bulk_fix else ui("bulk_fix_disabled_hint")
            )
            placeholders = {
                "old_entity_id": old_entity_id,
                "new_entity_id": pending.new_entity_id,
                "references": references_md,
                "manual_note": f"{manual_note}\n\n" if manual_note else "",
                "bulk_fix_hint": bulk_fix_hint,
            }
            if not self._issue_is_current(issue_id, placeholders):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=TRANSLATION_KEY_LOST,
                    data={
                        "old_entity_id": old_entity_id,
                        "new_entity_id": pending.new_entity_id,
                    },
                    translation_placeholders=placeholders,
                )
            if self.store.is_ignored(old_entity_id):
                ir.async_ignore_issue(self.hass, DOMAIN, issue_id, True)

        for issue_id in self._active_issue_ids - seen_issue_ids:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            self._issue_snapshots.pop(issue_id, None)

        self._active_issue_ids = seen_issue_ids
        self._async_notify_listeners()

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to manager state updates."""

        def remove_listener() -> None:
            self._listeners.remove(update_callback)

        self._listeners.append(update_callback)
        return remove_listener

    @callback
    def _async_notify_listeners(self) -> None:
        """Notify entity listeners."""
        for listener in self._listeners:
            listener()

    @callback
    def get_lost_count(self) -> int:
        """Return the number of active (non-ignored) lost entity IDs."""
        return len(self.get_lost_entity_ids())

    @callback
    def get_lost_entity_ids(self) -> list[str]:
        """Return old entity IDs with unresolved lost entity references."""
        lost: set[str] = set()
        for old_id, pending in self.store.get_pending_changes().items():
            if self.store.is_ignored(old_id):
                continue
            if self._current_hits.get(old_id) or pending.keep_without_references:
                lost.add(old_id)
        for old_id in self._awaiting_scan:
            if not self.store.is_ignored(old_id):
                lost.add(old_id)
        return sorted(lost)

    def get_hits_for_old_entity(self, old_entity_id: str) -> list[ReferenceHit]:
        """Return latest scan hits for an old entity ID."""
        return list(self._current_hits.get(old_entity_id, []))

    def drop_hits_for_keys(
        self, old_entity_id: str, keys: list[tuple[str, str]] | set[tuple[str, str]]
    ) -> None:
        """Drop hits for resources already Auto-Replaced successfully.

        A later rescan that fails for that scope must not revive those hits.
        """
        keyset = set(keys)
        if not keyset:
            return
        hits = self._current_hits.get(old_entity_id)
        if not hits:
            return
        remaining = [
            hit
            for hit in hits
            if (hit.resource_type, hit.resource_id) not in keyset
        ]
        if remaining:
            self._current_hits[old_entity_id] = remaining
        else:
            self._current_hits.pop(old_entity_id, None)

    async def async_ignore(
        self, old_entity_id: str, issue_id: str | None = None
    ) -> None:
        """Ignore a lost entity ID change repair."""
        await self.store.async_ignore(old_entity_id)
        resolved_issue_id = issue_id or slugify_issue_id(old_entity_id)
        ir.async_ignore_issue(self.hass, DOMAIN, resolved_issue_id, True)
        self._async_notify_listeners()

    async def async_mark_completed(
        self, old_entity_id: str, issue_id: str | None = None
    ) -> None:
        """Mark a repair completed after manual reference updates."""
        await self.store.async_remove_pending(old_entity_id)
        resolved_issue_id = issue_id or slugify_issue_id(old_entity_id)
        ir.async_delete_issue(self.hass, DOMAIN, resolved_issue_id)
        self._current_hits.pop(old_entity_id, None)
        self._awaiting_scan.discard(old_entity_id)
        self._async_notify_listeners()

    async def async_ignore_all(self) -> None:
        """Ignore all active lost entity ID repairs."""
        old_entity_ids = self.get_lost_entity_ids()
        if not old_entity_ids:
            return
        await self.store.async_ignore_many(old_entity_ids)
        for old_entity_id in old_entity_ids:
            ir.async_ignore_issue(
                self.hass, DOMAIN, slugify_issue_id(old_entity_id), True
            )
        self._async_notify_listeners()

    async def async_restore_ignored(self) -> None:
        """Restore previously ignored lost entity repairs."""
        for old_entity_id in self.store.get_ignored_entity_ids():
            ir.async_ignore_issue(
                self.hass,
                DOMAIN,
                slugify_issue_id(old_entity_id),
                False,
            )
        await self.store.async_clear_ignored()
        await self.async_trigger_rescan()

    async def async_auto_replace_all(self) -> None:
        """Replace all lost entity references when bulk fix is enabled."""
        if not get_enable_bulk_fix(self.hass, self.entry):
            return
        if self._auto_replace_all_running:
            return

        from .replacer import async_apply_replace

        self._auto_replace_all_running = True
        try:
            attempted: list[str] = []
            processed: list[str] = []
            failed: list[str] = []
            for old_entity_id in list(self.get_lost_entity_ids()):
                hits = self.get_hits_for_old_entity(old_entity_id)
                if not has_auto_replaceable_hits(hits):
                    continue

                pending = self.store.get_pending_changes().get(old_entity_id)
                if pending is None:
                    continue

                attempted.append(old_entity_id)
                try:
                    result = await async_apply_replace(
                        self.hass,
                        hits,
                        old_entity_id,
                        pending.new_entity_id,
                    )
                    self.drop_hits_for_keys(old_entity_id, result.updated_keys)
                    if result.failed:
                        failed.append(
                            f"{old_entity_id}: {'; '.join(result.failed)}"
                        )
                    if result.updated:
                        processed.append(old_entity_id)
                    elif result.failed:
                        pass
                    else:
                        failed.append(
                            f"{old_entity_id}: no auto-replaceable changes applied"
                        )
                except Exception as err:  # noqa: BLE001 - continue bulk run after per-entity failure
                    _LOGGER.exception(
                        "Auto-Replace All failed for %s", old_entity_id
                    )
                    failed.append(f"{old_entity_id}: {err}")
            await self.async_trigger_rescan()
            completed: list[str] = []
            still_referenced: list[str] = []
            for old_entity_id in processed:
                pending = self.store.get_pending_changes().get(old_entity_id)
                if pending is None or pending.keep_without_references:
                    continue
                if self.get_hits_for_old_entity(old_entity_id):
                    still_referenced.append(old_entity_id)
                    continue
                await self.async_mark_completed(old_entity_id)
                completed.append(old_entity_id)

            summary = (
                "Auto-Replace All summary: attempted=%s updated=%s completed=%s "
                "failed=%s still_referenced=%s"
                % (
                    len(attempted),
                    len(processed),
                    len(completed),
                    len(failed),
                    len(still_referenced),
                )
            )
            if failed or still_referenced:
                details: list[str] = []
                if failed:
                    details.append("failed: " + "; ".join(failed))
                if still_referenced:
                    details.append(
                        "still referenced: " + ", ".join(still_referenced)
                    )
                _LOGGER.warning("%s (%s)", summary, "; ".join(details))
            else:
                _LOGGER.info(summary)
        finally:
            self._auto_replace_all_running = False

    async def async_trigger_rescan(self) -> None:
        """Run one full scan. Do not start a second scan beside a debounced one."""
        if self._unloaded:
            return
        self._scan_full = True
        self._mark_scan_busy()
        cancel = getattr(self._debouncer, "async_cancel", None)
        if callable(cancel):
            cancel()
        await self._async_run_scan()
