"""Unit tests for SiteOps (ops/site.py): slug renames and allowlisted site settings.

Fakes at the gateway boundary (RestContentGateway's slug + settings surface) so the
tests pin the disciplines that keep a site-level edit from doing damage:

  1. **Input is validated BEFORE the gateway.** A bad post id or a slug that
     sanitizes to nothing must not reach the wire at all (the gateway is checked for
     "no calls", not merely "no writes") - WP would answer an empty slug by
     substituting the post id, silently changing a URL nobody asked to change.
  2. **A setting is only writable if it is on the allowlist.** Anything else is
     refused with the full list of what IS editable; `url`/`email` are excluded by
     design, and writing either would take a live site offline or hijack its admin
     notifications.
  3. **The site's own rewrites are reported, not hidden and not mistaken for
     failures.** WP uniquifies a colliding slug, escapes text as it stores it, casts
     "25" to 25 - and does NOT redirect a renamed page's old URL. Each of those is
     surfaced (`uniquified`, `value` vs `requested`, `old_url_redirects`) rather than
     papered over in either direction.

Verification is real: a write that lands but cannot be confirmed comes back as an
error that still names what changed (`write_landed`), and every method never raises.
"""
import pytest

from wp_ops_mcp.ops.site import (
    SETTINGS_ALLOWLIST, SiteError, SiteOps, redirect_outlook, sanitize_slug)

SETTINGS = {
    "title": "Site", "description": "Tagline", "timezone": "America/Denver",
    "posts_per_page": 10, "show_on_front": "page", "page_on_front": 5,
    "page_for_posts": 9,
    # NOT on the allowlist - these must never come back out of get_settings, and
    # must never be writable through set_setting.
    "url": "https://site.test", "email": "admin@site.test", "language": "en_US",
    "default_ping_status": "open",
}
ALLOWED = {k: SETTINGS[k] for k in SETTINGS_ALLOWLIST}


class FakeSiteGateway:
    """REST-shaped gateway (post info + slug + settings) recording what reached the wire.

    Knobs model the honest behaviours of a real site:
      `post_type`/`post_status` - decide whether WP will redirect the old URL;
      `wp_slug`      - WP uniquified the requested slug (about -> about-2);
      `stored_slug`  - what a readback sees, when it differs from what WP reported;
      `store`        - whether update_settings actually persists (False = write lost);
      `resp_drop`    - the settings response omits the written key (verify must fall
                       back to a readback rather than assume success);
      `sanitize`     - how WP rewrites a value as it stores it (esc_html, int cast);
      `error`        - every call raises;
      `error_after_write` - reads raise only AFTER a write has landed (the case where
                       a rename really happened but cannot be confirmed).
    """

    def __init__(self, slug="old-slug", post_type="page", post_status="publish",
                 wp_slug=None, stored_slug=None, settings=None, store=True,
                 resp_drop=False, sanitize=None, error=None, error_after_write=None):
        self.slug = slug
        self.post_type = post_type
        self.post_status = post_status
        self.wp_slug = wp_slug
        self.stored_slug = stored_slug
        self.settings = dict(SETTINGS if settings is None else settings)
        self.store = store
        self.resp_drop = resp_drop
        self.sanitize = sanitize or (lambda v: v)
        self.error = error
        self.error_after_write = error_after_write
        self.wrote = False
        self.calls = []
        self.slug_writes = []
        self.settings_writes = []

    def _boom(self, read=False):
        if self.error is not None:
            raise self.error
        if read and self.wrote and self.error_after_write is not None:
            raise self.error_after_write

    async def get_post_info(self, post_id):
        self.calls.append(("get_post_info", post_id))
        self._boom(read=True)
        return {"id": post_id, "name": self.slug, "status": self.post_status,
                "type": self.post_type, "title": "T"}

    async def update_post_slug(self, post_id, slug):
        self.calls.append(("update_post_slug", post_id, slug))
        self._boom()
        self.slug_writes.append({"post_id": post_id, "slug": slug})
        self.wrote = True
        actual = self.wp_slug or slug              # WP uniquifies on collision
        self.slug = actual if self.stored_slug is None else self.stored_slug
        return {"id": post_id, "slug": actual, "link": f"https://site.test/{actual}/"}

    async def get_settings(self):
        self.calls.append(("get_settings",))
        self._boom(read=True)
        return dict(self.settings)

    async def update_settings(self, fields):
        self.calls.append(("update_settings", dict(fields)))
        self._boom()
        if not fields:                             # gateway contract (Task 1)
            raise ValueError("no settings fields to update")
        self.settings_writes.append(dict(fields))
        self.wrote = True
        if self.store:
            self.settings.update({k: self.sanitize(v) for k, v in fields.items()})
        resp = dict(self.settings)                 # WP re-reads and returns everything
        if self.resp_drop:
            for k in fields:
                resp.pop(k, None)
        return resp


# --- sanitize_slug (pure) ---------------------------------------------------

def test_sanitize_slug_lowercases_and_hyphenates_spaces():
    assert sanitize_slug("About Us") == "about-us"
    assert sanitize_slug("ALLCAPS") == "allcaps"


def test_sanitize_slug_collapses_repeats_and_strips_edges():
    assert sanitize_slug("About   Us!!!") == "about-us"
    assert sanitize_slug("--about--") == "about"
    assert sanitize_slug("  spaced  out  ") == "spaced-out"
    assert sanitize_slug("a_b") == "a-b"


def test_sanitize_slug_keeps_digits_and_existing_hyphens():
    assert sanitize_slug("botox-101") == "botox-101"


def test_sanitize_slug_replaces_non_latin_rather_than_transliterating():
    """No transliteration is attempted: unrepresentable characters become hyphens."""
    assert sanitize_slug("Uber Cafe") == "uber-cafe"
    assert sanitize_slug("Über Café") == "ber-caf"


def test_sanitize_slug_returns_empty_when_nothing_usable_is_left():
    for junk in ("", "   ", "***", "!!! ???", "已经"):
        assert sanitize_slug(junk) == "", junk


def test_sanitize_slug_refuses_non_strings():
    """123 is not "123": a silent str() would invent a slug the caller never asked for."""
    for bad in (123, None, True, ["about"], {"slug": "about"}):
        with pytest.raises(SiteError, match="must be a string"):
            sanitize_slug(bad)


# --- redirect_outlook (pure) ------------------------------------------------
# WP core: wp_check_for_changed_slugs() and wp_old_slug_redirect() both bail out on
# hierarchical post types, and the first also requires post_status 'publish'. So a
# renamed PAGE url 404s - the opposite of what "WP handles the redirect" implies.

def test_redirect_outlook_true_only_for_published_posts():
    ok, warning = redirect_outlook({"type": "post", "status": "publish"})
    assert ok is True and warning == ""


def test_redirect_outlook_false_for_pages():
    ok, warning = redirect_outlook({"type": "page", "status": "publish"})
    assert ok is False and "404" in warning and "hierarchical" in warning


def test_redirect_outlook_false_for_unpublished_posts():
    ok, warning = redirect_outlook({"type": "post", "status": "draft"})
    assert ok is False and "published" in warning and "404" in warning


# --- change_slug: validation happens BEFORE the gateway ---------------------

async def test_change_slug_rejects_bad_post_ids_before_the_gateway():
    for bad in (0, -1, None, "5", True, 1.5):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).change_slug(bad, "about-us", dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "post_id must be a positive int" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_change_slug_refuses_a_slug_that_sanitizes_to_nothing():
    for junk in ("", "   ", "***", "已经"):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).change_slug(5, junk, dry_run=False)
        assert out["action"] == "error", (junk, out)
        assert "empty after sanitizing" in out["error"], (junk, out)
        assert gw.calls == [], (junk, gw.calls)


async def test_change_slug_refuses_non_string_slugs_before_the_gateway():
    gw = FakeSiteGateway()
    out = await SiteOps(gw).change_slug(5, 123, dry_run=False)
    assert out["action"] == "error" and "must be a string" in out["error"]
    assert gw.calls == []


async def test_change_slug_validates_in_dry_run_too():
    """A preview that would fail for real is a lie; dry-run refuses identically."""
    gw = FakeSiteGateway()
    assert (await SiteOps(gw).change_slug(0, "about", dry_run=True))["action"] == "error"
    assert (await SiteOps(gw).change_slug(5, "***", dry_run=True))["action"] == "error"
    assert gw.calls == []


# --- change_slug: dry run ---------------------------------------------------

async def test_change_slug_dry_run_previews_old_and_new_and_writes_nothing():
    gw = FakeSiteGateway(slug="old-slug", post_type="post", post_status="publish")
    out = await SiteOps(gw).change_slug(5, "About Us!")
    assert out == {"action": "preview", "post_id": 5, "old_slug": "old-slug",
                   "new_slug": "about-us", "old_url_redirects": True}
    assert gw.slug_writes == []
    assert gw.calls == [("get_post_info", 5)]      # read only, and the post IS read


async def test_change_slug_dry_run_warns_that_a_page_rename_breaks_the_old_url():
    """The 404 risk has to be visible BEFORE the rename, not after it ranks nowhere."""
    gw = FakeSiteGateway(slug="botox", post_type="page")
    out = await SiteOps(gw).change_slug(5, "botox-injections")
    assert out["action"] == "preview" and out["old_url_redirects"] is False
    assert "404" in out["warning"]


async def test_change_slug_dry_run_surfaces_a_missing_post():
    """The preview fetches the post, so a bad id fails now instead of at apply time."""
    gw = FakeSiteGateway(error=RuntimeError("post info failed (HTTP 404)"))
    out = await SiteOps(gw).change_slug(5, "about-us")
    assert out["action"] == "error" and "404" in out["error"]
    assert "write_landed" not in out


# --- change_slug: real run + readback ---------------------------------------

async def test_change_slug_applies_the_sanitized_slug_and_verifies():
    gw = FakeSiteGateway(slug="old-slug", post_type="post")
    out = await SiteOps(gw).change_slug(5, "About Us", dry_run=False)
    assert out == {"action": "changed", "post_id": 5, "old_slug": "old-slug",
                   "slug": "about-us", "uniquified": False, "old_url_redirects": True,
                   "link": "https://site.test/about-us/", "verified": True}
    assert gw.slug_writes == [{"post_id": 5, "slug": "about-us"}]   # sanitized on the wire
    assert gw.calls.count(("get_post_info", 5)) == 2                # before + readback


async def test_change_slug_result_warns_when_the_old_page_url_will_404():
    gw = FakeSiteGateway(slug="botox", post_type="page")
    out = await SiteOps(gw).change_slug(5, "botox-injections", dry_run=False)
    assert out["action"] == "changed" and out["verified"] is True
    assert out["old_url_redirects"] is False and "404" in out["warning"]


async def test_change_slug_flags_a_wp_uniquified_slug_but_still_succeeds():
    """WP appends -2 on collision: report the ACTUAL slug and flag it, never claim
    an exact rename that did not happen."""
    gw = FakeSiteGateway(slug="services", wp_slug="about-2")
    out = await SiteOps(gw).change_slug(5, "About", dry_run=False)
    assert out["action"] == "changed" and out["uniquified"] is True
    assert out["slug"] == "about-2" and out["old_slug"] == "services"
    assert out["link"] == "https://site.test/about-2/" and out["verified"] is True


async def test_change_slug_readback_mismatch_is_an_error_that_names_the_change():
    """A 200 is the site's intent; if the stored slug is something else, say so - and
    still report what the URL is now, or the operator cannot revert it."""
    gw = FakeSiteGateway(slug="old-slug", stored_slug="something-else")
    out = await SiteOps(gw).change_slug(5, "about-us", dry_run=False)
    assert out["action"] == "error"
    assert "about-us" in out["error"] and "something-else" in out["error"]
    assert out["write_landed"] is True
    assert out["old_slug"] == "old-slug" and out["slug"] == "about-us"


async def test_change_slug_reports_a_landed_write_whose_readback_failed():
    """The rename happened and then the site went unreachable: an error that hides the
    new slug leaves a changed URL nobody can revert."""
    gw = FakeSiteGateway(slug="old-slug",
                         error_after_write=RuntimeError("get post info failed (HTTP 502)"))
    out = await SiteOps(gw).change_slug(5, "about-us", dry_run=False)
    assert out["action"] == "error" and "502" in out["error"]
    assert out["write_landed"] is True
    assert out["post_id"] == 5 and out["old_slug"] == "old-slug"
    assert out["slug"] == "about-us"
    assert gw.slug_writes == [{"post_id": 5, "slug": "about-us"}]


async def test_change_slug_gateway_failure_is_an_error_dict():
    gw = FakeSiteGateway(error=RuntimeError("boom"))
    out = await SiteOps(gw).change_slug(5, "about-us", dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


async def test_change_slug_never_raises_on_pathological_input():
    for pid, slug in ((5, None), (5, 3.5), (5, []), (5, {}), (None, None), ("5", b"x")):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).change_slug(pid, slug, dry_run=False)
        assert isinstance(out, dict) and out["action"] == "error", (pid, slug, out)
        assert gw.slug_writes == [], (pid, slug)


# --- get_settings -----------------------------------------------------------

async def test_get_settings_returns_only_allowlisted_keys():
    gw = FakeSiteGateway()
    out = await SiteOps(gw).get_settings()
    assert out == {"action": "ok", "settings": ALLOWED}
    for dropped in ("url", "email", "language", "default_ping_status"):
        assert dropped not in out["settings"], dropped


async def test_get_settings_omits_keys_the_site_does_not_report():
    gw = FakeSiteGateway(settings={"title": "Site", "url": "https://site.test"})
    out = await SiteOps(gw).get_settings()
    assert out == {"action": "ok", "settings": {"title": "Site"}}


async def test_get_settings_gateway_failure_is_an_error_dict():
    gw = FakeSiteGateway(error=RuntimeError("boom"))
    out = await SiteOps(gw).get_settings()
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]


def test_settings_allowlist_is_the_documented_seven():
    """`timezone` is the REST field name for the timezone_string option
    (option.php: show_in_rest => ['name' => 'timezone']); the settings controller keys
    everything by the REST name, so timezone_string on the wire is a silent no-op.
    Excluded BY DESIGN (spec): url, email, language, default_ping_status.
    """
    assert SETTINGS_ALLOWLIST == ("title", "description", "timezone",
                                  "posts_per_page", "show_on_front",
                                  "page_on_front", "page_for_posts")


# --- set_setting: allowlist + value guards ----------------------------------

async def test_set_setting_refuses_keys_off_the_allowlist_naming_all_seven():
    for bad in ("url", "email", "language", "default_ping_status", "timezone_string",
                "titel", ""):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).set_setting(bad, "x", dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert repr(bad) in out["error"], (bad, out)
        for key in SETTINGS_ALLOWLIST:
            assert key in out["error"], (bad, key)
        assert gw.calls == [], (bad, gw.calls)


async def test_set_setting_refuses_bad_keys_in_dry_run_too():
    gw = FakeSiteGateway()
    out = await SiteOps(gw).set_setting("url", "https://evil.test", dry_run=True)
    assert out["action"] == "error" and "url" in out["error"]
    assert gw.calls == []


async def test_set_setting_refuses_null_which_would_delete_the_option():
    """WP core deletes the option for a null value, resetting it to a default."""
    for key in ("title", "page_on_front"):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).set_setting(key, None, dry_run=False)
        assert out["action"] == "error" and "null" in out["error"], (key, out)
        assert gw.calls == [], key


async def test_set_setting_refuses_a_show_on_front_outside_posts_and_page():
    """WP does not enum-check this: any other value silently swaps the homepage."""
    for bad in ("static", "Page", "", 1):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).set_setting("show_on_front", bad, dry_run=False)
        assert out["action"] == "error", (bad, out)
        assert "show_on_front must be one of posts, page" in out["error"], (bad, out)
        assert gw.calls == [], (bad, gw.calls)


async def test_set_setting_allows_the_two_valid_show_on_front_values():
    for good in ("posts", "page"):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).set_setting("show_on_front", good, dry_run=False)
        assert out["action"] == "applied", (good, out)


async def test_set_setting_never_raises_on_pathological_keys():
    for bad in (None, 3.5, ["title"], {"title": "x"}, True):
        gw = FakeSiteGateway()
        out = await SiteOps(gw).set_setting(bad, "x", dry_run=False)
        assert isinstance(out, dict) and out["action"] == "error", (bad, out)
        assert gw.calls == [], (bad, gw.calls)


# --- set_setting: dry run ---------------------------------------------------

async def test_set_setting_dry_run_previews_and_writes_nothing():
    gw = FakeSiteGateway()
    out = await SiteOps(gw).set_setting("title", "New Title")
    assert out == {"action": "preview", "key": "title", "value": "New Title"}
    assert gw.calls == []


# --- set_setting: real run + verify -----------------------------------------

async def test_set_setting_applies_and_verifies_from_the_response():
    gw = FakeSiteGateway()
    out = await SiteOps(gw).set_setting("title", "New Title", dry_run=False)
    assert out == {"action": "applied", "key": "title", "requested": "New Title",
                   "value": "New Title", "verified": True}
    assert gw.settings_writes == [{"title": "New Title"}]     # ONLY the one field
    assert gw.calls == [("update_settings", {"title": "New Title"})]  # no extra readback


async def test_set_setting_accepts_int_values():
    gw = FakeSiteGateway()
    out = await SiteOps(gw).set_setting("posts_per_page", 25, dry_run=False)
    assert out == {"action": "applied", "key": "posts_per_page", "requested": 25,
                   "value": 25, "verified": True}
    assert gw.settings_writes == [{"posts_per_page": 25}]


async def test_set_setting_accepts_wp_escaping_the_value_it_stored():
    """sanitize_option() esc_html's the site title: "Smith & Jones" is stored encoded.
    That is a landed write, not a failure - report the site's ACTUAL value."""
    gw = FakeSiteGateway(sanitize=lambda v: v.replace("&", "&amp;").replace("'", "&#039;"))
    out = await SiteOps(gw).set_setting("title", "Smith & Jones Dermatology",
                                        dry_run=False)
    assert out == {"action": "applied", "key": "title",
                   "requested": "Smith & Jones Dermatology",
                   "value": "Smith &amp; Jones Dermatology", "verified": True}


async def test_set_setting_accepts_wp_casting_a_numeric_string():
    """WP's REST schema casts "25" to 25; a quoted number is not a failed write."""
    gw = FakeSiteGateway(sanitize=int)
    out = await SiteOps(gw).set_setting("posts_per_page", "25", dry_run=False)
    assert out["action"] == "applied" and out["value"] == 25
    assert out["requested"] == "25" and out["verified"] is True


async def test_set_setting_reads_back_when_the_response_omits_the_key():
    gw = FakeSiteGateway(resp_drop=True)
    out = await SiteOps(gw).set_setting("title", "New Title", dry_run=False)
    assert out["action"] == "applied" and out["value"] == "New Title"
    assert out["verified"] is True
    assert ("get_settings",) in gw.calls          # verification really happened


async def test_set_setting_lost_write_is_an_error():
    """Response omits the key AND the site still holds the old value -> lost write."""
    gw = FakeSiteGateway(store=False, resp_drop=True)
    out = await SiteOps(gw).set_setting("title", "New Title", dry_run=False)
    assert out["action"] == "error"
    assert "New Title" in out["error"] and "Site" in out["error"]
    assert out["write_landed"] is True and out["key"] == "title"


async def test_set_setting_clamped_value_is_an_error():
    """WP storing something genuinely different (a clamp/filter) still fails verify."""
    gw = FakeSiteGateway(sanitize=lambda v: 10)
    out = await SiteOps(gw).set_setting("posts_per_page", 500, dry_run=False)
    assert out["action"] == "error" and "500" in out["error"] and "10" in out["error"]


async def test_set_setting_reports_a_landed_write_whose_readback_failed():
    gw = FakeSiteGateway(resp_drop=True,
                         error_after_write=RuntimeError("get settings failed (HTTP 502)"))
    out = await SiteOps(gw).set_setting("title", "New Title", dry_run=False)
    assert out["action"] == "error" and "502" in out["error"]
    assert out["write_landed"] is True and out["requested"] == "New Title"
    assert gw.settings_writes == [{"title": "New Title"}]


async def test_set_setting_gateway_failure_is_an_error_dict():
    gw = FakeSiteGateway(error=RuntimeError("boom"))
    out = await SiteOps(gw).set_setting("title", "New Title", dry_run=False)
    assert out["action"] == "error" and "RuntimeError: boom" in out["error"]
    assert "write_landed" not in out
