"""Contract tests for the Phase 5k taxonomy/term tools (wp_list_taxonomies /
wp_list_terms / wp_create_term / wp_update_term / wp_delete_term / wp_set_post_terms)
on the FastMCP server.

Mirrors test_server_menu_tools.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE any
term change reaches the site. Terms are builder-independent, so (like SEO/media/menus)
there is no profile/builder precondition - only the REST transport. The two read-only
tools carry NO prod gate: listing a prod site's taxonomies changes nothing.
"""
import asyncio

from wp_ops_mcp import server
from wp_ops_mcp.registry import SiteRegistry

RAW = [
    {"domain": "a.com", "wpe_account": "hostacct1", "wpe_install": "aprd",
     "cf_zone_id": "z1", "php_version": "8.2", "active": True},   # aprd -> prod
    {"domain": "", "wpe_account": "hostacct1", "wpe_install": "astg",
     "cf_zone_id": "", "php_version": "8.2", "active": True},     # astg -> staging
]

TAXONOMIES = [
    {"slug": "category", "name": "Categories", "rest_base": "categories",
     "hierarchical": True, "types": ["post"]},
]

TERMS = [
    {"id": 3, "name": "News", "slug": "news", "parent": 0, "count": 4,
     "taxonomy": "category"},
]

WRITES = ("wp_create_term", "wp_update_term", "wp_delete_term", "wp_set_post_terms")


def _reg():
    return SiteRegistry.from_records(RAW)


class FakeTermGateway:
    """REST-shaped gateway: exposes list_taxonomies, records every write."""

    def __init__(self, stored=None):
        self.terms = [dict(t) for t in TERMS]
        self.stored = stored
        self.creates = []
        self.updates = []
        self.deletes = []
        self.assignments = []

    async def list_taxonomies(self):
        return [dict(t) for t in TAXONOMIES]

    async def list_terms(self, taxonomy, search=None, per_page=100):
        rows = [dict(t) for t in self.terms]
        if search:
            rows = [r for r in rows if search.lower() in str(r["name"]).lower()]
        return rows

    async def create_term(self, taxonomy, name, slug=None, parent=None,
                          description=None):
        self.creates.append({"taxonomy": taxonomy, "name": name, "slug": slug,
                             "parent": parent, "description": description})
        term = {"id": 99, "name": name, "slug": slug or name.lower(),
                "parent": parent if parent is not None else 0, "count": 0,
                "taxonomy": taxonomy}
        self.terms.append(dict(term))
        return term

    async def update_term(self, taxonomy, term_id, fields):
        self.updates.append({"taxonomy": taxonomy, "term_id": term_id,
                             "fields": dict(fields)})
        row = {"id": term_id, "name": "News", "slug": "news", "parent": 0,
               "count": 4, "taxonomy": taxonomy}
        row.update(fields)
        return row

    async def delete_term(self, taxonomy, term_id):
        self.deletes.append({"taxonomy": taxonomy, "term_id": term_id})
        return {"deleted": True, "term": term_id}

    async def set_post_terms(self, post_id, taxonomy, term_ids):
        self.assignments.append({"post_id": post_id, "taxonomy": taxonomy,
                                 "term_ids": list(term_ids)})
        kept = list(term_ids) if self.stored is None else list(self.stored)
        return {"post_id": post_id, "taxonomy": taxonomy, "terms": kept}


class FakeWpcliGateway:
    """wpcli-style gateway: NO list_taxonomies, so the term tools must refuse."""

    async def list_posts(self, post_type, query=None):
        return []


def _wrote_nothing(gw):
    return not (gw.creates or gw.updates or gw.deletes or gw.assignments)


# --- registration -----------------------------------------------------------

def test_term_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"wp_list_taxonomies", "wp_list_terms", *WRITES} <= names


def test_delete_term_docstring_states_the_blast_radius():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    doc = (tools["wp_delete_term"].description or "").lower()
    assert "permanently" in doc and "every post" in doc and "no trash" in doc


# --- unknown install --------------------------------------------------------

async def test_list_taxonomies_unknown_install():
    payload = await server.list_taxonomies_payload(_reg(), "nope",
                                                   gateway=FakeTermGateway())
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]


async def test_list_terms_unknown_install():
    payload = await server.list_terms_payload(_reg(), "nope", "category",
                                              gateway=FakeTermGateway())
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]


async def test_create_term_unknown_install():
    gw = FakeTermGateway()
    payload = await server.create_term_payload(_reg(), "nope", "category", "Skin",
                                               dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert _wrote_nothing(gw)


async def test_update_term_unknown_install():
    gw = FakeTermGateway()
    payload = await server.update_term_payload(_reg(), "nope", "category", 3,
                                               {"name": "X"}, dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert _wrote_nothing(gw)


async def test_delete_term_unknown_install():
    gw = FakeTermGateway()
    payload = await server.delete_term_payload(_reg(), "nope", "category", 3,
                                               dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert _wrote_nothing(gw)


async def test_set_post_terms_unknown_install():
    gw = FakeTermGateway()
    payload = await server.set_post_terms_payload(_reg(), "nope", 5, "category", [3],
                                                  dry_run=False, gateway=gw)
    assert payload["action"] == "refused" and "unknown install" in payload["reason"]
    assert _wrote_nothing(gw)


# --- non-REST transport -----------------------------------------------------

async def test_list_taxonomies_wpcli_gateway_refused():
    payload = await server.list_taxonomies_payload(_reg(), "astg",
                                                   gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "taxonomy tools require the REST transport" in payload["reason"]


async def test_list_terms_wpcli_gateway_refused():
    payload = await server.list_terms_payload(_reg(), "astg", "category",
                                              gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


async def test_create_term_wpcli_gateway_refused():
    payload = await server.create_term_payload(_reg(), "astg", "category", "Skin",
                                               dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


async def test_update_term_wpcli_gateway_refused():
    payload = await server.update_term_payload(_reg(), "astg", "category", 3,
                                               {"name": "X"}, dry_run=False,
                                               gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


async def test_delete_term_wpcli_gateway_refused():
    payload = await server.delete_term_payload(_reg(), "astg", "category", 3,
                                               dry_run=False, gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


async def test_set_post_terms_wpcli_gateway_refused():
    payload = await server.set_post_terms_payload(_reg(), "astg", 5, "category", [3],
                                                  dry_run=False,
                                                  gateway=FakeWpcliGateway())
    assert payload["action"] == "refused" and "REST transport" in payload["reason"]


# --- prod gate honours dry_run (the four writes) ----------------------------

async def test_create_term_prod_blocked_without_allow_prod():
    gw = FakeTermGateway()
    payload = await server.create_term_payload(_reg(), "aprd", "category", "Skin",
                                               dry_run=False, allow_prod=False,
                                               gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.creates == []


async def test_create_term_prod_dry_run_passes_gate():
    gw = FakeTermGateway()
    payload = await server.create_term_payload(_reg(), "aprd", "category", "Skin",
                                               dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and gw.creates == []


async def test_create_term_prod_with_allow_prod_writes():
    gw = FakeTermGateway()
    payload = await server.create_term_payload(_reg(), "aprd", "category", "Skin",
                                               dry_run=False, allow_prod=True,
                                               gateway=gw)
    assert payload["action"] == "created" and payload["verified"] is True
    assert len(gw.creates) == 1


async def test_update_term_prod_blocked_without_allow_prod():
    gw = FakeTermGateway()
    payload = await server.update_term_payload(_reg(), "aprd", "category", 3,
                                               {"name": "X"}, dry_run=False,
                                               allow_prod=False, gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.updates == []


async def test_update_term_prod_dry_run_passes_gate():
    gw = FakeTermGateway()
    payload = await server.update_term_payload(_reg(), "aprd", "category", 3,
                                               {"name": "X"}, dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and gw.updates == []


async def test_delete_term_prod_blocked_without_allow_prod():
    gw = FakeTermGateway()
    payload = await server.delete_term_payload(_reg(), "aprd", "category", 3,
                                               dry_run=False, allow_prod=False,
                                               gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.deletes == []


async def test_delete_term_prod_dry_run_passes_gate():
    gw = FakeTermGateway()
    payload = await server.delete_term_payload(_reg(), "aprd", "category", 3,
                                               dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and gw.deletes == []


async def test_set_post_terms_prod_blocked_without_allow_prod():
    gw = FakeTermGateway()
    payload = await server.set_post_terms_payload(_reg(), "aprd", 5, "category", [3],
                                                  dry_run=False, allow_prod=False,
                                                  gateway=gw)
    assert payload["action"] == "refused" and "prod" in payload["reason"].lower()
    assert gw.assignments == []


async def test_set_post_terms_prod_dry_run_passes_gate():
    gw = FakeTermGateway()
    payload = await server.set_post_terms_payload(_reg(), "aprd", 5, "category", [3],
                                                  dry_run=True, gateway=gw)
    assert payload["action"] == "preview" and gw.assignments == []


# --- read-only tools have no prod gate --------------------------------------

async def test_list_taxonomies_happy_path_on_prod():
    payload = await server.list_taxonomies_payload(_reg(), "aprd",
                                                   gateway=FakeTermGateway())
    assert payload["action"] == "ok" and payload["taxonomies"] == TAXONOMIES


async def test_list_terms_happy_path_on_prod():
    payload = await server.list_terms_payload(_reg(), "aprd", "category",
                                              gateway=FakeTermGateway())
    assert payload["action"] == "ok" and payload["taxonomy"] == "category"
    assert [t["id"] for t in payload["terms"]] == [3]


# --- happy paths ------------------------------------------------------------

async def test_create_term_happy_path():
    gw = FakeTermGateway()
    payload = await server.create_term_payload(_reg(), "astg", "category", "Skin Care",
                                               slug="skin-care", parent=3,
                                               description="d", dry_run=False,
                                               gateway=gw)
    assert payload["action"] == "created" and payload["term"]["id"] == 99
    assert gw.creates == [{"taxonomy": "category", "name": "Skin Care",
                           "slug": "skin-care", "parent": 3, "description": "d"}]


async def test_update_term_happy_path():
    gw = FakeTermGateway()
    payload = await server.update_term_payload(_reg(), "astg", "category", 3,
                                               {"name": "Newsroom"}, dry_run=False,
                                               gateway=gw)
    assert payload["action"] == "updated" and payload["term"]["name"] == "Newsroom"
    assert gw.updates == [{"taxonomy": "category", "term_id": 3,
                           "fields": {"name": "Newsroom"}}]


async def test_delete_term_happy_path():
    gw = FakeTermGateway()
    payload = await server.delete_term_payload(_reg(), "astg", "category", 3,
                                               dry_run=False, gateway=gw)
    assert payload["action"] == "deleted" and payload["term_id"] == 3
    assert gw.deletes == [{"taxonomy": "category", "term_id": 3}]


async def test_set_post_terms_happy_path():
    gw = FakeTermGateway()
    payload = await server.set_post_terms_payload(_reg(), "astg", 5, "category", [3],
                                                  dry_run=False, gateway=gw)
    assert payload["action"] == "applied" and payload["verified"] is True
    assert gw.assignments == [{"post_id": 5, "taxonomy": "category", "term_ids": [3]}]


# --- ops-level outcomes still surface through the tool ----------------------

async def test_set_post_terms_mismatch_surfaces_as_an_error():
    """The carry-over case end to end: a taxonomy the post type does not register."""
    gw = FakeTermGateway(stored=[])
    payload = await server.set_post_terms_payload(_reg(), "astg", 5, "category", [3],
                                                  dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "TermAssignmentMismatch" in payload["error"]
    assert payload["write_landed"] is True


async def test_update_term_without_fields_is_refused_before_the_prod_gate():
    """Nothing to write means no write to gate: name the real mistake, not allow_prod."""
    gw = FakeTermGateway()
    payload = await server.update_term_payload(_reg(), "aprd", "category", 3, {},
                                               dry_run=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "no term fields provided" in payload["reason"]
    assert gw.updates == []


async def test_create_term_bad_name_is_an_error_dict():
    gw = FakeTermGateway()
    payload = await server.create_term_payload(_reg(), "astg", "category", "",
                                               dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "name must be a non-empty string" in payload["error"]
    assert gw.creates == []
