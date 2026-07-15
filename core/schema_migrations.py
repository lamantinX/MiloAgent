"""Ordered, transactional schema migrations for the MiloAgent database.

Design:
- The current applied schema version is stored in a `schema_metadata` table
  (key/value), NOT in PRAGMA user_version, so it survives backups/restores and
  is inspectable like any other data.
- The legacy ``Database._init_tables()`` establishes the v4 baseline (all the
  CREATE TABLE IF NOT EXISTS + ad-hoc ALTERs). This runner then applies every
  migration with ``version > current`` in strict ascending order.
- Each migration runs inside its own transaction. On failure the transaction is
  rolled back, the schema_metadata version is NOT advanced, and the exception
  propagates — leaving the database at the last fully-applied version and
  usable. A subsequent run retries from there.

Migrations must be idempotent-safe: re-running an already-applied migration
must not error. We guard each ALTER with "column already exists?" detection.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable, List, Tuple

logger = logging.getLogger(__name__)

SCHEMA_METADATA_TABLE = "schema_metadata"
BASELINE_VERSION = 4  # the version produced by Database._init_tables()

Migration = Callable[[sqlite3.Connection], None]

# ── Tenant-owned tables that need a business_id column ───────────────────
# Every table whose rows are owned by a tenant and which stores a `project`
# column today. Tables that derive ownership ONLY via a strict foreign key
# (analytics, ab_results, conversations) are intentionally included here as
# well: deriving through joins is fragile once business_id is introduced, so we
# add business_id directly per the plan's "be conservative" guidance. We never
# guess values here — the migrator (plan 003 step 3) backfills them.
TENANT_TABLES_WITH_BUSINESS_ID: Tuple[str, ...] = (
    # core/database.py
    "actions",
    "opportunities",
    "performance",
    "learned_weights",
    "discoveries",
    "subreddit_intel",
    "community_presence",
    "knowledge_base",
    "subreddit_trends",
    "ab_experiments",
    "time_performance",
    "failure_patterns",
    "relationships",
    "reply_sentiment",
    "prompt_evolution_log",
    "decision_log",
    "account_subreddit_stats",
    # FK-derived, added directly for safety (plan says be conservative)
    "analytics",
    "ab_results",
    "conversations",
    # core/community_manager.py
    "community_setup_log",
    "subreddit_requests",
    # core/subreddit_hub.py
    "subreddit_hubs",
)


# ── helpers ──────────────────────────────────────────────────────────────

def _ensure_metadata_table(conn: sqlite3.Connection) -> None:
    """Create the schema_metadata table if it does not exist."""
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA_METADATA_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )


def _get_applied_version(conn: sqlite3.Connection) -> int:
    """Return the highest migration version recorded, or BASELINE if none.

    If the schema_metadata table is empty/missing the version key, the database
    is at the legacy baseline produced by _init_tables() (version 4).
    """
    _ensure_metadata_table(conn)
    row = conn.execute(
        f"SELECT value FROM {SCHEMA_METADATA_TABLE} WHERE key = ?",
        ("schema_version",),
    ).fetchone()
    if row is None:
        return BASELINE_VERSION
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return BASELINE_VERSION


def _set_applied_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        f"""INSERT INTO {SCHEMA_METADATA_TABLE} (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        ("schema_version", str(version)),
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if ``column`` exists on ``table`` (table must exist)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in cols)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _add_business_id_to_table(conn: sqlite3.Connection, table: str) -> bool:
    """Add business_id TEXT to ``table`` if missing. Returns True if added.

    Skips silently if the table does not yet exist (some tenant tables are
    created lazily by managers; they are migrated on the next run once present)
    or if the column already exists.
    """
    if not _table_exists(conn, table):
        return False
    if _column_exists(conn, table, "business_id"):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN business_id TEXT")
    logger.debug(f"Migrated {table}: added business_id column")
    return True


def _migrate_v5_business_id(conn: sqlite3.Connection) -> None:
    """Migration v5: add business_id to every tenant-owned table.

    Values are NOT backfilled here — that is the data migration's job
    (plan 003 step 3/4). This step only widens the schema.
    """
    added = []
    for table in TENANT_TABLES_WITH_BUSINESS_ID:
        if _add_business_id_to_table(conn, table):
            added.append(table)
    if added:
        logger.info(
            "v5 schema migration: added business_id to %d table(s)", len(added)
        )


# Ordered migrations. Index = target version after applying.
_MIGRATIONS: List[Tuple[int, Migration]] = [
    (5, _migrate_v5_business_id),
]


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations in order. Returns the final version.

    Each migration is applied in its own transaction. On failure the offending
    transaction is rolled back (leaving the DB at the previous version and
    usable) and the exception propagates.
    """
    current = _get_applied_version(conn)
    target = current
    for version, migrate_fn in _MIGRATIONS:
        if version <= current:
            continue
        logger.info(f"Applying schema migration v{version} (from v{current})")
        try:
            # SAVEPOINT gives us a nested, rollback-able scope even if the
            # outer autocommit state is unusual. BEGIN IMMEDIATE guarantees we
            # hold a write lock for the duration.
            conn.execute("BEGIN IMMEDIATE")
            try:
                migrate_fn(conn)
                _set_applied_version(conn, version)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except Exception:
            logger.exception(f"Schema migration v{version} failed; rolled back")
            raise
        target = version
        current = version
    return target


def pending_migrations(conn: sqlite3.Connection) -> List[int]:
    """Return the list of migration versions not yet applied (for reporting)."""
    current = _get_applied_version(conn)
    return [v for v, _ in _MIGRATIONS if v > current]
