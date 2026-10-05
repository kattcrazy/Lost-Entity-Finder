"""Load Python-built user-facing strings from ui_strings.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _ui_strings() -> dict[str, str]:
    path = Path(__file__).with_name("ui_strings.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in data.items()}


def ui(key: str, **placeholders: object) -> str:
    """Return a user-facing string, with optional str.format placeholders."""
    text = _ui_strings()[key]
    if placeholders:
        return text.format(**placeholders)
    return text
