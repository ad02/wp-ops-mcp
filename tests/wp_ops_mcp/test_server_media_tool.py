"""Contract tests for the Phase 5b media tool (wp_upload_media) on the FastMCP server.

Mirrors test_server_seo_tools.py: fake at the gateway boundary and assert the tool
layer refuses correctly (unknown install, non-REST transport, prod gate) BEFORE any
byte leaves the machine, and that a real local file round-trips to the gateway.
Media upload is builder-independent, so (like SEO) there is no profile/builder
precondition - only the REST transport.
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

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00pixels\xff"


def _reg():
    return SiteRegistry.from_records(RAW)


def _file(tmp_path, name="photo.png", data=PNG):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


class FakeMediaGateway:
    """REST-shaped gateway: exposes upload_media, records every call."""

    def __init__(self):
        self.calls = []

    async def upload_media(self, filename, content, mime, alt=None):
        self.calls.append({"filename": filename, "bytes": len(content),
                           "mime": mime, "alt": alt})
        return {"id": 512, "url": f"https://a.com/wp-content/uploads/{filename}",
                "alt": alt or ""}


class FakeWpcliGateway:
    """wpcli-style gateway: NO upload_media, so the media tool must refuse."""

    async def list_posts(self, post_type, query=None):
        return []


# --- registration -----------------------------------------------------------

def test_media_tool_is_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "wp_upload_media" in names


# --- unknown install --------------------------------------------------------

async def test_upload_media_unknown_install(tmp_path):
    gw = FakeMediaGateway()
    payload = await server.upload_media_payload(
        _reg(), "nope", _file(tmp_path), gateway=gw)
    assert payload["action"] == "refused"
    assert "unknown install" in payload["reason"]
    assert gw.calls == []


# --- non-REST transport -----------------------------------------------------

async def test_upload_media_wpcli_gateway_refused(tmp_path):
    payload = await server.upload_media_payload(
        _reg(), "astg", _file(tmp_path), gateway=FakeWpcliGateway())
    assert payload["action"] == "refused"
    assert "REST transport" in payload["reason"]


# --- prod gate honours dry_run ----------------------------------------------

async def test_upload_media_prod_blocked_without_allow_prod(tmp_path):
    gw = FakeMediaGateway()
    payload = await server.upload_media_payload(
        _reg(), "aprd", _file(tmp_path), dry_run=False, allow_prod=False, gateway=gw)
    assert payload["action"] == "refused"
    assert "prod" in payload["reason"].lower()
    assert gw.calls == []              # gate fired before any upload


async def test_upload_media_prod_dry_run_passes_gate(tmp_path):
    gw = FakeMediaGateway()
    payload = await server.upload_media_payload(
        _reg(), "aprd", _file(tmp_path), dry_run=True, gateway=gw)
    assert payload["action"] == "preview"    # dry-run bypasses the prod gate
    assert gw.calls == []


async def test_upload_media_prod_with_allow_prod_uploads(tmp_path):
    gw = FakeMediaGateway()
    payload = await server.upload_media_payload(
        _reg(), "aprd", _file(tmp_path), dry_run=False, allow_prod=True, gateway=gw)
    assert payload["action"] == "uploaded"
    assert len(gw.calls) == 1


# --- happy path + expected failure as a dict --------------------------------

async def test_upload_media_uploaded_happy_path(tmp_path):
    gw = FakeMediaGateway()
    payload = await server.upload_media_payload(
        _reg(), "astg", _file(tmp_path), alt="A photo", dry_run=False, gateway=gw)
    assert payload == {"action": "uploaded", "id": 512,
                       "url": "https://a.com/wp-content/uploads/photo.png",
                       "alt": "A photo"}
    assert gw.calls == [{"filename": "photo.png", "bytes": len(PNG),
                         "mime": "image/png", "alt": "A photo"}]


async def test_upload_media_missing_file_error(tmp_path):
    gw = FakeMediaGateway()
    payload = await server.upload_media_payload(
        _reg(), "astg", str(tmp_path / "gone.png"), dry_run=False, gateway=gw)
    assert payload["action"] == "error"
    assert "FileNotFound" in payload["error"]
    assert gw.calls == []
