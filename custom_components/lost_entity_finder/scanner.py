"""Orchestrate reference scanners for Lost Entity Find And Replace."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .models import ReferenceHit
from .scanners import (
    automation,
    lovelace,
    scene,
    script,
    yaml_config,
)
from .scanners.yaml_config import scan_core_config_cache
from .util import merge_reference_hits

_LOGGER = logging.getLogger(__name__)

SCANNERS = (
    automation.async_scan,
    script.async_scan,
    scene.async_scan,
    lovelace.async_scan,
    yaml_config.async_scan,
)

# Event scopes. Merged YAML stays on a full scan only.
SCANNER_SCOPES: dict[str, object] = {
    "automation": automation.async_scan,
    "script": script.async_scan,
    "scene": scene.async_scan,
    "lovelace": lovelace.async_scan,
    "yaml_config": yaml_config.async_scan,
}

SCOPE_RESOURCE_TYPES: dict[str, frozenset[str]] = {
    "automation": frozenset({"automation"}),
    "script": frozenset({"script"}),
    "scene": frozenset({"scene"}),
    "lovelace": frozenset({"dashboard"}),
    "yaml_config": frozenset({"yaml_config"}),
}


def resource_types_for_scopes(scopes: frozenset[str]) -> frozenset[str]:
    """Return hit resource types replaced by a scoped scan."""
    types: set[str] = set()
    for scope in scopes:
        types.update(SCOPE_RESOURCE_TYPES.get(scope, ()))
    return frozenset(types)


class ScanResult:
    """Hits from a scan, plus which scanners failed."""

    def __init__(
        self,
        hits: dict[str, list[ReferenceHit]],
        failed_scopes: frozenset[str] = frozenset(),
    ) -> None:
        """Store hits and the scopes that threw."""
        self.hits = hits
        self.failed_scopes = failed_scopes

    def get(
        self, key: str, default: list[ReferenceHit] | None = None
    ) -> list[ReferenceHit]:
        """Return hits for one old entity id."""
        if default is None:
            default = []
        return self.hits.get(key, default)


def coerce_scan_result(value: ScanResult | dict[str, list[ReferenceHit]]) -> ScanResult:
    """Accept a ScanResult or a plain hit dict from older callers and tests."""
    if isinstance(value, ScanResult):
        return value
    return ScanResult(value, frozenset())


_SCANNER_SCOPE_BY_FUNC = {
    scanner: scope for scope, scanner in SCANNER_SCOPES.items()
}


async def async_scan_tracked_references(
    hass: HomeAssistant,
    tracked: set[str],
    scopes: frozenset[str] | None = None,
) -> ScanResult:
    """Run scanners for tracked old entity IDs.

    scopes None runs every scanner. A scope set runs only those scanners.
    One scanner failure is logged and the others still return hits.
    Failed scopes are reported so the caller can keep the previous hits.
    """
    if not tracked:
        return ScanResult({})

    if scopes is None:
        selected = tuple((None, scanner) for scanner in SCANNERS)
    else:
        selected = tuple(
            (scope, scanner)
            for scope, scanner in (
                (scope, SCANNER_SCOPES.get(scope)) for scope in scopes
            )
            if scanner is not None
        )

    results: list[dict[str, list[ReferenceHit]]] = []
    failed: set[str] = set()
    with scan_core_config_cache():
        for scope, scanner in selected:
            try:
                results.append(await scanner(hass, tracked))
            except Exception:  # noqa: BLE001 - one scanner must not drop the rest
                failed_scope = scope or _SCANNER_SCOPE_BY_FUNC.get(scanner)
                if failed_scope:
                    failed.add(failed_scope)
                _LOGGER.exception(
                    "Scanner %s failed",
                    getattr(scanner, "__module__", None) or scanner,
                )
    return ScanResult(merge_reference_hits(results), frozenset(failed))
