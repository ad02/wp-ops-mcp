"""Content operations: read, and builder-aware create with dry-run + idempotency.

ContentOps is gateway-agnostic (the gateway does the actual WP I/O) so the planning,
rendering, dry-run and dedupe logic is unit-testable offline. Idempotency: creates are
deduped by slug (per site and within the batch), so re-running a batch never duplicates.
"""
from __future__ import annotations

import re

from ..builders.render import render_content, UnsupportedBuilderError

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    return _SLUG_STRIP.sub("-", str(title).lower()).strip("-")


def _item_slug(item: dict) -> str:
    return item.get("slug") or slugify(item.get("title", ""))


def plan_creates(items: list[dict], existing_slugs: set[str]) -> list[dict]:
    """Decide create vs skip-duplicate for each item (pure idempotency)."""
    seen = set(existing_slugs)
    plan = []
    for item in items:
        slug = _item_slug(item)
        if slug in seen:
            plan.append({"item": item, "slug": slug, "action": "skip-duplicate"})
        else:
            seen.add(slug)
            plan.append({"item": item, "slug": slug, "action": "create"})
    return plan


class ContentOps:
    def __init__(self, gateway):
        self.gw = gateway

    async def get_content(self, post_type: str, query: str | None = None) -> dict:
        posts = await self.gw.list_posts(post_type, query=query)
        items = [
            {"id": p.get("id"), "title": p.get("title"), "slug": p.get("slug"),
             "status": p.get("status"), "type": p.get("type", post_type)}
            for p in posts
        ]
        return {"count": len(items), "items": items}

    async def create_content(
        self,
        builder: str,
        divi_major: int | None,
        post_type: str,
        items: list[dict],
        dry_run: bool = True,
        status: str = "draft",
    ) -> dict:
        existing = await self.gw.list_posts(post_type)
        existing_slugs = {p.get("slug") for p in existing if p.get("slug")}
        plan = plan_creates(items, existing_slugs)

        results = []
        for entry in plan:
            item, slug, action = entry["item"], entry["slug"], entry["action"]

            if action == "skip-duplicate":
                results.append({"slug": slug, "action": "skip-duplicate" if dry_run
                                else "skipped-duplicate"})
                continue

            # Render (per-item, so one bad item doesn't abort the batch).
            try:
                rendered = render_content(item.get("blocks", []), builder, divi_major)
            except UnsupportedBuilderError as e:
                results.append({"slug": slug, "action": "error", "error": str(e)})
                continue

            if dry_run:
                results.append({
                    "slug": slug, "action": "create",
                    "preview": {"content_chars": len(rendered.content),
                                "meta_keys": sorted(rendered.meta.keys())},
                })
                continue

            try:
                post_id = await self.gw.create_post(
                    post_type=post_type, title=item.get("title", ""), slug=slug,
                    status=item.get("status", status), content=rendered.content,
                    meta=rendered.meta,
                )
                results.append({"slug": slug, "action": "created", "id": post_id})
            except Exception as e:  # gateway/WP failure — report, keep going
                results.append({"slug": slug, "action": "error", "error": str(e)})

        summary: dict[str, int] = {}
        for r in results:
            key = r["action"]
            if key in ("created", "create"):
                key = "create"
            elif key in ("skipped-duplicate", "skip-duplicate"):
                key = "skip-duplicate"
            summary[key] = summary.get(key, 0) + 1

        return {"dry_run": dry_run, "summary": summary, "items": results}
