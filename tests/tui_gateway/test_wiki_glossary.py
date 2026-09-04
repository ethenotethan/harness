from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from tui_gateway import server
from tui_gateway.wiki_glossary import (
    GlossaryConflictError,
    GlossaryValidationError,
    canonicalize_proper_noun,
    canonicalize_text,
    canonicalize_inventory,
    load_glossary,
    normalize_glossary,
    update_glossary,
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    primary = tmp_path / "primary"
    other = tmp_path / "other"
    primary.mkdir()
    other.mkdir()
    home.mkdir()
    (home / "wikis.yaml").write_text(
        yaml.safe_dump(
            {
                "default": "primary",
                "wikis": {"primary": str(primary), "other": str(other)},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    return primary, other


def _call(method: str, params: dict, rid: int = 7) -> dict:
    return server._methods[method](rid, params)


def test_missing_glossary_is_disabled_default(tmp_path: Path) -> None:
    glossary = load_glossary(tmp_path)
    assert glossary == {
        "enabled": False,
        "version": 1,
        "mode": "canonicalize",
        "proper_nouns": [],
        "revision": "",
    }


def test_loader_normalizes_and_canonicalizes_aliases(tmp_path: Path) -> None:
    raw = (
        "version: 1\n"
        "mode: strict\n"
        "proper_nouns:\n"
        "  - canonical: OpenAI\n"
        "    aliases: [open ai, OPEN-AI]\n"
        "    description: Model provider\n"
    )
    (tmp_path / "glossary.yaml").write_text(raw, encoding="utf-8")

    glossary = load_glossary(tmp_path)
    assert glossary["enabled"] is True
    assert glossary["revision"] == hashlib.sha256(raw.encode()).hexdigest()
    assert glossary["proper_nouns"] == [
        {
            "canonical": "OpenAI",
            "aliases": ["open ai", "OPEN-AI"],
            "description": "Model provider",
        }
    ]
    assert canonicalize_proper_noun("OPEN AI", glossary) == "OpenAI"
    assert canonicalize_proper_noun("unknown", glossary) is None
    assert canonicalize_text("Open ai uses OPEN-AI.", glossary) == "OpenAI uses OpenAI."
    assert canonicalize_inventory(["OPEN AI", "OpenAI"], glossary) == ["OpenAI"]
    with pytest.raises(GlossaryValidationError, match="unknown proper nouns"):
        canonicalize_inventory(["OpenAI", "Acme Cloud"], glossary)


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            {"version": 1, "mode": "canonicalize", "proper_nouns": [
                {"canonical": "x" * 257}
            ]},
            "at most 256",
        ),
        (
            {"version": 1, "mode": "canonicalize", "proper_nouns": [
                {"canonical": "OpenAI", "aliases": [f"alias-{i}" for i in range(51)]}
            ]},
            "at most 50 aliases",
        ),
        (
            {"version": 1, "mode": "canonicalize", "proper_nouns": [
                {"canonical": "OpenAI", "description": "x" * 2001}
            ]},
            "at most 2000",
        ),
        (
            {"version": 1, "mode": "canonicalize", "proper_nouns": [
                {"canonical": f"Term {i}"} for i in range(2001)
            ]},
            "at most 2000 entries",
        ),
    ],
)
def test_normalize_glossary_enforces_resource_bounds(payload: dict, message: str) -> None:
    with pytest.raises(GlossaryValidationError, match=message):
        normalize_glossary(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "mode": "canonicalize", "proper_nouns": []},
        {"version": 1, "mode": "permissive", "proper_nouns": []},
        {
            "version": 1,
            "mode": "canonicalize",
            "proper_nouns": [
                {"canonical": "OpenAI", "aliases": ["OA"]},
                {"canonical": "oa"},
            ],
        },
        {
            "version": 1,
            "mode": "canonicalize",
            "proper_nouns": [{"canonical": "OpenAI", "aliases": ["openai"]}],
        },
    ],
)
def test_malformed_or_ambiguous_present_glossary_fails_closed(
    tmp_path: Path, payload: dict
) -> None:
    (tmp_path / "glossary.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(GlossaryValidationError):
        load_glossary(tmp_path)


def test_update_is_atomic_and_optimistically_concurrent(tmp_path: Path) -> None:
    first = update_glossary(
        tmp_path,
        {"version": 1, "mode": "canonicalize", "proper_nouns": []},
        if_match="",
    )
    assert first["enabled"] is True
    assert first["revision"]
    assert load_glossary(tmp_path) == first

    with pytest.raises(GlossaryConflictError):
        update_glossary(
            tmp_path,
            {"version": 1, "mode": "strict", "proper_nouns": []},
            if_match="stale",
        )
    assert load_glossary(tmp_path) == first
    assert not list(tmp_path.glob(".glossary.yaml.*"))


def test_rpc_methods_registered_and_advertised() -> None:
    assert "wiki.glossary" in server._methods
    assert "wiki.glossary.update" in server._methods
    assert "wiki.glossary" in server._LONG_HANDLERS
    assert "wiki.glossary.update" in server._LONG_HANDLERS
    capabilities = _call("gateway.capabilities", {})["result"]["capability_names"]
    assert "wiki.glossary" in capabilities
    assert "wiki.glossary.update" in capabilities


def test_rpc_read_uses_only_configured_name_or_explicit_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary, _ = _configure(monkeypatch, tmp_path)

    assert _call("wiki.glossary", {})["result"]["enabled"] is False
    assert _call("wiki.glossary", {"wiki": "primary"})["result"]["enabled"] is False

    for forbidden_params in (
        {"wiki": str(primary)},
        {"wiki": "~/wiki"},
        {"wiki": "unknown"},
        {"wiki": ""},
        {"wiki": 3},
        {"path": str(primary)},
    ):
        response = _call("wiki.glossary", forbidden_params)
        assert response["error"]["code"] == 4001


def test_rpc_rejects_missing_default_without_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    home = Path(str(tmp_path / "home"))
    (home / "wikis.yaml").write_text(
        yaml.safe_dump({"wikis": {"primary": str(tmp_path / "primary")}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "forbidden-fallback"))

    response = _call("wiki.glossary", {})
    assert response["error"]["code"] == 4001


def test_rpc_read_fails_closed_for_present_invalid_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    primary, _ = _configure(monkeypatch, tmp_path)
    (primary / "glossary.yaml").write_text("version: [unterminated", encoding="utf-8")

    response = _call("wiki.glossary", {"wiki": "primary"})
    assert response["error"]["code"] == 4001


def test_rpc_update_validates_payload_and_maps_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    valid = {
        "wiki": "primary",
        "version": 1,
        "mode": "canonicalize",
        "proper_nouns": [{"canonical": "Nous Research", "aliases": ["Nous"]}],
        "if_match": "",
    }
    created = _call("wiki.glossary.update", valid)
    assert created["result"]["proper_nouns"][0]["canonical"] == "Nous Research"

    stale = _call("wiki.glossary.update", {**valid, "mode": "strict"})
    assert stale["error"]["code"] == 409

    ambiguous = _call(
        "wiki.glossary.update",
        {
            **valid,
            "if_match": created["result"]["revision"],
            "proper_nouns": [
                {"canonical": "Alpha", "aliases": ["shared"]},
                {"canonical": "Beta", "aliases": ["SHARED"]},
            ],
        },
    )
    assert ambiguous["error"]["code"] == 4001
