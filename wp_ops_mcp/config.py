"""Standalone site inventory loader.

Replaces an internal workspace's config loader so this repo has no dependency on the internal
workspace. Fleet data is OPTIONAL: with no data/sites.json the registry is simply empty
and every install you have credentials for still resolves, because the server builds a
synthetic Site from credentials.json (see server._site_or_synthetic). That is the normal
setup outside WP-Ops - you list the sites you have app passwords for and nothing else.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class SiteConfig:
    """Reads data/sites.json and data/accounts.json if present; empty otherwise."""

    def __init__(self, root_dir: Path | str | None = None):
        env_root = os.environ.get("WPOPS_DATA_ROOT")
        self.root_dir = Path(root_dir or env_root or Path.cwd())
        self._sites = self._load_json("data/sites.json")
        self._accounts = self._load_json("data/accounts.json")

    def _load_json(self, relative_path: str) -> dict:
        path = self.root_dir / relative_path
        if not path.exists():
            log.debug("no %s - registry will rely on credentials.json", path)
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {path}: {e}") from e

    def get_active_sites(self) -> list[dict]:
        return [s for s in self._sites.get("sites", []) if s.get("active", True)]

    def get_accounts(self) -> dict:
        return self._accounts
