import tempfile
from pathlib import Path

from tui_gateway import wiki_api as wiki


def _make_wiki():
    d = tempfile.mkdtemp()
    root = Path(d)
    (root / "entities").mkdir()
    (root / "concepts").mkdir()
    return root


class TestScan:
    def test_empty_dir(self):
        root = _make_wiki()
        result = wiki.wiki_scan(str(root))
        assert result["pages"] == []
        assert result["links"] == []

    def test_missing_dir(self):
        result = wiki.wiki_scan("/nonexistent/wiki/path")
        assert result == {"pages": [], "links": []}

    def test_scan_entities_and_concepts(self):
        root = _make_wiki()
        (root / "entities" / "dflash-mlx.md").write_text(
            "---\ntitle: dflash-mlx\ntype: entity\ntags: [optimization]\n---\n\nBody here. [[speculative-decoding]]\n",
            encoding="utf-8",
        )
        (root / "concepts" / "speculative-decoding.md").write_text(
            "---\ntitle: Speculative Decoding\ntype: concept\n---\n\nConcept body.\n",
            encoding="utf-8",
        )

        result = wiki.wiki_scan(str(root))
        pages = {p["id"]: p for p in result["pages"]}
        assert len(pages) == 2
        assert pages["dflash-mlx"]["type"] == "entity"
        assert pages["dflash-mlx"]["tags"] == ["optimization"]
        assert pages["speculative-decoding"]["type"] == "concept"

        links = result["links"]
        assert len(links) == 1
        assert links[0]["source"] == "dflash-mlx"
        assert links[0]["target"] == "speculative-decoding"
        assert links[0]["type"] == "wikilink"

    def test_no_frontmatter(self):
        root = _make_wiki()
        (root / "entities" / "plain.md").write_text(
            "No frontmatter. [[other]]", encoding="utf-8"
        )
        result = wiki.wiki_scan(str(root))
        assert len(result["pages"]) == 1
        assert result["pages"][0]["title"] == "plain"
        # No frontmatter -> the default page type is "concept".
        assert result["pages"][0]["type"] == "concept"
        assert result["pages"][0]["tags"] == []


class TestRootPages:
    def test_root_level_pages_scanned(self):
        root = _make_wiki()
        (root / "log.md").write_text(
            "# Wiki Log\n\nRecent changes. [[dflash-mlx]]\n", encoding="utf-8"
        )
        (root / "index.md").write_text(
            "---\ntitle: Index\ntype: index\n---\n\n[[dflash-mlx]]\n", encoding="utf-8"
        )
        (root / "entities" / "dflash-mlx.md").write_text(
            "---\ntitle: dflash-mlx\ntype: entity\n---\n\nBody.\n", encoding="utf-8"
        )

        result = wiki.wiki_scan(str(root))
        pages = {p["id"]: p for p in result["pages"]}
        assert "log" in pages
        assert "index" in pages
        # Root pages live at the wiki root — no subdir prefix.
        assert pages["log"]["path"] == "log.md"
        # No frontmatter type on a root page -> "meta"; explicit type wins.
        assert pages["log"]["type"] == "meta"
        assert pages["index"]["type"] == "index"

        # Root pages participate in the link graph.
        link_pairs = {(l["source"], l["target"]) for l in result["links"]}
        assert ("log", "dflash-mlx") in link_pairs
        assert ("index", "dflash-mlx") in link_pairs

    def test_root_page_readable_via_wiki_page(self):
        root = _make_wiki()
        (root / "log.md").write_text("# Log\n\nEntries.\n", encoding="utf-8")
        result = wiki.wiki_page("log.md", str(root))
        assert result is not None
        assert result["path"] == "log.md"
        assert "Entries." in result["body"]

    def test_non_markdown_root_files_ignored(self):
        root = _make_wiki()
        (root / "taxonomy.yaml").write_text("categories: {}\n", encoding="utf-8")
        (root / "notes.txt").write_text("not a page", encoding="utf-8")
        result = wiki.wiki_scan(str(root))
        assert result["pages"] == []


class TestPage:
    def test_read_page(self):
        root = _make_wiki()
        (root / "entities" / "swiftlm.md").write_text(
            "---\ntitle: SwiftLM\ntype: entity\n---\n\nBenchmark suite.\n",
            encoding="utf-8",
        )
        result = wiki.wiki_page("entities/swiftlm.md", str(root))
        assert result is not None
        assert result["path"] == "entities/swiftlm.md"
        assert result["frontmatter"]["title"] == "SwiftLM"
        assert result["body"].strip() == "Benchmark suite."

    def test_page_not_found(self):
        root = _make_wiki()
        assert wiki.wiki_page("entities/missing.md", str(root)) is None

    def test_path_traversal_blocked(self):
        root = _make_wiki()
        # Try to escape the wiki root
        assert wiki.wiki_page("../outside.md", str(root)) is None

    def test_absolute_path_rejected(self):
        root = _make_wiki()
        assert wiki.wiki_page("/etc/passwd", str(root)) is None


class TestFrontmatter:
    def test_valid_frontmatter(self):
        text = "---\ntitle: Foo\ntype: concept\n---\n\nBody"
        fm, body = wiki._parse_frontmatter(text)
        assert fm["title"] == "Foo"
        assert body.strip() == "Body"

    def test_no_frontmatter(self):
        text = "Just markdown"
        fm, body = wiki._parse_frontmatter(text)
        assert fm == {}
        assert body == "Just markdown"

    def test_invalid_yaml_treated_as_none(self):
        text = "---\n[bad yaml\n---\n\nBody"
        fm, body = wiki._parse_frontmatter(text)
        assert fm == {}
        assert body.strip() == "Body"


class TestUpdate:
    def _seed_page(self, root, updated="2026-07-01T00:00:00Z"):
        (root / "entities" / "dflash-mlx.md").write_text(
            "---\n"
            "title: dflash-mlx\n"
            "type: entity\n"
            "tags: [optimization]\n"
            "tag_path:\n"
            "  - ml/inference\n"
            f"updated: {updated}\n"
            "custom: keepme\n"
            "---\n"
            "\n"
            "Original body. [[speculative-decoding]]\n",
            encoding="utf-8",
        )

    def test_create_new_page(self):
        root = _make_wiki()
        result = wiki.wiki_update(
            "entities/new-page.md",
            "\nFresh body.\n",
            frontmatter={"title": "New Page", "type": "entity"},
            wiki_path=str(root),
        )
        assert "error" not in result
        assert result["frontmatter"]["title"] == "New Page"
        assert result["updated"] != ""
        # created is stamped on new pages
        assert result["frontmatter"]["created"] == result["updated"]
        # The file round-trips through the reader.
        page = wiki.wiki_page("entities/new-page.md", str(root))
        assert page is not None
        assert page["frontmatter"]["title"] == "New Page"
        assert page["body"].strip() == "Fresh body."

    def test_update_preserves_frontmatter_when_omitted(self):
        root = _make_wiki()
        self._seed_page(root)
        result = wiki.wiki_update(
            "entities/dflash-mlx.md", "\nReplaced body.\n", wiki_path=str(root)
        )
        assert "error" not in result
        fm = result["frontmatter"]
        assert fm["title"] == "dflash-mlx"
        assert fm["custom"] == "keepme"
        # Server bumps updated past the seeded value.
        assert fm["updated"] != "2026-07-01T00:00:00Z"
        page = wiki.wiki_page("entities/dflash-mlx.md", str(root))
        assert page["body"].strip() == "Replaced body."

    def test_frontmatter_replacement_drops_absent_keys(self):
        root = _make_wiki()
        self._seed_page(root)
        result = wiki.wiki_update(
            "entities/dflash-mlx.md",
            "\nBody.\n",
            frontmatter={"title": "Retitled", "type": "entity"},
            wiki_path=str(root),
        )
        fm = result["frontmatter"]
        assert fm["title"] == "Retitled"
        assert "custom" not in fm  # absent from the replacement → dropped

    def test_if_match_allows_write(self):
        root = _make_wiki()
        self._seed_page(root)
        result = wiki.wiki_update(
            "entities/dflash-mlx.md",
            "\nNew body.\n",
            if_match="2026-07-01T00:00:00Z",
            wiki_path=str(root),
        )
        assert "error" not in result

    def test_stale_if_match_conflicts_with_latest(self):
        root = _make_wiki()
        self._seed_page(root)
        result = wiki.wiki_update(
            "entities/dflash-mlx.md",
            "\nClobber body.\n",
            if_match="1999-01-01T00:00:00Z",
            wiki_path=str(root),
        )
        assert result.get("code") == "conflict"
        assert result["latest"]["updated"] == "2026-07-01T00:00:00Z"
        assert "Original body" in result["latest"]["body"]
        # The file is untouched.
        page = wiki.wiki_page("entities/dflash-mlx.md", str(root))
        assert "Original body" in page["body"]

    def test_force_bypasses_conflict(self):
        root = _make_wiki()
        self._seed_page(root)
        result = wiki.wiki_update(
            "entities/dflash-mlx.md",
            "\nForced body.\n",
            if_match="1999-01-01T00:00:00Z",
            force=True,
            wiki_path=str(root),
        )
        assert "error" not in result
        page = wiki.wiki_page("entities/dflash-mlx.md", str(root))
        assert "Forced body" in page["body"]

    def test_list_keys_survive_string_only_clients(self):
        root = _make_wiki()
        self._seed_page(root)
        # A string-only client round-trips tag_path as "" — the current list
        # must be preserved, not wiped.
        result = wiki.wiki_update(
            "entities/dflash-mlx.md",
            "\nBody.\n",
            frontmatter={"title": "dflash-mlx", "type": "entity", "tag_path": ""},
            wiki_path=str(root),
        )
        assert result["frontmatter"]["tag_path"] == ["ml/inference"]
        # A non-empty scalar is comma-split into a list.
        result = wiki.wiki_update(
            "entities/dflash-mlx.md",
            "\nBody.\n",
            frontmatter={"title": "dflash-mlx", "tag_path": "a/b, c"},
            wiki_path=str(root),
        )
        assert result["frontmatter"]["tag_path"] == ["a/b", "c"]
        # And the written file parses back to a list.
        page = wiki.wiki_page("entities/dflash-mlx.md", str(root))
        assert page["frontmatter"]["tag_path"] == ["a/b", "c"]

    def test_traversal_rejected(self):
        root = _make_wiki()
        result = wiki.wiki_update("../outside.md", "\nNope.\n", wiki_path=str(root))
        assert result.get("code") == "invalid"
        assert not (root.parent / "outside.md").exists()

    def test_non_markdown_rejected(self):
        root = _make_wiki()
        result = wiki.wiki_update("entities/notes.txt", "\nNope.\n", wiki_path=str(root))
        assert result.get("code") == "invalid"

    def test_changeset_recorded(self):
        root = _make_wiki()
        self._seed_page(root)
        wiki.wiki_update("entities/dflash-mlx.md", "\nTracked.\n", wiki_path=str(root))
        index = (root / "changesets" / "index.json")
        assert index.exists()
        import json
        entries = json.loads(index.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["action"] == "update"
        assert entries[0]["page"] == "entities/dflash-mlx.md"
        # The full changeset file carries the trigger.
        cs = json.loads(
            (root / "changesets" / f"{entries[0]['id']}.json").read_text(encoding="utf-8")
        )
        assert cs["trigger"] == "manual"

    def test_serialized_lists_parse_back(self):
        root = _make_wiki()
        result = wiki.wiki_update(
            "entities/lists.md",
            "\nBody.\n",
            frontmatter={
                "title": "Lists",
                "tag_path": ["a/b", "c"],
                "integration_links": ["github:org/repo#1"],
            },
            wiki_path=str(root),
        )
        assert "error" not in result
        page = wiki.wiki_page("entities/lists.md", str(root))
        assert page["frontmatter"]["tag_path"] == ["a/b", "c"]
        assert page["frontmatter"]["integration_links"] == ["github:org/repo#1"]
