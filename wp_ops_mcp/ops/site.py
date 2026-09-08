"""Site-level operations: slug renames and allowlisted site settings.

Both surfaces here change something global - a page's public URL, or an option every
page reads - so three rules do the heavy lifting:

  1. **Slugs are sanitized, and an unusable one is refused.** WP-safe means lowercase
     ``a-z0-9-`` only; anything else becomes a hyphen, repeats collapse, edges are
     stripped. If nothing survives ("***", a purely non-Latin title) the rename is
     refused rather than sent: WP answers an empty slug by substituting the post id,
     which silently changes a URL nobody asked to change.
  2. **The old URL is NOT always redirected, and that is reported up front.** WP core
     records ``_wp_old_slug`` (and ``wp_old_slug_redirect()`` 301s the old URL) only for
     PUBLISHED, NON-HIERARCHICAL posts - both ``wp_check_for_changed_slugs()`` and
     ``wp_old_slug_redirect()`` return early for hierarchical types, and ``page`` is
     hierarchical. Renaming a page's slug therefore 404s its old URL. Every preview and
     every result carries ``old_url_redirects`` (plus a ``warning`` when it is False) so
     an operator learns that before the rename, not from Search Console afterwards.
  3. **Settings are an allowlist, never a denylist.** Only ``SETTINGS_ALLOWLIST`` is
     readable/writable. ``url`` and ``email`` are excluded BY DESIGN - writing ``url``
     takes a live site off the air and writing ``email`` redirects its admin mail; a
     denylist would hand both back the day WordPress adds a new option. The permalink
     structure is not exposed by core REST at all, so it is out of scope here.

WP also UNIQUIFIES a colliding slug (about -> about-2) and SANITIZES setting values
(``sanitize_option()`` runs ``esc_html()`` over the site title/description, so
"Smith & Jones" is stored as "Smith &amp; Jones"). Neither is a failure: both are
reported as the site's ACTUAL value - with ``uniquified`` / alongside ``requested`` -
rather than claimed as an exact write or, worse, mistaken for one that never landed.

Like SeoOps/MediaOps/MenuOps, every public method NEVER raises: anticipated failures
and unanticipated ones alike come back as ``{"action": "error", "error": "<Type>: <msg>"}``
so an MCP tool returns a value instead of unwinding. When a write DID land but its
verification failed, the error carries ``write_landed`` plus the old/new values, because
an operator cannot revert what the error does not name.
"""
from __future__ import annotations

import html
import re

# Exact WP core REST field names (/wp/v2/settings). NOTE: the timezone option is
# `timezone_string` in the database but is registered for REST as `timezone`
# (wp-includes/option.php: show_in_rest => ['name' => 'timezone']), and the settings
# controller keys everything by the REST name - so `timezone` is what the wire wants.
# Excluded BY DESIGN - never add without a spec change: url, email, language,
# default_ping_status.
SETTINGS_ALLOWLIST = ("title", "description", "timezone", "posts_per_page",
                      "show_on_front", "page_on_front", "page_for_posts")

# WP does not enum-check this one; a value other than these two silently swaps the
# homepage to the blog index, so it is validated here.
_SHOW_ON_FRONT = ("posts", "page")

_UNSAFE = re.compile(r"[^a-z0-9-]")
_REPEATS = re.compile(r"-{2,}")


class SiteError(RuntimeError):
    """Refused slug or setting (invalid id, unusable slug, key off the allowlist)."""


def _positive_id(value, field: str) -> int:
    """A WordPress object id: a genuine positive int.

    ``bool`` is excluded explicitly - it subclasses int, so ``True`` would otherwise
    pass as id 1 and rename a real page. Same rule (and message shape) as MenuOps.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SiteError(f"{field} must be a positive int, got {value!r}")
    return value


def sanitize_slug(s: str) -> str:
    """A WP-safe slug: lowercase, ``[^a-z0-9-]`` -> '-', repeats collapsed, edges stripped.

    Returns "" when nothing usable is left; callers refuse rather than send it. No
    transliteration is attempted - "Uber Cafe" stays readable, "Über Café" degrades to
    "ber-caf", and a slug worth having is the operator's to choose.

    Non-strings raise instead of being coerced: ``str(123)`` would invent the slug
    "123" for a caller who passed an id by mistake.
    """
    if not isinstance(s, str):
        raise SiteError(f"slug must be a string, got {type(s).__name__}")
    return _REPEATS.sub("-", _UNSAFE.sub("-", s.lower())).strip("-")


def redirect_outlook(info: dict) -> tuple[bool, str]:
    """Will WP 301 the OLD URL after this rename? -> (yes/no, why-not).

    Only published, non-hierarchical posts get the free redirect (see the module
    docstring). ``page`` - the default type across this whole toolchain, and what a
    med-spa site's ranking URLs actually are - does not, so the honest answer is almost
    always "no, the old URL will 404".
    """
    ptype, status = info.get("type"), info.get("status")
    if ptype != "post":
        return False, (f"WordPress does not redirect a renamed {ptype} URL "
                       "(hierarchical post type): the old URL will 404 until a "
                       "redirect is added elsewhere")
    if status != "publish":
        return False, (f"WordPress records the old slug only for published posts "
                       f"(this one is {status!r}): the old URL will 404")
    return True, ""


def _stored_matches(stored, requested) -> bool:
    """Is what the site now holds the value we asked for, allowing for WP's own rewrite?

    Three forms count as a match, and only these:
      - identical;
      - the same value in a different scalar type (WP's REST schema casts "25" -> 25,
        so a quoted number from an MCP client is not a failed write);
      - the HTML-escaped form of what we sent (``sanitize_option()`` esc_html's the
        site title/description, so "Smith & Jones" comes back "Smith &amp; Jones").
    Two genuinely different values can never satisfy any of them, so a lost or clamped
    write still fails verification.
    """
    if stored == requested:
        return True
    if str(stored) == str(requested):
        return True
    return isinstance(stored, str) and html.unescape(stored) == str(requested)


class SiteOps:
    """Orchestrates slug renames and site settings over a REST content gateway.

    The gateway must expose ``get_post_info`` / ``update_post_slug`` / ``get_settings``
    / ``update_settings`` (RestContentGateway does; the SSH/wpcli gateway does not -
    the tool layer refuses that transport before getting here).
    """

    def __init__(self, gateway):
        self.gw = gateway

    async def change_slug(self, post_id: int, new_slug: str,
                          dry_run: bool = True) -> dict:
        """Rename one page/post's slug, reporting whether the old URL survives.

        Order is deliberate: the id and the slug are validated FIRST (an invalid one
        never reaches the gateway, in dry-run and for real alike, so a preview cannot
        promise a write that would fail), then the post is read - the preview shows the
        REAL old slug and the redirect outlook, and a bad id fails now rather than at
        apply time - then the write happens and is confirmed by reading the post back.

        ``uniquified`` is True when WP stored something other than the requested slug
        (a collision: about -> about-2). That is a success, not a failure, but the
        operator has to know the URL is not the one they asked for.
        """
        wrote = None
        try:
            _positive_id(post_id, "post_id")
            slug = sanitize_slug(new_slug)
            if not slug:
                raise SiteError(f"slug {new_slug!r} is empty after sanitizing "
                                "(allowed: a-z, 0-9, hyphen)")
            info = await self.gw.get_post_info(post_id)
            old_slug = info.get("name", "")
            redirects, warning = redirect_outlook(info)
            out = {"post_id": post_id, "old_slug": old_slug,
                   "old_url_redirects": redirects}
            if warning:
                out["warning"] = warning
            if dry_run:
                return {"action": "preview", "new_slug": slug, **out}
            result = await self.gw.update_post_slug(post_id, slug)
            wrote = result["slug"]               # WP's slug, not the requested one
            # Readback: a 200 is the site's intent, not proof. Compare against what WP
            # SAID it stored, so a legitimate uniquification still verifies.
            back = await self.gw.get_post_info(post_id)
            if back.get("name") != wrote:
                return {"action": "error", "slug": wrote, "write_landed": True,
                        "error": f"SlugReadbackMismatch: wrote {wrote!r} "
                                 f"read {back.get('name')!r}", **out}
            return {"action": "changed", "slug": wrote, "uniquified": wrote != slug,
                    "link": result.get("link", ""), "verified": True, **out}
        except Exception as e:
            err = {"action": "error", "error": f"{type(e).__name__}: {e}"}
            if wrote is not None:
                # The URL already changed; an error that does not say so leaves the
                # operator unable to revert.
                err.update({"post_id": post_id, "old_slug": old_slug,
                            "slug": wrote, "write_landed": True})
            return err

    async def get_settings(self) -> dict:
        """Read the allowlisted site settings (read-only, one call).

        The gateway passes WP's whole settings object through; the filter here is the
        policy boundary - ``url``/``email``/``language`` never leave this method, so
        they cannot end up in a transcript or a report.
        """
        try:
            data = await self.gw.get_settings()
            return {"action": "ok",
                    "settings": {k: data[k] for k in SETTINGS_ALLOWLIST if k in data}}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}

    async def set_setting(self, key: str, value, dry_run: bool = True) -> dict:
        """Change ONE allowlisted site setting, verified.

        A key off the allowlist is refused with the full list of what IS editable -
        naming the alternatives, not just the refusal, because the operator's next move
        is picking a valid key. Two values are refused for keys that ARE allowlisted,
        because WP itself does not stop them: ``None`` (core DELETES the option, which
        resets it to a default nobody asked for) and a ``show_on_front`` outside
        posts/page (anything else silently swaps the homepage to the blog index).

        Verification takes WP's response first (it re-reads and returns the whole
        settings object, so no second round-trip is needed in the normal case) and falls
        back to a readback when the response does not carry the key. The site's ACTUAL
        value is what comes back as ``value``, next to what was ``requested``: WP
        sanitizes as it stores, and treating its escaped/cast form as a failure would
        report a landed write as an error.
        """
        wrote = False
        try:
            if key not in SETTINGS_ALLOWLIST:
                raise SiteError(f"setting {key!r} is not editable - allowed: "
                                f"{', '.join(SETTINGS_ALLOWLIST)}")
            if value is None:
                raise SiteError(f"{key} cannot be set to null "
                                "(WordPress deletes the option and falls back to its default)")
            if key == "show_on_front" and value not in _SHOW_ON_FRONT:
                raise SiteError(f"show_on_front must be one of "
                                f"{', '.join(_SHOW_ON_FRONT)}, got {value!r}")
            if dry_run:
                return {"action": "preview", "key": key, "value": value}
            resp = await self.gw.update_settings({key: value})
            wrote = True
            stored = resp[key] if key in resp else (await self.gw.get_settings()).get(key)
            if not _stored_matches(stored, value):
                return {"action": "error", "key": key, "write_landed": True,
                        "error": f"SettingVerifyMismatch: {key} wrote {value!r} "
                                 f"read {stored!r} - the site did not store it"}
            return {"action": "applied", "key": key, "requested": value,
                    "value": stored, "verified": True}
        except Exception as e:
            err = {"action": "error", "error": f"{type(e).__name__}: {e}"}
            if wrote:
                err.update({"key": key, "requested": value, "write_landed": True})
            return err
