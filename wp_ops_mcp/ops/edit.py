"""Edit orchestration: draft-duplicate workflow with round-trip refusal gate.

Live pages are never written by edit_page - edits land on a draft duplicate.
publish_swap is the only path that touches the live post (WP keeps a revision).
"""
from __future__ import annotations

from ..builders import divi4_parse, block_parse
from ..builders.edit_ops import apply_ops, TargetError, DynamicContentError

_DIALECTS = {
    "divi4":     (divi4_parse.parse_divi4, divi4_parse.serialize_divi4,
                  divi4_parse.roundtrip_ok, "divi4", True),
    "divi5":     (block_parse.parse_blocks, block_parse.serialize_blocks,
                  block_parse.roundtrip_ok_blocks, "blocks", True),
    "gutenberg": (block_parse.parse_blocks, block_parse.serialize_blocks,
                  block_parse.roundtrip_ok_blocks, "blocks", False),
}


class EditOps:
    def __init__(self, gateway, builder: str):
        if builder not in _DIALECTS:
            raise ValueError(f"unsupported builder for editing: {builder!r}")
        self.gw = gateway
        self.parse, self.serialize, self.rt_ok, self.dialect, self.purge = _DIALECTS[builder]

    async def edit_page(self, post_id: int, ops: list[dict], dry_run: bool = True) -> dict:
        content = await self.gw.get_post_content(post_id)
        if not self.rt_ok(content):
            return {"action": "refused",
                    "reason": "round-trip mismatch - page needs a human"}
        root = self.parse(content)
        try:
            results = apply_ops(root, ops, self.dialect)
        except (TargetError, DynamicContentError) as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

        # Review carry-over (Task 3): serialize can raise RecursionError on
        # pathologically deep trees, and any gateway call can raise RuntimeError.
        # Convert ANY unexpected exception to a fail-closed error result. If a
        # draft was already created, best-effort delete it so no orphans pile up.
        draft_id = None
        try:
            new_content = self.serialize(root)
            if dry_run:
                return {"action": "preview", "results": results,
                        "chars_before": len(content), "chars_after": len(new_content)}
            # Pre-write gate: never create a draft from content that does not
            # round-trip. Refuse before the first gateway write so a bad edit
            # leaves no orphan duplicate behind.
            if not self.rt_ok(new_content):
                return {"action": "error",
                        "error": "EditProducedInvalidContent: edited content does "
                                 "not round-trip - refusing to stage"}
            draft_id = await self.gw.duplicate_post(post_id)
            await self.gw.update_post_content(draft_id, new_content)
            if self.purge:
                await self.gw.purge_et_cache(draft_id)
            readback = await self.gw.get_post_content(draft_id)
            # Byte-equality gate: a draft that parses but does not match what we
            # wrote (silent revert, truncation, mojibake) must not be promotable.
            # Equality subsumes the old parse check. Fail closed, cleaning up.
            if readback != new_content:
                try:
                    await self.gw.delete_post(draft_id)
                except Exception:
                    pass                            # best-effort cleanup
                return {"action": "error",
                        "error": "ReadbackMismatchError: draft readback differs "
                                 "from written content"}
            staged = {"action": "staged", "draft_id": draft_id, "live_id": post_id,
                      "results": results}
            # Give the human somewhere to LOOK before publish_swap. Gateways that cannot
            # build a URL (wp-cli) simply omit the key rather than returning a null link.
            build = getattr(self.gw, "preview_url", None)
            if callable(build):
                url = build(draft_id)
                if url:
                    staged["preview_url"] = url
            return staged
        except Exception as e:
            if draft_id is not None:
                try:
                    await self.gw.delete_post(draft_id)
                except Exception:
                    pass                            # best-effort cleanup; never mask original
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def _require_wpops_draft(self, draft_id: int) -> dict | None:
        """Fail-closed guard against force-deleting/overwriting a non-draft post.

        publish_swap and discard_draft both --force operations on `draft_id`; a
        wrong id (a live published page) would be destroyed silently. Confirm the
        id is a wpops draft (name carries the '-wpops-draft-' marker AND status is
        'draft') first. Returns a refusal/error dict to short-circuit, or None to
        proceed. An info-fetch failure fails closed (error) and keeps the draft.
        """
        try:
            info = await self.gw.get_post_info(draft_id)
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}
        name = info.get("name") or ""
        status = info.get("status")
        if "-wpops-draft-" not in name or status != "draft":
            return {"action": "refused",
                    "reason": f"post {draft_id} is not a wpops draft "
                              f"(name={name!r}, status={status!r})"}
        return None

    async def publish_swap(self, live_id: int, draft_id: int) -> dict:
        guard = await self._require_wpops_draft(draft_id)
        if guard is not None:
            return guard
        # Pre-write: reading the draft or writing the live post. A failure here
        # leaves the live post untouched, so we fail closed AND keep the draft
        # (do not delete it) so the staged work survives for a retry.
        try:
            content = await self.gw.get_post_content(draft_id)
            await self.gw.update_post_content(live_id, content)
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

        # Post-write: the live change already landed. Returning "error" now would
        # be a lie, so cleanup failures are surfaced as warnings instead. Each
        # step runs independently; delete is still attempted even if purge fails.
        warnings: list[str] = []
        if self.purge:
            try:
                await self.gw.purge_et_cache(live_id)
            except Exception as e:
                warnings.append(f"{type(e).__name__}: {e}")
        try:
            await self.gw.delete_post(draft_id)
        except Exception as e:
            warnings.append(f"{type(e).__name__}: {e}")

        if warnings:
            return {"action": "published_with_warnings", "live_id": live_id,
                    "revision_kept": True, "warnings": warnings}
        return {"action": "published", "live_id": live_id, "revision_kept": True}

    async def discard_draft(self, draft_id: int) -> dict:
        guard = await self._require_wpops_draft(draft_id)
        if guard is not None:
            return guard
        try:
            await self.gw.delete_post(draft_id)
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}
        return {"action": "discarded", "draft_id": draft_id}
