import asyncio
import pytest
from wp_ops_mcp.ops.edit import EditOps

LIVE = ('[et_pb_section][et_pb_row][et_pb_column type="4_4"]'
        '[et_pb_text admin_label="Body"]<p>old</p>[/et_pb_text]'
        '[/et_pb_column][/et_pb_row][/et_pb_section]')


class FakeGateway:
    def __init__(self, content=LIVE, fail_update=False, fail_delete=False,
                 mangle_readback=False):
        self.posts = {10: content}
        self.purged, self.deleted = [], []
        self._next = 100
        self._orig = content
        self.fail_update = fail_update
        self.fail_delete = fail_delete
        self.mangle_readback = mangle_readback
        self._get_calls = 0
        self._drafts = set()

    async def get_post_content(self, post_id):
        self._get_calls += 1
        # mangle_readback simulates a write that silently reverted: the draft
        # readback (2nd get) returns the ORIGINAL live content, not what we wrote.
        if self.mangle_readback and self._get_calls >= 2:
            return self._orig
        return self.posts[post_id]

    async def duplicate_post(self, post_id):
        self._next += 1
        self.posts[self._next] = self.posts[post_id]
        self._drafts.add(self._next)
        return self._next

    async def update_post_content(self, post_id, content):
        if self.fail_update:
            raise RuntimeError("wp-cli exploded mid-write")
        self.posts[post_id] = content
        return post_id

    async def delete_post(self, post_id):
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(post_id)
        self.posts.pop(post_id, None)

    async def purge_et_cache(self, post_id):
        self.purged.append(post_id)

    async def get_post_info(self, post_id):
        # Drafts this gateway created (via duplicate_post) look like wpops drafts;
        # anything else (e.g. the live page 10) reports as a normal published post.
        if post_id in self._drafts:
            return {"name": f"page-wpops-draft-{post_id}", "status": "draft"}
        return {"name": "home", "status": "publish"}


UPDATE = [{"op": "update_element", "target": {"admin_label": "Body"},
           "content": "<p>new</p>"}]


def test_dry_run_previews_without_writes():
    gw = FakeGateway()
    r = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=True))
    assert r["action"] == "preview"
    assert gw.posts[10] == LIVE                      # nothing written


def test_staged_edit_lands_on_draft_not_live():
    gw = FakeGateway()
    r = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    assert r["action"] == "staged"
    draft = r["draft_id"]
    assert "<p>new</p>" in gw.posts[draft]
    assert gw.posts[10] == LIVE                      # live untouched
    assert draft in gw.purged                        # divi cache purged on draft


def test_refuses_page_that_does_not_roundtrip():
    gw = FakeGateway(content='[et_pb_section][et_pb_row][/et_pb_section]')
    r = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    assert r["action"] == "refused"
    assert 10 in gw.posts and len(gw.posts) == 1     # no draft created


def test_publish_swap_promotes_and_cleans_up():
    gw = FakeGateway()
    staged = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    r = asyncio.run(EditOps(gw, "divi4").publish_swap(10, staged["draft_id"]))
    assert r["action"] == "published"
    assert "<p>new</p>" in gw.posts[10]
    assert staged["draft_id"] in gw.deleted
    assert 10 in gw.purged


def test_gutenberg_dialect_and_no_cache_purge():
    gb = ('<!-- wp:paragraph -->\n<p>old</p>\n<!-- /wp:paragraph -->')
    gw = FakeGateway(content=gb)
    ops = [{"op": "update_element", "target": {"tag": "core/paragraph"},
            "content": "\n<p>new</p>\n"}]
    r = asyncio.run(EditOps(gw, "gutenberg").edit_page(10, ops, dry_run=False))
    assert r["action"] == "staged"
    assert "<p>new</p>" in gw.posts[r["draft_id"]]
    assert gw.purged == []                           # no et-cache on gutenberg


# --- Review carry-over (Task 3): fail-closed on unexpected exceptions ---
# serialize can raise RecursionError; gateway calls can raise RuntimeError.
# Any unexpected exception must convert to {"action":"error"} and, if a draft
# was already created, that draft must be best-effort delete_post'd so no
# orphan drafts accumulate.

class BoomOnUpdateGateway(FakeGateway):
    async def update_post_content(self, post_id, content):
        raise RuntimeError("wp-cli exploded mid-write")


def test_unexpected_gateway_error_fails_closed_and_cleans_up_draft():
    gw = BoomOnUpdateGateway()
    r = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    assert r["action"] == "error"
    assert "RuntimeError" in r["error"]
    # a draft was created before the write blew up; it must be cleaned up
    assert gw.deleted != []
    assert gw.posts[10] == LIVE                      # live never touched


# --- Readback equality gate: a parseable-but-wrong draft must fail closed ---

def test_readback_mismatch_fails_closed_and_cleans_up():
    gw = FakeGateway(mangle_readback=True)
    r = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    assert r["action"] == "error"
    assert "ReadbackMismatch" in r["error"]
    assert gw.deleted != []                          # bad draft cleaned up


# --- publish_swap: honest fail-open vs fail-closed semantics ---

def test_publish_swap_prewrite_failure_keeps_draft():
    gw = FakeGateway()
    staged = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    draft = staged["draft_id"]
    gw.fail_update = True                            # live write will raise
    r = asyncio.run(EditOps(gw, "divi4").publish_swap(10, draft))
    assert r["action"] == "error"
    assert draft in gw.posts                         # staged work survives for retry
    assert draft not in gw.deleted
    assert gw.posts[10] == LIVE                      # live untouched


def test_publish_swap_postwrite_failure_reports_published_with_warnings():
    gw = FakeGateway()
    staged = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    draft = staged["draft_id"]
    gw.fail_delete = True                            # cleanup after live write raises
    r = asyncio.run(EditOps(gw, "divi4").publish_swap(10, draft))
    assert r["action"] == "published_with_warnings"
    assert "<p>new</p>" in gw.posts[10]              # live IS the draft content
    assert r["warnings"]                             # failure surfaced, not swallowed


def test_discard_draft_failure_returns_error():
    gw = FakeGateway(fail_delete=True)
    staged = asyncio.run(EditOps(gw, "divi4").edit_page(10, UPDATE, dry_run=False))
    r = asyncio.run(EditOps(gw, "divi4").discard_draft(staged["draft_id"]))
    assert r["action"] == "error"


# --- Draft-marker guard (final review): never force-delete/overwrite a post
# that is not a wpops draft. publish_swap and discard_draft must fail closed
# when the target id points at a normal (published) page. ---

def test_publish_swap_refuses_when_draft_id_is_normal_page():
    gw = FakeGateway()
    # 10 is the live published page, not a wpops draft -> must refuse pre-write.
    r = asyncio.run(EditOps(gw, "divi4").publish_swap(5, 10))
    assert r["action"] == "refused"
    assert "not a wpops draft" in r["reason"]
    assert gw.posts[10] == LIVE                       # live untouched
    assert 10 not in gw.deleted


def test_discard_refuses_when_draft_id_is_normal_page():
    gw = FakeGateway()
    r = asyncio.run(EditOps(gw, "divi4").discard_draft(10))
    assert r["action"] == "refused"
    assert "not a wpops draft" in r["reason"]
    assert 10 in gw.posts and 10 not in gw.deleted    # nothing deleted


# --- Pre-write round-trip gate (final review): if an edit serializes to content
# that does not round-trip, refuse BEFORE creating any draft (no orphan). ---

def test_edit_refuses_content_that_breaks_roundtrip():
    gw = FakeGateway()
    ops = [{"op": "update_element", "target": {"admin_label": "Body"},
            "content": "[et_pb_text]broken"}]     # unbalanced et_pb lookalike
    r = asyncio.run(EditOps(gw, "divi4").edit_page(10, ops, dry_run=False))
    assert r["action"] == "error"
    assert "EditProducedInvalidContent" in r["error"]
    assert gw.deleted == []                           # no draft created...
    assert len(gw.posts) == 1                         # ...no duplicate written
