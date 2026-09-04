"""Validated per-wiki proper-noun glossary storage.

This module is deliberately part of the gateway rather than a skill: native
clients and any Harness caller share one schema, validator, canonicalizer, and
optimistic-concurrency implementation.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_hermes_home

GLOSSARY_FILENAME = "glossary.yaml"
GLOSSARY_VERSION = 1
GLOSSARY_MODES = frozenset({"canonicalize", "strict"})
MAX_GLOSSARY_ENTRIES = 2_000
MAX_ALIASES_PER_ENTRY = 50
MAX_TERM_LENGTH = 256
MAX_DESCRIPTION_LENGTH = 2_000
_WRITE_LOCK = threading.Lock()


class GlossaryError(ValueError):
    """Base class for client-actionable glossary failures."""


class GlossaryValidationError(GlossaryError):
    """The registry or glossary does not satisfy its public schema."""


class GlossaryConflictError(GlossaryError):
    """An optimistic-concurrency revision did not match current storage."""


def _disabled_glossary() -> dict[str, Any]:
    return {
        "enabled": False,
        "version": GLOSSARY_VERSION,
        "mode": "canonicalize",
        "proper_nouns": [],
        "revision": "",
    }


def _nonempty_string(value: Any, field: str, *, max_length: int = MAX_TERM_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GlossaryValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise GlossaryValidationError(f"{field} must be at most {max_length} characters")
    return normalized


def normalize_glossary(data: Any) -> dict[str, Any]:
    """Validate and normalize the persisted/update glossary payload.

    Proper-noun spellings form one case-insensitive namespace. A canonical
    spelling or alias therefore cannot duplicate any other canonical spelling
    or alias, including another spelling in the same entry.
    """
    if not isinstance(data, Mapping):
        raise GlossaryValidationError("glossary must be an object")
    allowed_root = {"version", "mode", "proper_nouns"}
    unknown = set(data) - allowed_root
    if unknown:
        raise GlossaryValidationError(
            f"unknown glossary field(s): {', '.join(sorted(map(str, unknown)))}"
        )

    version = data.get("version")
    if isinstance(version, bool) or version != GLOSSARY_VERSION:
        raise GlossaryValidationError("version must be 1")
    mode = data.get("mode")
    if mode not in GLOSSARY_MODES:
        raise GlossaryValidationError("mode must be 'canonicalize' or 'strict'")
    raw_entries = data.get("proper_nouns")
    if not isinstance(raw_entries, list):
        raise GlossaryValidationError("proper_nouns must be an array")
    if len(raw_entries) > MAX_GLOSSARY_ENTRIES:
        raise GlossaryValidationError(
            f"proper_nouns must contain at most {MAX_GLOSSARY_ENTRIES} entries"
        )

    seen: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_entries):
        field = f"proper_nouns[{index}]"
        if not isinstance(raw, Mapping):
            raise GlossaryValidationError(f"{field} must be an object")
        allowed_entry = {"canonical", "aliases", "description"}
        entry_unknown = set(raw) - allowed_entry
        if entry_unknown:
            raise GlossaryValidationError(
                f"unknown {field} field(s): "
                + ", ".join(sorted(map(str, entry_unknown)))
            )

        canonical = _nonempty_string(raw.get("canonical"), f"{field}.canonical")
        aliases_raw = raw.get("aliases", [])
        if not isinstance(aliases_raw, list):
            raise GlossaryValidationError(f"{field}.aliases must be an array of strings")
        if len(aliases_raw) > MAX_ALIASES_PER_ENTRY:
            raise GlossaryValidationError(
                f"{field} must contain at most {MAX_ALIASES_PER_ENTRY} aliases"
            )
        aliases = [
            _nonempty_string(alias, f"{field}.aliases[{alias_index}]")
            for alias_index, alias in enumerate(aliases_raw)
        ]
        description = raw.get("description")
        if description is not None:
            description = _nonempty_string(
                description,
                f"{field}.description",
                max_length=MAX_DESCRIPTION_LENGTH,
            )

        for spelling in [canonical, *aliases]:
            folded = spelling.casefold()
            if folded in seen:
                raise GlossaryValidationError(
                    f"ambiguous proper noun spelling {spelling!r}; "
                    f"already used by {seen[folded]!r}"
                )
            seen[folded] = canonical

        entry: dict[str, Any] = {"canonical": canonical}
        if aliases:
            entry["aliases"] = aliases
        if description is not None:
            entry["description"] = description
        entries.append(entry)

    return {"version": GLOSSARY_VERSION, "mode": mode, "proper_nouns": entries}


def load_glossary(wiki_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``glossary.yaml`` or return the disabled state when absent.

    A present file is never treated as absent after a parse, decode, or schema
    error; callers receive :class:`GlossaryValidationError` (fail closed).
    """
    path = Path(wiki_root) / GLOSSARY_FILENAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _disabled_glossary()
    except OSError as exc:
        raise GlossaryValidationError(f"cannot read {GLOSSARY_FILENAME}: {exc}") from exc

    try:
        decoded = raw.decode("utf-8")
        parsed = yaml.safe_load(decoded)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GlossaryValidationError(f"invalid {GLOSSARY_FILENAME}: {exc}") from exc

    normalized = normalize_glossary(parsed)
    return {
        "enabled": True,
        **normalized,
        "revision": hashlib.sha256(raw).hexdigest(),
    }


def canonicalize_proper_noun(value: str, glossary: Mapping[str, Any]) -> str | None:
    """Resolve a canonical/alias spelling according to a loaded glossary.

    Unknown spellings pass through in ``canonicalize`` mode and are rejected
    (``None``) in ``strict`` mode. A disabled glossary is a no-op.
    """
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if not glossary.get("enabled", False):
        return value
    folded = value.casefold()
    for entry in glossary.get("proper_nouns", []):
        spellings = [entry["canonical"], *entry.get("aliases", [])]
        if any(folded == spelling.casefold() for spelling in spellings):
            return entry["canonical"]
    return None if glossary.get("mode") == "strict" else value


def _canonical_mapping(glossary: Mapping[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in glossary.get("proper_nouns", []):
        canonical = entry["canonical"]
        for spelling in [canonical, *entry.get("aliases", [])]:
            mapping[spelling.casefold()] = canonical
    return mapping


def canonicalize_text(value: str, glossary: Mapping[str, Any]) -> str:
    """Normalize every configured canonical spelling or alias in free text."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if not glossary.get("enabled", False):
        return value
    mapping = _canonical_mapping(glossary)
    forms = sorted(mapping, key=lambda form: (-len(form), form))
    if not forms:
        return value
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(form) for form in forms) + r")(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: mapping[match.group(0).casefold()], value)


def canonicalize_inventory(values: Any, glossary: Mapping[str, Any]) -> list[str]:
    """Normalize a declared proper-noun inventory and enforce strict mode."""
    if not isinstance(values, list):
        raise GlossaryValidationError("proper-noun inventory must be an array")
    if len(values) > MAX_GLOSSARY_ENTRIES:
        raise GlossaryValidationError(
            f"proper-noun inventory must contain at most {MAX_GLOSSARY_ENTRIES} entries"
        )
    result: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        normalized = _nonempty_string(value, f"proper_nouns[{index}]")
        canonical = canonicalize_proper_noun(normalized, glossary)
        if canonical is None:
            unknown.append(normalized)
            continue
        folded = canonical.casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(canonical)
    if unknown:
        raise GlossaryValidationError(
            "unknown proper nouns in strict mode: "
            + ", ".join(sorted(set(unknown), key=str.casefold))
        )
    return result


def update_glossary(
    wiki_root: str | os.PathLike[str],
    data: Any,
    *,
    if_match: str | None = None,
) -> dict[str, Any]:
    """Validate and atomically replace one wiki glossary."""
    if if_match is not None and not isinstance(if_match, str):
        raise GlossaryValidationError("if_match must be a string")
    normalized = normalize_glossary(data)
    root = Path(wiki_root)
    path = root / GLOSSARY_FILENAME

    with _WRITE_LOCK:
        current = load_glossary(root)
        if if_match is not None and if_match != current["revision"]:
            raise GlossaryConflictError("glossary revision conflict")

        root.mkdir(parents=True, exist_ok=True)
        rendered = yaml.safe_dump(
            normalized,
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        fd, temporary = tempfile.mkstemp(
            prefix=f".{GLOSSARY_FILENAME}.", suffix=".tmp", dir=root
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some platforms/filesystems do not support directory fsync.
                pass
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    return load_glossary(root)


def load_configured_wikis() -> tuple[str | None, dict[str, str]]:
    """Return one validated registry snapshot as ``(default, wikis)``."""
    registry_path = Path(get_hermes_home()) / "wikis.yaml"
    try:
        parsed = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GlossaryValidationError("no configured wiki registry") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GlossaryValidationError(f"invalid wiki registry: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise GlossaryValidationError("wiki registry must be an object")
    raw_wikis = parsed.get("wikis")
    if not isinstance(raw_wikis, Mapping):
        raise GlossaryValidationError("wiki registry wikis must be an object")

    wikis: dict[str, str] = {}
    for raw_name, raw_path in raw_wikis.items():
        name = _nonempty_string(raw_name, "wiki name")
        path = _nonempty_string(raw_path, f"wiki {name!r} path")
        wikis[name] = os.path.expanduser(path)
    default = parsed.get("default")
    if default is not None:
        default = _nonempty_string(default, "wiki registry default")
    return default, wikis


def resolve_configured_wiki(name: Any = None) -> str:
    """Resolve only a configured wiki name, never a raw path or fallback."""
    default, wikis = load_configured_wikis()
    if name is None:
        if default is None:
            raise GlossaryValidationError("wiki is required; no default is configured")
        name = default
    elif not isinstance(name, str) or not name.strip():
        raise GlossaryValidationError("wiki must be a configured non-empty name")
    if name not in wikis:
        raise GlossaryValidationError(f"unknown configured wiki: {name!r}")
    root = Path(wikis[name])
    if not root.is_dir():
        raise GlossaryValidationError(f"configured wiki is not a directory: {name!r}")
    return str(root)
