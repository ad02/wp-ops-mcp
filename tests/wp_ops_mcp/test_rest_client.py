import asyncio
import pytest
from wp_ops_mcp.transport.rest import WPRestClient, RestError


class FakeResp:
    def __init__(self, status, data=None, text="x", headers=None):
        self.status_code, self._data, self.text = status, data, text
        self.headers = headers if headers is not None else {}

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


class FakeHttp:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    async def request(self, method, url, params=None, json=None, headers=None, auth=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "json": json, "headers": headers, "auth": auth})
        if self.exc:
            raise self.exc
        return self.resp

    async def post(self, url, content=None, headers=None, auth=None):
        self.calls.append({"method": "POST", "url": url, "content": content,
                           "headers": headers, "auth": auth})
        if self.exc:
            raise self.exc
        return self.resp


def _client():
    return WPRestClient("https://site.test", app_user="u", app_password="a b c")


def test_request_sends_auth_ua_and_returns_json():
    http = FakeHttp(FakeResp(200, {"ok": True}))
    status, data = asyncio.run(_client().request("GET", "/wp-json/wpops/v1/info", http=http))
    assert (status, data) == (200, {"ok": True})
    call = http.calls[0]
    assert call["url"] == "https://site.test/wp-json/wpops/v1/info"
    assert call["auth"] == ("u", "abc")                     # spaces stripped
    assert call["headers"]["User-Agent"] == "WP-Ops-MCP/1.0"


def test_request_network_error_raises_resterror():
    import httpx
    http = FakeHttp(exc=httpx.ConnectError("boom"))
    with pytest.raises(RestError):
        asyncio.run(_client().request("GET", "/wp-json/", http=http))


def test_2xx_non_json_raises_and_4xx_passes_body_through():
    with pytest.raises(RestError):
        asyncio.run(_client().request("GET", "/wp-json/x", http=FakeHttp(FakeResp(200, None))))
    status, data = asyncio.run(_client().request(
        "GET", "/wp-json/x", http=FakeHttp(FakeResp(404, {"code": "rest_no_route"}))))
    assert status == 404 and data["code"] == "rest_no_route"


def test_redirect_is_surfaced_not_followed():
    # A 3xx on the authenticated path must be surfaced, never followed: following
    # would downgrade POST->GET / drop the body and strip auth across hosts.
    http = FakeHttp(FakeResp(301, None, headers={"location": "https://www.x.test/wp-json/y"}))
    status, data = asyncio.run(_client().request("POST", "/wp-json/x", http=http))
    assert status == 301
    assert data["redirect"] == "https://www.x.test/wp-json/y"
    assert len(http.calls) == 1                             # exactly one request, no follow


def test_delete_with_params_and_no_auth():
    # No credentials -> no auth tuple; params must still pass straight through.
    http = FakeHttp(FakeResp(200, {"deleted": True}))
    client = WPRestClient("https://site.test")
    status, data = asyncio.run(
        client.request("DELETE", "/wp-json/x/1", params={"force": "true"}, http=http))
    assert (status, data) == (200, {"deleted": True})
    call = http.calls[0]
    assert call["auth"] is None
    assert call["params"] == {"force": "true"}


def test_upload_sends_binary_with_disposition():
    # WP core's REST media route takes the raw file body plus Content-Disposition -
    # no multipart wrapper - so the bytes must reach httpx untouched via content=.
    png = b"\x89PNG\r\n\x1a\n\x00binary\xff"
    http = FakeHttp(FakeResp(201, {"id": 9, "source_url": "https://site.test/up/x.png"}))
    status, data = asyncio.run(_client().upload(
        "/wp-json/wp/v2/media", "my-photo.png", png, "image/png", http=http))
    assert (status, data) == (201, {"id": 9, "source_url": "https://site.test/up/x.png"})
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://site.test/wp-json/wp/v2/media"
    assert call["content"] == png
    assert call["headers"]["Content-Disposition"] == 'attachment; filename="my-photo.png"'
    assert call["headers"]["Content-Type"] == "image/png"
    assert call["headers"]["User-Agent"] == "WP-Ops-MCP/1.0"
    assert call["auth"] == ("u", "abc")                     # spaces stripped


def test_upload_redirect_surfaced_and_network_error_raises():
    # Same semantics as request(): a 3xx is reported, never followed (following would
    # drop the body and strip auth across hosts); network failure becomes RestError.
    import httpx
    http = FakeHttp(FakeResp(301, None, headers={"location": "https://www.x.test/media"}))
    status, data = asyncio.run(
        _client().upload("/wp-json/wp/v2/media", "a.png", b"x", "image/png", http=http))
    assert status == 301
    assert data["redirect"] == "https://www.x.test/media"
    assert len(http.calls) == 1                             # exactly one request, no follow
    with pytest.raises(RestError):
        asyncio.run(_client().upload("/wp-json/wp/v2/media", "a.png", b"x", "image/png",
                                     http=FakeHttp(exc=httpx.ConnectError("boom"))))


# respx is a test-only dependency; skip the real-path test cleanly if it is absent.
try:
    import respx
    _HAS_RESPX = True
except ImportError:
    _HAS_RESPX = False


@pytest.mark.skipif(not _HAS_RESPX, reason="respx not installed")
def test_real_path_sends_fleet_scanner_ua_via_respx():
    # Exercises the real (http=None) branch end to end: opens a live httpx client
    # against a respx-mocked route and asserts the fleet UA reaches the wire.
    import httpx
    with respx.mock:
        route = respx.get("https://site.test/wp-json/ping").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        status, data = asyncio.run(
            WPRestClient("https://site.test").request("GET", "/wp-json/ping", http=None))
    assert (status, data) == (200, {"ok": True})
    assert route.calls.last.request.headers.get("user-agent") == "WP-Ops-MCP/1.0"


@pytest.mark.skipif(not _HAS_RESPX, reason="respx not installed")
def test_real_path_upload_puts_bytes_on_the_wire():
    # Proves the http=None branch against real httpx: the body is the raw file bytes
    # (no multipart), and the disposition/type headers survive the client construction.
    import httpx
    png = b"\x89PNG\r\n\x1a\nbytes"
    with respx.mock:
        route = respx.post("https://site.test/wp-json/wp/v2/media").mock(
            return_value=httpx.Response(201, json={"id": 3, "source_url": "https://site.test/a.png"})
        )
        status, data = asyncio.run(WPRestClient("https://site.test", "u", "p").upload(
            "/wp-json/wp/v2/media", "a.png", png, "image/png", http=None))
    assert (status, data) == (201, {"id": 3, "source_url": "https://site.test/a.png"})
    req = route.calls.last.request
    assert req.content == png
    assert req.headers["content-disposition"] == 'attachment; filename="a.png"'
    assert req.headers["content-type"] == "image/png"
    assert req.headers["authorization"].startswith("Basic ")


# --- per-site User-Agent override -------------------------------------------------
# Some CF zones key rules to the fleet-scanner UA. On demositestg that rule 301s core
# REST to http://, so the UA has to be overridable per site (the problem is per-zone).

def test_default_user_agent_is_unchanged():
    from wp_ops_mcp.transport.rest import FLEET_SCANNER_UA
    http = FakeHttp(FakeResp(200, {"ok": True}))
    asyncio.run(_client().request("GET", "/wp-json/wp/v2/pages", http=http))
    assert http.calls[0]["headers"]["User-Agent"] == FLEET_SCANNER_UA


def test_user_agent_can_be_overridden_per_site():
    http = FakeHttp(FakeResp(200, {"ok": True}))
    c = WPRestClient("https://site.test", app_user="u", app_password="a",
                     user_agent="WP-Ops-MCP/1.0")
    asyncio.run(c.request("GET", "/wp-json/wp/v2/pages", http=http))
    assert http.calls[0]["headers"]["User-Agent"] == "WP-Ops-MCP/1.0"


def test_user_agent_override_applies_to_binary_post():
    http = FakeHttp(FakeResp(200, {"ok": True}))
    c = WPRestClient("https://site.test", app_user="u", app_password="a",
                     user_agent="WP-Ops-MCP/1.0")
    asyncio.run(c.upload("/wp-json/wp/v2/media", "a.png", b"x", "image/png", http=http))
    assert http.calls[0]["headers"]["User-Agent"] == "WP-Ops-MCP/1.0"


def test_redirect_error_names_the_user_agent_so_the_cause_is_findable():
    """A 3xx on the authenticated path is usually an edge rule keyed to the UA.

    The bare {'redirect': ...} payload sent operators hunting siteurl and credentials;
    the detail has to name the UA and the override.
    """
    http = FakeHttp(FakeResp(301, None, headers={"location": "http://site.test/wp-json/wp/v2/pages"}))
    status, data = asyncio.run(_client().request("GET", "/wp-json/wp/v2/pages", http=http))
    assert status == 301
    assert data["redirect"] == "http://site.test/wp-json/wp/v2/pages"
    assert "WP-Ops-MCP/1.0" in data["sent_user_agent"]
    assert "user_agent" in data["hint"]
