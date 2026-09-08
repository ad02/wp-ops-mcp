"""Unit tests for TermOps (ops/terms.py): taxonomy/term listing, term CRUD and
post<->term assignment.

Fakes at the gateway boundary (RestContentGateway's taxonomy surface, Task 2) so the
tests pin the disciplines that keep a term edit honest:

  1. input is validated BEFORE the gateway - an empty taxonomy, a non-positive term id
     or a malformed term_ids list must not reach the wire at all (the fake is checked
     for "no calls", not just "no writes");
  2. a create is confirmed by READING THE TERM BACK - a 201 is the site's intent, not
     proof;
  3. an assignment is verified as a SET. WordPress reorders the ids it stores AND
     silently drops ids the taxonomy does not own, so order must not fail a landed
     write and a dropped id must not pass as one. The pathological case is a taxonomy
     that is not registered for the post's type: WP answers 200 with the field absent
     (terms: []), which reads as success and is exactly the mismatch->error path.
"""
from wp_ops_mcp.ops.terms import TermOps

TAXONOMIES = [
    {"slug": "category", "name": "Categories", "rest_base": "categories",
     "hierarchical": True, "types": ["post"]},
    {"slug": "post_tag", "name": "Tags", "rest_base": "tags",
     "hierarchical": False, "types": ["post"]},
]

TERMS = {
    "category": [
        {"id": 3, "name": "News", "slug": "news", "parent": 0, "count": 4,
         "taxonomy": "category"},
        {"id": 7, "name": "Updates", "slug": "updates", "parent": 3, "count": 1,
         "taxonomy": "category"},
    ],
    "post_tag": [
        {"id": 11, "name": "botox", "slug": "botox", "parent": None, "count": 2,
         "taxonomy": "post_tag"},
    ],
}


class FakeTermGateway:
    """REST-shaped taxonomy gateway that records every call that reached the wire.

    `calls` is the assertion surface for "never reached the gateway" claims;
    `creates`/`updates`/`deletes`/`assignments` record the write arguments.
    `stored` overrides what set_post_terms reports WP kept (reorders, drops, the
    empty list a wrong post type produces).
    """

    def __init__(self, taxonomies=None, terms=None, error=None,
                 readback_miss=False, stored=None, search_blind=False):
        self.taxonomies = [dict(t) for t in (TAXONOMIES if taxonomies is None
                                             else taxonomies)]
        self.terms = {k: [dict(t) for t in v]
                      for k, v in (TERMS if terms is None else terms).items()}
        self.error = error
        self.readback_miss = readback_miss     # create succeeds, term never lands
        self.stored = stored
        self.search_blind = search_blind       # WP rewrote the name: search finds it not
        self.calls = []
        self.creates = []
        self.updates = []
        self.deletes = []
        self.assignments = []
        self.new_id = 99

    def _boom(self):
        if self.error is not None:
            raise self.error

    async def list_taxonomies(self):
        self.calls.append("list_taxonomies")
        self._boom()
        return [dict(t) for t in self.taxonomies]

    async def list_terms(self, taxonomy, search=None, per_page=100):
        self.calls.append(("list_terms", taxonomy, search))
        self._boom()
        rows = [dict(t) for t in self.terms.get(taxonomy, [])]
        if search:
            if self.search_blind:
                return []
            rows = [r for r in rows if search.lower() in str(r["name"]).lower()]
        return rows

    async def create_term(self, taxonomy, name, slug=None, parent=None,
                          description=None):
        self.calls.append("create_term")
        self._boom()
        self.creates.append({"taxonomy": taxonomy, "name": name, "slug": slug,
                             "parent": parent, "description": description})
        term = {"id": self.new_id, "name": name, "slug": slug or name.lower(),
                "parent": parent if parent is not None else 0, "count": 0,
                "taxonomy": taxonomy}
        if not self.readback_miss:
            self.terms.setdefault(taxonomy, []).append(dict(term))
        return term

    async def update_term(self, taxonomy, term_id, fields):
        self.calls.append("update_term")
        self._boom()
        self.updates.append({"taxonomy": taxonomy, "term_id": term_id,
                             "fields": dict(fields)})
        row = {"id": term_id, "name": "News", "slug": "news", "parent": 0,
               "count": 4, "taxonomy": taxonomy}
        row.update(fields)                      # mapped row reflects the change
        return row

    async def delete_term(self, taxonomy, term_id):
        self.calls.append("delete_term")
        self._boom()
        self.deletes.append({"taxonomy": taxonomy, "term_id": term_id})
        return {"deleted": True, "term": term_id}

    async def set_post_terms(self, post_id, taxonomy, term_ids):
        self.calls.append("set_post_terms")
        self._boom()
        self.assignments.append({"post_id": post_id, "taxonomy": taxonomy,
                                 "term_ids": list(term_ids)})
        kept = list(term_ids) if self.stored is None else list(self.stored)
        return {"post_id": post_id, "taxonomy": taxonomy, "terms": kept}


# --- list_taxonomies --------------------------------------------------------

async def test_list_taxonomies_returns_rows():
    gw = FakeTermGateway()
    out = await TermOps(gw).list_taxonomies()
    assert out["action"] == "ok"
    assert out["taxonomies"] == TAXONOMIES
    assert gw.calls == ["list_taxonomies"]


async def test_list_taxonomies_gateway_failure_is_an_error_dict():
    out = await TermOps(FakeTermGateway(error=RuntimeError("boom"))).list_taxonomies()
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- list_terms -------------------------------------------------------------

async def test_list_terms_returns_rows_and_echoes_the_taxonomy():
    gw = FakeTermGateway()
    out = await TermOps(gw).list_terms("category")
    assert out["action"] == "ok" and out["taxonomy"] == "category"
    assert [t["id"] for t in out["terms"]] == [3, 7]


async def test_list_terms_passes_search_through():
    gw = FakeTermGateway()
    out = await TermOps(gw).list_terms("category", search="upd")
    assert gw.calls == [("list_terms", "category", "upd")]
    assert [t["id"] for t in out["terms"]] == [7]


async def test_list_terms_rejects_an_empty_taxonomy_before_the_gateway():
    for bad in ("", "   ", None, 3, True):
        gw = FakeTermGateway()
        out = await TermOps(gw).list_terms(bad)
        assert out["action"] == "error", (bad, out)
        assert "taxonomy must be a non-empty string" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_list_terms_gateway_failure_is_an_error_dict():
    gw = FakeTermGateway(error=RuntimeError("boom"))
    out = await TermOps(gw).list_terms("category")
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- create_term ------------------------------------------------------------

async def test_create_term_dry_run_previews_and_writes_nothing():
    gw = FakeTermGateway()
    out = await TermOps(gw).create_term("category", "Skin Care", slug="skin-care",
                                        parent=3, description="d")
    assert out == {"action": "preview", "taxonomy": "category",
                   "term": {"name": "Skin Care", "slug": "skin-care",
                            "parent": 3, "description": "d"}}
    assert gw.creates == [] and gw.calls == []


async def test_create_term_validates_the_name_in_dry_run_too():
    """A preview that would fail for real is a lie; dry-run refuses identically."""
    for bad in ("", "   ", None, 12):
        gw = FakeTermGateway()
        out = await TermOps(gw).create_term("category", bad, dry_run=True)
        assert out["action"] == "error", (bad, out)
        assert "name must be a non-empty string" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_create_term_rejects_a_bad_parent_before_the_gateway():
    for bad in (-1, "3", True, 1.5):
        gw = FakeTermGateway()
        out = await TermOps(gw).create_term("category", "Skin", parent=bad,
                                            dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "parent must be a non-negative int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_create_term_real_run_passes_every_field_through():
    gw = FakeTermGateway()
    out = await TermOps(gw).create_term("category", "Skin Care", slug="skin-care",
                                        parent=3, description="d", dry_run=False)
    assert out["action"] == "created" and out["verified"] is True
    assert out["term"]["id"] == 99 and out["term"]["name"] == "Skin Care"
    assert gw.creates == [{"taxonomy": "category", "name": "Skin Care",
                           "slug": "skin-care", "parent": 3, "description": "d"}]


async def test_create_term_verifies_by_reading_the_term_back():
    gw = FakeTermGateway()
    await TermOps(gw).create_term("category", "Skin Care", dry_run=False)
    # readback is a bounded search, not an unfiltered list: a taxonomy with more
    # terms than one page would otherwise fail verification for every create.
    assert ("list_terms", "category", "Skin Care") in gw.calls


async def test_create_term_readback_falls_back_to_a_plain_listing():
    """WP rewrites some names on save ("R&D" -> "R&amp;D"), so the search can miss a
    term that really landed. A search miss must not be the verdict on its own."""
    gw = FakeTermGateway(search_blind=True)
    out = await TermOps(gw).create_term("category", "R&D", dry_run=False)
    assert out["action"] == "created" and out["verified"] is True
    assert ("list_terms", "category", "R&D") in gw.calls     # searched first
    assert ("list_terms", "category", None) in gw.calls      # then listed


async def test_create_term_readback_miss_is_an_error_naming_the_term():
    """Search AND listing both miss: the create genuinely did not land."""
    gw = FakeTermGateway(readback_miss=True)
    out = await TermOps(gw).create_term("category", "Skin Care", dry_run=False)
    assert out["action"] == "error"
    assert "99" in out["error"] and "category" in out["error"]
    assert gw.calls.count(("list_terms", "category", None)) == 1   # fallback tried once


async def test_create_term_gateway_failure_is_an_error_dict():
    gw = FakeTermGateway(error=RuntimeError("boom"))
    out = await TermOps(gw).create_term("category", "Skin Care", dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- update_term ------------------------------------------------------------

async def test_update_term_dry_run_previews_the_exact_fields():
    gw = FakeTermGateway()
    out = await TermOps(gw).update_term("category", 3, {"name": "Newsroom"})
    assert out == {"action": "preview", "taxonomy": "category", "term_id": 3,
                   "fields": {"name": "Newsroom"}}
    assert gw.calls == []


async def test_update_term_real_run_returns_the_stored_row():
    gw = FakeTermGateway()
    out = await TermOps(gw).update_term("category", 3, {"name": "Newsroom"},
                                        dry_run=False)
    assert out["action"] == "updated"
    assert out["term"]["id"] == 3 and out["term"]["name"] == "Newsroom"
    assert gw.updates == [{"taxonomy": "category", "term_id": 3,
                           "fields": {"name": "Newsroom"}}]


async def test_update_term_without_fields_never_reaches_the_gateway():
    """An empty POST would report success for a change that never happened."""
    for bad in ({}, None, [], "name"):
        gw = FakeTermGateway()
        out = await TermOps(gw).update_term("category", 3, bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "no term fields provided" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_update_term_rejects_bad_term_ids_before_the_gateway():
    for bad in (0, -1, None, "3", True):
        gw = FakeTermGateway()
        out = await TermOps(gw).update_term("category", bad, {"name": "X"},
                                            dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "term_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_update_term_validates_the_id_before_the_fields():
    """Id first: a bad id is a bad id whether or not fields were passed."""
    gw = FakeTermGateway()
    out = await TermOps(gw).update_term("category", 0, {}, dry_run=False)
    assert "term_id must be a positive int" in out["error"]
    assert gw.calls == []


async def test_update_term_gateway_failure_is_an_error_dict():
    gw = FakeTermGateway(error=ValueError("unsupported term field(s) colour"))
    out = await TermOps(gw).update_term("category", 3, {"colour": "red"},
                                        dry_run=False)
    assert out["action"] == "error" and "unsupported term field(s)" in out["error"]


# --- delete_term ------------------------------------------------------------

async def test_delete_term_dry_run_states_the_blast_radius():
    gw = FakeTermGateway()
    out = await TermOps(gw).delete_term("category", 3)
    assert out["action"] == "preview" and out["term_id"] == 3
    assert out["taxonomy"] == "category"
    effect = out["effect"].lower()
    assert "permanently" in effect and "unassign" in effect
    assert gw.calls == []


async def test_delete_term_real_run_deletes():
    gw = FakeTermGateway()
    out = await TermOps(gw).delete_term("category", 3, dry_run=False)
    assert out["action"] == "deleted" and out["term_id"] == 3
    assert out["taxonomy"] == "category"
    assert gw.deletes == [{"taxonomy": "category", "term_id": 3}]


async def test_delete_term_rejects_bad_term_ids_before_the_gateway():
    for bad in (0, -1, None, "3", True):
        gw = FakeTermGateway()
        out = await TermOps(gw).delete_term("category", bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "term_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_delete_term_gateway_failure_is_an_error_dict():
    gw = FakeTermGateway(error=RuntimeError("boom"))
    out = await TermOps(gw).delete_term("category", 3, dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


# --- set_post_terms ---------------------------------------------------------

async def test_set_post_terms_dry_run_previews_and_writes_nothing():
    gw = FakeTermGateway()
    out = await TermOps(gw).set_post_terms(5, "category", [3, 7])
    assert out == {"action": "preview", "post_id": 5, "taxonomy": "category",
                   "term_ids": [3, 7]}
    assert gw.assignments == [] and gw.calls == []


async def test_set_post_terms_real_run_applies_and_verifies():
    gw = FakeTermGateway()
    out = await TermOps(gw).set_post_terms(5, "category", [3, 7], dry_run=False)
    assert out == {"action": "applied", "post_id": 5, "taxonomy": "category",
                   "terms": [3, 7], "verified": True}
    assert gw.assignments == [{"post_id": 5, "taxonomy": "category",
                               "term_ids": [3, 7]}]


async def test_set_post_terms_reordered_response_still_verifies():
    """WP returns the ids in ITS order. Comparing as a set is the whole point -
    an order-sensitive check would fail every landed multi-term write."""
    gw = FakeTermGateway(stored=[7, 3])
    out = await TermOps(gw).set_post_terms(5, "category", [3, 7], dry_run=False)
    assert out["action"] == "applied" and out["verified"] is True
    assert out["terms"] == [7, 3]           # reported as the site holds it


async def test_set_post_terms_empty_response_is_a_mismatch_error():
    """A taxonomy that is not registered for the post's type: WP answers 200 with
    the field absent, so the gateway reports terms: []. That is a silent no-op and
    must NOT read as success."""
    gw = FakeTermGateway(stored=[])
    out = await TermOps(gw).set_post_terms(5, "category", [3, 7], dry_run=False)
    assert out["action"] == "error"
    assert "TermAssignmentMismatch" in out["error"]
    assert "3" in out["error"] and "7" in out["error"]
    assert "may not be registered for this post type" in out["error"]
    assert out["write_landed"] is True


async def test_set_post_terms_dropped_id_is_a_mismatch_error():
    gw = FakeTermGateway(stored=[3])
    out = await TermOps(gw).set_post_terms(5, "category", [3, 7], dry_run=False)
    assert out["action"] == "error" and "TermAssignmentMismatch" in out["error"]
    assert out["write_landed"] is True


async def test_set_post_terms_extra_id_is_a_mismatch_error():
    """More than was asked for is as wrong as less - the set must match exactly."""
    gw = FakeTermGateway(stored=[3, 7, 11])
    out = await TermOps(gw).set_post_terms(5, "category", [3, 7], dry_run=False)
    assert out["action"] == "error" and "TermAssignmentMismatch" in out["error"]


async def test_set_post_terms_duplicate_request_ids_still_verify():
    """WP stores a set; [3, 3, 7] landing as [3, 7] is not a lost write."""
    gw = FakeTermGateway(stored=[3, 7])
    out = await TermOps(gw).set_post_terms(5, "category", [3, 3, 7], dry_run=False)
    assert out["action"] == "applied" and out["verified"] is True


async def test_set_post_terms_empty_list_clears_the_taxonomy():
    gw = FakeTermGateway()
    out = await TermOps(gw).set_post_terms(5, "category", [], dry_run=False)
    assert out["action"] == "applied" and out["terms"] == []
    assert gw.assignments == [{"post_id": 5, "taxonomy": "category", "term_ids": []}]


async def test_set_post_terms_rejects_bad_term_id_lists_before_the_gateway():
    for bad in (3, "3", None, [0], [-1], ["3"], [True], [3, None], {3: 1}):
        gw = FakeTermGateway()
        out = await TermOps(gw).set_post_terms(5, "category", bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "term_ids must be a list of positive ints" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_set_post_terms_rejects_bad_post_ids_before_the_gateway():
    for bad in (0, -1, None, "5", True):
        gw = FakeTermGateway()
        out = await TermOps(gw).set_post_terms(bad, "category", [3], dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "post_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_set_post_terms_validates_in_dry_run_too():
    gw = FakeTermGateway()
    out = await TermOps(gw).set_post_terms(5, "category", [0], dry_run=True)
    assert out["action"] == "error"
    assert "term_ids must be a list of positive ints" in out["error"]
    assert gw.calls == []


async def test_set_post_terms_gateway_failure_is_an_error_dict():
    gw = FakeTermGateway(error=RuntimeError("boom"))
    out = await TermOps(gw).set_post_terms(5, "category", [3], dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]
    assert "write_landed" not in out       # nothing landed: the call itself failed


# --- never raises -----------------------------------------------------------

async def test_every_method_returns_a_dict_on_pathological_input():
    gw = FakeTermGateway()
    calls = [
        TermOps(gw).list_terms(object()),
        TermOps(gw).create_term(None, None, dry_run=False),
        TermOps(gw).update_term(None, object(), object(), dry_run=False),
        TermOps(gw).delete_term(object(), object(), dry_run=False),
        TermOps(gw).set_post_terms(object(), object(), object(), dry_run=False),
    ]
    for coro in calls:
        out = await coro
        assert isinstance(out, dict) and out["action"] == "error", out
    assert gw.calls == []
