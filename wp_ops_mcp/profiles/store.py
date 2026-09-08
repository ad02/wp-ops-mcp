"""SQLite store for site fingerprints.

Mirrors the repo's fleet_db.py schema-as-code style, but profiles ACCUMULATE per
site (upsert keyed by install) rather than being rebuilt from scratch each run.
Single-file, zero-ops; swap for DuckDB later if needed. Synchronous — async callers
wrap in asyncio.to_thread.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_profiles (
    install              TEXT PRIMARY KEY,
    account              TEXT,
    environment          TEXT,
    domain               TEXT,
    discovered_at        TEXT,
    wp_version           TEXT,
    php_version          TEXT,
    active_theme         TEXT,
    active_theme_version TEXT,
    builder              TEXT,
    divi_major           INTEGER,
    multisite            INTEGER,
    plugins_json         TEXT,
    raw_json             TEXT
);
"""

_COLUMNS = [
    "install", "account", "environment", "domain", "discovered_at",
    "wp_version", "php_version", "active_theme", "active_theme_version",
    "builder", "divi_major", "multisite", "plugins_json", "raw_json",
]


@dataclass
class SiteProfile:
    install: str
    account: str
    environment: str
    domain: str
    discovered_at: str
    wp_version: str | None
    php_version: str | None
    active_theme: str | None
    active_theme_version: str | None
    builder: str
    divi_major: int | None
    multisite: bool
    plugins: list[dict]
    raw: dict


class ProfileStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def upsert(self, profile: SiteProfile) -> None:
        row = {
            "install": profile.install,
            "account": profile.account,
            "environment": profile.environment,
            "domain": profile.domain,
            "discovered_at": profile.discovered_at,
            "wp_version": profile.wp_version,
            "php_version": profile.php_version,
            "active_theme": profile.active_theme,
            "active_theme_version": profile.active_theme_version,
            "builder": profile.builder,
            "divi_major": profile.divi_major,
            "multisite": 1 if profile.multisite else 0,
            "plugins_json": json.dumps(profile.plugins),
            "raw_json": json.dumps(profile.raw),
        }
        placeholders = ", ".join(f":{c}" for c in _COLUMNS)
        cols = ", ".join(_COLUMNS)
        with self._conn() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO site_profiles ({cols}) VALUES ({placeholders})",
                row,
            )

    def get(self, install: str) -> SiteProfile | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM site_profiles WHERE install = ?", (install,)
            ).fetchone()
        return self._to_profile(row) if row else None

    def all(self) -> list[SiteProfile]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM site_profiles").fetchall()
        return [self._to_profile(r) for r in rows]

    @staticmethod
    def _to_profile(row: sqlite3.Row) -> SiteProfile:
        return SiteProfile(
            install=row["install"],
            account=row["account"],
            environment=row["environment"],
            domain=row["domain"],
            discovered_at=row["discovered_at"],
            wp_version=row["wp_version"],
            php_version=row["php_version"],
            active_theme=row["active_theme"],
            active_theme_version=row["active_theme_version"],
            builder=row["builder"],
            divi_major=row["divi_major"],
            multisite=bool(row["multisite"]),
            plugins=json.loads(row["plugins_json"]) if row["plugins_json"] else [],
            raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
        )
