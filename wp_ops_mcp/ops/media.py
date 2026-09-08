"""Media upload checks: extension/mime allowlist, size cap, filename sanitizing.

Allowlist, not denylist: only the five raster formats below can reach a fleet site.
SVG is refused outright even though WordPress can be coaxed into accepting it - an SVG
is a script vector (inline <script>/on* handlers execute in the browser under the site's
origin), so it is a stored-XSS upload path, not an image.

The checks are pure functions - no I/O, no gateway - so they stay trivially testable;
MediaOps below is the thin layer that reads a local file and drives a REST gateway.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_BYTES = 10 * 1024 * 1024

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class MediaError(RuntimeError):
    """Refused upload (disallowed type, oversize)."""


def sanitize_filename(name: str) -> str:
    """Basename only, with everything outside [A-Za-z0-9._-] replaced by '-'.

    Both separators are handled regardless of host OS: the path may come from a Windows
    operator ("C:\\pics\\a.png") while the code runs on the Linux VM, where
    os.path.basename would keep the whole backslash string as one 'filename' and let
    traversal-ish input through into the Content-Disposition header.
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return _UNSAFE.sub("-", base)


def validate_media(filename: str, size: int) -> str:
    """Return the mime type for an allowed upload, else raise MediaError.

    The extension is read off the sanitized basename so a path like
    "../../../etc/passwd" cannot smuggle a dot from a parent directory into the check.
    """
    ext = os.path.splitext(sanitize_filename(filename))[1].lower()
    if ext == ".svg":
        raise MediaError("svg refused (script vector) - convert to png/webp first")
    if ext not in MIME_BY_EXT:
        raise MediaError(f"refused extension {ext or '(none)'} - "
                         f"allowed: {', '.join(sorted(MIME_BY_EXT))}")
    if size > MAX_BYTES:
        raise MediaError(f"file too large: {size / 1024 / 1024:.1f} MB "
                         f"(max {MAX_BYTES // (1024 * 1024)} MB)")
    return MIME_BY_EXT[ext]


class MediaOps:
    """Uploads a local image file to a site's media library over a REST gateway.

    The gateway must expose ``upload_media`` (RestContentGateway does; the SSH/wpcli
    gateway does not - the tool refuses that transport before getting here). Like
    SeoOps, ``upload`` NEVER raises, for ANY input: the anticipated failures (missing
    file, refused type, oversize, gateway/HTTP error) get their own precise message,
    and a final catch-all makes the contract total - the path plumbing itself throws
    outside those families (``Path(None)`` -> TypeError, an embedded null byte ->
    ValueError from ``stat()``). Everything comes back as ``{"action": "error", ...}``
    so the MCP tool returns a value instead of unwinding.

    Two ordering rules matter and are load-bearing:
      1. The size is taken from ``stat()`` and validated BEFORE the bytes are read,
         so pointing the tool at a multi-GB file refuses it instead of loading it
         into memory.
      2. The filename is sanitized BEFORE it reaches the gateway. It lands in the
         upload's ``Content-Disposition`` header, so an operator path containing
         quotes/semicolons/newlines (or a directory) must never travel verbatim.
    """

    def __init__(self, gateway):
        self.gw = gateway

    async def upload(self, file_path: str, alt: str | None = None,
                     dry_run: bool = True) -> dict:
        # One try over the WHOLE body - including Path()/stat(), which are not
        # exception-free, and the gateway's response unpacking, where a malformed dict
        # would otherwise raise KeyError. Handlers run most-specific first
        # (FileNotFoundError before its OSError parent); the trailing Exception is the
        # net that makes "never raises" true for inputs we did not anticipate.
        try:
            path = Path(file_path)
            filename = sanitize_filename(path.name)
            mime = validate_media(filename, path.stat().st_size)
            content = path.read_bytes()
            if dry_run:
                return {"action": "preview", "filename": filename,
                        "mime": mime, "bytes": len(content)}
            result = await self.gw.upload_media(filename, content, mime, alt=alt)
            return {"action": "uploaded", "id": result["id"], "url": result["url"],
                    "alt": result.get("alt", alt or "")}
        except FileNotFoundError:
            return {"action": "error", "error": f"FileNotFound: {file_path}"}
        except OSError as e:
            return {"action": "error", "error": f"IOError: {file_path}: {e}"}
        except MediaError as e:
            return {"action": "error", "error": f"MediaError: {e}"}
        except Exception as e:
            return {"action": "error", "error": f"{type(e).__name__}: {e}"}
