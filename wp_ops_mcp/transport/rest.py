"""WordPress REST transport (Application Passwords).

Phase 1 uses only the unauthenticated reachability probe (GET /wp-json/). Authenticated
content/settings methods are added in later phases once app passwords are provisioned
(bootstrapped over SSH during wp_discover_site).

Mirrors the repo's async client conventions: shared httpx, tenacity retry, a semaphore
for rate limiting. The Application-Password "spaces" gotcha is handled in one place
(strip_app_password) so callers never have to remember it.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx

# Every CF zone in the fleet has a WAF skip rule for this UA, so requests bypass the
# "Challenge Non-US Traffic" rule (the operator runs from the Philippines). Sending it
# keeps probes/reads from false-negativing behind the country challenge.
FLEET_SCANNER_UA = "WP-Ops-MCP/1.0"


@dataclass
class RestProbe:
    """Result of a WP REST reachability check."""
    reachable: bool
    status_code: int | None
    detail: str


class RestError(RuntimeError):
    """Transport-level REST failure (network, malformed response)."""


def _parse_response(resp, path: str, user_agent: str = FLEET_SCANNER_UA) -> tuple[int, object]:
    """Shared response semantics for every authenticated call (JSON and binary alike)."""
    status = resp.status_code
    if 300 <= status < 400:
        # Never follow redirects on the authenticated path: a 3xx would silently
        # downgrade POST->GET and drop the body, and a cross-host hop strips the
        # Authorization header. Surface the target instead so a canonical-host
        # mismatch fails loudly (callers treat unexpected statuses as errors) and
        # the operator fixes base_url in credentials.json.
        # A 3xx here is usually an EDGE rule keyed to our User-Agent, not a WordPress
        # problem - on demositestg a CF rule 301s the fleet-scanner UA to http://
        # while siteurl/home are correct https and the same URL is 200 under another UA.
        # Name the UA and the override, or operators hunt credentials and siteurl first.
        return status, {
            "redirect": resp.headers.get("location", ""),
            "sent_user_agent": user_agent,
            "hint": ("a 3xx on the authenticated path is usually an edge/CDN rule keyed "
                     "to this User-Agent; set a per-site \"user_agent\" in "
                     "credentials.json (or WPOPS_USER_AGENT) and retry"),
        }
    try:
        data = resp.json()
    except ValueError:
        if 200 <= status < 300:
            raise RestError(f"non-JSON 2xx from {path}: {resp.text[:120]!r}")
        data = None
    return status, data


async def _do_request(client: "WPRestClient", method: str, path: str,
                      params, json_body, http) -> tuple[int, object]:
    url = f"{client.base_url}{path}"
    headers = {"Accept": "application/json", "User-Agent": client.user_agent}
    try:
        resp = await http.request(method, url, params=params, json=json_body,
                                  headers=headers, auth=client._auth)
    except httpx.HTTPError as e:
        raise RestError(f"network error {method} {path}: {e}") from e
    return _parse_response(resp, path, client.user_agent)


async def _do_upload(client: "WPRestClient", path: str, filename: str,
                     content: bytes, mime: str, http) -> tuple[int, object]:
    """POST raw file bytes - WP core's REST media route wants the body plus a
    Content-Disposition filename, not a multipart form."""
    url = f"{client.base_url}{path}"
    headers = {"Accept": "application/json", "User-Agent": client.user_agent,
               "Content-Type": mime,
               "Content-Disposition": f'attachment; filename="{filename}"'}
    try:
        resp = await http.post(url, content=content, headers=headers, auth=client._auth)
    except httpx.HTTPError as e:
        raise RestError(f"network error POST {path}: {e}") from e
    return _parse_response(resp, path, client.user_agent)


# Real fleet sites answer some admin endpoints slowly: /wp/v2/taxonomies?context=edit
# measured 18.6s on examplestg (2026-07-28), which blew the old 15s default and
# surfaced as a bare "network error". 45s covers the slow-but-working case; a site that
# needs longer is a site problem, not a client-tuning problem. Override per environment
# with WPOPS_TIMEOUT (seconds).
DEFAULT_TIMEOUT = float(os.environ.get("WPOPS_TIMEOUT") or 45.0)


class WPRestClient:
    """Per-site WordPress REST client."""

    def __init__(
        self,
        base_url: str,
        app_user: str | None = None,
        app_password: str | None = None,
        semaphore: asyncio.Semaphore | None = None,
        timeout: float | None = None,
        user_agent: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        # Per-site first: the UA problem is per-ZONE, so per-site config is the right
        # granularity. Env is a blunt fallback for a whole deployment.
        self.user_agent = (user_agent or os.environ.get("WPOPS_USER_AGENT")
                           or FLEET_SCANNER_UA)
        self.app_user = app_user
        self.app_password = self.strip_app_password(app_password)
        self._semaphore = semaphore or asyncio.Semaphore(4)
        self._timeout = DEFAULT_TIMEOUT if timeout is None else timeout

    @staticmethod
    def strip_app_password(pw: str | None) -> str | None:
        """WordPress prints app passwords chunked with spaces; strip them before use."""
        if pw is None:
            return None
        return pw.replace(" ", "")

    @property
    def _auth(self) -> tuple[str, str] | None:
        if self.app_user and self.app_password:
            return (self.app_user, self.app_password)
        return None

    async def probe(self) -> RestProbe:
        """Check whether the WordPress REST API is reachable at this site.

        Reachable == HTTP 200 with a WordPress REST root payload (has 'namespaces'
        or 'name'). Anything else (non-WP JSON, 4xx/5xx, network error) is unreachable.
        """
        url = f"{self.base_url}/wp-json/"
        async with self._semaphore:
            try:
                async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as http:
                    resp = await http.get(url, headers={
                        "Accept": "application/json",
                        "User-Agent": FLEET_SCANNER_UA,
                    })
            except httpx.HTTPError as e:
                return RestProbe(reachable=False, status_code=None, detail=f"network error: {e}")

        if resp.status_code != 200:
            return RestProbe(reachable=False, status_code=resp.status_code,
                             detail=f"HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            return RestProbe(reachable=False, status_code=200, detail="non-JSON response")
        if isinstance(data, dict) and ("namespaces" in data or "name" in data):
            return RestProbe(reachable=True, status_code=200, detail="ok")
        return RestProbe(reachable=False, status_code=200, detail="not a WP REST root")

    async def request(self, method: str, path: str, *, params: dict | None = None,
                      json_body: dict | None = None, http=None) -> tuple[int, object]:
        if http is not None:
            return await _do_request(self, method, path, params, json_body, http)
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as real:
                return await _do_request(self, method, path, params, json_body, real)

    async def upload(self, path: str, filename: str, content: bytes, mime: str,
                     http=None) -> tuple[int, object]:
        """Binary POST (media upload). Same auth/UA/redirect/error semantics as request()."""
        if http is not None:
            return await _do_upload(self, path, filename, content, mime, http)
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as real:
                return await _do_upload(self, path, filename, content, mime, real)
