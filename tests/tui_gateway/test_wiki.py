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
