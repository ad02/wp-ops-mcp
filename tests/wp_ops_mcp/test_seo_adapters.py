# tests/wp_ops_mcp/test_seo_adapters.py
import pytest
from wp_ops_mcp.ops.seo import (
    SeoError, meta_keys_for, fields_to_meta, meta_to_fields)


def test_key_maps_per_plugin():
    assert meta_keys_for("seopress")["title"] == "_seopress_titles_title"
    assert meta_keys_for("rank-math")["description"] == "rank_math_description"
    assert meta_keys_for("yoast")["canonical"] == "_yoast_wpseo_canonical"
    # Rank Math noindex IS supported now (array-valued rank_math_robots).
    assert meta_keys_for("rank-math")["noindex"] == "rank_math_robots"
    with pytest.raises(SeoError, match="no supported SEO plugin"):
        meta_keys_for(None)


def test_fields_to_meta_encodes_noindex():
    m = fields_to_meta("seopress", {"title": "T", "noindex": True})
    assert m == {"_seopress_titles_title": "T", "_seopress_robots_index": "yes"}
    assert fields_to_meta("yoast", {"noindex": False}) == {"_yoast_wpseo_meta-robots-noindex": ""}
    # Rank Math noindex is supported now: array-valued rank_math_robots, not a raise.
    assert fields_to_meta("rank-math", {"noindex": True}) == {"rank_math_robots": ["noindex"]}
    with pytest.raises(SeoError, match="unknown SEO field"):
        fields_to_meta("seopress", {"titel": "typo"})


def test_meta_to_fields_roundtrip():
    meta = {"_seopress_titles_title": "T", "_seopress_titles_desc": "D",
            "_seopress_robots_index": "yes", "_other": "ignored"}
    f = meta_to_fields("seopress", meta)
    assert f == {"title": "T", "description": "D", "noindex": True}
    assert meta_to_fields("yoast", {"_yoast_wpseo_meta-robots-noindex": ""}) == {"noindex": False}


def test_noindex_requires_real_bool():
    # A truthy string like "no"/"false" would silently encode noindex=YES - reject it.
    for bad in ("no", 1, "false"):
        with pytest.raises(SeoError):
            fields_to_meta("seopress", {"noindex": bad})


def test_text_fields_require_str():
    # None would otherwise serialize to the literal "None"; a non-str is a caller bug.
    with pytest.raises(SeoError):
        fields_to_meta("seopress", {"title": None})
    with pytest.raises(SeoError):
        fields_to_meta("seopress", {"canonical": 123})


# --- Rank Math noindex ---------------------------------------------------------------
# Rank Math stores robots as an ARRAY (rank_math_robots), not a scalar, so noindex was
# unsupported. Fleet sites on Rank Math (clientsite3, clientsite4) could set title /
# description / canonical but never noindex.

def test_rank_math_noindex_true_writes_the_robots_array():
    from wp_ops_mcp.ops.seo import fields_to_meta
    out = fields_to_meta("rank-math", {"noindex": True})
    assert out["rank_math_robots"] == ["noindex"]


def test_rank_math_noindex_false_clears_to_inherit():
    from wp_ops_mcp.ops.seo import fields_to_meta
    out = fields_to_meta("rank-math", {"noindex": False})
    assert out["rank_math_robots"] == []


def test_rank_math_noindex_reads_back_from_the_array():
    from wp_ops_mcp.ops.seo import meta_to_fields
    assert meta_to_fields("rank-math", {"rank_math_robots": ["noindex"]})["noindex"] is True
    assert meta_to_fields("rank-math", {"rank_math_robots": ["index", "follow"]})["noindex"] is False
    assert meta_to_fields("rank-math", {"rank_math_robots": []})["noindex"] is False


def test_rank_math_noindex_still_type_checked():
    import pytest
    from wp_ops_mcp.ops.seo import fields_to_meta, SeoError
    # Rank Math noindex used to raise; it now writes the robots array.
    assert fields_to_meta("rank-math", {"noindex": True})["rank_math_robots"] == ["noindex"]


def test_other_plugins_noindex_unchanged():
    """Rank Math support must not disturb the scalar plugins."""
    from wp_ops_mcp.ops.seo import fields_to_meta
    assert fields_to_meta("seopress", {"noindex": True})["_seopress_robots_index"] == "yes"
    assert fields_to_meta("yoast", {"noindex": True})["_yoast_wpseo_meta-robots-noindex"] == "1"
    assert fields_to_meta("yoast", {"noindex": False})["_yoast_wpseo_meta-robots-noindex"] == ""
