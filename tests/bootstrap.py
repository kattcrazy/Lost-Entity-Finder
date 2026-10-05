"""Shared test bootstrap for Lost Entity Find And Replace unit tests."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Callable, TypeVar
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1] / "custom_components"
PKG = ROOT / "lost_entity_finder"

_F = TypeVar("_F", bound=Callable[..., Any])


def _passthrough_callback(func: _F) -> _F:
    """No-op callback decorator for tests."""
    return func


def _ensure_module(name: str) -> MagicMock:
    module = sys.modules.get(name)
    if module is None or not isinstance(module, types.ModuleType):
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module  # type: ignore[return-value]


# Core HA package and modules used by Lost Entity Find And Replace.
ha = _ensure_module("homeassistant")
ha_core = _ensure_module("homeassistant.core")
ha_core.callback = _passthrough_callback
ha_core.HomeAssistant = MagicMock
ha_core.Event = MagicMock
ha_core.valid_entity_id = lambda value: (
    isinstance(value, str)
    and "." in value
    and not value.startswith(".")
    and " " not in value
)

ha_helpers = _ensure_module("homeassistant.helpers")
ha_helpers.template = _ensure_module("homeassistant.helpers.template")
ha_helpers.template.Template = MagicMock
ha_helpers.storage = _ensure_module("homeassistant.helpers.storage")
ha_helpers.storage.Store = lambda *args, **kwargs: MagicMock()
ha_helpers.entity_registry = _ensure_module("homeassistant.helpers.entity_registry")
ha_helpers.entity_registry.async_get = MagicMock()
ha_helpers.entity_registry.EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"
ha_helpers.debounce = _ensure_module("homeassistant.helpers.debounce")
ha_helpers.debounce.Debouncer = MagicMock
ha_helpers.event = _ensure_module("homeassistant.helpers.event")
ha_helpers.event.async_call_later = MagicMock()
ha_helpers.issue_registry = _ensure_module("homeassistant.helpers.issue_registry")
ha_helpers.issue_registry.async_create_issue = MagicMock()
ha_helpers.issue_registry.async_delete_issue = MagicMock()
ha_helpers.issue_registry.async_ignore_issue = MagicMock()
ha_helpers.issue_registry.IssueSeverity = MagicMock()

ha_components = _ensure_module("homeassistant.components")
ha_components.automation = _ensure_module("homeassistant.components.automation")
ha_components.automation.DOMAIN = "automation"
ha_components.automation.EVENT_AUTOMATION_RELOADED = "automation_reloaded"
ha_components.script = _ensure_module("homeassistant.components.script")
ha_components.script.DOMAIN = "script"
ha_components.scene = _ensure_module("homeassistant.components.scene")
ha_components.scene.DOMAIN = "scene"
ha_components.homeassistant = _ensure_module("homeassistant.components.homeassistant")
ha_components.homeassistant.scene = _ensure_module(
    "homeassistant.components.homeassistant.scene"
)
ha_components.homeassistant.scene.EVENT_SCENE_RELOADED = "scene_reloaded"
ha_components.lovelace = _ensure_module("homeassistant.components.lovelace")
ha_components.lovelace.DOMAIN = "lovelace"
ha_components.lovelace.const = _ensure_module("homeassistant.components.lovelace.const")
ha_components.lovelace.const.EVENT_LOVELACE_UPDATED = "lovelace_updated"

ha_config_entries = _ensure_module("homeassistant.config_entries")
ha_config_entries.ConfigEntry = MagicMock
ha_config_entries.ConfigEntryState = MagicMock()
ha_config_entries.ConfigFlow = type("ConfigFlow", (), {})
ha_config_entries.OptionsFlow = type("OptionsFlow", (), {})

ha_const = _ensure_module("homeassistant.const")
ha_const.EVENT_COMPONENT_LOADED = "component_loaded"
ha_const.EVENT_CORE_CONFIG_UPDATE = "core_config_updated"
ha_const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
ha_const.SERVICE_RELOAD = "reload"
ha_const.CONF_ID = "id"

ha_config = _ensure_module("homeassistant.config")
ha_config.load_yaml_config_file = MagicMock(return_value=[])
ha_config.async_hass_config_yaml = MagicMock()

ha_exceptions = _ensure_module("homeassistant.exceptions")


class _HomeAssistantError(Exception):
    """Minimal stand-in for Home Assistant config errors in unit tests."""

    def __init__(
        self,
        *args: Any,
        translation_domain: str | None = None,
        translation_key: str | None = None,
        translation_placeholders: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders or {}
        if args:
            super().__init__(*args)
        elif translation_key:
            super().__init__(translation_key)
        else:
            super().__init__()


ha_exceptions.HomeAssistantError = _HomeAssistantError


class _ConfigNotFound(_HomeAssistantError):
    """Minimal stand-in for Lovelace ConfigNotFound."""


ha_components.lovelace.const.ConfigNotFound = _ConfigNotFound

ha_components.group = _ensure_module("homeassistant.components.group")
ha_components.group.DOMAIN = "group"

ha_util = _ensure_module("homeassistant.util")
ha_util.file = _ensure_module("homeassistant.util.file")
ha_util.file.write_utf8_file_atomic = MagicMock()
ha_util.yaml = _ensure_module("homeassistant.util.yaml")
ha_util.yaml.dump = MagicMock()


class _RepairsFlow:
    """Minimal repairs flow base so repair steps can be unit tested."""

    def async_abort(self, *, reason: str) -> dict[str, str]:
        return {"type": "abort", "reason": reason}

    def async_create_entry(
        self, title: str = "", data: dict | None = None
    ) -> dict[str, object]:
        return {"type": "create_entry", "title": title, "data": data or {}}

    def async_show_form(self, **kwargs: object) -> dict[str, object]:
        return {"type": "form", **kwargs}


ha_repairs = _ensure_module("homeassistant.components.repairs")
ha_repairs.RepairsFlow = _RepairsFlow

ha.config_entries = ha_config_entries
ha.core = ha_core
ha.exceptions = ha_exceptions
ha.helpers = ha_helpers
ha.components = ha_components
ha.const = ha_const
ha.config = ha_config

sys.modules.setdefault("voluptuous", MagicMock())
sys.modules.setdefault("homeassistant.data_entry_flow", MagicMock())
sys.modules.setdefault("homeassistant.helpers.config_validation", MagicMock())
sys.modules.setdefault("homeassistant.helpers.entity_platform", MagicMock())
sys.modules.setdefault("homeassistant.helpers.entity_component", MagicMock())
sys.modules.setdefault("homeassistant.components.button", MagicMock())
sys.modules.setdefault("homeassistant.components.sensor", MagicMock())
sys.modules.setdefault("homeassistant.helpers.entity", MagicMock())

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

package = types.ModuleType("lost_entity_finder")
package.__path__ = [str(PKG)]
sys.modules["lost_entity_finder"] = package
