"""Plugin-owned synchronous locale bundle loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast


def _load_bundle(locale: str) -> dict[str, str]:
    path = Path(__file__).with_name("locales") / f"{locale}.json"
    return cast(dict[str, str], json.loads(path.read_text(encoding="utf-8")))


_BUNDLES = {locale: _load_bundle(locale) for locale in ("en", "zh")}


def tr(key: str, locale: str = "en") -> str:
    language = "zh" if locale.lower().startswith("zh") else "en"
    return _BUNDLES[language].get(key, _BUNDLES["en"].get(key, key))
