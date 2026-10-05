"""Scan configuration.yaml, including merged packages, includes, and themes."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..models import ReferenceHit
from ..util import extract_entities_from_value, looks_like_entity_id

_LOGGER = logging.getLogger(__name__)

_MISSING = object()
# When a scan sets this to a dict, async_load_core_config fills it once and reuses.
_scan_core_config: ContextVar[Any] = ContextVar(
    "lost_entity_finder_scan_core_config", default=_MISSING
)

# Covered by the basics scanners, or outside this integration's scope.
# automation/script/scene also drops automations.yaml, scripts.yaml, and scenes.yaml
# after Home Assistant merges those includes. Package copies of other domains stay.
SKIP_DOMAIN_CONFIG_KEYS = frozenset({"automation", "script", "scene", "group", "lovelace"})

CONFIGURATION_EDIT_URL = "/config/configuration"
CONFIGURATION_RESOURCE_PREFIX = "configuration.yaml"
CONFIGURATION_FILE = "configuration.yaml"
PACKAGES_DIR = "packages"
THEMES_DIR = "themes"


@dataclass
class LoadedCoreConfig:
    """configuration.yaml after merge, or after a file walk fallback."""

    data: dict[str, Any]
    merged: bool


async def async_scan(
    hass: HomeAssistant, tracked: set[str]
) -> dict[str, list[ReferenceHit]]:
    """Scan merged Home Assistant YAML configuration for tracked old entity IDs."""
    if not tracked:
        return {}

    loaded = await async_load_core_config(hass)
    if loaded is None:
        return {}
    config = loaded.data

    hits: dict[str, list[ReferenceHit]] = {}
    for key, value in config.items():
        if key in SKIP_DOMAIN_CONFIG_KEYS:
            continue
        if key == "homeassistant" and isinstance(value, dict):
            await _async_scan_homeassistant_section(
                hass, value, tracked, hits, skip_packages=loaded.merged
            )
            continue
        if key == "frontend" and isinstance(value, dict):
            await _async_scan_frontend_section(hass, value, tracked, hits)
            continue
        await _async_scan_config_section(
            hass, f"{CONFIGURATION_RESOURCE_PREFIX}:{key}", key, value, tracked, hits
        )

    return hits


@contextmanager
def scan_core_config_cache() -> Iterator[None]:
    """Reuse one core config load for every scanner in a scan pass."""
    token = _scan_core_config.set({"loaded": _MISSING})
    try:
        yield
    finally:
        _scan_core_config.reset(token)


async def async_load_core_config(hass: HomeAssistant) -> LoadedCoreConfig | None:
    """Load configuration.yaml, with a file walk if HA's merge helper is gone."""
    cache = _scan_core_config.get()
    if isinstance(cache, dict) and cache.get("loaded") is not _MISSING:
        return cache["loaded"]

    loaded = await _async_load_core_config_uncached(hass)
    if isinstance(cache, dict):
        cache["loaded"] = loaded
    return loaded


async def _async_load_core_config_uncached(
    hass: HomeAssistant,
) -> LoadedCoreConfig | None:
    """Load configuration.yaml without using the per-scan cache."""
    try:
        from homeassistant.config import async_hass_config_yaml
    except ImportError:
        _LOGGER.warning(
            "Home Assistant yaml merge is unavailable; "
            "scanning configuration.yaml, packages, and themes as files"
        )
        return await _async_load_configuration_files(hass)
    try:
        config = await async_hass_config_yaml(hass)
    except (FileNotFoundError, HomeAssistantError) as err:
        _LOGGER.warning(
            "Could not merge configuration.yaml (%s); "
            "scanning configuration.yaml, packages, and themes as files",
            err,
        )
        return await _async_load_configuration_files(hass)
    except Exception:  # noqa: BLE001 - merge can fail for many reasons
        _LOGGER.warning(
            "Could not merge configuration.yaml; "
            "scanning configuration.yaml, packages, and themes as files",
            exc_info=True,
        )
        return await _async_load_configuration_files(hass)
    if not isinstance(config, dict):
        return LoadedCoreConfig(data={}, merged=True)
    return LoadedCoreConfig(data=config, merged=True)


def collect_domain_section(loaded: LoadedCoreConfig, domain: str) -> Any:
    """Return one domain from merged config, or from packages when unmerged."""
    data = loaded.data
    if loaded.merged:
        return data.get(domain)
    parts: list[Any] = []
    top = data.get(domain)
    if top is not None:
        parts.append(top)
    homeassistant_block = data.get("homeassistant")
    if isinstance(homeassistant_block, dict):
        packages = homeassistant_block.get("packages")
        if isinstance(packages, dict):
            for package in packages.values():
                if isinstance(package, dict) and domain in package:
                    parts.append(package[domain])
    return _combine_domain_parts(parts)


def dashboard_yaml_filenames(loaded: LoadedCoreConfig | None) -> list[tuple[str, str]]:
    """Return (url_path, filename) pairs for YAML dashboards in configuration.yaml."""
    if loaded is None:
        return []
    lovelace = loaded.data.get("lovelace")
    if not isinstance(lovelace, dict):
        return []
    dashboards = lovelace.get("dashboards")
    if not isinstance(dashboards, dict):
        return []
    found: list[tuple[str, str]] = []
    for url_path, config in dashboards.items():
        if not isinstance(config, dict):
            continue
        filename = config.get("filename")
        if isinstance(filename, str) and filename:
            found.append((str(url_path), filename))
    return found


def _combine_domain_parts(parts: list[Any]) -> Any:
    """Join list or mapping domain chunks for scanning."""
    if not parts:
        return None
    if all(isinstance(part, list) for part in parts):
        combined: list[Any] = []
        for part in parts:
            combined.extend(part)
        return combined
    if all(isinstance(part, dict) for part in parts):
        combined_map: dict[Any, Any] = {}
        for part in parts:
            combined_map.update(part)
        return combined_map
    items: list[Any] = []
    for part in parts:
        if isinstance(part, list):
            items.extend(item for item in part if isinstance(item, dict))
        elif isinstance(part, dict):
            items.extend(
                item for item in part.values() if isinstance(item, dict)
            )
    return items or None


def _load_yaml_file(path: str) -> Any | None:
    """Load one yaml file with Home Assistant's secrets-aware loader."""
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
        return {}
    except Exception:  # noqa: BLE001 - secrets or tags
        _LOGGER.debug("Unable to read %s", path, exc_info=True)
        return None
    return loaded


def _yaml_files_in(directory: Path) -> list[Path]:
    """Return *.yaml files under a documented include directory."""
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.rglob("*.yaml")
        if path.is_file() and not path.name.startswith(".")
    )


async def _async_load_configuration_files(
    hass: HomeAssistant,
) -> LoadedCoreConfig | None:
    """Load configuration.yaml plus packages/ and themes/ when includes were not merged."""
    config_path = hass.config.path(CONFIGURATION_FILE)

    def _load() -> dict[str, Any] | None:
        loaded = _load_yaml_file(config_path)
        if loaded is None:
            return None
        if loaded == {} and not Path(config_path).is_file():
            data: dict[str, Any] = {}
        elif not isinstance(loaded, dict):
            return None
        else:
            data = dict(loaded)

        homeassistant_block = data.get("homeassistant")
        packages = (
            homeassistant_block.get("packages")
            if isinstance(homeassistant_block, dict)
            else None
        )
        if not isinstance(packages, dict) or not packages:
            walked = _load_named_yaml_dir(Path(hass.config.path(PACKAGES_DIR)))
            if walked:
                if not isinstance(homeassistant_block, dict):
                    homeassistant_block = {}
                    data["homeassistant"] = homeassistant_block
                homeassistant_block["packages"] = walked

        frontend = data.get("frontend")
        themes = frontend.get("themes") if isinstance(frontend, dict) else None
        if not isinstance(themes, dict) or not themes:
            walked_themes = _load_merge_named_yaml_dir(
                Path(hass.config.path(THEMES_DIR))
            )
            if walked_themes:
                if not isinstance(frontend, dict):
                    frontend = {}
                    data["frontend"] = frontend
                frontend["themes"] = walked_themes
        return data

    try:
        data = await hass.async_add_executor_job(_load)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Unable to scan configuration files", exc_info=True)
        return None
    if data is None:
        return None
    return LoadedCoreConfig(data=data, merged=False)


def _load_named_yaml_dir(directory: Path) -> dict[str, Any]:
    """Load a folder the way !include_dir_named does (filename -> contents)."""
    named: dict[str, Any] = {}
    for path in _yaml_files_in(directory):
        loaded = _load_yaml_file(str(path))
        if loaded is None:
            continue
        named[path.stem] = loaded
    return named


def _load_merge_named_yaml_dir(directory: Path) -> dict[str, Any]:
    """Load a folder the way !include_dir_merge_named does."""
    merged: dict[str, Any] = {}
    for path in _yaml_files_in(directory):
        loaded = _load_yaml_file(str(path))
        if not isinstance(loaded, dict):
            continue
        merged.update(loaded)
    return merged


async def _async_scan_frontend_section(
    hass: HomeAssistant,
    frontend_config: dict[str, Any],
    tracked: set[str],
    hits: dict[str, list[ReferenceHit]],
) -> None:
    """Scan frontend config, including themes loaded with !include_dir_merge_named."""
    themes = frontend_config.get("themes")
    if themes is not None:
        await _async_scan_config_section(
            hass,
            f"{CONFIGURATION_RESOURCE_PREFIX}:themes",
            "themes",
            themes,
            tracked,
            hits,
        )
    rest = {key: value for key, value in frontend_config.items() if key != "themes"}
    if not rest:
        return
    await _async_scan_config_section(
        hass,
        f"{CONFIGURATION_RESOURCE_PREFIX}:frontend",
        "frontend",
        rest,
        tracked,
        hits,
    )


async def _async_scan_homeassistant_section(
    hass: HomeAssistant,
    homeassistant_config: dict[str, Any],
    tracked: set[str],
    hits: dict[str, list[ReferenceHit]],
    skip_packages: bool = True,
) -> None:
    """Scan the homeassistant: block, skipping merged packages and covered domains."""
    for key, value in homeassistant_config.items():
        if key in SKIP_DOMAIN_CONFIG_KEYS:
            continue
        if key == "packages":
            if skip_packages:
                continue
            if isinstance(value, dict):
                for package_name, package in value.items():
                    await _async_scan_config_section(
                        hass,
                        f"{CONFIGURATION_RESOURCE_PREFIX}:packages.{package_name}",
                        f"packages.{package_name}",
                        package,
                        tracked,
                        hits,
                    )
            continue
        section_key = f"homeassistant.{key}"
        await _async_scan_config_section(
            hass,
            f"{CONFIGURATION_RESOURCE_PREFIX}:{section_key}",
            section_key,
            value,
            tracked,
            hits,
        )


async def _async_scan_config_section(
    hass: HomeAssistant,
    resource_id: str,
    section_key: str,
    data: Any,
    tracked: set[str],
    hits: dict[str, list[ReferenceHit]],
) -> None:
    """Scan one configuration section and record hits for tracked entity IDs."""
    found = await _async_extract_from_config_tree(hass, data, tracked)
    if not found:
        return

    hit = ReferenceHit(
        resource_type="yaml_config",
        label=section_label(section_key),
        edit_url=CONFIGURATION_EDIT_URL,
        resource_id=resource_id,
        extra={"section": section_key},
        auto_replaceable=False,
    )
    for entity_id in found:
        hits.setdefault(entity_id, []).append(hit)


async def _async_extract_from_config_tree(
    hass: HomeAssistant, node: Any, tracked: set[str]
) -> set[str]:
    """Extract tracked entity IDs from a config tree, skipping covered domain sections."""
    if isinstance(node, dict):
        found: set[str] = set()
        for key, value in node.items():
            if key in SKIP_DOMAIN_CONFIG_KEYS:
                continue
            if (
                isinstance(key, str)
                and key in tracked
                and looks_like_entity_id(key)
            ):
                found.add(key)
            found |= await _async_extract_from_config_tree(hass, value, tracked)
        return found

    if isinstance(node, list):
        found = set()
        for item in node:
            found |= await _async_extract_from_config_tree(hass, item, tracked)
        return found

    return await extract_entities_from_value(hass, node, tracked)


def section_label(section_key: str) -> str:
    """Build a human-readable label for a configuration section."""
    return f"configuration.yaml ({section_key})"
