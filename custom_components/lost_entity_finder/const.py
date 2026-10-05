"""Constants for Lost Entity Finder."""

DOMAIN = "lost_entity_finder"

CONF_ENABLE_BULK_FIX = "enable_bulk_fix"
DEFAULT_ENABLE_BULK_FIX = False

CONF_MAX_PENDING_CHANGES = "max_pending_changes"
DEFAULT_MAX_PENDING_CHANGES = 1000
MIN_MAX_PENDING_CHANGES = 50
MAX_MAX_PENDING_CHANGES = 5000

STORAGE_KEY_PENDING = f"{DOMAIN}.pending_id_changes"
STORAGE_KEY_IGNORED = f"{DOMAIN}.ignored"
STORAGE_VERSION = 1

MAX_IGNORED = 50
DEBOUNCE_COOLDOWN = 3

TRANSLATION_KEY_LOST = "lost_entity_references"
TRANSLATION_KEY_FOUND = "found_entity_references"
ISSUE_KIND_FIND = "find"

RESCAN_COMPONENT_DOMAINS = frozenset(
    {"automation", "script", "scene", "lovelace"}
)
REGISTRY_RESCAN_DOMAINS = frozenset({"automation", "script", "scene"})

# Documented UI yaml files. Do not import private config-editor path constants.
UI_CONFIG_PATHS = {
    "automation": "automations.yaml",
    "script": "scripts.yaml",
    "scene": "scenes.yaml",
}

ENTITY_AUTO_REPLACE_ALL = "auto_replace_all"
ENTITY_IGNORE_ALL = "ignore_all"
ENTITY_RESTORE_IGNORED = "restore_ignored"
ENTITY_RESCAN = "rescan"
