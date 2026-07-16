"""SQLite bootstrap, migrations-lite, and the api_calls ledger.

The ledger is the single source of truth for BudgetGuard: every outbound
call is recorded here (with cost for AI providers) so month-to-date usage is
always reconstructable from the DB alone.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

# --- Schema versioning (migrations-lite) -----------------------------------
# Each migration is (version, SQL) and is applied exactly once, in order.
# Bump by appending; never edit an already-shipped migration.

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE api_calls (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT    NOT NULL,           -- 'autodev' | 'anthropic' | ...
            endpoint TEXT    NOT NULL,           -- logical endpoint / operation
            ts       TEXT    NOT NULL,           -- ISO8601 UTC, e.g. 2026-07-15T12:00:00Z
            run_id   TEXT,                        -- correlates calls within one sync run
            cost_usd REAL    NOT NULL DEFAULT 0.0 -- 0 for count-metered providers
        );
        CREATE INDEX idx_api_calls_provider_ts ON api_calls (provider, ts);
        """,
    ),
    (
        2,
        """
        -- Window sticker fetch outcomes. One row per VIN, updated on retry,
        -- so a not-found VIN isn't hammered every sync run.
        CREATE TABLE stickers (
            vin        TEXT PRIMARY KEY,
            status     TEXT NOT NULL,            -- 'found' | 'not_found' | 'blocked'
            fetched_at TEXT NOT NULL,            -- ISO8601 UTC of last attempt
            path       TEXT,                     -- cached PDF path when found
            attempts   INTEGER NOT NULL DEFAULT 1
        );
        """,
    ),
    (
        3,
        """
        -- Raw per-source listings. A VIN can appear from multiple sources;
        -- (source, vin) is the natural key. raw_json keeps the full payload
        -- for reprocessing without a refetch.
        CREATE TABLE listings (
            source     TEXT NOT NULL,            -- 'autodev' | 'marketcheck'
            vin        TEXT NOT NULL,
            price      REAL,
            miles      INTEGER,
            year       INTEGER,
            make       TEXT,
            model      TEXT,
            trim       TEXT,
            color      TEXT,
            created_at TEXT,                      -- source's listing-created ts
            url        TEXT,
            raw_json   TEXT NOT NULL,
            first_seen TEXT NOT NULL,             -- when we first ingested it
            last_seen  TEXT NOT NULL,             -- last sync run that saw it
            PRIMARY KEY (source, vin)
        );

        -- Merged canonical record per VIN (deduped across sources) + delist
        -- bookkeeping. Scoring/sticker status live alongside for the UI.
        CREATE TABLE vehicles (
            vin           TEXT PRIMARY KEY,
            model_key     TEXT,                   -- which profile it was matched under
            year          INTEGER,
            make          TEXT,
            model         TEXT,
            trim          TEXT,
            color         TEXT,
            miles         INTEGER,
            price         REAL,                   -- best (lowest) current price
            status        TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'presumed_sold'
            missing_passes INTEGER NOT NULL DEFAULT 0,     -- full passes VIN was absent
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );

        -- Append-only price observations for drop detection + sparklines.
        CREATE TABLE price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vin         TEXT NOT NULL,
            source      TEXT NOT NULL,
            price       REAL NOT NULL,
            observed_at TEXT NOT NULL
        );
        CREATE INDEX idx_price_history_vin ON price_history (vin);

        -- One row per sync run for auditing + weekly-full-pass scheduling.
        CREATE TABLE sync_runs (
            run_id        TEXT PRIMARY KEY,
            source        TEXT NOT NULL,
            model_key     TEXT,
            mode          TEXT NOT NULL,          -- 'incremental' | 'full'
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            pages_fetched INTEGER NOT NULL DEFAULT 0,
            new_vins      INTEGER NOT NULL DEFAULT 0,
            updated_vins  INTEGER NOT NULL DEFAULT 0,
            calls         INTEGER NOT NULL DEFAULT 0
        );

        -- Latest scorer output per VIN (breakdown kept as JSON for the UI).
        CREATE TABLE match_results (
            vin            TEXT PRIMARY KEY,
            model_key      TEXT NOT NULL,
            verdict        TEXT NOT NULL,         -- 'ranked'|'needs_review'|'excluded'
            soft_score     REAL NOT NULL,
            highlight_bonus REAL NOT NULL,
            total          REAL NOT NULL,
            breakdown_json TEXT NOT NULL,
            scored_at      TEXT NOT NULL
        );
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0] if MIGRATIONS else 0


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection, creating parent dirs for file-backed DBs."""
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations. Returns the resulting schema version."""
    conn.executescript(_SCHEMA_VERSION_DDL)
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version > current:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            current = version
    conn.commit()
    return current


def bootstrap(db_path: str | Path) -> sqlite3.Connection:
    """Open + migrate in one step. The normal entry point for callers."""
    conn = connect(db_path)
    migrate(conn)
    return conn


# --- api_calls ledger helpers ----------------------------------------------


def _iso(ts: datetime) -> str:
    """Render a datetime as ISO8601 UTC with a trailing Z (lexically sortable)."""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def record_call(
    conn: sqlite3.Connection,
    provider: str,
    endpoint: str,
    ts: datetime,
    run_id: str | None = None,
    cost_usd: float = 0.0,
) -> None:
    """Append one row to the ledger. Caller commits (or relies on autocommit)."""
    conn.execute(
        "INSERT INTO api_calls (provider, endpoint, ts, run_id, cost_usd) VALUES (?, ?, ?, ?, ?)",
        (provider, endpoint, _iso(ts), run_id, cost_usd),
    )
    conn.commit()


def count_calls(conn: sqlite3.Connection, provider: str, start: datetime, end: datetime) -> int:
    """Count ledger rows for a provider in [start, end)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM api_calls WHERE provider = ? AND ts >= ? AND ts < ?",
        (provider, _iso(start), _iso(end)),
    ).fetchone()
    return int(row[0])


def sum_cost(conn: sqlite3.Connection, provider: str, start: datetime, end: datetime) -> float:
    """Sum cost_usd for a provider in [start, end)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM api_calls "
        "WHERE provider = ? AND ts >= ? AND ts < ?",
        (provider, _iso(start), _iso(end)),
    ).fetchone()
    return float(row[0])


# --- sticker fetch outcomes -------------------------------------------------


def get_sticker_status(conn: sqlite3.Connection, vin: str) -> sqlite3.Row | None:
    """Return the recorded fetch outcome for a VIN, or None if never attempted."""
    return conn.execute("SELECT * FROM stickers WHERE vin = ?", (vin,)).fetchone()


def record_sticker_outcome(
    conn: sqlite3.Connection,
    vin: str,
    status: str,
    ts: datetime,
    path: str | None = None,
) -> None:
    """Upsert the fetch outcome for a VIN, bumping the attempt counter."""
    conn.execute(
        """
        INSERT INTO stickers (vin, status, fetched_at, path, attempts)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(vin) DO UPDATE SET
            status = excluded.status,
            fetched_at = excluded.fetched_at,
            path = excluded.path,
            attempts = attempts + 1
        """,
        (vin, status, _iso(ts), path),
    )
    conn.commit()
