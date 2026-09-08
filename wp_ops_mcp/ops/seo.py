"""SEO meta adapters: generic fields <-> plugin-specific post meta.

Fleet standard is SEOPress (data/standards/required-plugins.yml); Rank Math and Yoast
exist on stragglers. All mapping intelligence lives here - the site-side plugin only
reads/writes whitelisted meta keys.

Type contract (fields_to_meta): `noindex` must be a real bool - a truthy string like
"no"/"false"/"0" would silently encode noindex=YES - and title/description/canonical
must be str (a None would otherwise serialize to the literal "None").

Rank Math noindex limitation: Rank Math keeps robots directives in an ARRAY
(`rank_math_robots`), e.g. ["noindex","nofollow"]. Our boolean model writes ["noindex"]
for True and [] for False, so setting noindex=False on a page that also carried
"nofollow"/"noarchive" DROPS those directives - this layer is stateless and does not
merge with what is already stored. Read the page first if other directives matter.

Yoast noindex limitation: Yoast's `_yoast_wpseo_meta-robots-noindex` is really tri-state
("0" = inherit default, "1" = noindex, "2" = force index). Our boolean model maps
True -> "1" and False -> "" (which clears the meta back to inherit); it cannot express
the "2" force-index state.
"""
from __future__ import annotations

_KEYS = {
    "seopress": {"title": "_seopress_titles_title", "description": "_seopress_titles_desc",
                 "canonical": "_seopress_robots_canonical", "noindex": "_seopress_robots_index"},
    # rank_math_robots is ARRAY-valued, unlike every other key here - see _ARRAY_NOINDEX.
    "rank-math": {"title": "rank_math_title", "description": "rank_math_description",
                  "canonical": "rank_math_canonical_url", "noindex": "rank_math_robots"},
    # noindex tri-state limit: Yoast supports "0" inherit / "1" noindex / "2" force-index; our bool maps True->"1", False->"" (inherit), never "2".
    "yoast": {"title": "_yoast_wpseo_title", "description": "_yoast_wpseo_metadesc",
              "canonical": "_yoast_wpseo_canonical", "noindex": "_yoast_wpseo_meta-robots-noindex"},
}
_NOINDEX_TRUE = {"seopress": "yes", "yoast": "1"}
# Plugins whose noindex lives in an array of robots directives rather than a scalar.
_ARRAY_NOINDEX = {"rank-math"}
SUPPORTED = tuple(_KEYS)  # single source of truth - derived from the key maps above


class SeoError(RuntimeError):
    pass


def meta_keys_for(plugin: str | None) -> dict[str, str]:
    if plugin not in _KEYS:
        raise SeoError(f"no supported SEO plugin active (got {plugin!r}; supported: {SUPPORTED})")
    return dict(_KEYS[plugin])


def fields_to_meta(plugin: str | None, fields: dict) -> dict[str, object]:
    keys = meta_keys_for(plugin)
    out: dict[str, object] = {}
    for name, value in fields.items():
        if name == "noindex":
            if "noindex" not in keys:
                raise SeoError(f"{plugin} noindex not supported (array-valued robots meta is out of scope)")
            if not isinstance(value, bool):
                raise SeoError(f"noindex must be a bool, got {type(value).__name__}")
            if plugin in _ARRAY_NOINDEX:
                # Rank Math: ["noindex"] to hide, [] to fall back to the site default.
                out[keys["noindex"]] = ["noindex"] if value else []
            else:
                out[keys["noindex"]] = _NOINDEX_TRUE[plugin] if value else ""
        elif name in keys:
            if not isinstance(value, str):
                raise SeoError(f"{name} must be a str, got {type(value).__name__}")
            out[keys[name]] = value
        else:
            raise SeoError(f"unknown SEO field {name!r} (use title/description/canonical/noindex)")
    return out


def meta_to_fields(plugin: str | None, meta: dict) -> dict:
    keys = meta_keys_for(plugin)
    out: dict = {}
    for name, key in keys.items():
        if key in meta:
            if name == "noindex":
                if plugin in _ARRAY_NOINDEX:
                    v = meta[key]
                    out[name] = isinstance(v, (list, tuple)) and "noindex" in v
                else:
                    out[name] = meta[key] == _NOINDEX_TRUE[plugin]
            else:
                out[name] = meta[key]
    return out


class SeoOps:
    """Orchestrates SEO meta get/set over a REST content gateway.

    The gateway must expose ``ensure_seo_capable``, ``get_post_meta`` and
    ``set_post_meta`` (RestContentGateway does). The constructor does NOT probe
    capability - every method surfaces errors as ``{"action": "error", ...}`` and
    NEVER raises to the caller, so a bad plugin/write is a value the MCP tool can
    return, not an exception to unwind.

    ``set_seo`` verifies a write in two independent nets before claiming success:
      1. SeoPartialApply - the site plugin's /meta POST silently skips keys not on
         its whitelist and returns 200 with a shorter ``applied`` list; if what it
         reports applied != what we sent, the whitelist is out of sync. Caught first
         because it is a cheap, precise signal (no second round-trip needed).
      2. SeoReadbackMismatch - re-fetch the meta and confirm every written key reads
         back the intended value. An absent key equals "" (WP returns nothing for
         empty single meta), so clearing a value (empty string) verifies correctly.
    """

    def __init__(self, gateway):
        self.gw = gateway

    async def get_seo(self, post_id: int) -> dict:
        try:
            info = await self.gw.ensure_seo_capable()
            plugin = info.get("seo_plugin")
            meta_keys_for(plugin)   # validate plugin support BEFORE the meta round-trip
            meta = await self.gw.get_post_meta(post_id)
            return {"plugin": plugin, "fields": meta_to_fields(plugin, meta)}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def set_seo(self, post_id: int, fields: dict, dry_run: bool = True) -> dict:
        try:
            info = await self.gw.ensure_seo_capable()
            plugin = info.get("seo_plugin")
            meta = fields_to_meta(plugin, fields)
            if dry_run:
                return {"action": "preview", "plugin": plugin, "meta": meta}
            applied = await self.gw.set_post_meta(post_id, meta)
            # Net 1: the plugin whitelist silently drops (or adds) keys -> 200 + shorter list.
            if set(applied) != set(meta.keys()):
                return {"action": "error",
                        "error": f"SeoPartialApply: sent {sorted(meta)} applied "
                                 f"{sorted(applied)} - plugin whitelist out of sync"}
            # Net 2: read the meta back and confirm each written value landed.
            back = await self.gw.get_post_meta(post_id)
            for k, v in meta.items():
                if back.get(k, "") != v:
                    return {"action": "error",
                            "error": f"SeoReadbackMismatch: {k} wrote {v!r} read {back.get(k, '')!r}"}
            return {"action": "applied", "plugin": plugin, "applied": applied, "verified": True}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}
