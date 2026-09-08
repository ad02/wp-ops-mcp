"""Taxonomy + term operations: list taxonomies, list/create/update/delete terms, and
assign a post's terms for one taxonomy.

Terms are the site's own filing system - categories, tags and every CPT taxonomy - and
three WordPress behaviours make a naive wrapper report success it did not earn:

  1. **A term delete is permanent.** Terms have no trash state; WP's REST route
     requires ``force`` and answers by unassigning the term from every post that had
     it. The preview therefore states that outcome in words, because there is nothing
     to restore afterwards.
  2. **An assignment is a SET, not a list.** ``set_post_terms`` writes the taxonomy's
     field on the POST object, and WP answers with the ids it actually stored - in ITS
     order, and having silently dropped any id the taxonomy does not own. Comparing
     order-sensitively would fail every landed multi-term write; comparing nothing at
     all would pass a half-lost one. So the check is ``set(stored) == set(requested)``.
  3. **The worst case of (2) is silent.** Assigning a taxonomy that is not registered
     for the post's type (a category on a `page`, a CPT taxonomy on a `post`) is a 200
     with the field absent -> ``terms: []``. That reads as a successful write of
     nothing, so it comes back here as a ``TermAssignmentMismatch`` error carrying
     ``write_landed`` - the POST did reach the site, and an operator needs to know that
     even though no term stuck.

``create_term`` is verified by reading the term back, like MenuOps.add: a 201 is the
site's intent, not proof. The readback is a bounded SEARCH rather than an unfiltered
listing - a taxonomy with more terms than one page would otherwise fail verification
for every create.

Like SeoOps/MenuOps/AdminOps, every public method NEVER raises: anticipated failures
and unanticipated ones alike come back as
``{"action": "error", "error": "<Type>: <msg>"}`` so an MCP tool returns a value
instead of unwinding.
"""
from __future__ import annotations


class TermError(RuntimeError):
    """Refused term call (blank taxonomy/name, bad id, empty fields, bad term_ids)."""


def _error(e: Exception, **extra) -> dict:
    return {"action": "error", "error": f"{type(e).__name__}: {e}", **extra}


def _positive_id(value, field: str) -> int:
    """A WordPress object id: a genuine positive int.

    ``bool`` is excluded explicitly - it subclasses int, so ``True`` would otherwise
    pass as id 1 and delete a real term. Same rule (and message shape) as
    MenuOps/AdminOps.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TermError(f"{field} must be a positive int, got {value!r}")
    return value


def _text(value, field: str) -> str:
    """A required string argument: non-empty and not whitespace-only."""
    if not isinstance(value, str) or not value.strip():
        raise TermError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _parent(value):
    """A term's parent: None (omit) or a NON-negative int - 0 is WP's "top level"."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TermError("parent must be a non-negative int (0 = top level), "
                        f"got {value!r}")
    return value


def _term_ids(value) -> list[int]:
    """The ids to assign: a list of genuine positive ints.

    One message for both failure shapes (not a list / a bad element) because both are
    the same caller mistake. An EMPTY list is legal and means "clear this taxonomy on
    that post" - a real operation, not an accident.
    """
    if not isinstance(value, (list, tuple)):
        raise TermError(f"term_ids must be a list of positive ints, got {value!r}")
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise TermError(f"term_ids must be a list of positive ints, got {value!r}")
    return [int(v) for v in value]


def _id_set(values) -> set:
    """Ids as a comparison-ready set. Anything uncastable is kept as its repr so it can
    never accidentally compare equal to a requested id (a false verify is the one
    failure mode an operator cannot detect afterwards)."""
    out = set()
    for v in values or []:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            out.add(repr(v))
    return out


class TermOps:
    """Orchestrates taxonomy/term reads and writes over a REST content gateway.

    The gateway must expose ``list_taxonomies`` / ``list_terms`` / ``create_term`` /
    ``update_term`` / ``delete_term`` / ``set_post_terms`` (RestContentGateway does;
    the SSH/wpcli gateway does not - the tool layer refuses that transport before
    getting here). Every term route and post-object field name is resolved from the
    taxonomy's ``rest_base`` inside the gateway, so nothing here builds a URL.
    """

    def __init__(self, gateway):
        self.gw = gateway

    # -- reads ----------------------------------------------------------------
    async def list_taxonomies(self) -> dict:
        """Every REST-enabled taxonomy (slug, name, rest_base, hierarchical, types).

        Read-only, one call. Start here: the ``slug`` it returns is what every other
        method takes as ``taxonomy``.
        """
        try:
            rows = await self.gw.list_taxonomies()
            return {"action": "ok", "count": len(rows), "taxonomies": rows}
        except Exception as e:
            return _error(e)

    async def list_terms(self, taxonomy: str, search: str | None = None) -> dict:
        """The terms in one taxonomy (id, name, slug, parent, count). Read-only.

        ``parent`` is None on a flat taxonomy (tags) - that is "no hierarchy", not
        "top level", which WP reports as 0.
        """
        try:
            tax = _text(taxonomy, "taxonomy")
            terms = await self.gw.list_terms(tax, search=search)
            return {"action": "ok", "taxonomy": tax, "count": len(terms),
                    "terms": terms}
        except Exception as e:
            return _error(e)

    # -- writes ---------------------------------------------------------------
    async def _term_present(self, taxonomy: str, term_id, search: str) -> bool:
        """Is ``term_id`` really in the taxonomy? Bounded search first, listing second.

        The search keeps the readback cheap on a taxonomy with hundreds of terms; the
        unfiltered listing is the tiebreaker for the names WP rewrites on save, so a
        landed create is never reported as a failure just because the term's stored
        name no longer matches what was asked for.
        """
        rows = await self.gw.list_terms(taxonomy, search=search)
        if any(t.get("id") == term_id for t in rows):
            return True
        return any(t.get("id") == term_id
                   for t in await self.gw.list_terms(taxonomy))


    async def create_term(self, taxonomy: str, name: str, slug: str | None = None,
                          parent: int | None = None, description: str | None = None,
                          dry_run: bool = True) -> dict:
        """Create one term, verified by reading it back.

        Everything is validated BEFORE the gateway, in dry-run and for real alike, so
        a preview cannot promise a write that would fail. ``parent`` only means
        anything on a hierarchical taxonomy; WP ignores it on tags.
        """
        try:
            tax = _text(taxonomy, "taxonomy")
            _text(name, "name")
            parent_id = _parent(parent)
            if dry_run:
                return {"action": "preview", "taxonomy": tax,
                        "term": {"name": name, "slug": slug, "parent": parent_id,
                                 "description": description}}
            term = await self.gw.create_term(tax, name, slug=slug, parent=parent_id,
                                             description=description)
            # Readback: a 201 is the site's intent, not proof (a plugin can filter a
            # term out on save). Searched by name first, not listed wholesale - see
            # module docstring. A search MISS is not yet a verdict: WP rewrites some
            # names on save ("R&D" -> "R&amp;D"), which would make the search miss a
            # term that is really there, so the miss falls back to a plain listing
            # before failing. Two calls only on the rare miss path.
            if not await self._term_present(tax, term.get("id"), search=name):
                return {"action": "error",
                        "error": f"TermReadbackMissing: term {term.get('id')} is not "
                                 f"in taxonomy {tax} after the create"}
            return {"action": "created", "term": term, "verified": True}
        except Exception as e:
            return _error(e)

    async def update_term(self, taxonomy: str, term_id: int, fields: dict,
                          dry_run: bool = True) -> dict:
        """Change an existing term's name/slug/parent/description.

        An empty ``fields`` is refused rather than sent: WP answers an empty POST with
        200 and the unchanged term, which would read as a successful edit that never
        happened. A key the gateway does not allow raises there and surfaces here as
        an error dict. No readback is needed - WP's 200 response IS the stored row.
        """
        try:
            tax = _text(taxonomy, "taxonomy")
            _positive_id(term_id, "term_id")
            if not isinstance(fields, dict) or not fields:
                raise TermError("no term fields provided (pass an object of field -> "
                                "value: name, slug, parent, description)")
            clean = dict(fields)
            if dry_run:
                return {"action": "preview", "taxonomy": tax, "term_id": term_id,
                        "fields": clean}
            return {"action": "updated",
                    "term": await self.gw.update_term(tax, term_id, clean)}
        except Exception as e:
            return _error(e)

    async def delete_term(self, taxonomy: str, term_id: int,
                          dry_run: bool = True) -> dict:
        """PERMANENTLY delete a term and unassign it from every post that had it.

        There is no recoverable form of this call: terms have no trash state and WP's
        route requires force. The preview says so in words, because the posts keep
        their content and quietly lose their filing - which is invisible until someone
        looks for the archive page that no longer exists.
        """
        try:
            tax = _text(taxonomy, "taxonomy")
            _positive_id(term_id, "term_id")
            if dry_run:
                return {"action": "preview", "taxonomy": tax, "term_id": term_id,
                        "effect": f"term {term_id} will be permanently deleted from "
                                  f"{tax} and unassigned from every post that has it "
                                  f"(terms have no trash - this cannot be undone)"}
            result = await self.gw.delete_term(tax, term_id)
            return {"action": "deleted", "taxonomy": tax, "term_id": term_id,
                    "previous": result.get("term")}
        except Exception as e:
            return _error(e)

    async def set_post_terms(self, post_id: int, taxonomy: str, term_ids: list,
                             dry_run: bool = True) -> dict:
        """Set a post's terms for one taxonomy - REPLACES, never appends.

        ``term_ids`` is the complete list the post should end up with; an empty list
        clears the taxonomy on that post. The write is verified as a set against what
        the site says it stored (rules 2 and 3 in the module docstring): a reorder is
        a success, a dropped or extra id is a ``TermAssignmentMismatch`` error carrying
        ``write_landed: True`` - the POST reached the site, so an operator cannot be
        told "nothing happened".
        """
        try:
            _positive_id(post_id, "post_id")
            tax = _text(taxonomy, "taxonomy")
            ids = _term_ids(term_ids)
            if dry_run:
                return {"action": "preview", "post_id": post_id, "taxonomy": tax,
                        "term_ids": ids}
            result = await self.gw.set_post_terms(post_id, tax, ids)
            stored = result.get("terms") or []
            if _id_set(stored) == set(ids):
                return {"action": "applied", "post_id": post_id, "taxonomy": tax,
                        "terms": stored, "verified": True}
            return {"action": "error",
                    "error": f"TermAssignmentMismatch: requested {sorted(set(ids))} "
                             f"stored {stored} (taxonomy {tax!r} may not be registered "
                             f"for this post type, or those term ids belong to another "
                             f"taxonomy)",
                    "write_landed": True}
        except Exception as e:
            return _error(e)
