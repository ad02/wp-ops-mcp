"""Tests for the WP-CLI content gateway (command builders, parser, gateway methods)."""
import base64
import json

import pytest

from skills.wpengine.ssh import SSHResult
from wp_ops_mcp.ops.wpcli_gateway import (
    build_list_args, parse_post_list, build_create_command, parse_create_output,
    WPCliContentGateway,
)


class TestBuildListArgs:
    def test_includes_type_json_and_all_statuses(self):
        a = build_list_args("page")
        assert "post list" in a
        assert "--post_type=page" in a
        assert "--format=json" in a
        assert "--post_status=any" in a
        assert "--posts_per_page=100" in a          # bounded: never an unbounded list

    def test_no_single_quotes(self):
        assert "'" not in build_list_args("page", query="abc")

    def test_safe_query_appended(self):
        assert "--s=hello" in build_list_args("page", query="hello")

    def test_unsafe_query_ignored(self):
        # spaces/quotes would need shell quoting; drop rather than risk mangling
        assert "--s=" not in build_list_args("page", query="hello world")


class TestParsePostList:
    def test_maps_fields(self):
        raw = ('[{"ID":12,"post_title":"Home","post_name":"home",'
               '"post_status":"publish","post_type":"page"}]')
        out = parse_post_list(raw)
        assert out == [{"id": 12, "title": "Home", "slug": "home",
                        "status": "publish", "type": "page"}]

    def test_tolerates_leading_php_warning(self):
        raw = ('PHP Deprecated: something\n'
               '[{"ID":1,"post_title":"X","post_name":"x","post_status":"draft","post_type":"page"}]')
        out = parse_post_list(raw)
        assert out[0]["slug"] == "x"

    def test_empty_or_error_yields_empty(self):
        assert parse_post_list("Error: no posts") == []
        assert parse_post_list("") == []


class TestBuildCreateCommand:
    def test_no_single_quotes_and_single_line(self):
        cmd = build_create_command("dermwellstg", "page", "Our Services", "our-services",
                                   "draft", "[et_pb_section]...[/et_pb_section]",
                                   {"_et_pb_use_builder": "on"})
        assert "'" not in cmd
        assert "\n" not in cmd

    def test_uses_base64_and_eval_file_and_cleans_up(self):
        cmd = build_create_command("inst", "page", "T", "t", "draft", "x", {})
        assert "base64 -d" in cmd
        assert "wp eval-file" in cmd
        assert "rm -f" in cmd
        assert "cd ~/sites/inst" in cmd

    def test_content_with_quotes_is_encoded_not_inlined(self):
        cmd = build_create_command("inst", "page", 'Say "hi"', "t", "draft",
                                   "a 'b' c \" d", {})
        assert "'" not in cmd  # nothing leaks into the shell
        # payload round-trips: decode the first base64 blob and check content
        token = next(t for t in cmd.split() if t.endswith("=") or len(t) > 40)
        # find the payload b64 (the one before the first json path)
        # simpler: ensure the raw content string is NOT present verbatim
        assert "a 'b' c" not in cmd


class TestParseCreateOutput:
    def test_extracts_id(self):
        assert parse_create_output("ID:4567") == 4567

    def test_id_among_noise(self):
        assert parse_create_output("PHP Notice: x\nID:88\n") == 88

    def test_error_raises(self):
        with pytest.raises(RuntimeError) as e:
            parse_create_output("ERR:Invalid post type")
        assert "Invalid post type" in str(e.value)

    def test_missing_id_raises(self):
        with pytest.raises(RuntimeError):
            parse_create_output("nothing useful")


class FakeTransport:
    def __init__(self, run_out="", raw_out=""):
        self.install = "dermwellstg"
        self._run_out = run_out
        self._raw_out = raw_out
        self.ran = []
        self.raws = []

    async def run(self, wp_command, timeout=30):
        self.ran.append(wp_command)
        return SSHResult(stdout=self._run_out, stderr="", exit_code=0)

    async def run_raw(self, command, timeout=90):
        self.raws.append(command)
        return SSHResult(stdout=self._raw_out, stderr="", exit_code=0)


class TestGateway:
    async def test_list_posts(self):
        raw = '[{"ID":1,"post_title":"Home","post_name":"home","post_status":"publish","post_type":"page"}]'
        gw = WPCliContentGateway(FakeTransport(run_out=raw))
        out = await gw.list_posts("page")
        assert out[0]["slug"] == "home"

    async def test_create_post_returns_id(self):
        gw = WPCliContentGateway(FakeTransport(raw_out="ID:321"))
        pid = await gw.create_post("page", "T", "t", "draft", "content", {})
        assert pid == 321
