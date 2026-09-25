"""The language packs the compiler knows, keyed by name, and how a file finds its pack."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .base import LanguagePack
from .go import PACK as GO
from .python import PACK as PYTHON
from .rust import PACK as RUST
from .swift import PACK as SWIFT
from .typescript import PACK as TYPESCRIPT

PACKS: Dict[str, LanguagePack] = {pack.name: pack for pack in (SWIFT, TYPESCRIPT, GO, RUST, PYTHON)}

_BY_EXTENSION: Dict[str, LanguagePack] = {}
for _pack in PACKS.values():
    for _extension in _pack.extensions:
        if _extension in _BY_EXTENSION:
            raise RuntimeError(f"extension {_extension} claimed by two packs")
        _BY_EXTENSION[_extension] = _pack


def pack_for_path(path: Path) -> Optional[LanguagePack]:
    return _BY_EXTENSION.get(path.suffix.lower())


__all__ = ["PACKS", "LanguagePack", "pack_for_path"]
