import base64
import pytest
from skills.wpengine.ssh import SSHResult
import json
from wp_ops_mcp.ops.wpcli_gateway import (
    build_create_command,
    build_get_content_command, parse_get_content_output,
    build_get_post_info_command, parse_get_post_info_output,
    build_duplicate_command, build_update_content_command, parse_update_output,
    build_delete_command, build_purge_et_cache_command,
    WPCliContentGateway)


def test_get_content_command_is_one_line_and_quote_free():
    cmd = build_get_content_command("dermwellstg", 42)
    assert "\n" not in cmd and "'" not in cmd and '"' not in cmd
    assert "wp eval-file" in cmd and "42" in cmd


def test_parse_get_content_decodes_exact_bytes():
    content = '[et_pb_text]a "b"\n\nc[/et_pb_text]'
    b64 = base64.b64encode(content.encode()).decode()
    assert parse_get_content_output(f"B64:{b64}\n") == content


def test_parse_get_content_without_marker_raises():
    with pytest.raises(RuntimeError):
        parse_get_content_output("Error establishing a database connection")


def test_duplicate_and_update_commands_are_shell_safe():
    for cmd in (build_duplicate_command("dermwellstg", 42),
                build_update_content_command("dermwellstg", 42, 'x "y" $z\nnew')):
        assert "\n" not in cmd and "'" not in cmd and '"' not in cmd


def test_parse_update_output():
    assert parse_update_output("OK:42\n") == 42
    with pytest.raises(RuntimeError):
        parse_update_output("ERR:invalid post")


def test_delete_and_purge_commands():
    assert "post delete 42 --force" in build_delete_command("dermwellstg", 42)
    purge = build_purge_et_cache_command("dermwellstg", 42)
    assert purge.endswith("rm -rf wp-content/et-cache/42")
    assert "cd ~/sites/dermwellstg" in purge


# --- helpers ----------------------------------------------------------------

def _decode_b64_writes(cmd: str) -> dict[str, str]:
    """Map each `/tmp` path written via `echo <b64>|base64 -d>PATH` to its decoded text."""
    out: dict[str, str] = {}
    marker = "|base64 -d>"
    for part in cmd.split("; "):
        if part.startswith("echo ") and marker in part:
            b64 = part[len("echo "):part.index(marker)]
            path = part[part.index(marker) + len(marker):]
            out[path] = base64.b64decode(b64).decode("utf-8")
    return out


class FakeTransport:
    """Records run_raw commands; returns a canned SSHResult-shaped object."""

    def __init__(self, install="dermwellstg", stdout="", stderr="", exit_code=0):
        self.install = install
        self._result = SSHResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        self.raws: list[str] = []

    async def run(self, wp_command, timeout=30):
        self.raws.append(wp_command)
        return self._result

    async def run_raw(self, command, timeout=90):
        self.raws.append(command)
        return self._result


# --- new: raw-context + existence check on get ------------------------------

def test_get_content_command_uses_raw_context_and_existence_check():
    php = _decode_b64_writes(build_get_content_command("inst", 42))
    src = next(iter(php.values()))
    assert "get_post_field('post_content'" in src
    assert "'raw'" in src  # explicit raw context, no shortcode/filter mangling
    assert "get_post(" in src and "ERR:post not found" in src


def test_parse_get_content_checks_err_before_b64():
    with pytest.raises(RuntimeError) as e:
        parse_get_content_output("ERR:post not found\n")
    assert "post not found" in str(e.value)


def test_parse_get_content_malformed_b64_raises():
    # Matches _B64_RE's alphabet but has invalid length/padding -> decode fails.
    with pytest.raises(RuntimeError) as e:
        parse_get_content_output("B64:AAAAA")
    assert "malformed B64" in str(e.value)


# --- new: wp_slash on writes ------------------------------------------------

def test_write_commands_wp_slash_content():
    dup = _decode_b64_writes(build_duplicate_command("inst", 42))
    dup_src = next(iter(dup.values()))
    assert "wp_slash(" in dup_src and "wp_insert_post" in dup_src
    upd_php = [v for k, v in _decode_b64_writes(
        build_update_content_command("inst", 42, "x")).items() if k.endswith(".php")][0]
    assert "wp_slash(" in upd_php and "wp_update_post" in upd_php


# --- new: kses filters disabled on writes -----------------------------------

def _decode_php_from(cmd: str) -> str:
    """Return the decoded PHP source (the base64 blob that decodes to '<?php...')."""
    for text in _decode_b64_writes(cmd).values():
        if text.startswith("<?php"):
            return text
    raise AssertionError("no PHP blob found in command")


def test_write_commands_disable_kses_filters():
    # Under eval-file (anonymous user id 0) current_user_can('unfiltered_html') is
    # false, so wp_filter_post_kses would silently strip iframes/attrs on every write.
    for cmd, write_fn in [
        (build_create_command("s", "page", "T", "t", "draft",
                              "<iframe src=x></iframe>", {}), "wp_insert_post"),
        (build_duplicate_command("s", 42), "wp_insert_post"),
        (build_update_content_command("s", 42, "<iframe src=x></iframe>"),
         "wp_update_post"),
    ]:
        php = _decode_php_from(cmd)
        assert "kses_remove_filters();" in php
        assert php.index("kses_remove_filters();") < php.index(write_fn)


# --- new: unique /tmp paths + path-sync -------------------------------------

def test_update_commands_unique_paths():
    a = build_update_content_command("inst", 42, "one")
    b = build_update_content_command("inst", 42, "two")
    ap = [p for p in _decode_b64_writes(a)]
    bp = [p for p in _decode_b64_writes(b)]
    assert set(ap).isdisjoint(bp)  # no shared /tmp path across concurrent ops


def test_update_command_php_json_path_matches_shell():
    cmd = build_update_content_command("inst", 42, "hello")
    writes = _decode_b64_writes(cmd)
    json_path = next(p for p in writes if p.endswith(".json"))
    php_src = next(writes[p] for p in writes if p.endswith(".php"))
    # PHP must read exactly the json path the shell wrote.
    assert json_path in php_src


def test_get_and_duplicate_paths_unique_per_call():
    assert set(_decode_b64_writes(build_get_content_command("inst", 42))).isdisjoint(
        _decode_b64_writes(build_get_content_command("inst", 42)))
    assert set(_decode_b64_writes(build_duplicate_command("inst", 42))).isdisjoint(
        _decode_b64_writes(build_duplicate_command("inst", 42)))


# --- new: post id coercion --------------------------------------------------

@pytest.mark.parametrize("bad", [42.9, "42.9", "42; rm -rf /", "abc"])
def test_build_commands_reject_non_integral_ids(bad):
    for fn in (build_get_content_command, build_duplicate_command, build_delete_command,
               build_purge_et_cache_command):
        with pytest.raises(ValueError):
            fn("inst", bad)
    with pytest.raises(ValueError):
        build_update_content_command("inst", bad, "x")


def test_build_commands_accept_integral_ids():
    assert "43" in build_delete_command("inst", 43.0)
    assert "44" in build_delete_command("inst", "44")


# --- new: async method coverage ---------------------------------------------

# --- new: get_post_info command builder + parser (draft-marker guard) -------

def test_get_post_info_command_is_one_line_and_quote_free():
    cmd = build_get_post_info_command("dermwellstg", 42)
    assert "\n" not in cmd and "'" not in cmd and '"' not in cmd
    assert "wp eval-file" in cmd and "42" in cmd


def test_get_post_info_command_reads_name_and_status():
    php = _decode_php_from(build_get_post_info_command("inst", 42))
    assert "post_name" in php and "post_status" in php
    assert "get_post(" in php and "ERR:post not found" in php


def test_parse_get_post_info_roundtrip():
    info = {"name": "contact-wpops-draft-12", "status": "draft"}
    assert parse_get_post_info_output("INFO:" + json.dumps(info) + "\n") == info


def test_parse_get_post_info_err_raises():
    with pytest.raises(RuntimeError):
        parse_get_post_info_output("ERR:post not found\n")


def test_parse_get_post_info_no_marker_raises():
    with pytest.raises(RuntimeError):
        parse_get_post_info_output("Error establishing a database connection")


async def test_get_post_info_method_parses_dict():
    info = {"name": "x-wpops-draft-7", "status": "draft"}
    gw = WPCliContentGateway(FakeTransport(stdout="INFO:" + json.dumps(info) + "\n"))
    assert await gw.get_post_info(7) == info


async def test_get_post_content_roundtrips():
    content = "emoji 🚀 and backslash C:\\path\\to"
    b64 = base64.b64encode(content.encode()).decode()
    gw = WPCliContentGateway(FakeTransport(stdout=f"B64:{b64}\n"))
    assert await gw.get_post_content(42) == content


async def test_get_post_content_missing_post_raises():
    gw = WPCliContentGateway(FakeTransport(stdout="ERR:post not found\n"))
    with pytest.raises(RuntimeError):
        await gw.get_post_content(999)


async def test_duplicate_and_update_methods_parse_ids():
    dup_gw = WPCliContentGateway(FakeTransport(stdout="ID:101\n"))
    assert await dup_gw.duplicate_post(42) == 101
    upd_gw = WPCliContentGateway(FakeTransport(stdout="OK:42\n"))
    assert await upd_gw.update_post_content(42, "new content") == 42


async def test_delete_post_raises_without_success():
    ok = WPCliContentGateway(FakeTransport(stdout="Success: Deleted post 42.\n"))
    await ok.delete_post(42)  # no raise
    bad = WPCliContentGateway(FakeTransport(stdout="Warning: post not found\n"))
    with pytest.raises(RuntimeError):
        await bad.delete_post(42)


async def test_purge_et_cache_raises_on_nonzero_exit():
    ok = WPCliContentGateway(FakeTransport(stdout="", exit_code=0))
    await ok.purge_et_cache(42)  # no raise
    bad = WPCliContentGateway(FakeTransport(stderr="rm: permission denied", exit_code=1))
    with pytest.raises(RuntimeError):
        await bad.purge_et_cache(42)
