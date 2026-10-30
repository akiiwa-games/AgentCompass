"""AgentCompass language preference and display translations, independent of Usage Float.

Only JSON changes trigger a file read. Resolve STATE_DIR on every access so tests and
embedded hosts can override router_data.STATE_DIR without reloading this module.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

_LOCK = threading.RLock()
_cache_key = None
_language = "zh"
_catalog = None


def settings_path() -> Path:
    data = sys.modules.get("router_data")
    return Path(getattr(data, "STATE_DIR", Path.home() / ".agent-compass")) / "ui-settings.json"


def get_language() -> str:
    global _cache_key, _language
    with _LOCK:
        path = settings_path()
        try:
            stat = path.stat()
            key = (path, stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            key = (path, None)
        if key != _cache_key:
            try:
                document = json.loads(path.read_text(encoding="utf-8-sig"))
                value = document.get("language") if isinstance(document, dict) else None
            except (OSError, ValueError):
                value = None
            _language = value if value in ("zh", "en") else "zh"
            _cache_key = key
        return _language


def set_language(lang: str) -> str:
    """Atomically persist a supported language; invalid input falls back to Chinese."""
    global _cache_key
    lang = lang if lang in ("zh", "en") else "zh"
    with _LOCK:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".ui-settings-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"language": lang}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _cache_key = None
        return get_language()


def is_en() -> bool:
    return get_language() == "en"


def tr(zh: str, en: str) -> str:
    return en if is_en() else zh


def english(text: str) -> str:
    global _catalog
    if _catalog is None:
        from i18n_catalog import EN

        _catalog = EN
    return _catalog.get(text, text)


def translate(text: str) -> str:
    """Translate a catalog key before formatting it; arbitrary user text is untouched."""
    return english(text) if is_en() else text


def localized(value):
    if isinstance(value, str):
        return translate(value)
    if isinstance(value, tuple):
        return tuple(localized(item) for item in value)
    return value


class LocalizedSequence(Sequence):
    """Keep legacy sequence attributes while resolving labels at access time."""
    def __init__(self, rows):
        self.rows = tuple(rows)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return localized(self.rows[index])

    def __iter__(self):
        return (localized(row) for row in self.rows)
