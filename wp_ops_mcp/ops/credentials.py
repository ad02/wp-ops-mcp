"""Per-install REST credentials (application passwords).

Gitignored store: data/wp_ops_mcp/credentials.json
  {"<install>": {"base_url": "...", "username": "...", "app_password": "..."}}
Team distribution happens via the vault later; this module only reads the local file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..transport.rest import WPRestClient

_DEFAULT = Path("data/wp_ops_mcp/credentials.json")


@dataclass
class SiteCredentials:
    base_url: str
    username: str
    app_password: str
    # Optional per-site override. Some CF zones key rules to the fleet-scanner UA and
    # 301 it (demositestg, 2026-08-26), so the UA has to be settable per ZONE.
    user_agent: str | None = None
    # Optional per-site environment. The name heuristic guessed wrong on real staging
    # installs (demositestg), which made the prod write-guard refuse them.
    environment: str | None = None


def load_credentials(install: str, path: str | Path | None = None) -> SiteCredentials | None:
    # Explicit path arg wins even when empty ("" -> read fails -> None; never falls through).
    # An empty env var is treated as unset (documented convention), falling back to the default.
    env = os.environ.get("WPOPS_CREDENTIALS")
    p = Path(path) if path is not None else Path(env) if env else _DEFAULT
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(install) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    base_url = entry.get("base_url")
    username = entry.get("username")
    app_password = entry.get("app_password")
    # A present-but-null/non-string/empty field behaves like a missing key.
    if not all(isinstance(v, str) and v for v in (base_url, username, app_password)):
        return None
    # Optional: unlike the required trio, a blank/non-string user_agent degrades to
    # None (use the default) rather than invalidating an otherwise-good entry.
    ua = entry.get("user_agent")
    if not (isinstance(ua, str) and ua.strip()):
        ua = None
    env = entry.get("environment")
    if not (isinstance(env, str) and env.strip()):
        env = None
    return SiteCredentials(
        base_url=base_url.rstrip("/"),
        username=username,
        app_password=WPRestClient.strip_app_password(app_password),
        user_agent=ua,
        environment=env,
    )
