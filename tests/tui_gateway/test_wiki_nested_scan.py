"""Contract tests for wiki_scan over NESTED wiki layouts and typed relations.

These lock four behaviours that a real two-level wiki depends on. Each one was
a live bug: a 971-page wiki scanned as 5 pages, every nested page 404'd in
wiki.page, every page reported zero tags, and every structural edge arrived
untyped.

The assertions are invariants (a scanned page's advertised path must load; a
declared tag axis must survive to the client), not frozen counts, so they stay
meaningful as the wiki grows.
"""

from tui_gateway.wiki_api import wiki_page, wiki_scan


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _nested_wiki(tmp_path):
    """A wiki shaped like a real controlled-taxonomy corpus: two levels deep,
    nested `tags:` mapping, typed relations, and path-style wikilinks."""
    _write(
        tmp_path / "entities" / "org" / "stableenrich.md",
        """---
title: "StableEnrich"
type: org
tags:
  protocol: [mpp, x402]
  chain: [base, solana]
  maturity: deployed
---

## Relations

<!-- RELATIONS:x402 — GENERATED, do not hand-edit -->
- settles_through: [[coinbase]]
- implements: [[x402]]
- deployed_on: [[base]]
<!-- /RELATIONS:x402 -->

## Links

- [[queries/x402-market]]
""",
    )
    _write(
        tmp_path / "entities" / "org" / "coinbase.md",
        """---
title: "Coinbase"
type: org
tags:
  protocol: [x402]
  maturity: deployed
---
Facilitator.
""",
    )
    _write(
        tmp_path / "entities" / "protocol" / "x402.md",
        """---
title: "x402"
type: protocol
tags:
  protocol: [x402]
---
Protocol.
""",
    )
    _write(
        tmp_path / "entities" / "chain" / "base.md",
        """---
title: "Base"
type: chain
tags:
  chain: [base]
---
Chain.
""",
    )
    _write(
        tmp_path / "queries" / "x402-market.md",
        """---
title: "x402 Market"
type: query
---
Projection.
""",
    )
    return tmp_path


def test_scan_finds_pages_nested_more_than_one_level_deep(tmp_path):
    """Pages under entities/<bucket>/ must be scanned.

    Regression: _iter_page_files used iterdir() (non-recursive). Since nothing
    lives directly in entities/, every entity page was invisible and a large
    wiki scanned as only its root + queries files.
    """
    wiki = _nested_wiki(tmp_path)
    ids = {p["id"] for p in wiki_scan(str(wiki))["pages"]}
    assert {"stableenrich", "coinbase", "x402", "base", "x402-market"} <= ids


def test_every_scanned_path_is_loadable_by_wiki_page(tmp_path):
    """The path wiki.scan advertises must be the path wiki.page accepts.

    Regression: rel_path joined only the top-level bucket, so a file at
    entities/org/foo.md was advertised as entities/foo.md and the client's
    follow-up wiki.page call 404'd ("Failed to load page").
    """
    wiki = _nested_wiki(tmp_path)
    pages = wiki_scan(str(wiki))["pages"]
    assert pages, "scan returned no pages"
    unloadable = [p["path"] for p in pages if wiki_page(p["path"], str(wiki)) is None]
    assert unloadable == []


def test_nested_tags_mapping_reaches_the_client_as_axis_values(tmp_path):
    """A nested `tags:` mapping must survive to the client.

    Regression: the flat frontmatter parser hoisted the children to top level
    and left `tags` itself None, so every page reported tags == [] and clients
    had nothing to colour or filter nodes by.
    """
    wiki = _nested_wiki(tmp_path)
    page = next(p for p in wiki_scan(str(wiki))["pages"] if p["id"] == "stableenrich")
    assert page["tags"], "nested tags mapping produced no tags"
    assert "protocol:x402" in page["tags"]
    assert "protocol:mpp" in page["tags"]
    assert "maturity:deployed" in page["tags"]


def test_flat_tag_shapes_still_supported(tmp_path):
    """Nested-mapping support must not regress the simpler shapes."""
    _write(
        tmp_path / "entities" / "bracketed.md",
        '---\ntitle: B\ntype: concept\ntags: [ml, research]\n---\nbody\n',
    )
    _write(
        tmp_path / "entities" / "bare.md",
        "---\ntitle: C\ntype: concept\ntags: one, two\n---\nbody\n",
    )
    pages = {p["id"]: p for p in wiki_scan(str(tmp_path))["pages"]}
    assert pages["bracketed"]["tags"] == ["ml", "research"]
    assert pages["bare"]["tags"] == ["one", "two"]


def test_typed_relations_are_emitted_with_their_predicate_as_edge_type(tmp_path):
    """Structural edges must carry their predicate, not a generic label.

    Regression: wiki_scan hardcoded type="wikilink" for every edge, so the
    graph could not distinguish "settles through" from "mentions".
    """
    wiki = _nested_wiki(tmp_path)
    links = wiki_scan(str(wiki))["links"]
    typed = {(l["source"], l["type"], l["target"]) for l in links}
    assert ("stableenrich", "settles_through", "coinbase") in typed
    assert ("stableenrich", "implements", "x402") in typed
    assert ("stableenrich", "deployed_on", "base") in typed


def test_typed_relation_is_not_also_duplicated_as_a_plain_wikilink(tmp_path):
    """One relation is one edge: the predicate edge, not predicate + wikilink."""
    wiki = _nested_wiki(tmp_path)
    links = wiki_scan(str(wiki))["links"]
    coinbase_edges = [
        l for l in links if l["source"] == "stableenrich" and l["target"] == "coinbase"
    ]
    assert len(coinbase_edges) == 1
    assert coinbase_edges[0]["type"] == "settles_through"


def test_path_style_wikilink_targets_resolve_to_their_page(tmp_path):
    """[[queries/x402-market]] must resolve to the x402-market page.

    Regression: targets were matched only against bare page ids, so every
    path-style link resolved to nothing and vanished from the graph.
    """
    wiki = _nested_wiki(tmp_path)
    links = wiki_scan(str(wiki))["links"]
    assert any(
        l["source"] == "stableenrich" and l["target"] == "x402-market" for l in links
    )


def test_links_never_reference_a_nonexistent_page(tmp_path):
    """Every edge endpoint must be a real scanned page (no dangling edges)."""
    wiki = _nested_wiki(tmp_path)
    result = wiki_scan(str(wiki))
    ids = {p["id"] for p in result["pages"]}
    for link in result["links"]:
        assert link["source"] in ids
        assert link["target"] in ids


def test_unresolvable_relation_target_is_dropped_not_dangling(tmp_path):
    """A relation naming a page that doesn't exist yields no edge."""
    _write(
        tmp_path / "entities" / "org" / "solo.md",
        """---
title: Solo
type: org
---

## Relations

<!-- RELATIONS:x402 — GENERATED -->
- settles_through: [[ghost-facilitator]]
<!-- /RELATIONS:x402 -->
""",
    )
    links = wiki_scan(str(tmp_path))["links"]
    assert not [l for l in links if l["target"] == "ghost-facilitator"]


def test_self_referencing_relation_is_not_emitted(tmp_path):
    """A page relating to itself must not produce a self-loop edge."""
    _write(
        tmp_path / "entities" / "org" / "selfy.md",
        """---
title: Selfy
type: org
---

## Relations

<!-- RELATIONS:x402 — GENERATED -->
- implements: [[selfy]]
<!-- /RELATIONS:x402 -->
""",
    )
    links = wiki_scan(str(tmp_path))["links"]
    assert not [l for l in links if l["source"] == l["target"]]
