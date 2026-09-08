"""Tests for the wp_ops_mcp WP REST transport."""
import httpx
import pytest
import respx

from wp_ops_mcp.transport.rest import WPRestClient, RestProbe


class TestStripAppPassword:
    def test_removes_display_spaces(self):
        # WP prints app passwords chunked with spaces for readability.
        assert WPRestClient.strip_app_password("abcd efgh ijkl mnop qrst uvwx") == \
            "abcdefghijklmnopqrstuvwx"

    def test_none_passthrough(self):
        assert WPRestClient.strip_app_password(None) is None

    def test_no_spaces_unchanged(self):
        assert WPRestClient.strip_app_password("plainpassword") == "plainpassword"


class TestProbe:
    @respx.mock
    async def test_reachable_on_wp_rest_json(self):
        respx.get("https://example.com/wp-json/").mock(
            return_value=httpx.Response(200, json={"name": "Example", "namespaces": ["wp/v2"]})
        )
        client = WPRestClient("https://example.com")
        probe = await client.probe()
        assert isinstance(probe, RestProbe)
        assert probe.reachable is True
        assert probe.status_code == 200

    @respx.mock
    async def test_not_reachable_when_json_is_not_wp(self):
        respx.get("https://example.com/wp-json/").mock(
            return_value=httpx.Response(200, json={"hello": "world"})
        )
        client = WPRestClient("https://example.com")
        probe = await client.probe()
        assert probe.reachable is False
        assert probe.status_code == 200

    @respx.mock
    async def test_not_reachable_on_404(self):
        respx.get("https://example.com/wp-json/").mock(
            return_value=httpx.Response(404, text="not found")
        )
        client = WPRestClient("https://example.com")
        probe = await client.probe()
        assert probe.reachable is False
        assert probe.status_code == 404

    @respx.mock
    async def test_not_reachable_on_connect_error(self):
        respx.get("https://example.com/wp-json/").mock(
            side_effect=httpx.ConnectError("boom")
        )
        client = WPRestClient("https://example.com")
        probe = await client.probe()
        assert probe.reachable is False
        assert probe.status_code is None
        assert "boom" in probe.detail or "error" in probe.detail.lower()

    def test_base_url_trailing_slash_normalized(self):
        client = WPRestClient("https://example.com/")
        assert client.base_url == "https://example.com"

    @respx.mock
    async def test_probe_sends_fleet_scanner_user_agent(self):
        # The operator runs from PH; the CF country-challenge WAF rule skips the
        # WP-Ops-MCP/1.0 UA on every zone, so the probe must send it or it
        # false-negatives behind the challenge.
        route = respx.get("https://example.com/wp-json/").mock(
            return_value=httpx.Response(200, json={"name": "Example"})
        )
        client = WPRestClient("https://example.com")
        await client.probe()
        sent_ua = route.calls.last.request.headers.get("user-agent")
        assert sent_ua == "WP-Ops-MCP/1.0"
