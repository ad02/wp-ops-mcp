"""Every corpus page must either round-trip byte-identical or be explicitly refused.
Corpus is pulled by scripts/wpops_pull_corpus.py (live, read-only) into
data/wp_ops_mcp/corpus/. Empty corpus -> test skips (CI-safe)."""
import pathlib
import pytest
from wp_ops_mcp.builders.divi4_parse import roundtrip_ok
from wp_ops_mcp.builders.block_parse import roundtrip_ok_blocks

CORPUS = pathlib.Path("data/wp_ops_mcp/corpus")
files = sorted(CORPUS.glob("*.txt")) if CORPUS.exists() else []


@pytest.mark.parametrize("path", files, ids=[f.name for f in files])
def test_corpus_page_roundtrips_or_is_refused(path):
    content = path.read_text(encoding="utf-8")
    if "[et_pb_" in content:
        assert roundtrip_ok(content), f"{path.name}: divi4 round-trip MISMATCH"
    elif "<!-- wp:" in content:
        assert roundtrip_ok_blocks(content), f"{path.name}: block round-trip MISMATCH"
    else:
        pytest.skip("classic content - no builder markup")


def test_corpus_note_when_empty():
    if not files:
        pytest.skip("corpus empty - run scripts/wpops_pull_corpus.py (read-only) to populate")
