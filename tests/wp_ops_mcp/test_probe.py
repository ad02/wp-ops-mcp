# tests/wp_ops_mcp/test_probe.py
import asyncio
from wp_ops_mcp.verify.probe import probe_url


class FakeResp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


class FakeClient:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    async def get(self, url, headers=None, **kw):
        self.calls.append((url, headers))
        return self.resp


def test_probe_ok_with_expected_text():
    client = FakeClient(FakeResp(200, "<html>new body</html>"))
    r = asyncio.run(probe_url("https://x.com/p", expect_text="new body", client=client))
    assert r["ok"] is True and r["text_found"] is True
    assert client.calls[0][1]["User-Agent"] == "WP-Ops-MCP/1.0"


def test_probe_fails_on_500_or_missing_text():
    assert asyncio.run(probe_url("https://x.com", client=FakeClient(FakeResp(500, ""))))["ok"] is False
    r = asyncio.run(probe_url("https://x.com", expect_text="zz",
                              client=FakeClient(FakeResp(200, "other"))))
    assert r["ok"] is False and r["text_found"] is False
