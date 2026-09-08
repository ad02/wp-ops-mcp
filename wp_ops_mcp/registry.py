"""Site registry for the WordPress fleet.

Adapts an existing site inventory (data/sites.json via the workspace config loader)
into typed Site records the MCP tools consume. Pure logic so it is unit-testable
offline; the only I/O is delegated to SiteConfig.

Environment is *derived* from the install name when nothing better is available, but an
explicit `environment` on the record ALWAYS wins - the heuristic proved wrong on
`demositestg`, a real WPE staging install whose name carries no stg/dev/test marker,
which the prod write-guard then refused as production (2026-08-26).
"""
from __future__ import annotations

from dataclasses import dataclass

# Substrings in an install name that mark it as a non-production environment.
# Conservative: only well-known staging/dev/test markers flip to "staging";
# everything else is treated as prod (the safe default for guardrails).
_STAGING_MARKERS = ("stg", "staging", "dev", "test")

# Only these are real environments. Anything else (blank, typo, None) is not trusted and
# falls back to the name heuristic rather than being written through as an environment.
_VALID_ENVIRONMENTS = ("production", "prod", "staging", "development")


def derive_environment(install_name: str) -> str:
    """Return 'staging' or 'prod' inferred from the install name."""
    name = install_name.lower()
    if any(marker in name for marker in _STAGING_MARKERS):
        return "staging"
    return "prod"


def resolve_environment(install_name: str, explicit: object = None) -> str:
    """Explicit environment wins; otherwise fall back to the name heuristic.

    WPE reports "production"/"staging"/"development"; normalise "production" to the
    "prod" this codebase uses. Non-production values that are not "prod" are treated as
    non-production, which is the safe direction for write guards.
    """
    if isinstance(explicit, str):
        e = explicit.strip().lower()
        if e in _VALID_ENVIRONMENTS:
            return "prod" if e in ("production", "prod") else e
    return derive_environment(install_name)


@dataclass(frozen=True)
class Site:
    install: str
    account: str
    domain: str
    environment: str
    php_version: str
    cf_zone_id: str

    @property
    def ssh_host(self) -> str:
        return f"{self.install}.ssh.example.net"

    @property
    def primary_url(self) -> str | None:
        """Best-effort https URL for the site, or None if no domain is known."""
        return f"https://{self.domain}" if self.domain else None


class SiteRegistry:
    """In-memory collection of active fleet sites with filtering."""

    def __init__(self, sites: list[Site]):
        self._sites = sites
        self._by_install = {s.install: s for s in sites}

    @classmethod
    def from_records(cls, records: list[dict]) -> "SiteRegistry":
        """Build from raw sites.json-shaped dicts, skipping inactive installs."""
        sites = [
            Site(
                install=r["wpe_install"],
                account=r.get("wpe_account", ""),
                domain=r.get("domain", "") or "",
                environment=resolve_environment(r["wpe_install"], r.get("environment")),
                php_version=r.get("php_version", "") or "",
                cf_zone_id=r.get("cf_zone_id", "") or "",
            )
            for r in records
            if r.get("active", True) and r.get("wpe_install")
        ]
        return cls(sites)

    @classmethod
    def from_config(cls, config) -> "SiteRegistry":
        """Build from a the workspace config loader instance."""
        return cls.from_records(config.get_active_sites())

    def all(self) -> list[Site]:
        return list(self._sites)

    def get(self, install: str) -> Site | None:
        return self._by_install.get(install)

    def filter(
        self,
        account: str | None = None,
        environment: str | None = None,
        query: str | None = None,
    ) -> list[Site]:
        """Return sites matching all provided criteria (AND).

        `query` is a case-insensitive substring matched against install or domain.
        """
        q = query.lower() if query else None
        out = []
        for s in self._sites:
            if account and s.account != account:
                continue
            if environment and s.environment != environment:
                continue
            if q and q not in s.install.lower() and q not in s.domain.lower():
                continue
            out.append(s)
        return out
