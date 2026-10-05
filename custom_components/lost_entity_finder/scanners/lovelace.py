"""Scan Lovelace dashboards for tracked old entity IDs."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lovelace import DOMAIN as LOVELACE_DOMAIN
from homeassistant.core import HomeAssistant

from ..models import ReferenceHit
from ..util import extract_entities_from_value

_LOGGER = logging.getLogger(__name__)

_DEFAULT_YAML_DASHBOARD = "ui-lovelace.yaml"
_store_missing_warned = False
_store_unusable_warned = False


def _warn_dashboard_store_unavailable(reason: str) -> None:
    """Log once when storage dashboards cannot be used."""
    global _store_missing_warned
    if _store_missing_warned:
        return
    _LOGGER.warning(
        "Dashboard store %s; YAML dashboard files will still be scanned as "
        "manual edits. Storage dashboards cannot be searched or Auto-Replaced "
        "until Home Assistant exposes a usable store again",
        reason,
    )
    _store_missing_warned = True


def _warn_dashboard_store_unusable() -> None:
    """Log once when the store is present but storage dashboards cannot load."""
    global _store_unusable_warned
    if _store_unusable_warned:
        return
    _LOGGER.warning(
        "Dashboard store shape looks unusable (no loadable storage dashboards); "
        "YAML dashboard files will still be scanned as manual edits. Storage "
        "dashboards cannot be searched or Auto-Replaced until this integration "
        "is updated for the current Home Assistant lovelace layout"
    )
    _store_unusable_warned = True


def _dashboard_mode(dashboard: Any) -> str | None:
    """Return storage/yaml when the dashboard exposes a mode string."""
    mode = getattr(dashboard, "mode", None)
    return mode if isinstance(mode, str) else None


def is_yaml_dashboard(dashboard: Any) -> bool:
    """Return True when this dashboard is file-backed YAML."""
    mode = _dashboard_mode(dashboard)
    if mode == "yaml":
        return True
    save = getattr(dashboard, "async_save", None)
    return not callable(save)


def _dashboard_store(hass: HomeAssistant) -> Any | None:
    """Return the Lovelace dashboards mapping when it is present."""
    lovelace_data = hass.data.get(LOVELACE_DOMAIN)
    if lovelace_data is None:
        _warn_dashboard_store_unavailable("is missing")
        return None
    dashboards = getattr(lovelace_data, "dashboards", None)
    if dashboards is None and hasattr(lovelace_data, "get"):
        dashboards = lovelace_data.get("dashboards")
    if not dashboards or not hasattr(dashboards, "values"):
        _warn_dashboard_store_unavailable("is missing or has no dashboards mapping")
        return None
    return dashboards


def find_dashboard(hass: HomeAssistant, url_path: str) -> Any | None:
    """Return one dashboard by url path, or None."""
    dashboards = _dashboard_store(hass)
    if dashboards is None:
        return None
    getter = getattr(dashboards, "get", None)
    if callable(getter):
        found = getter(url_path)
        if found is not None:
            return found
        if url_path == "lovelace":
            found = getter(None)
            if found is not None:
                return found
    for dashboard in dashboards.values():
        path = getattr(dashboard, "url_path", None) or "lovelace"
        if path == url_path:
            return dashboard
    return None


async def async_scan(
    hass: HomeAssistant, tracked: set[str]
) -> dict[str, list[ReferenceHit]]:
    """Scan dashboards."""
    hits: dict[str, list[ReferenceHit]] = {}
    dashboards = _dashboard_store(hass)
    seen_files: set[str] = set()

    if dashboards is not None:
        storage_candidates = 0
        storage_loaded = 0
        for dashboard in dashboards.values():
            yaml_mode = is_yaml_dashboard(dashboard)
            if not yaml_mode:
                storage_candidates += 1
            load = getattr(dashboard, "async_load", None)
            if not callable(load):
                continue
            url_path = getattr(dashboard, "url_path", None) or "lovelace"
            title = url_path
            config_meta = getattr(dashboard, "config", None)
            if isinstance(config_meta, dict):
                title = config_meta.get("title", url_path)

            try:
                config = await load(force=False)
            except Exception:  # noqa: BLE001 - missing storage or renamed helper
                _LOGGER.debug("Skipping dashboard %s", url_path, exc_info=True)
                continue
            if not yaml_mode:
                storage_loaded += 1

            extracted = await _async_extract_entities(hass, config, tracked)
            for entity_id, views in extracted.items():
                if entity_id not in tracked:
                    continue
                for view_path, view_title in views:
                    hit = ReferenceHit(
                        resource_type="dashboard",
                        label=f"{title} - {view_title}",
                        edit_url=(
                            f"/{url_path}/{view_path}"
                            if yaml_mode
                            else f"/{url_path}/{view_path}?edit=1"
                        ),
                        resource_id=f"{url_path}/{view_path}",
                        extra={
                            "view_path": view_path,
                            "dashboard_url": url_path,
                        },
                        auto_replaceable=not yaml_mode,
                    )
                    hits.setdefault(entity_id, []).append(hit)
            yaml_path = getattr(dashboard, "path", None)
            if isinstance(yaml_path, str) and yaml_path:
                seen_files.add(yaml_path)
        if storage_candidates and storage_loaded == 0:
            _warn_dashboard_store_unusable()

    await _async_scan_yaml_dashboard_files(hass, tracked, hits, seen_files)
    return hits


async def _async_scan_yaml_dashboard_files(
    hass: HomeAssistant,
    tracked: set[str],
    hits: dict[str, list[ReferenceHit]],
    seen_files: set[str],
) -> None:
    """Scan documented YAML dashboard files as manual-only hits."""
    from .yaml_config import async_load_core_config, dashboard_yaml_filenames

    files: list[tuple[str, str, str]] = []
    default_path = hass.config.path(_DEFAULT_YAML_DASHBOARD)
    files.append(("lovelace", _DEFAULT_YAML_DASHBOARD, default_path))
    try:
        loaded = await async_load_core_config(hass)
    except Exception:  # noqa: BLE001 - merge fallback already logs
        loaded = None
    for url_path, filename in dashboard_yaml_filenames(loaded):
        files.append((url_path, filename, hass.config.path(filename)))

    for url_path, filename, abs_path in files:
        if abs_path in seen_files:
            continue
        seen_files.add(abs_path)
        config = await _async_load_dashboard_yaml(hass, abs_path)
        if not isinstance(config, dict):
            continue
        extracted = await _async_extract_entities(hass, config, tracked)
        for entity_id, views in extracted.items():
            if entity_id not in tracked:
                continue
            for view_path, view_title in views:
                hit = ReferenceHit(
                    resource_type="dashboard",
                    label=f"{filename} - {view_title}",
                    edit_url=f"/{url_path}/{view_path}",
                    resource_id=f"{url_path}/{view_path}",
                    extra={
                        "view_path": view_path,
                        "dashboard_url": url_path,
                        "yaml_file": filename,
                    },
                    auto_replaceable=False,
                )
                hits.setdefault(entity_id, []).append(hit)


async def _async_load_dashboard_yaml(
    hass: HomeAssistant, path: str
) -> dict[str, Any] | None:
    """Load a dashboard yaml file from the config directory."""

    def _load() -> dict[str, Any] | None:
        try:
            from homeassistant.config import load_yaml_config_file
        except ImportError:
            try:
                from homeassistant.util.yaml import load_yaml as load_yaml_config_file
            except ImportError:
                return None
        try:
            loaded = load_yaml_config_file(path)
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Unable to read dashboard yaml %s", path, exc_info=True)
            return None
        return loaded if isinstance(loaded, dict) else None

    try:
        return await hass.async_add_executor_job(_load)
    except Exception:  # noqa: BLE001 - missing executor in tests, IO in production
        _LOGGER.debug("Unable to read dashboard yaml %s", path, exc_info=True)
        return None


def _remember_view(
    entities: dict[str, list[tuple[int | str, str]]],
    entity_id: str,
    view_path: int | str,
    view_title: str,
) -> None:
    """Record one view for an entity without duplicating it."""
    item = (view_path, view_title)
    bucket = entities.setdefault(entity_id, [])
    if item not in bucket:
        bucket.append(item)


async def _async_extract_entities(
    hass: HomeAssistant, config: dict[str, Any], tracked: set[str]
) -> dict[str, list[tuple[int | str, str]]]:
    """Extract entity IDs mapped to every view they appear on."""
    entities: dict[str, list[tuple[int | str, str]]] = {}
    if not isinstance(config, dict):
        return entities

    views = config.get("views")
    if isinstance(views, list):
        for view_index, view in enumerate(views):
            if not isinstance(view, dict):
                continue
            view_path = str(view.get("path") or view_index)
            view_title = str(view.get("title") or view_path or f"View {view_index + 1}")
            found = await extract_entities_from_value(hass, view, tracked)
            for entity_id in found:
                _remember_view(entities, entity_id, view_path, view_title)
        return entities

    found = await extract_entities_from_value(hass, config, tracked)
    for entity_id in found:
        _remember_view(entities, entity_id, "0", "View 1")
    return entities
