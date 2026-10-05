"""Read and write the UI yaml files Home Assistant documents.

automations.yaml, scripts.yaml, and scenes.yaml. Load goes through
hass.config.path plus Home Assistant's yaml helpers, then the public reload
actions. Auto-Replace copies the file, edits entity id tokens in the original
text, and restores the copy if the file changed or the edit is not clean.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import UI_CONFIG_PATHS
from .models import ReferenceHit
from .util import extract_entities_from_value, replace_entity_id_tokens

_LOGGER = logging.getLogger(__name__)

BACKUP_SUFFIX = ".pre-lost-entity-finder"

_EDIT_URL = {
    "automation": "/config/automation/edit/{id}",
    "script": "/config/script/edit/{id}",
    "scene": "/config/scene/edit/{id}",
}
_EMPTY: dict[str, list | dict] = {
    "automation": [],
    "script": {},
    "scene": [],
}
_LIST_ITEM_START = re.compile(r"^- ", re.MULTILINE)
_MAP_KEY_START = re.compile(
    r"^([^:\s#][^:\n]*):\s*(?:#.*)?$", re.MULTILINE
)
_ITEM_ID_LINE = re.compile(
    r"^(?:-\s*)?id:\s*(.+?)\s*$", re.MULTILINE
)


def is_blueprint_config(item: dict[str, Any]) -> bool:
    """Return True when the YAML item is backed by a blueprint."""
    return isinstance(item.get("use_blueprint"), dict)


def is_auto_replaceable_ui_item(
    item_id: str | None,
    item: dict[str, Any],
    ui_ids: set[str] | None,
) -> bool:
    """Return True when this item lives in the UI yaml file and can be written."""
    if not item_id or ui_ids is None:
        return False
    if is_blueprint_config(item):
        return False
    return str(item_id) in ui_ids


def iter_config_items(data: Any) -> list[tuple[str | None, dict[str, Any]]]:
    """Yield (id, item) pairs from a list or mapping yaml file."""
    items: list[tuple[str | None, dict[str, Any]]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            items.append((None if raw_id is None else str(raw_id), item))
        return items
    if isinstance(data, dict):
        for key, item in data.items():
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            items.append(
                (str(raw_id) if raw_id is not None else str(key), item)
            )
        return items
    return items


def ids_from_config(data: Any) -> set[str]:
    """Return ids present in a loaded UI yaml document."""
    return {item_id for item_id, _item in iter_config_items(data) if item_id}


def _strip_yaml_scalar(value: str) -> str:
    """Strip quotes and an inline comment from a yaml scalar."""
    text = value.strip()
    if " #" in text:
        text = text.split(" #", 1)[0].rstrip()
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _chunk_item_id(chunk: str, map_key: str | None = None) -> str | None:
    """Return the item id from a yaml chunk."""
    match = _ITEM_ID_LINE.search(chunk)
    if match:
        return _strip_yaml_scalar(match.group(1))
    return map_key


def _list_item_spans(text: str) -> list[tuple[int, int, str | None]]:
    """Return (start, end, id) for each top-level list item."""
    starts = [match.start() for match in _LIST_ITEM_START.finditer(text)]
    spans: list[tuple[int, int, str | None]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        spans.append((start, end, _chunk_item_id(text[start:end])))
    return spans


def _map_item_spans(text: str) -> list[tuple[int, int, str | None]]:
    """Return (start, end, id) for each top-level mapping item."""
    starts: list[tuple[int, str]] = []
    for match in _MAP_KEY_START.finditer(text):
        key = _strip_yaml_scalar(match.group(1))
        if key in {"---", "..."}:
            continue
        starts.append((match.start(), key))
    spans: list[tuple[int, int, str | None]] = []
    for index, (start, key) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        spans.append((start, end, _chunk_item_id(text[start:end], key)))
    return spans


def _is_yaml_mapping_key(text: str, entity_id: str) -> bool:
    """Return True when entity_id is a YAML key, such as a scene member."""
    pattern = re.compile(
        rf"^[ \t]+['\"]?{re.escape(entity_id)}['\"]?:(?:[ \t]|$)",
        re.MULTILINE,
    )
    return pattern.search(text) is not None


def replace_entity_in_ui_yaml(
    text: str,
    domain: str,
    resource_id: str,
    old_entity_id: str,
    new_entity_id: str,
) -> tuple[str, int] | None:
    """Replace entity tokens in one UI yaml item. None means abort."""
    spans = _map_item_spans(text) if domain == "script" else _list_item_spans(text)
    wanted = str(resource_id)
    matches = [
        (start, end) for start, end, item_id in spans if item_id == wanted
    ]
    if len(matches) != 1:
        return None
    start, end = matches[0]
    chunk = text[start:end]
    if re.search(r"^\s*use_blueprint:\s*$", chunk, re.MULTILINE):
        return None
    if _is_yaml_mapping_key(chunk, old_entity_id) and _is_yaml_mapping_key(
        chunk, new_entity_id
    ):
        return None
    new_chunk, count = replace_entity_id_tokens(chunk, old_entity_id, new_entity_id)
    if count == 0 or new_chunk == chunk:
        return None
    expected, expected_count = replace_entity_id_tokens(
        chunk, old_entity_id, new_entity_id
    )
    if new_chunk != expected or expected_count != count:
        return None
    return text[:start] + new_chunk + text[end:], count


def _mtime_ns(path: str) -> int | None:
    """Return mtime in ns, or None when the file is missing."""
    try:
        return os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return None


def _backup_path(path: str) -> str:
    """Sidecar copy used to restore a failed Auto-Replace."""
    return path + BACKUP_SUFFIX


def _read_text(path: str) -> str:
    """Read a utf-8 yaml file as-is, including original newlines."""
    return Path(path).read_bytes().decode("utf-8")


def _write_text_atomic(path: str, contents: str) -> None:
    """Replace a file with utf-8 text."""
    target = Path(path)
    tmp = target.with_name(target.name + ".lost-entity-finder.tmp")
    try:
        tmp.write_bytes(contents.encode("utf-8"))
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


async def async_load_ui_config(hass: HomeAssistant, domain: str) -> Any | None:
    """Load automations.yaml, scripts.yaml, or scenes.yaml.

    None means the file could not be read. A missing file is an empty
    list or mapping, not None.
    """
    rel_path = UI_CONFIG_PATHS.get(domain)
    if not rel_path:
        return None
    path = hass.config.path(rel_path)
    empty = _EMPTY.get(domain, [])

    def _load() -> Any:
        try:
            from homeassistant.config import load_yaml_config_file
        except ImportError:
            try:
                from homeassistant.util.yaml import load_yaml as load_yaml_config_file
            except ImportError:
                raise
        try:
            loaded = load_yaml_config_file(path)
        except FileNotFoundError:
            return empty
        if loaded is None:
            return empty
        return loaded

    try:
        return await hass.async_add_executor_job(_load)
    except Exception:  # noqa: BLE001 - yaml/secrets errors must fail closed
        _LOGGER.debug("Unable to read %s", rel_path, exc_info=True)
        return None


async def async_ui_managed_ids(hass: HomeAssistant, domain: str) -> set[str] | None:
    """Return ids in the UI yaml file, or None when that file could not be read."""
    data = await async_load_ui_config(hass, domain)
    if data is None:
        return None
    return ids_from_config(data)


async def ui_file_has_id(hass: HomeAssistant, domain: str, resource_id: str) -> bool:
    """Return False unless the UI yaml file was read and contains this id."""
    ids = await async_ui_managed_ids(hass, domain)
    if ids is None:
        return False
    return str(resource_id) in ids


async def async_replace_ui_item(
    hass: HomeAssistant,
    domain: str,
    resource_id: str,
    old_entity_id: str,
    new_entity_id: str,
) -> bool:
    """Replace an entity id inside one UI yaml item and reload that domain."""
    rel_path = UI_CONFIG_PATHS.get(domain)
    if not rel_path:
        return False
    path = hass.config.path(rel_path)

    def _prepare() -> tuple[str, str, int] | None:
        mtime = _mtime_ns(path)
        if mtime is None:
            return None
        original = _read_text(path)
        if _mtime_ns(path) != mtime:
            _LOGGER.warning(
                "Auto-Replace skipped %s: the file changed while it was being read",
                rel_path,
            )
            return None
        replaced = replace_entity_in_ui_yaml(
            original, domain, str(resource_id), old_entity_id, new_entity_id
        )
        if replaced is None:
            return None
        new_text, count = replaced
        if count == 0 or new_text == original:
            return None
        if _mtime_ns(path) != mtime:
            _LOGGER.warning(
                "Auto-Replace skipped %s: the file changed since it was read",
                rel_path,
            )
            return None
        backup_path = _backup_path(path)
        shutil.copy2(path, backup_path)
        if _mtime_ns(path) != mtime:
            _LOGGER.warning(
                "Auto-Replace skipped %s: the file changed since it was read",
                rel_path,
            )
            try:
                Path(backup_path).unlink(missing_ok=True)
            except OSError:
                _LOGGER.debug(
                    "Auto-Replace could not remove orphan backup %s",
                    backup_path,
                    exc_info=True,
                )
            return None
        _write_text_atomic(path, new_text)
        written = _read_text(path)
        backup = _read_text(_backup_path(path))
        expected_result = replace_entity_in_ui_yaml(
            backup, domain, str(resource_id), old_entity_id, new_entity_id
        )
        if (
            expected_result is None
            or written != new_text
            or written != expected_result[0]
            or expected_result[1] != count
        ):
            shutil.copy2(_backup_path(path), path)
            _LOGGER.warning(
                "Auto-Replace restored %s: extra changes besides %s -> %s",
                rel_path,
                old_entity_id,
                new_entity_id,
            )
            return None
        return written, rel_path, count

    try:
        result = await hass.async_add_executor_job(_prepare)
    except OSError:
        _LOGGER.warning("Auto-Replace could not update %s", rel_path, exc_info=True)
        return False
    except Exception:  # noqa: BLE001 - never leave a half-applied file without restore
        _LOGGER.warning("Auto-Replace failed for %s", rel_path, exc_info=True)
        try:
            await hass.async_add_executor_job(shutil.copy2, _backup_path(path), path)
        except OSError:
            _LOGGER.warning("Auto-Replace could not restore %s", rel_path, exc_info=True)
        return False
    if result is None:
        return False

    service_data: dict[str, str] = {}
    if domain == "automation":
        service_data["id"] = resource_id
    try:
        from homeassistant.const import SERVICE_RELOAD
    except ImportError:
        SERVICE_RELOAD = "reload"
    reload_ok = True
    try:
        await hass.services.async_call(
            domain, SERVICE_RELOAD, service_data, blocking=True
        )
    except Exception:  # noqa: BLE001 - file already verified; surface reload separately
        reload_ok = False
        _LOGGER.warning(
            "Auto-Replace updated %s but reload failed; the file change is kept",
            rel_path,
            exc_info=True,
        )
    if reload_ok:
        backup = _backup_path(path)

        def _delete_backup() -> None:
            Path(backup).unlink(missing_ok=True)

        try:
            await hass.async_add_executor_job(_delete_backup)
        except OSError:
            _LOGGER.debug("Auto-Replace could not remove backup %s", backup, exc_info=True)
    return True


def _item_label(item_id: str | None, item: dict[str, Any]) -> str:
    """Pick a human label from YAML alias/name, then the id."""
    for key in ("alias", "name"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return item_id or "unnamed"


async def _async_merged_domain_section(
    hass: HomeAssistant, domain: str
) -> Any:
    """Return automations/scripts/scenes from configuration.yaml and packages."""
    from .scanners.yaml_config import async_load_core_config, collect_domain_section

    loaded = await async_load_core_config(hass)
    if loaded is None:
        return None
    return collect_domain_section(loaded, domain)


async def async_scan_ui_domain(
    hass: HomeAssistant, domain: str, tracked: set[str]
) -> dict[str, list[ReferenceHit]]:
    """Scan the UI yaml file and package copies of automations, scripts, or scenes."""
    hits: dict[str, list[ReferenceHit]] = {}
    edit_url = _EDIT_URL.get(domain)
    if not edit_url:
        return hits

    ui_data = await async_load_ui_config(hass, domain)
    ui_ids = ids_from_config(ui_data) if ui_data is not None else None
    seen_ids: set[str] = set()

    async def _record(item_id: str | None, item: dict[str, Any], auto: bool) -> None:
        found = await extract_entities_from_value(hass, item, tracked)
        if not found:
            return
        resource_id = item_id or f"unnamed_{id(item)}"
        hit = ReferenceHit(
            resource_type=domain,
            label=_item_label(item_id, item),
            edit_url=edit_url.format(id=resource_id) if item_id else "/config/configuration",
            resource_id=resource_id,
            auto_replaceable=auto,
        )
        for entity_id in found:
            hits.setdefault(entity_id, []).append(hit)

    if ui_data is not None:
        for item_id, item in iter_config_items(ui_data):
            if item_id:
                seen_ids.add(item_id)
            await _record(
                item_id,
                item,
                is_auto_replaceable_ui_item(item_id, item, ui_ids),
            )

    merged = await _async_merged_domain_section(hass, domain)
    if merged is None:
        return hits
    for item_id, item in iter_config_items(merged):
        if item_id and item_id in seen_ids:
            continue
        await _record(item_id, item, False)
    return hits
