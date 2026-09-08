"""Unit tests for AdminOps (ops/admin.py): user CRUD, arbitrary options, comment moderation.

Fakes at the gateway boundary (RestContentGateway's user/option/comment surface) so the
tests pin the disciplines that make the highest-blast-radius surface in this server safe
to hand to a model:

  1. **A password never comes back out.** create_user/update_user previews and results are
     asserted to contain the literal secret NOWHERE in their repr - a preview that echoes
     a password puts it in every transcript and log that touches the tool.
  2. **Input is validated BEFORE the gateway.** Bad ids, empty option names, empty field
     dicts and off-vocabulary comment statuses must not reach the wire at all (the fake's
     `calls` list is the assertion surface for "never reached the gateway").
  3. **The site's own answers are read honestly, in both directions.** A landed option
     write that the site stored in another scalar form is a SUCCESS, not a mismatch; a
     delete of an option that was never there is a no-op, not an error; a comment that
     WordPress trashed (deleted=False, status="trash") is a success, not a failure.

Every method never raises: a gateway that explodes on every call still yields
``{"action": "error"}``.
"""
import pytest

from wp_ops_mcp.ops.admin import COMMENT_STATUSES, AdminError, AdminOps

SECRET = "S3cret-passw0rd!"

USERS = [
    {"id": 1, "username": "admin", "name": "Admin", "email": "a@x.test",
     "roles": ["administrator"], "url": ""},
    {"id": 7, "username": "editor1", "name": "Ed", "email": "e@x.test",
     "roles": ["editor"], "url": ""},
]

COMMENTS = [
    {"id": 11, "post": 5, "author_name": "Bot", "content": "buy pills",
     "status": "hold", "date": "2026-07-01T00:00:00"},
]

_UNSET = object()

_USER_EDITABLE = ("name", "email", "roles", "password", "url", "description")


class FakeAdminGateway:
    """REST-shaped admin gateway recording every call that reached the wire.

    Knobs model the honest behaviours of a real site:
      `stored`        - what get_option reports AFTER a set (the readback knob: WP casts
                        and escapes as it stores, and can also lose a write entirely);
      `option_deleted`- delete_option's bool (False = there was nothing to delete);
      `update_status` - the status WP echoes after a comment update (a site that did not
                        take the moderation);
      `delete_comment_result` - the raw gateway shape for a comment delete;
      `error`         - every call raises;
      `options_error` - only the option version gate raises (old plugin).
    """

    def __init__(self, users=None, comments=None, options=None, error=None,
                 options_error=None, stored=_UNSET, option_deleted=True,
                 update_status=None, delete_comment_result=None):
        self.users = [dict(u) for u in (USERS if users is None else users)]
        self.comments = [dict(c) for c in (COMMENTS if comments is None else comments)]
        self.options = dict({"blogname": "Site"} if options is None else options)
        self.error = error
        self.options_error = options_error
        self.stored = stored
        self.option_deleted = option_deleted
        self.update_status = update_status
        self.delete_comment_result = delete_comment_result
        self.calls = []
        self.creates = []
        self.updates = []
        self.deletes = []
        self.sets = []
        self.new_user_id = 42

    def _boom(self):
        if self.error is not None:
            raise self.error

    # -- users ---------------------------------------------------------------
    async def list_users(self, search=None):
        self.calls.append(("list_users", search))
        self._boom()
        rows = self.users if not search else [u for u in self.users if search in u["username"]]
        return [dict(u) for u in rows]

    async def get_user(self, user_id):
        self.calls.append(("get_user", user_id))
        self._boom()
        for u in self.users:
            if u["id"] == user_id:
                return dict(u)
        raise KeyError(f"no user {user_id}")

    async def create_user(self, username, email, password, roles=None, name=None):
        self.calls.append("create_user")
        self._boom()
        self.creates.append({"username": username, "email": email, "password": password,
                             "roles": roles, "name": name})
        # Deliberately echoes the password back: the ops layer must strip it, so a
        # future gateway change cannot leak one into a transcript.
        return {"id": self.new_user_id, "username": username, "name": name or username,
                "email": email, "url": "", "password": password,
                "roles": [roles] if isinstance(roles, str) else list(roles or [])}

    async def update_user(self, user_id, fields):
        self.calls.append("update_user")
        self._boom()
        bad = [k for k in fields if k not in _USER_EDITABLE]
        if bad:
            raise ValueError(f"unsupported user field(s) {', '.join(sorted(bad))}")
        self.updates.append({"user_id": user_id, "fields": dict(fields)})
        row = dict(self.users[0])
        row["id"] = user_id
        row.update({k: v for k, v in fields.items() if k != "password"})
        return row

    async def delete_user(self, user_id, reassign=0):
        self.calls.append("delete_user")
        self._boom()
        self.deletes.append({"user_id": user_id, "reassign": reassign})
        return {"deleted": True, "previous": user_id}

    # -- options -------------------------------------------------------------
    async def ensure_options_capable(self):
        self.calls.append("ensure_options_capable")
        if self.options_error is not None:
            raise self.options_error
        self._boom()
        return {"plugin_version": "1.3.0"}

    async def list_option_names(self):
        self.calls.append("list_option_names")
        self._boom()
        return sorted(self.options)

    async def get_option(self, name):
        self.calls.append(("get_option", name))
        self._boom()
        if self.stored is not _UNSET and self.sets:       # post-write readback knob
            return {"name": name, "value": self.stored, "exists": True}
        if name in self.options:
            return {"name": name, "value": self.options[name], "exists": True}
        return {"name": name, "value": None, "exists": False}

    async def set_option(self, name, value):
        self.calls.append("set_option")
        self._boom()
        self.sets.append({"name": name, "value": value})
        self.options[name] = value
        return {"name": name, "value": value}

    async def delete_option(self, name):
        self.calls.append(("delete_option", name))
        self._boom()
        self.deletes.append({"option": name})
        self.options.pop(name, None)
        return self.option_deleted

    # -- comments ------------------------------------------------------------
    async def list_comments(self, post_id=None, status=None):
        self.calls.append(("list_comments", post_id, status))
        self._boom()
        return [dict(c) for c in self.comments]

    async def update_comment(self, comment_id, fields):
        self.calls.append("update_comment")
        self._boom()
        self.updates.append({"comment_id": comment_id, "fields": dict(fields)})
        row = dict(self.comments[0])
        row["id"] = comment_id
        row.update(fields)
        if self.update_status is not None:
            row["status"] = self.update_status
        return row

    async def delete_comment(self, comment_id, force=False):
        self.calls.append(("delete_comment", comment_id, force))
        self._boom()
        self.deletes.append({"comment_id": comment_id, "force": force})
        if self.delete_comment_result is not None:
            return dict(self.delete_comment_result)
        if force:
            return {"deleted": True, "id": comment_id, "status": "deleted"}
        return {"deleted": False, "id": comment_id, "status": "trash"}


def _boomer(exc=None):
    return FakeAdminGateway(error=exc or RuntimeError("wire down"))


# ============================ users =========================================

async def test_list_users_returns_rows():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).list_users()
    assert out["action"] == "ok" and out["count"] == 2
    assert [u["username"] for u in out["users"]] == ["admin", "editor1"]


async def test_list_users_passes_search_through():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).list_users(search="editor")
    assert out["count"] == 1
    assert gw.calls == [("list_users", "editor")]


async def test_list_users_never_raises():
    out = await AdminOps(_boomer()).list_users()
    assert out["action"] == "error" and "RuntimeError: wire down" in out["error"]


async def test_get_user_ok():
    out = await AdminOps(FakeAdminGateway()).get_user(7)
    assert out["action"] == "ok" and out["user"]["username"] == "editor1"


@pytest.mark.parametrize("bad", [0, -3, True, "7", None])
async def test_get_user_bad_id_never_reaches_gateway(bad):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).get_user(bad)
    assert out["action"] == "error" and "user_id must be a positive int" in out["error"]
    assert gw.calls == []


async def test_get_user_never_raises():
    out = await AdminOps(_boomer()).get_user(7)
    assert out["action"] == "error"


async def test_create_user_dry_run_previews_without_writing():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).create_user("newbie", "n@x.test", SECRET,
                                         roles="editor", name="New Bie")
    assert out["action"] == "preview"
    assert out["user"]["username"] == "newbie" and out["user"]["roles"] == "editor"
    assert gw.creates == [] and gw.calls == []


async def test_create_user_preview_never_echoes_the_password():
    out = await AdminOps(FakeAdminGateway()).create_user("newbie", "n@x.test", SECRET)
    assert SECRET not in repr(out)
    assert out["password"] == "<not echoed>"


async def test_create_user_result_never_echoes_the_password():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).create_user("newbie", "n@x.test", SECRET,
                                         roles="editor", dry_run=False)
    assert out["action"] == "created" and out["user"]["id"] == 42
    assert SECRET not in repr(out)              # gateway echoed it; ops must strip it
    assert "password" not in out["user"]
    assert gw.creates[0]["password"] == SECRET  # ...but it DID reach the site


@pytest.mark.parametrize("username,email,password", [
    ("", "n@x.test", SECRET),
    ("   ", "n@x.test", SECRET),
    ("newbie", "", SECRET),
    ("newbie", "n@x.test", ""),
    (None, "n@x.test", SECRET),
    ("newbie", "n@x.test", None),
])
async def test_create_user_empty_args_never_reach_gateway(username, email, password):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).create_user(username, email, password, dry_run=False)
    assert out["action"] == "error"
    assert gw.creates == [] and gw.calls == []


async def test_create_user_never_raises():
    out = await AdminOps(_boomer()).create_user("n", "n@x.test", SECRET, dry_run=False)
    assert out["action"] == "error" and SECRET not in repr(out)


async def test_update_user_dry_run_previews_fields():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).update_user(7, {"name": "Edward"})
    assert out == {"action": "preview", "user_id": 7, "fields": {"name": "Edward"}}
    assert gw.updates == []


async def test_update_user_preview_masks_a_password_field():
    out = await AdminOps(FakeAdminGateway()).update_user(7, {"password": SECRET})
    assert SECRET not in repr(out)
    assert out["fields"]["password"] == "<not echoed>"


async def test_update_user_applies():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).update_user(7, {"name": "Edward"}, dry_run=False)
    assert out["action"] == "updated" and out["user"]["name"] == "Edward"
    assert gw.updates == [{"user_id": 7, "fields": {"name": "Edward"}}]


@pytest.mark.parametrize("fields", [{}, None, "name=x", []])
async def test_update_user_empty_fields_refused_before_the_gateway(fields):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).update_user(7, fields, dry_run=False)
    assert out["action"] == "error" and "no user fields" in out["error"]
    assert gw.calls == []


async def test_update_user_bad_key_surfaces_the_gateway_valueerror():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).update_user(7, {"usename": "typo"}, dry_run=False)
    assert out["action"] == "error" and "ValueError" in out["error"]
    assert gw.updates == []


async def test_update_user_bad_id_never_reaches_gateway():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).update_user(0, {"name": "x"}, dry_run=False)
    assert out["action"] == "error" and "user_id must be a positive int" in out["error"]
    assert gw.calls == []


async def test_delete_user_preview_states_content_deletion_when_reassign_zero():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_user(7)
    assert out["action"] == "preview" and out["user_id"] == 7 and out["reassign"] == 0
    assert "deleted" in out["effect"].lower()
    assert gw.deletes == []


async def test_delete_user_preview_names_the_reassign_target():
    out = await AdminOps(FakeAdminGateway()).delete_user(7, reassign=1)
    assert out["reassign"] == 1
    assert "1" in out["effect"] and "reassign" in out["effect"].lower()


async def test_delete_user_applies_with_reassign():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_user(7, reassign=1, dry_run=False)
    assert out["action"] == "deleted" and out["user_id"] == 7 and out["reassign"] == 1
    assert gw.deletes == [{"user_id": 7, "reassign": 1}]


async def test_delete_user_applies_with_reassign_zero_and_zero_reaches_the_gateway():
    """0 is a real reassign target ("delete their content"), not a missing argument.

    A falsy-check anywhere on this path would silently drop it, and the gateway's own
    default would then decide the fate of the user's posts.
    """
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_user(7, reassign=0, dry_run=False)
    assert out["action"] == "deleted" and out["reassign"] == 0
    assert gw.deletes == [{"user_id": 7, "reassign": 0}]


@pytest.mark.parametrize("bad", [-1, True, "0", None, 1.5])
async def test_delete_user_bad_reassign_never_reaches_gateway(bad):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_user(7, reassign=bad, dry_run=False)
    assert out["action"] == "error" and "reassign" in out["error"]
    assert gw.calls == []


async def test_delete_user_never_raises():
    out = await AdminOps(_boomer()).delete_user(7, dry_run=False)
    assert out["action"] == "error"


# ============================ options =======================================

async def test_list_options_returns_names_and_says_it_is_autoloaded_only():
    out = await AdminOps(FakeAdminGateway(options={"a": 1, "b": 2})).list_options()
    assert out["action"] == "ok" and out["names"] == ["a", "b"] and out["count"] == 2
    assert "autoload" in out["note"].lower()


async def test_list_options_version_gate_is_an_error_dict():
    gw = FakeAdminGateway(options_error=RuntimeError("wp-ops-connect 1.2.0 < required 1.3.0"))
    out = await AdminOps(gw).list_options()
    assert out["action"] == "error" and "1.3.0" in out["error"]
    assert "list_option_names" not in gw.calls


async def test_get_option_existing():
    out = await AdminOps(FakeAdminGateway()).get_option("blogname")
    assert out == {"action": "ok", "name": "blogname", "value": "Site", "exists": True}


async def test_get_option_absent_reports_exists_false_not_an_error():
    out = await AdminOps(FakeAdminGateway()).get_option("nope")
    assert out["action"] == "ok" and out["exists"] is False and out["value"] is None


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
async def test_get_option_bad_name_never_reaches_gateway(bad):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).get_option(bad)
    assert out["action"] == "error" and "name must be a non-empty string" in out["error"]
    assert gw.calls == []


async def test_get_option_never_raises():
    out = await AdminOps(_boomer()).get_option("blogname")
    assert out["action"] == "error"


async def test_set_option_dry_run_previews_without_writing():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).set_option("wpops_test", 42)
    assert out["action"] == "preview" and out["name"] == "wpops_test" and out["value"] == 42
    assert out["exists"] is False               # preview reports the CURRENT state
    assert gw.sets == []


async def test_set_option_dry_run_shows_the_current_value_it_would_overwrite():
    out = await AdminOps(FakeAdminGateway()).set_option("blogname", "New")
    assert out["current_value"] == "Site" and out["exists"] is True


async def test_set_option_applies_and_verifies_by_readback():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).set_option("wpops_test", "hello", dry_run=False)
    assert out["action"] == "applied" and out["verified"] is True
    assert out["value"] == "hello"
    assert gw.sets == [{"name": "wpops_test", "value": "hello"}]


@pytest.mark.parametrize("sent,stored", [
    (25, "25"),                 # WP stores every option as a string
    ("25", 25),
    (True, 1),                  # a bool round-trips as 1/0
    (False, 0),
    ("Smith & Jones", "Smith &amp; Jones"),   # sanitize_option() esc_html's some options
    ([1, 2], ["1", "2"]),
])
async def test_set_option_tolerates_wp_storage_casts(sent, stored):
    gw = FakeAdminGateway(stored=stored)
    out = await AdminOps(gw).set_option("wpops_test", sent, dry_run=False)
    assert out["action"] == "applied", out
    assert out["value"] == stored               # the site's ACTUAL value is reported


@pytest.mark.parametrize("stored", ["partial", "yes", "on", 2, "true", "false"])
async def test_set_option_bool_request_rejects_any_other_truthy_or_falsy_form(stored):
    """A bool write verifies against WP's canonical forms ONLY, never by truthiness.

    Writing True to an option that stayed at an unrelated value ("partial") must not
    read as a landed write just because "partial" is truthy - that is a false verify
    on the widest surface in the server.
    """
    gw = FakeAdminGateway(stored=stored)
    out = await AdminOps(gw).set_option("wpops_test", True, dry_run=False)
    assert out["action"] == "error", out
    assert out["write_landed"] is True
    assert out["value"] == stored


@pytest.mark.parametrize("sent,stored", [
    (True, True), (True, 1), (True, "1"),
    (False, False), (False, 0), (False, ""), (False, "0"),
])
async def test_set_option_bool_request_accepts_wp_canonical_forms(sent, stored):
    gw = FakeAdminGateway(stored=stored)
    out = await AdminOps(gw).set_option("wpops_test", sent, dry_run=False)
    assert out["action"] == "applied" and out["verified"] is True, out


async def test_set_option_non_bool_request_keeps_the_tolerant_compare():
    """Only a bool REQUEST is strict; a plain string still uses the tolerant path."""
    gw = FakeAdminGateway(stored="yes")
    out = await AdminOps(gw).set_option("wpops_test", "yes", dry_run=False)
    assert out["action"] == "applied" and out["verified"] is True


async def test_set_option_readback_mismatch_is_an_error_naming_both_values():
    gw = FakeAdminGateway(stored="something else")
    out = await AdminOps(gw).set_option("wpops_test", "hello", dry_run=False)
    assert out["action"] == "error" and out["write_landed"] is True
    assert "hello" in out["error"] and "something else" in out["error"]


async def test_set_option_version_gate_blocks_even_the_preview():
    gw = FakeAdminGateway(options_error=RuntimeError("wp-ops-connect 1.2.0 < required 1.3.0"))
    out = await AdminOps(gw).set_option("wpops_test", 1)
    assert out["action"] == "error" and "1.3.0" in out["error"]
    assert gw.sets == []


@pytest.mark.parametrize("bad", ["", None, 5])
async def test_set_option_bad_name_never_reaches_gateway(bad):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).set_option(bad, 1, dry_run=False)
    assert out["action"] == "error" and "name must be a non-empty string" in out["error"]
    assert gw.calls == []


async def test_set_option_never_raises():
    out = await AdminOps(_boomer()).set_option("x", 1, dry_run=False)
    assert out["action"] == "error"


async def test_delete_option_dry_run_previews_without_deleting():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_option("blogname")
    assert out["action"] == "preview" and out["name"] == "blogname" and out["exists"] is True
    assert gw.deletes == []


async def test_delete_option_deletes():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_option("blogname", dry_run=False)
    assert out["action"] == "deleted" and out["name"] == "blogname"
    assert gw.deletes == [{"option": "blogname"}]


async def test_delete_option_absent_is_a_noop_not_an_error():
    gw = FakeAdminGateway(option_deleted=False)
    out = await AdminOps(gw).delete_option("never_existed", dry_run=False)
    assert out["action"] == "noop" and out["deleted"] is False
    assert "nothing to delete" in out["reason"]


async def test_delete_option_never_raises():
    out = await AdminOps(_boomer()).delete_option("x", dry_run=False)
    assert out["action"] == "error"


# ============================ comments ======================================

async def test_list_comments_passes_filters_through():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).list_comments(post_id=5, status="hold")
    assert out["action"] == "ok" and out["count"] == 1
    assert gw.calls == [("list_comments", 5, "hold")]


async def test_list_comments_never_raises():
    out = await AdminOps(_boomer()).list_comments()
    assert out["action"] == "error"


@pytest.mark.parametrize("status", COMMENT_STATUSES)
async def test_moderate_comment_accepts_the_wp_vocabulary(status):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).moderate_comment(11, status, dry_run=False)
    assert out["action"] == "moderated" and out["comment"]["status"] == status
    assert gw.updates == [{"comment_id": 11, "fields": {"status": status}}]


@pytest.mark.parametrize("bad", ["approve", "APPROVED", "deleted", "", None, 1])
async def test_moderate_comment_bad_status_never_reaches_gateway(bad):
    gw = FakeAdminGateway()
    out = await AdminOps(gw).moderate_comment(11, bad, dry_run=False)
    assert out["action"] == "error" and "approved" in out["error"]
    assert gw.calls == []


async def test_moderate_comment_dry_run_previews():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).moderate_comment(11, "spam")
    assert out == {"action": "preview", "comment_id": 11, "status": "spam"}
    assert gw.updates == []


async def test_moderate_comment_status_not_taken_is_an_error():
    gw = FakeAdminGateway(update_status="hold")     # site did not take the change
    out = await AdminOps(gw).moderate_comment(11, "approved", dry_run=False)
    assert out["action"] == "error" and out["write_landed"] is True
    assert "hold" in out["error"]


async def test_moderate_comment_bad_id_never_reaches_gateway():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).moderate_comment(0, "spam", dry_run=False)
    assert out["action"] == "error" and "comment_id must be a positive int" in out["error"]
    assert gw.calls == []


async def test_moderate_comment_never_raises():
    out = await AdminOps(_boomer()).moderate_comment(11, "spam", dry_run=False)
    assert out["action"] == "error"


async def test_delete_comment_dry_run_says_trash_by_default():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_comment(11)
    assert out["action"] == "preview" and out["force"] is False
    assert "trash" in out["effect"].lower()
    assert gw.deletes == []


async def test_delete_comment_dry_run_says_permanent_when_forced():
    out = await AdminOps(FakeAdminGateway()).delete_comment(11, force=True)
    assert out["force"] is True and "permanent" in out["effect"].lower()


async def test_delete_comment_without_force_reports_trashed_not_an_error():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_comment(11, dry_run=False)
    assert out["action"] == "trashed" and out["comment_id"] == 11
    assert out["recoverable"] is True
    assert gw.deletes == [{"comment_id": 11, "force": False}]


async def test_delete_comment_force_reports_deleted():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_comment(11, force=True, dry_run=False)
    assert out["action"] == "deleted" and out["comment_id"] == 11
    assert gw.deletes == [{"comment_id": 11, "force": True}]


async def test_delete_comment_neither_deleted_nor_trashed_is_an_error():
    gw = FakeAdminGateway(delete_comment_result={"deleted": False, "id": 11,
                                                 "status": "hold"})
    out = await AdminOps(gw).delete_comment(11, dry_run=False)
    assert out["action"] == "error" and "hold" in out["error"]


async def test_delete_comment_bad_id_never_reaches_gateway():
    gw = FakeAdminGateway()
    out = await AdminOps(gw).delete_comment(True, dry_run=False)
    assert out["action"] == "error" and "comment_id must be a positive int" in out["error"]
    assert gw.calls == []


async def test_delete_comment_never_raises():
    out = await AdminOps(_boomer()).delete_comment(11, dry_run=False)
    assert out["action"] == "error"


# ============================ contract ======================================

def test_comment_statuses_are_wps_own_vocabulary():
    assert COMMENT_STATUSES == ("approved", "hold", "spam", "trash")


def test_admin_error_is_a_runtimeerror():
    assert issubclass(AdminError, RuntimeError)
