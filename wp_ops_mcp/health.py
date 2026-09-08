"""Per-site health aggregation: REST reachability + SSH/WP-CLI reachability.

Pure orchestration over the two transports so it is unit-testable with fakes.
Real callers (the MCP server) let the transports default to live clients.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING

from .registry import Site
from .transport.rest import WPRestClient

if TYPE_CHECKING:  # SSH transport is imported lazily at runtime (cloud REST-only rule)
    from .transport.wpcli import WPCliTransport


@dataclass
class SiteHealth:
    install: str
    account: str
    environment: str
    domain: str
    rest_reachable: bool | None  # None => not probed (no domain known)
    ssh_reachable: bool
    wp_cli: bool
    rest_detail: str
    ssh_detail: str

    def to_dict(self) -> dict:
        return asdict(self)


async def check_site_health(
    site: Site,
    rest_client: WPRestClient | None = None,
    cli_transport: WPCliTransport | None = None,
) -> SiteHealth:
    """Probe REST (if a domain is known) and SSH/WP-CLI for a single site."""
    if site.domain:
        rc = rest_client or WPRestClient(site.primary_url)
        rest = await rc.probe()
        rest_reachable: bool | None = rest.reachable
        rest_detail = rest.detail
    else:
        rest_reachable = None
        rest_detail = "skipped: no domain known for this install"

    ct = cli_transport
    if ct is None:
        # Lazy import: keeps server.py's import chain SSH-free so a cloud REST-only
        # build never loads skills.wpengine at module import time.
        from .transport.wpcli import WPCliTransport
        ct = WPCliTransport(site.install)
    ssh = await ct.probe()

    return SiteHealth(
        install=site.install,
        account=site.account,
        environment=site.environment,
        domain=site.domain,
        rest_reachable=rest_reachable,
        ssh_reachable=ssh.reachable,
        wp_cli=ssh.wp_cli,
        rest_detail=rest_detail,
        ssh_detail=ssh.detail,
    )
