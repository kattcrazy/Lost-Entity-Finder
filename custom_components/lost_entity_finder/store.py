"""Persistent storage for Lost Entity Find And Replace."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_MAX_PENDING_CHANGES,
    MAX_IGNORED,
    STORAGE_KEY_IGNORED,
    STORAGE_KEY_PENDING,
    STORAGE_VERSION,
)
from .models import PendingEntityIdChange

_LOGGER = logging.getLogger(__name__)


class EntityFinderStore:
    """Manage pending entity ID changes and ignored entity IDs."""

    def __init__(
        self, hass: HomeAssistant, max_pending_changes: int = DEFAULT_MAX_PENDING_CHANGES
    ) -> None:
        """Initialize stores."""
        self.hass = hass
        self._max_pending_changes = max_pending_changes
        self._pending_store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_PENDING,
        )
        self._ignored_store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_IGNORED,
        )
        self._pending: dict[str, dict[str, Any]] = {}
        self._ignored: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _mutation_lock(self) -> asyncio.Lock:
        """Return the store lock, creating it if tests constructed via __new__."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock
        return lock

    async def async_load(self) -> None:
        """Load both stores."""
        async with self._mutation_lock():
            pending_data = await self._pending_store.async_load() or {}
            self._pending = pending_data.get("changes", {})
            self._collapse_pending_chains()
            ignored_data = await self._ignored_store.async_load() or {}
            self._ignored = _ignored_from_storage(ignored_data)

    async def async_save_pending(self) -> None:
        """Persist pending entity ID changes."""
        await self._pending_store.async_save({"changes": self._pending})

    async def async_save_ignored(self) -> None:
        """Persist ignored entity IDs, oldest ignore time first."""
        ordered = sorted(self._ignored.items(), key=lambda item: (item[1], item[0]))
        await self._ignored_store.async_save(
            {
                "entity_ids": [entity_id for entity_id, _stamp in ordered],
                "ignored_at": {entity_id: stamp for entity_id, stamp in ordered},
            }
        )

    @callback
    def get_pending_changes(self) -> dict[str, PendingEntityIdChange]:
        """Return pending entity ID changes keyed by old entity ID."""
        result: dict[str, PendingEntityIdChange] = {}
        for old_id, data in self._pending.items():
            result[old_id] = PendingEntityIdChange(
                old_entity_id=old_id,
                new_entity_id=data["new_entity_id"],
                changed_at=data.get("changed_at", ""),
                unique_id=data.get("unique_id"),
                keep_without_references=bool(data.get("keep_without_references")),
            )
        return result

    @callback
    def get_tracked_old_ids(self) -> set[str]:
        """Return old entity IDs being tracked."""
        return set(self._pending.keys())

    @callback
    def is_ignored(self, old_entity_id: str) -> bool:
        """Return True if the old entity ID is ignored."""
        return old_entity_id.lower().strip() in self._ignored

    @callback
    def get_new_entity_id(self, old_entity_id: str) -> str | None:
        """Return the new entity ID for a tracked old ID."""
        entry = self._pending.get(old_entity_id.lower().strip())
        if not entry:
            return None
        return entry.get("new_entity_id")

    def _record_entity_id_change_locked(
        self,
        old_entity_id: str,
        new_entity_id: str,
        unique_id: str | None = None,
        *,
        keep_without_references: bool = False,
    ) -> None:
        """Mutate pending renames under the lock. Caller saves."""
        now = datetime.now(timezone.utc).isoformat()

        # Chain collapse: if new_entity_id was previously tracked as an old ID's target,
        # update any entry pointing to new_entity_id as its new target.
        for old_id, data in list(self._pending.items()):
            if data.get("new_entity_id") == old_entity_id and old_id != old_entity_id:
                data["new_entity_id"] = new_entity_id
                data["changed_at"] = now

        if old_entity_id in self._pending:
            self._pending[old_entity_id]["new_entity_id"] = new_entity_id
            self._pending[old_entity_id]["changed_at"] = now
            if unique_id:
                self._pending[old_entity_id]["unique_id"] = unique_id
            self._pending[old_entity_id]["keep_without_references"] = (
                keep_without_references
            )
        else:
            self._pending[old_entity_id] = {
                "new_entity_id": new_entity_id,
                "changed_at": now,
                "unique_id": unique_id,
                "keep_without_references": keep_without_references,
            }

        self._collapse_pending_chains()

    async def async_record_entity_id_change(
        self,
        old_entity_id: str,
        new_entity_id: str,
        unique_id: str | None = None,
        *,
        keep_without_references: bool = False,
    ) -> None:
        """Record an entity ID change, collapsing chains when needed."""
        old_entity_id = old_entity_id.lower().strip()
        new_entity_id = new_entity_id.lower().strip()

        async with self._mutation_lock():
            self._record_entity_id_change_locked(
                old_entity_id,
                new_entity_id,
                unique_id,
                keep_without_references=keep_without_references,
            )
            dropped = self._enforce_pending_cap()
            await self.async_save_pending()
        self._warn_dropped_pending(dropped)

    async def async_record_entity_id_changes(
        self,
        changes: list[tuple[str, str, str | None]],
        *,
        keep_without_references: bool = False,
    ) -> int:
        """Record many entity ID changes with one save. Returns imported count."""
        if not changes:
            return 0

        async with self._mutation_lock():
            for old_entity_id, new_entity_id, unique_id in changes:
                self._record_entity_id_change_locked(
                    old_entity_id.lower().strip(),
                    new_entity_id.lower().strip(),
                    unique_id,
                    keep_without_references=keep_without_references,
                )
            dropped = self._enforce_pending_cap()
            await self.async_save_pending()
        self._warn_dropped_pending(dropped)
        return len(changes)

    async def async_remove_pending(self, old_entity_id: str) -> None:
        """Remove a pending entity ID change."""
        old_entity_id = old_entity_id.lower().strip()
        async with self._mutation_lock():
            if old_entity_id in self._pending:
                del self._pending[old_entity_id]
                await self.async_save_pending()

    async def async_ignore(self, old_entity_id: str) -> None:
        """Ignore repairs for an old entity ID."""
        await self.async_ignore_many([old_entity_id])

    async def async_ignore_many(self, old_entity_ids: list[str]) -> None:
        """Ignore repairs for many old entity IDs with one save."""
        normalized = [
            entity_id.lower().strip()
            for entity_id in old_entity_ids
            if isinstance(entity_id, str) and entity_id.lower().strip()
        ]
        if not normalized:
            return
        async with self._mutation_lock():
            for old_entity_id in normalized:
                self._ignored[old_entity_id] = datetime.now(timezone.utc).isoformat()
                self._trim_ignored(old_entity_id)
            await self.async_save_ignored()

    async def async_unignore(self, old_entity_id: str) -> None:
        """Stop ignoring an old entity ID."""
        old_entity_id = old_entity_id.lower().strip()
        async with self._mutation_lock():
            self._ignored.pop(old_entity_id, None)
            await self.async_save_ignored()

    @callback
    def get_ignored_entity_ids(self) -> set[str]:
        """Return ignored old entity IDs."""
        return set(self._ignored)

    async def async_clear_ignored(self) -> None:
        """Clear all ignored old entity IDs."""
        async with self._mutation_lock():
            self._ignored.clear()
            await self.async_save_ignored()

    @callback
    def set_max_pending_changes(self, max_pending_changes: int) -> None:
        """Update the pending entity ID change cap."""
        self._max_pending_changes = max_pending_changes

    async def async_enforce_pending_cap(self) -> None:
        """Trim pending changes to the cap and persist."""
        async with self._mutation_lock():
            dropped = self._enforce_pending_cap()
            await self.async_save_pending()
        self._warn_dropped_pending(dropped)

    def _enforce_pending_cap(self) -> list[str]:
        """Keep pending entity ID changes under the safety cap."""
        if len(self._pending) <= self._max_pending_changes:
            return []
        sorted_items = sorted(
            self._pending.items(),
            key=lambda item: item[1].get("changed_at", ""),
        )
        dropped = [
            old_id
            for old_id, _data in sorted_items[: -self._max_pending_changes]
        ]
        self._pending = dict(sorted_items[-self._max_pending_changes :])
        return dropped

    def _warn_dropped_pending(self, dropped: list[str]) -> None:
        """Log when the pending cap drops tracked renames."""
        if not dropped:
            return
        _LOGGER.warning(
            "Dropped %s oldest pending entity ID change(s) over the cap of %s: %s",
            len(dropped),
            self._max_pending_changes,
            ", ".join(dropped),
        )

    def _collapse_pending_chains(self) -> None:
        """Point every pending row at the final entity id in a rename chain.

        Pure cycles (A->B / B->A) have no stable final id, so drop them.
        """

        def resolve(entity_id: str, seen: set[str]) -> str | None:
            """Return the final id, or None when the chain cycles."""
            if entity_id in seen:
                return None
            if entity_id not in self._pending:
                return entity_id
            nxt = self._pending[entity_id].get("new_entity_id")
            if not isinstance(nxt, str) or not nxt:
                return entity_id
            if nxt == entity_id:
                return None
            seen.add(entity_id)
            return resolve(nxt, seen)

        updates: dict[str, str] = {}
        drop: set[str] = set()
        for old_id, data in self._pending.items():
            target = data.get("new_entity_id")
            if not isinstance(target, str):
                continue
            resolved = resolve(target, {old_id})
            if resolved is None or resolved == old_id:
                drop.add(old_id)
            elif resolved != target:
                updates[old_id] = resolved

        for old_id, resolved in updates.items():
            if old_id in drop:
                continue
            self._pending[old_id]["new_entity_id"] = resolved
        for old_id in drop:
            self._pending.pop(old_id, None)

    def _trim_ignored(self, protected: str) -> None:
        """Drop the oldest ignores. Never drop the id that was just ignored."""
        overflow = len(self._ignored) - MAX_IGNORED
        if overflow <= 0:
            return
        ranked = sorted(self._ignored.items(), key=lambda item: (item[1], item[0]))
        for entity_id, _stamp in ranked:
            if overflow <= 0:
                break
            if entity_id == protected:
                continue
            del self._ignored[entity_id]
            overflow -= 1

    @staticmethod
    def parse_entity_id_change(
        event_data: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Extract an entity ID change from a registry update event."""
        if event_data.get("action") != "update":
            return None

        new_entity_id = event_data.get("entity_id")
        if not new_entity_id:
            return None

        old_entity_id = event_data.get("old_entity_id")
        if not old_entity_id:
            changes = event_data.get("changes") or {}
            old_entity_id = changes.get("entity_id")

        if not isinstance(old_entity_id, str) or not isinstance(new_entity_id, str):
            return None
        old_entity_id = old_entity_id.lower().strip()
        new_entity_id = new_entity_id.lower().strip()
        if not old_entity_id or old_entity_id == new_entity_id:
            return None

        return old_entity_id, new_entity_id

    async def async_apply_registry_event(self, event_data: dict[str, Any]) -> None:
        """Apply an entity ID change from a registry event."""
        parsed = self.parse_entity_id_change(event_data)
        if parsed is None:
            return

        old_entity_id, new_entity_id = parsed
        registry = er.async_get(self.hass)
        entry = registry.async_get(new_entity_id)
        unique_id = entry.unique_id if entry else None
        await self.async_record_entity_id_change(
            old_entity_id, new_entity_id, unique_id
        )


def _ignored_from_storage(ignored_data: dict[str, Any]) -> dict[str, str]:
    """Load ignored ids keyed by ignore time.

    Older storage only has a list of ids. Those get a synthetic time so a new
    ignore is newer than all of them and is not the one trimmed away.
    """
    loaded: dict[str, str] = {}
    raw_at = ignored_data.get("ignored_at")
    if isinstance(raw_at, dict):
        for entity_id, stamp in raw_at.items():
            if isinstance(entity_id, str) and isinstance(stamp, str) and stamp:
                loaded[entity_id] = stamp
    raw_ids = ignored_data.get("entity_ids") or []
    if isinstance(raw_ids, list):
        for index, entity_id in enumerate(raw_ids):
            if isinstance(entity_id, str) and entity_id not in loaded:
                loaded[entity_id] = f"1970-01-01T00:00:00.{index:06d}+00:00"
    return loaded
