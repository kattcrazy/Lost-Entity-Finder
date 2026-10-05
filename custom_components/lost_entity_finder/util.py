"""Utility helpers for Lost Entity Find And Replace."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers import template

if TYPE_CHECKING:
    from .models import ReferenceHit
else:
    from .models import ReferenceHit  # used at runtime in merge_reference_hits

from .ui_text import ui

_LOGGER = logging.getLogger(__name__)

# One pattern for every scan. Lookahead keeps sensor.door from matching sensor.doorbell.
# A slash lookbehind skips tokens inside paths or URLs.
_ENTITY_ID_PATTERN = re.compile(
    r"(?<![a-z0-9_/\\])([a-z0-9_]+\.[a-z0-9_]+)(?![a-z0-9_])"
)

# Cache a successful signature check on the underlying function. A failed
# inspect is not cached, so a later render can still go quiet.
_LOG_FN_BY_FUNC: dict[int, bool] = {}


def _noop_template_log(*_args: Any, **_kwargs: Any) -> None:
    """Drop template render logs. Undefined variables are expected during a scan."""


def _accepts_template_log_fn(render: Callable[..., Any]) -> bool:
    """Return True when async_render_to_info still accepts log_fn."""
    func = getattr(render, "__func__", render)
    key = id(func)
    cached = _LOG_FN_BY_FUNC.get(key)
    if cached is not None:
        return cached
    try:
        accepts = "log_fn" in inspect.signature(render).parameters
    except (TypeError, ValueError):
        return False
    _LOG_FN_BY_FUNC[key] = accepts
    return accepts


async def _async_render_template_info(tmpl: template.Template) -> Any:
    """Render template info. Pass a no-op log_fn only when that parameter exists."""
    render = tmpl.async_render_to_info
    if _accepts_template_log_fn(render):
        result = render({}, log_fn=_noop_template_log)
    else:
        result = render({})
    if inspect.isawaitable(result):
        return await result
    return result


_MISSING = object()


def _tracked_ids_in_text(text: str, tracked: set[str]) -> set[str]:
    """Return tracked entity IDs that appear as whole tokens in text."""
    if not tracked or not text:
        return set()
    return {
        match
        for match in _ENTITY_ID_PATTERN.findall(text)
        if match in tracked and looks_like_entity_id(match)
    }


def looks_like_entity_id(value: str) -> bool:
    """Return True when a string looks like a Home Assistant entity ID."""
    return valid_entity_id(value)

TEMPLATE_MARKERS = ("{{", "{%")


def _slug_entity_id(entity_id: str) -> str:
    """Slug an entity id without collapsing the domain boundary.

    sensor.foo_bar and sensor_foo.bar must not share one repair id.
    """
    domain, separator, object_id = entity_id.partition(".")
    if not separator:
        return entity_id.replace("-", "_")
    return (
        domain.replace("-", "_")
        + "__"
        + object_id.replace(".", "_").replace("-", "_")
    )


def slugify_issue_id(old_entity_id: str) -> str:
    """Build a stable issue ID from an old entity ID."""
    return "lost_" + _slug_entity_id(old_entity_id)


def slugify_find_issue_id(entity_id: str) -> str:
    """Build a stable issue ID for a find-references repair."""
    return "find_" + _slug_entity_id(entity_id)


def is_template_string(value: str) -> bool:
    """Return True if value looks like a Jinja template."""
    return any(marker in value for marker in TEMPLATE_MARKERS)


async def extract_entities_from_value(
    hass: HomeAssistant, value: Any, tracked: set[str]
) -> set[str]:
    """Extract tracked entity IDs from a config value."""
    found: set[str] = set()

    if isinstance(value, str):
        if is_template_string(value):
            try:
                tmpl = template.Template(value, hass)
                info = await _async_render_template_info(tmpl)
                for entity_id in info.entities:
                    if entity_id in tracked:
                        found.add(entity_id)
            except Exception:  # noqa: BLE001 - template parse errors are skipped
                _LOGGER.debug("Template entity extraction failed", exc_info=True)
            found.update(_tracked_ids_in_text(value, tracked))
        elif value in tracked:
            found.add(value)
        else:
            found.update(_tracked_ids_in_text(value, tracked))
    elif isinstance(value, list):
        for item in value:
            found.update(await extract_entities_from_value(hass, item, tracked))
    elif isinstance(value, dict):
        if (
            "entity" in value
            and isinstance(value["entity"], str)
            and value["entity"] in tracked
        ):
            found.add(value["entity"])
        if (
            "entity_id" in value
            and isinstance(value["entity_id"], str)
            and value["entity_id"] in tracked
        ):
            found.add(value["entity_id"])
        for action_key in ("action", "service"):
            action_value = value.get(action_key)
            if isinstance(action_value, str) and action_value in tracked:
                found.add(action_value)
        for key, item in value.items():
            if isinstance(key, str) and key in tracked and looks_like_entity_id(key):
                found.add(key)
            found.update(await extract_entities_from_value(hass, item, tracked))

    return found


async def deep_replace_entity_ids(
    hass: HomeAssistant,
    value: Any,
    old_entity_id: str,
    new_entity_id: str,
) -> tuple[Any, int, bool]:
    """Deep-replace old entity ID with new in structured config.

    Returns (value, count, clean). clean is False when a dict already uses the
    new entity id as a key with a different value. That key is left as it was
    so the caller can keep the hit manual instead of overwriting it.
    """
    if isinstance(value, str):
        if value == old_entity_id:
            return new_entity_id, 1, True
        if is_template_string(value) or old_entity_id in value:
            new_value, count = _replace_in_text(value, old_entity_id, new_entity_id)
            return new_value, count, True
        return value, 0, True

    if isinstance(value, list):
        new_list: list[Any] = []
        replacements = 0
        clean = True
        for item in value:
            new_item, count, item_clean = await deep_replace_entity_ids(
                hass, item, old_entity_id, new_entity_id
            )
            replacements += count
            clean = clean and item_clean
            if (
                isinstance(new_item, str)
                and new_item == new_entity_id
                and new_entity_id in new_list
            ):
                continue
            new_list.append(new_item)
        return new_list, replacements, clean

    if isinstance(value, dict):
        replacements = 0
        clean = True
        replaced: dict[Any, Any] = {}
        for key, item in value.items():
            if key == "entity" and item == old_entity_id:
                replaced[key] = new_entity_id
                replacements += 1
                continue
            if key == "entity_id" and item == old_entity_id:
                replaced[key] = new_entity_id
                replacements += 1
                continue
            new_item, count, item_clean = await deep_replace_entity_ids(
                hass, item, old_entity_id, new_entity_id
            )
            replaced[key] = new_item
            replacements += count
            clean = clean and item_clean

        new_dict: dict[Any, Any] = {}
        for key, item in replaced.items():
            if key != old_entity_id:
                new_dict[key] = item
                continue
            existing = replaced.get(new_entity_id, _MISSING)
            if existing is not _MISSING and existing != item:
                # Both ids are already keys and their values differ. Keep each value.
                new_dict[key] = item
                clean = False
                continue
            if existing is not _MISSING:
                replacements += 1
                continue
            new_dict[new_entity_id] = item
            replacements += 1
        return new_dict, replacements, clean

    return value, 0, True


def replace_entity_id_tokens(
    text: str, old_entity_id: str, new_entity_id: str
) -> tuple[str, int]:
    """Replace whole entity ID tokens in a string."""
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(1) != old_entity_id:
            return match.group(0)
        count += 1
        return new_entity_id

    return _ENTITY_ID_PATTERN.sub(_replace, text), count


_replace_in_text = replace_entity_id_tokens


def merge_reference_hits(
    results: list[dict[str, list["ReferenceHit"]]],
) -> dict[str, list["ReferenceHit"]]:
    """Merge scanner outputs keyed by old entity ID."""
    merged: dict[str, list[ReferenceHit]] = {}
    for result in results:
        for entity_id, hits in result.items():
            bucket = merged.setdefault(entity_id, [])
            seen = {(hit.resource_type, hit.resource_id) for hit in bucket}
            for hit in hits:
                key = (hit.resource_type, hit.resource_id)
                if key not in seen:
                    bucket.append(hit)
                    seen.add(key)
    return merged


def dedupe_reference_hits(
    hits: list[ReferenceHit],
) -> dict[tuple[str, str], ReferenceHit]:
    """Deduplicate reference hits by resource type and ID."""
    unique: dict[tuple[str, str], ReferenceHit] = {}
    for hit in hits:
        unique[(hit.resource_type, hit.resource_id)] = hit
    return unique


def has_auto_replaceable_hits(hits: list[ReferenceHit]) -> bool:
    """Return True when at least one reference can be auto-replaced."""
    return any(hit.auto_replaceable for hit in dedupe_reference_hits(hits).values())


def manual_reference_reason(hit: ReferenceHit) -> str:
    """Return a short reason why a reference must be updated manually."""
    if hit.resource_type == "yaml_config":
        return ui("manual_reason_yaml")
    if hit.resource_type in {"automation", "script", "scene"}:
        return ui("manual_reason_yaml")
    return ui("manual_reason_edit")


def format_references_for_repair(
    hits: list[ReferenceHit],
) -> tuple[str, str]:
    """Format references for repairs, marking items that need manual updates."""
    lines: list[str] = []
    manual_count = 0

    for hit in dedupe_reference_hits(hits).values():
        line = f"- [{hit.label}]({hit.edit_url})"
        if not hit.auto_replaceable:
            manual_count += 1
            line += ui("manual_update_suffix")
        lines.append(line)

    manual_note = ""
    if manual_count:
        manual_note = ui("manual_count_note", count=manual_count)

    return "\n".join(lines), manual_note
