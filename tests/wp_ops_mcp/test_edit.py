

# --- preview link for staged drafts --------------------------------------------------
# A staged edit returned only a draft_id, so the only way to judge the change was to
# re-read markup. A preview URL lets a human LOOK at the draft before publish_swap.

class _PreviewGateway:
    """Minimal gateway that stages successfully and exposes a preview URL."""
    def __init__(self, preview=None):
        self._preview = preview
        self.content = "<!-- wp:paragraph -->\n<p>hi</p>\n<!-- /wp:paragraph -->\n"

    async def get_post_content(self, pid):
        return self.content

    async def duplicate_post(self, pid):
        return 999

    async def update_post_content(self, pid, content):
        self.content = content

    async def purge_et_cache(self, pid):
        return None

    async def delete_post(self, pid):
        return None

    def preview_url(self, draft_id):
        return self._preview


def _noop_ops():
    return [{"op": "update_element", "target": {"contains_text": "hi"},
             "content": "<p>bye</p>"}]


def test_staged_result_includes_preview_url_when_gateway_provides_one():
    import asyncio
    from wp_ops_mcp.ops.edit import EditOps
    gw = _PreviewGateway(preview="https://s.test/?p=999&preview=true")
    r = asyncio.run(EditOps(gw, "gutenberg").edit_page(1, _noop_ops(), dry_run=False))
    assert r["action"] == "staged", r
    assert r["preview_url"] == "https://s.test/?p=999&preview=true"


def test_staged_result_omits_preview_url_when_gateway_has_none():
    """A gateway that cannot build a URL must not produce a null/broken link."""
    import asyncio
    from wp_ops_mcp.ops.edit import EditOps
    gw = _PreviewGateway(preview=None)
    r = asyncio.run(EditOps(gw, "gutenberg").edit_page(1, _noop_ops(), dry_run=False))
    assert r["action"] == "staged", r
    assert "preview_url" not in r


def test_rest_gateway_builds_preview_url_from_base_url():
    from wp_ops_mcp.ops.rest_gateway import RestContentGateway
    from wp_ops_mcp.transport.rest import WPRestClient
    gw = RestContentGateway(WPRestClient("https://site.test/", app_user="u", app_password="p"))
    assert gw.preview_url(42) == "https://site.test/?p=42&preview=true"
