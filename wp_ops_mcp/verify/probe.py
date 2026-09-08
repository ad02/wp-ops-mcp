"""Public-URL verification after publishes (fleet rule: verify after ANY change)."""
from __future__ import annotations

_UA = {"User-Agent": "WP-Ops-MCP/1.0"}   # persistent per-zone CF bypass UA


async def probe_url(url: str, expect_text: str | None = None, client=None) -> dict:
    if client is None:                       # pragma: no cover - live path
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
            return await probe_url(url, expect_text, client=c)
    resp = await client.get(url, headers=_UA)
    status = resp.status_code
    text_found = (expect_text in resp.text) if expect_text is not None else None
    ok = status in (200, 301, 302) and (text_found is not False)
    return {"url": url, "status": status, "ok": ok, "text_found": text_found}
