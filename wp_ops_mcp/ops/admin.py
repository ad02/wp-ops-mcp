"""Admin operations: user CRUD, ARBITRARY site options, and comment moderation.

This is the widest-blast-radius surface in the server, by design (full-parity
authorization, 2026-07-28). There is no allowlist here and no confirm flag: an option
write can set ``siteurl``, a user delete really deletes. What keeps that safe is the
same universal contract every other ops module carries, applied without exception:

  1. **Every write previews by default.** ``dry_run=True`` on all seven write methods
     (``create_user``, ``update_user``, ``delete_user``, ``set_option``,
     ``delete_option``, ``moderate_comment``, ``delete_comment``), and the preview
     describes exactly what WOULD happen - including the reassign
     target of a user delete and the value an option write would overwrite - so the
     decision is made from the site's real state, not from the caller's assumption.
  2. **A password NEVER comes back out.** ``create_user``/``update_user`` replace it
     with ``PASSWORD_MASK`` in every preview, and ``_safe_user`` strips a ``password``
     key from anything the gateway returns. A tool result is a transcript entry, a log
     line and often a model's context; a secret echoed once is a secret leaked
     everywhere.
  3. **Input is validated BEFORE the gateway.** Ids, option names, field dicts and the
     comment-status vocabulary are checked here, so an invalid call never reaches the
     wire in dry-run or for real.
  4. **The site's own answer is read honestly, in both directions.** Three WordPress
     behaviours would otherwise be misreported:
       - an option write is verified by RE-READING the option, and WP's storage casts
         (25 -> "25", True -> 1, "&" -> "&amp;") are tolerated - a landed write is not
         a mismatch (``_option_matches``). A BOOL write is the exception: it verifies
         only against WP's canonical forms, never by truthiness, so an option that
         stayed at some other truthy value is a mismatch and not a false success;
       - ``delete_option`` returning False means "there was nothing to delete", which
         comes back as ``action: "noop"``, not an error;
       - a comment DELETE without ``force`` answers ``deleted: False`` with status
         "trash" - WordPress trashed it, which is a SUCCESS (``action: "trashed"``,
         ``recoverable: True``), not a failure.

Like SeoOps/MenuOps/SiteOps/AcfOps, every public method NEVER raises: anticipated
failures and unanticipated ones alike come back as
``{"action": "error", "error": "<Type>: <msg>"}`` so an MCP tool returns a value
instead of unwinding. When a write DID land but its verification failed, the error
carries ``write_landed`` plus both values, because an operator cannot revert what the
error does not name.
"""
from __future__ import annotations

from .acf import _acf_equal
from .site import _stored_matches

# WordPress's own comment-status strings (wp/v2/comments schema enum). Not an
# invented vocabulary: "approve"/"approved" is exactly the kind of near-miss that
# would otherwise POST a status WP silently ignores.
COMMENT_STATUSES = ("approved", "hold", "spam", "trash")

PASSWORD_MASK = "<not echoed>"
"""What a password is replaced by in any preview. See rule 2 in the module docstring."""

# The autoloaded-only caveat travels with the RESULT, not only the tool docstring: a
# caller reads the list, not the manual, before concluding an option does not exist.
_LIST_NOTE = ("autoloaded option names only (wp_load_alloptions) - a non-autoloaded "
              "option is absent from this list but still readable by name")


class AdminError(RuntimeError):
    """Refused admin call (bad id, empty option name, empty fields, unknown status)."""


def _error(e: Exception, **extra) -> dict:
    return {"action": "error", "error": f"{type(e).__name__}: {e}", **extra}


def _positive_id(value, field: str) -> int:
    """A WordPress object id: a genuine positive int.

    ``bool`` is excluded explicitly - it subclasses int, so ``True`` would otherwise
    pass as id 1 and delete a real user. Same rule (and message shape) as MenuOps/SiteOps.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AdminError(f"{field} must be a positive int, got {value!r}")
    return value


def _text(value, field: str) -> str:
    """A required string argument: non-empty and not whitespace-only."""
    if not isinstance(value, str) or not value.strip():
        raise AdminError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _reassign_id(value) -> int:
    """The delete_user reassign target: a non-negative int (0 = delete their content).

    0 is legal here (unlike an object id) because it is WP's own sentinel for "delete
    the user's posts with them"; bools and negatives are not.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AdminError("reassign must be a non-negative int "
                         f"(0 = delete their content), got {value!r}")
    return value


def _require_fields(value, noun: str) -> dict:
    """A non-empty field dict, refused otherwise.

    WordPress answers an empty POST with 200 and the unchanged object, so an empty
    dict would read as a successful edit that never happened (carry-over from the
    gateway task: only update_settings refuses it at that layer).
    """
    if not isinstance(value, dict) or not value:
        raise AdminError(f"no {noun} fields provided (pass an object of field -> value)")
    return dict(value)


def _mask(fields: dict) -> dict:
    """A copy of ``fields`` safe to echo: any password value replaced by the mask."""
    return {k: (PASSWORD_MASK if k == "password" else v) for k, v in fields.items()}


def _safe_user(row: dict) -> dict:
    """A user row with any ``password`` key stripped.

    The REST gateway's mapper cannot produce one today; this is deliberate defence in
    depth, because the cost of being wrong once is a leaked credential.
    """
    return {k: v for k, v in row.items() if k != "password"}


_BOOL_TRUE, _BOOL_FALSE = ({True, 1, "1"}, {False, 0, "", "0"})
"""The forms WordPress actually stores a ``True``/``False`` option write as.

Membership, not truthiness: ``True in _BOOL_TRUE`` is a set lookup, and ``True == 1``
in Python so both spellings land in the same bucket by design.
"""


def _bool_matches(stored, requested: bool) -> bool:
    """Did a bool write land? Only WP's own canonical storage forms count.

    Deliberately STRICTER than ``_acf_equal``'s truthiness rule, which would call any
    non-empty value equal to ``True``: on an ARBITRARY-option surface an option that
    silently stayed at an unrelated truthy value ("partial", "on", 2) would then be
    reported ``applied``/``verified`` on a write that never landed - a false verify is
    the one failure mode an operator cannot detect afterwards. ``_acf_equal`` keeps its
    tolerance where it belongs (ACF fields, whose 1/0/"" storage is known); here the
    accepted set is enumerated instead.
    """
    try:
        return stored in (_BOOL_TRUE if requested else _BOOL_FALSE)
    except TypeError:                 # unhashable (list/dict) is never a stored bool
        return False


def _option_matches(stored, requested) -> bool:
    """Is what the site now holds the value we asked for, allowing for WP's own rewrite?

    A bool REQUEST is decided first and alone, by ``_bool_matches`` - see there for why
    truthiness is not good enough on this surface.

    Everything else reuses the two tolerant comparisons this codebase already trusts,
    rather than inventing a third: ``_acf_equal`` (scalars compared as strings,
    lists/dicts element-wise - WP serializes every option, so an int written can read
    back as its string form) OR ``_stored_matches`` (adds the HTML-escaped form, since
    ``sanitize_option()`` esc_html's several core options such as blogname).

    Either one matching is enough: this is strictly MORE tolerant than either alone,
    which is the right direction - a false mismatch reports a landed write as an error
    and sends an operator chasing a problem that does not exist. Two genuinely
    different values still satisfy neither, so a lost or clamped write fails.
    """
    if isinstance(requested, bool):
        return _bool_matches(stored, requested)
    return _acf_equal(stored, requested) or _stored_matches(stored, requested)


class AdminOps:
    """Orchestrates user/option/comment admin over a REST content gateway.

    The gateway must expose the user + comment surface (``list_users``/``get_user``/
    ``create_user``/``update_user``/``delete_user``/``list_comments``/
    ``update_comment``/``delete_comment``) and the option surface
    (``ensure_options_capable``/``list_option_names``/``get_option``/``set_option``/
    ``delete_option``). RestContentGateway does; the SSH/wpcli gateway does not - the
    tool layer refuses that transport before getting here.

    The option methods call ``ensure_options_capable()`` FIRST, including on a dry run:
    the /option route only exists from wp-ops-connect 1.3.0, and a preview that
    promises a write the plugin cannot perform is worse than an early refusal.
    """

    def __init__(self, gateway):
        self.gw = gateway

    # -- users ----------------------------------------------------------------
    async def list_users(self, search: str | None = None) -> dict:
        """All users (id, username, name, email, roles, url). Read-only, one call."""
        try:
            users = [_safe_user(u) for u in await self.gw.list_users(search=search)]
            return {"action": "ok", "count": len(users), "users": users}
        except Exception as e:
            return _error(e)

    async def get_user(self, user_id: int) -> dict:
        """One user by id. Read-only."""
        try:
            _positive_id(user_id, "user_id")
            return {"action": "ok", "user": _safe_user(await self.gw.get_user(user_id))}
        except Exception as e:
            return _error(e)

    async def create_user(self, username: str, email: str, password: str,
                          roles: list | str | None = None, name: str | None = None,
                          dry_run: bool = True) -> dict:
        """Create a user. The password is sent to the site but NEVER echoed back.

        All three required arguments are validated first: WordPress refuses a blank
        username/email/password anyway, and failing here keeps a doomed write off the
        wire (and out of the preview) entirely.
        """
        try:
            _text(username, "username")
            _text(email, "email")
            _text(password, "password")
            if dry_run:
                # PASSWORD_MASK, not the value: see rule 2 in the module docstring.
                return {"action": "preview", "password": PASSWORD_MASK,
                        "user": {"username": username, "email": email,
                                 "roles": roles, "name": name}}
            user = await self.gw.create_user(username, email, password,
                                             roles=roles, name=name)
            return {"action": "created", "user": _safe_user(user)}
        except Exception as e:
            return _error(e)

    async def update_user(self, user_id: int, fields: dict, dry_run: bool = True) -> dict:
        """Change an existing user's fields (name/email/roles/password/url/description).

        An empty ``fields`` is refused rather than sent: WP answers an empty POST with
        200 and the unchanged user, which would read as a successful edit. A key the
        gateway does not allow raises there and surfaces here as an error dict.
        """
        try:
            _positive_id(user_id, "user_id")
            clean = _require_fields(fields, "user")
            if dry_run:
                return {"action": "preview", "user_id": user_id, "fields": _mask(clean)}
            user = await self.gw.update_user(user_id, clean)
            return {"action": "updated", "user": _safe_user(user)}
        except Exception as e:
            return _error(e)

    async def delete_user(self, user_id: int, reassign: int = 0,
                          dry_run: bool = True) -> dict:
        """PERMANENTLY delete a user; their content goes to ``reassign``.

        WP core REST requires both force and a reassign target, so there is no
        recoverable form of this call. The preview therefore states the content
        outcome in words - "deleted with them" vs "reassigned to user N" - because
        ``reassign: 0`` is the default and is also the destructive one.
        """
        try:
            _positive_id(user_id, "user_id")
            target = _reassign_id(reassign)
            effect = ("their posts and pages will be DELETED with them"
                      if target == 0 else
                      f"their posts and pages will be reassigned to user {target}")
            if dry_run:
                return {"action": "preview", "user_id": user_id, "reassign": target,
                        "effect": f"user {user_id} will be permanently deleted; {effect}"}
            result = await self.gw.delete_user(user_id, reassign=target)
            return {"action": "deleted", "user_id": user_id, "reassign": target,
                    "previous": result.get("previous")}
        except Exception as e:
            return _error(e)

    # -- options --------------------------------------------------------------
    async def list_options(self) -> dict:
        """Every AUTOLOADED option name (see ``_LIST_NOTE``). Read-only, one call."""
        try:
            await self.gw.ensure_options_capable()
            names = await self.gw.list_option_names()
            return {"action": "ok", "count": len(names), "names": names,
                    "note": _LIST_NOTE}
        except Exception as e:
            return _error(e)

    async def get_option(self, name: str) -> dict:
        """Read one option by name (works for non-autoloaded options too).

        ``exists`` is the only absence signal - an option may legitimately hold
        ""/0/false - so a falsy ``value`` says nothing on its own.
        """
        try:
            _text(name, "name")
            await self.gw.ensure_options_capable()
            data = await self.gw.get_option(name)
            return {"action": "ok", "name": data["name"], "value": data["value"],
                    "exists": data["exists"]}
        except Exception as e:
            return _error(e)

    async def set_option(self, name: str, value, dry_run: bool = True) -> dict:
        """Write ANY option, verified by re-reading it.

        The preview carries the option's CURRENT value and whether it exists, so an
        overwrite is visible as an overwrite before it happens. A real write is
        confirmed by a fresh ``get_option`` (not by the write response): the plugin's
        ``update_option`` returns false for an unchanged value, so only a re-read is
        honest. The comparison tolerates WP's storage casts (``_option_matches``); a
        genuine mismatch is an error that names both values AND ``write_landed``,
        because the site has already changed.
        """
        wrote = False
        try:
            _text(name, "name")
            await self.gw.ensure_options_capable()
            if dry_run:
                before = await self.gw.get_option(name)
                return {"action": "preview", "name": name, "value": value,
                        "current_value": before["value"], "exists": before["exists"]}
            await self.gw.set_option(name, value)
            wrote = True
            stored = (await self.gw.get_option(name))["value"]
            if not _option_matches(stored, value):
                return {"action": "error", "name": name, "write_landed": True,
                        "requested": value, "value": stored,
                        "error": f"OptionReadbackMismatch: {name} wrote {value!r} "
                                 f"read {stored!r} - the site did not store it"}
            return {"action": "applied", "name": name, "requested": value,
                    "value": stored, "verified": True}
        except Exception as e:
            return _error(e, **({"name": name, "requested": value, "write_landed": True}
                                if wrote else {}))

    async def delete_option(self, name: str, dry_run: bool = True) -> dict:
        """Delete an option row entirely (not "set it empty").

        WordPress's ``delete_option`` returns false when the option was not there.
        That is a no-op, not a failure, and comes back as ``action: "noop"`` - an
        error would send an operator hunting a problem that does not exist.
        """
        try:
            _text(name, "name")
            await self.gw.ensure_options_capable()
            if dry_run:
                before = await self.gw.get_option(name)
                return {"action": "preview", "name": name,
                        "current_value": before["value"], "exists": before["exists"]}
            if await self.gw.delete_option(name):
                return {"action": "deleted", "name": name, "deleted": True}
            return {"action": "noop", "name": name, "deleted": False,
                    "reason": f"option {name!r} was not set - nothing to delete"}
        except Exception as e:
            return _error(e)

    # -- comments -------------------------------------------------------------
    async def list_comments(self, post_id: int | None = None,
                            status: str | None = None) -> dict:
        """Comments, optionally filtered by post id and/or status. Read-only."""
        try:
            rows = await self.gw.list_comments(post_id=post_id, status=status)
            return {"action": "ok", "count": len(rows), "comments": rows}
        except Exception as e:
            return _error(e)

    async def moderate_comment(self, comment_id: int, status: str,
                               dry_run: bool = True) -> dict:
        """Set one comment's moderation status (approved/hold/spam/trash).

        The vocabulary is WP's own and is checked here: a near-miss like "approve" or
        "APPROVED" is refused rather than POSTed for WordPress to ignore. Verification
        is free - WP's 200 response IS the stored row - so a status that did not take
        is reported as an error with ``write_landed``, not as a success.
        """
        try:
            _positive_id(comment_id, "comment_id")
            if status not in COMMENT_STATUSES:
                raise AdminError(f"status must be one of {', '.join(COMMENT_STATUSES)}, "
                                 f"got {status!r}")
            if dry_run:
                return {"action": "preview", "comment_id": comment_id, "status": status}
            comment = await self.gw.update_comment(comment_id, {"status": status})
            got = comment.get("status")
            if got != status:
                return {"action": "error", "comment_id": comment_id, "write_landed": True,
                        "error": f"CommentStatusMismatch: asked for {status!r}, "
                                 f"the site reports {got!r}"}
            return {"action": "moderated", "comment": comment}
        except Exception as e:
            return _error(e)

    async def delete_comment(self, comment_id: int, force: bool = False,
                             dry_run: bool = True) -> dict:
        """Trash a comment (recoverable) or, with ``force``, delete it permanently.

        WordPress answers a plain DELETE with ``deleted: False`` and status "trash":
        the comment went to the trash, which is exactly what was asked for. That is
        reported as ``action: "trashed"`` with ``recoverable: True``. Only an answer
        that is neither deleted nor trashed is a real failure.
        """
        try:
            _positive_id(comment_id, "comment_id")
            if dry_run:
                effect = ("permanently deleted (not recoverable)" if force
                          else "moved to the trash (recoverable from wp-admin)")
                return {"action": "preview", "comment_id": comment_id, "force": bool(force),
                        "effect": f"comment {comment_id} will be {effect}"}
            result = await self.gw.delete_comment(comment_id, force=force)
            if result.get("deleted"):
                return {"action": "deleted", "comment_id": result.get("id", comment_id)}
            if result.get("status") == "trash":
                return {"action": "trashed", "comment_id": result.get("id", comment_id),
                        "recoverable": True}
            return {"action": "error", "comment_id": comment_id,
                    "error": f"CommentDeleteFailed: the site neither deleted nor trashed "
                             f"comment {comment_id} (status {result.get('status')!r})"}
        except Exception as e:
            return _error(e)
