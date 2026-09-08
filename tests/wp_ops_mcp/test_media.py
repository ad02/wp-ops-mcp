import pytest
from wp_ops_mcp.ops.media import (
    MediaError, MediaOps, MIME_BY_EXT, MAX_BYTES, sanitize_filename, validate_media)

# Smallest thing that is honestly a PNG on the wire: the 8-byte signature + filler.
# Content is never inspected (the allowlist is extension-driven), but writing real
# bytes keeps the fixtures from implying otherwise.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00pixels\xff"


def test_sanitize_filename():
    assert sanitize_filename(r"C:\evil path\my photo (1).png") == "my-photo--1-.png"
    assert sanitize_filename("../../../etc/passwd.png") == "passwd.png"


def test_validate_media_allows_images_and_returns_mime():
    assert validate_media("a.png", 100) == "image/png"
    assert validate_media("b.JPG", 100) == "image/jpeg"       # case-insensitive ext


def test_validate_media_refusals():
    with pytest.raises(MediaError, match="svg refused"):
        validate_media("logo.svg", 100)
    with pytest.raises(MediaError, match="extension"):
        validate_media("doc.pdf", 100)
    with pytest.raises(MediaError, match="MB"):
        validate_media("big.png", MAX_BYTES + 1)


# --- MediaOps (file read + gateway wiring) ----------------------------------

class FakeMediaGateway:
    """REST-shaped media gateway that records exactly what reached the wire.

    `calls` is the assertion surface for the security-relevant claim: the filename
    handed to the gateway (and from there into Content-Disposition) is sanitized.
    """

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def upload_media(self, filename, content, mime, alt=None):
        self.calls.append({"filename": filename, "bytes": len(content),
                           "mime": mime, "alt": alt})
        if self.error is not None:
            raise self.error
        return {"id": 77, "url": f"https://a.com/wp-content/uploads/{filename}",
                "alt": alt or ""}


def _file(tmp_path, name="photo.png", data=PNG):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


async def test_upload_dry_run_previews_and_never_calls_the_gateway(tmp_path):
    gw = FakeMediaGateway()
    out = await MediaOps(gw).upload(_file(tmp_path), alt="A photo")
    assert out == {"action": "preview", "filename": "photo.png",
                   "mime": "image/png", "bytes": len(PNG)}
    assert gw.calls == []


async def test_upload_sends_bytes_and_returns_uploaded(tmp_path):
    gw = FakeMediaGateway()
    out = await MediaOps(gw).upload(_file(tmp_path), alt="A photo", dry_run=False)
    assert out == {"action": "uploaded", "id": 77,
                   "url": "https://a.com/wp-content/uploads/photo.png", "alt": "A photo"}
    assert gw.calls == [{"filename": "photo.png", "bytes": len(PNG),
                         "mime": "image/png", "alt": "A photo"}]


async def test_upload_missing_file_is_an_error_dict(tmp_path):
    gw = FakeMediaGateway()
    out = await MediaOps(gw).upload(str(tmp_path / "nope.png"), dry_run=False)
    assert out["action"] == "error"
    assert "FileNotFound" in out["error"] and "nope.png" in out["error"]
    assert gw.calls == []


async def test_upload_svg_is_refused_before_the_gateway(tmp_path):
    gw = FakeMediaGateway()
    out = await MediaOps(gw).upload(_file(tmp_path, "logo.svg", b"<svg/>"), dry_run=False)
    assert out["action"] == "error"
    assert "svg refused" in out["error"]
    assert gw.calls == []


async def test_upload_oversize_is_refused_before_the_gateway(tmp_path):
    big = tmp_path / "big.png"
    with open(big, "wb") as f:
        f.truncate(MAX_BYTES + 1)      # sparse: no 10 MB of RAM, and never read
    gw = FakeMediaGateway()
    out = await MediaOps(gw).upload(str(big), dry_run=False)
    assert out["action"] == "error" and "MB" in out["error"]
    assert gw.calls == []


async def test_upload_sanitizes_the_filename_before_the_gateway(tmp_path):
    """Content-Disposition injection guard: the operator's path never reaches the
    header verbatim - directories are dropped and unsafe characters collapse to '-'."""
    nested = tmp_path / "some dir"
    nested.mkdir()
    gw = FakeMediaGateway()
    out = await MediaOps(gw).upload(_file(nested, "my photo (1);x.png"), dry_run=False)
    assert out["action"] == "uploaded"
    sent = gw.calls[0]["filename"]
    assert sent == "my-photo--1--x.png"
    assert "some dir" not in sent and ";" not in sent and " " not in sent


async def test_upload_gateway_failure_becomes_an_error_dict(tmp_path):
    gw = FakeMediaGateway(error=RuntimeError("boom"))
    out = await MediaOps(gw).upload(_file(tmp_path), dry_run=False)
    assert out["action"] == "error" and "boom" in out["error"]


async def test_upload_never_raises_on_pathological_inputs():
    """The "never raises" contract is total, not just for the failures we anticipated.

    These inputs blow up in the path plumbing itself, before any file is opened, and
    each one used to escape as an exception: `None`/`123` -> TypeError out of `Path()`,
    an embedded null byte -> ValueError out of `stat()` (neither is OSError/MediaError).
    An MCP tool must return a value for every input, so all of them are error dicts.
    """
    gw = FakeMediaGateway()
    for bad in [None, "with\x00null.png", 123, b"bytes.png", ["a.png"]]:
        out = await MediaOps(gw).upload(bad, dry_run=True)
        assert isinstance(out, dict), (bad, out)
        assert out["action"] == "error", (bad, out)
        assert out["error"], (bad, out)
    assert gw.calls == []
