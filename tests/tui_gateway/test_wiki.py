import tempfile
from pathlib import Path

import pytest

from tui_gateway import wiki


@pytest.fixture
def tmp_wiki():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "entities").mkdir()
        (root / "concepts").mkdir()
        yield root


class TestScan:
    def test_empty_dir(self, tmp_wiki):
        # No markdown files yet
        result = wiki.scan(str(tmp_wiki))
        assert result["pages"] == []
        assert result["links"] == []

    def test_missing_dir(self):
        result = wiki.scan("/nonexistent/wiki/path")
        assert result == {"pages": [], "links": []}

    def test_scan_entities_and_concepts(self, tmp_wiki):
        (tmp_wiki / "entities" / "dflash-mlx.md").write_text(
            "---\ntitle: dflash-mlx\ntype: entity\ntags: [optimization]\n---\n\nBody here. [[speculative-decoding]]\n",
            encoding="utf-8",
        )
        (tmp_wiki / "concepts" / "speculative-decoding.md").write_text(
            "---\ntitle: Speculative Decoding\ntype: concept\n---\n\nConcept body.\n",
            encoding="utf-8",
        )

        result = wiki.scan(str(tmp_wiki))
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

    def test_no_frontmatter(self, tmp_wiki):
        (tmp_wiki / "entities" / "plain.md").write_text(
            "No frontmatter. [[other]]", encoding="utf-8"
        )
        result = wiki.scan(str(tmp_wiki))
        assert len(result["pages"]) == 1
        assert result["pages"][0]["title"] == "plain"
        assert result["pages"][0]["type"] == "page"
        assert result["pages"][0]["tags"] == []


class TestPage:
    def test_read_page(self, tmp_wiki):
        (tmp_wiki / "entities" / "swiftlm.md").write_text(
            "---\ntitle: SwiftLM\ntype: entity\n---\n\nBenchmark suite.\n",
            encoding="utf-8",
        )
        result = wiki.page("entities/swiftlm.md", str(tmp_wiki))
        assert result is not None
        assert result["id"] == "swiftlm"
        assert result["frontmatter"]["title"] == "SwiftLM"
        assert result["body"].strip() == "Benchmark suite."

    def test_page_not_found(self, tmp_wiki):
        assert wiki.page("entities/missing.md", str(tmp_wiki)) is None

    def test_path_traversal_blocked(self, tmp_wiki):
        # Try to escape the wiki root
        assert wiki.page("../outside.md", str(tmp_wiki)) is None

    def test_absolute_path_rejected(self, tmp_wiki):
        assert wiki.page("/etc/passwd", str(tmp_wiki)) is None


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
