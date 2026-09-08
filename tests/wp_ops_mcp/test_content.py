"""Tests for content ops: slugify, idempotency planning, get/create orchestration."""
import pytest

from wp_ops_mcp.ops.content import slugify, plan_creates, ContentOps


class TestSlugify:
    @pytest.mark.parametrize("title,expected", [
        ("Our Services", "our-services"),
        ("A & B!", "a-b"),
        ("  Trim  Me  ", "trim-me"),
        ("Already-Slug", "already-slug"),
        ("Multiple   spaces", "multiple-spaces"),
    ])
    def test_slugify(self, title, expected):
        assert slugify(title) == expected


class TestPlanCreates:
    def test_uses_explicit_slug(self):
        plan = plan_creates([{"title": "X", "slug": "custom"}], existing_slugs=set())
        assert plan[0]["slug"] == "custom"
        assert plan[0]["action"] == "create"

    def test_derives_slug_from_title(self):
        plan = plan_creates([{"title": "Hello World"}], existing_slugs=set())
        assert plan[0]["slug"] == "hello-world"

    def test_existing_slug_is_skip_duplicate(self):
        plan = plan_creates([{"title": "Home"}], existing_slugs={"home"})
        assert plan[0]["action"] == "skip-duplicate"

    def test_intra_batch_duplicate_skipped(self):
        plan = plan_creates(
            [{"title": "Dup"}, {"title": "Dup"}], existing_slugs=set()
        )
        assert plan[0]["action"] == "create"
        assert plan[1]["action"] == "skip-duplicate"


class FakeGateway:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.created = []
        self._next_id = 1000

    async def list_posts(self, post_type, query=None):
        return list(self.existing)

    async def create_post(self, post_type, title, slug, status, content, meta):
        self._next_id += 1
        self.created.append({"post_type": post_type, "title": title, "slug": slug,
                             "status": status, "content": content, "meta": meta,
                             "id": self._next_id})
        return self._next_id


PAGE = {"title": "Our Services", "type": "page",
        "blocks": [{"kind": "heading", "text": "Services"},
                   {"kind": "paragraph", "text": "We help."}]}


class TestGetContent:
    async def test_returns_lean_list(self):
        gw = FakeGateway(existing=[
            {"id": 1, "title": "Home", "slug": "home", "status": "publish", "type": "page"},
        ])
        ops = ContentOps(gw)
        out = await ops.get_content("page")
        assert out["count"] == 1
        assert out["items"][0]["slug"] == "home"


class TestCreateContentDryRun:
    async def test_dry_run_does_not_create(self):
        gw = FakeGateway()
        ops = ContentOps(gw)
        res = await ops.create_content("divi", 4, "page", [PAGE], dry_run=True)
        assert res["dry_run"] is True
        assert gw.created == []
        assert res["items"][0]["action"] == "create"
        # preview surfaces that something would be written, token-lean
        assert res["items"][0]["preview"]["content_chars"] > 0

    async def test_dry_run_flags_duplicate(self):
        gw = FakeGateway(existing=[{"id": 1, "slug": "our-services", "title": "Our Services",
                                    "status": "publish", "type": "page"}])
        ops = ContentOps(gw)
        res = await ops.create_content("divi", 4, "page", [PAGE], dry_run=True)
        assert res["items"][0]["action"] == "skip-duplicate"
        assert res["summary"]["skip-duplicate"] == 1


class TestCreateContentApply:
    async def test_creates_and_returns_id(self):
        gw = FakeGateway()
        ops = ContentOps(gw)
        res = await ops.create_content("divi", 4, "page", [PAGE], dry_run=False)
        assert res["dry_run"] is False
        assert len(gw.created) == 1
        assert res["items"][0]["action"] == "created"
        assert res["items"][0]["id"] == gw.created[0]["id"]

    async def test_divi4_passes_builder_meta_on_apply(self):
        gw = FakeGateway()
        ops = ContentOps(gw)
        await ops.create_content("divi", 4, "page", [PAGE], dry_run=False)
        meta = gw.created[0]["meta"]
        assert meta.get("_et_pb_use_builder") == "on"
        # content rendered as shortcodes
        assert gw.created[0]["content"].startswith("[et_pb_section")

    async def test_gutenberg_empty_meta(self):
        gw = FakeGateway()
        ops = ContentOps(gw)
        await ops.create_content("gutenberg", None, "page", [PAGE], dry_run=False)
        assert gw.created[0]["meta"] == {}
        assert "<!-- wp:heading" in gw.created[0]["content"]

    async def test_idempotent_skip_existing(self):
        gw = FakeGateway(existing=[{"id": 1, "slug": "our-services", "title": "Our Services",
                                    "status": "publish", "type": "page"}])
        ops = ContentOps(gw)
        res = await ops.create_content("divi", 4, "page", [PAGE], dry_run=False)
        assert gw.created == []
        assert res["items"][0]["action"] == "skipped-duplicate"

    async def test_unsupported_builder_is_per_item_error_not_crash(self):
        gw = FakeGateway()
        ops = ContentOps(gw)
        res = await ops.create_content("divi", 5, "page", [PAGE], dry_run=False)
        assert res["items"][0]["action"] == "error"
        assert "5" in res["items"][0]["error"]
        assert gw.created == []
