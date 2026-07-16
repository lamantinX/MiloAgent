"""Tests for the schema migration runner and tenant business_id columns.

Uses fresh temp databases only. No real data.
"""

import sqlite3

import pytest

from core.database import Database
from core.schema_migrations import (
    BASELINE_VERSION,
    SCHEMA_METADATA_TABLE,
    TENANT_TABLES_WITH_BUSINESS_ID,
    _column_exists,
    _get_applied_version,
    _table_exists,
    pending_migrations,
    run_migrations,
)


# ── fresh DB migrates once and is idempotent ─────────────────────────────

def test_schema_v5_migrates_once(tmp_sqlite_path):
    """A fresh Database initializes to v5 and adds business_id everywhere."""
    db = Database(str(tmp_sqlite_path))
    try:
        conn = db.conn
        assert _get_applied_version(conn) == 5
        # No pending migrations after init.
        assert pending_migrations(conn) == []
    finally:
        db.close()


def test_schema_rerun_is_idempotent(tmp_sqlite_path):
    """Calling run_migrations again does nothing and keeps version at 5."""
    db = Database(str(tmp_sqlite_path))
    try:
        before = _get_applied_version(db.conn)
        final = run_migrations(db.conn)
        assert final == before == 5
    finally:
        db.close()


def test_schema_baseline_version_is_4():
    """The legacy _init_tables() baseline is v4."""
    assert BASELINE_VERSION == 4


# ── business_id added to every tenant-owned table ────────────────────────

def test_all_tenant_tables_have_business_id(tmp_sqlite_path):
    """Every core/database.py tenant table gains a business_id column."""
    db = Database(str(tmp_sqlite_path))
    try:
        conn = db.conn
        # Core tables created by _init_tables() must all be migrated.
        core_tables = (
            "actions", "opportunities", "performance", "learned_weights",
            "discoveries", "subreddit_intel", "community_presence",
            "knowledge_base", "subreddit_trends", "ab_experiments",
            "time_performance", "failure_patterns", "relationships",
            "reply_sentiment", "prompt_evolution_log", "decision_log",
            "account_subreddit_stats", "analytics", "ab_results",
            "conversations",
        )
        for t in core_tables:
            assert _table_exists(conn, t), f"table {t} should exist"
            assert _column_exists(conn, t, "business_id"), (
                f"{t} missing business_id"
            )
    finally:
        db.close()


def test_business_id_column_is_nullable(tmp_sqlite_path):
    """business_id is nullable so historical rows survive until backfilled."""
    db = Database(str(tmp_sqlite_path))
    try:
        conn = db.conn
        # Insert a row with NULL business_id must succeed (schema allows it).
        conn.execute(
            "INSERT INTO actions "
            "(platform, action_type, account, project, target_id, business_id) "
            "VALUES ('reddit', 'comment', 'a', 'p', 't3_x', NULL)"
        )
        conn.commit()
    finally:
        db.close()


# ── migration runner applies to a hand-built v4 fixture ──────────────────

def _build_v4_fixture(path) -> sqlite3.Connection:
    """Create a database at the v4 baseline (no schema_metadata, no business_id)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            platform TEXT NOT NULL,
            action_type TEXT NOT NULL,
            account TEXT NOT NULL,
            project TEXT NOT NULL,
            target_id TEXT NOT NULL,
            content TEXT,
            metadata TEXT,
            success INTEGER DEFAULT 1,
            error_message TEXT
        );
    """)
    # A couple of historical rows without business_id.
    conn.execute(
        "INSERT INTO actions (platform, action_type, account, project, target_id) "
        "VALUES ('reddit','comment','acct','MyProject','t3_1')"
    )
    conn.execute(
        "INSERT INTO actions (platform, action_type, account, project, target_id) "
        "VALUES ('reddit','post','acct','MyProject','t3_2')"
    )
    conn.commit()
    return conn


def test_v4_fixture_migrates_and_keeps_rows(tmp_path):
    """A hand-built v4 DB migrates to v5 without losing existing rows."""
    path = tmp_path / "v4.db"
    conn = _build_v4_fixture(path)
    conn.close()

    # Open via Database, which runs _init_tables + migrations.
    db = Database(str(path))
    try:
        c = db.conn
        assert _get_applied_version(c) == 5
        assert _column_exists(c, "actions", "business_id")
        # Historical rows survive, business_id NULL until backfilled.
        rows = c.execute(
            "SELECT id, project, business_id FROM actions ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert all(r["business_id"] is None for r in rows)
        assert all(r["project"] == "MyProject" for r in rows)
    finally:
        db.close()


# ── rollback: a failing migration leaves DB usable ───────────────────────

def test_migration_failure_rolls_back(tmp_path):
    """A migration that errors rolls back its transaction; DB stays usable."""

    def boom(conn):
        # Create a table, then reference a column that does not exist.
        # SQLite raises OperationalError (no such column) — genuine failure.
        conn.execute("CREATE TABLE will_fail (x INTEGER)")
        conn.execute("INSERT INTO will_fail (x, missing_col) VALUES (1, 2)")

    # Seed a metadata table at baseline so our bogus migration is "pending".
    path = tmp_path / "rb.db"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE {SCHEMA_METADATA_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        f"INSERT INTO {SCHEMA_METADATA_TABLE} (key, value) VALUES (?, ?)",
        ("schema_version", str(BASELINE_VERSION)),
    )
    conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    # Monkeypatch the migration list to inject our failing one.
    import core.schema_migrations as sm
    orig = sm._MIGRATIONS[:]
    sm._MIGRATIONS[:] = [(BASELINE_VERSION + 1, boom)]
    try:
        db = Database(str(path))
        db.close()
        pytest.fail("expected migration to raise")
    except Exception:
        pass  # expected
    finally:
        sm._MIGRATIONS[:] = orig

    # The DB must still be openable and usable at the baseline version.
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    assert _get_applied_version(conn) == BASELINE_VERSION
    # The partially-created table must NOT exist (rolled back).
    tables = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "will_fail" not in tables
    # Pre-existing data intact.
    assert conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 0
    conn.close()


def test_tenant_table_list_is_comprehensive():
    """Sanity: the tenant table list matches the documented set."""
    assert "actions" in TENANT_TABLES_WITH_BUSINESS_ID
    assert "conversations" in TENANT_TABLES_WITH_BUSINESS_ID
    assert "analytics" in TENANT_TABLES_WITH_BUSINESS_ID
    assert "ab_results" in TENANT_TABLES_WITH_BUSINESS_ID


# =========================================================================
# Part 2: tenant data migration (plan 003 steps 3 & 4)
# =========================================================================

import yaml  # noqa: E402

from core.business_migrator import (  # noqa: E402
    MigrationError,
    OwnershipEntry,
    apply_migration,
    build_ownership_map,
    plan_migration,
)


def _prod(name, pid, bid):
    return {"project": {"name": name, "id": pid, "business_id": bid}}


# ── ownership map ────────────────────────────────────────────────────────

def test_ownership_map_simple():
    products = [_prod("MyProduct", "my_product", "acme")]
    om = build_ownership_map(products)
    assert om["MyProduct"].product_id == "my_product"
    assert om["MyProduct"].business_id == "acme"


def test_ownership_map_multi_business_same_name_aborts():
    """A single project name mapping to >1 business is a STOP."""
    products = [
        _prod("Dup", "dup1", "acme"),
        _prod("Dup", "dup2", "beta"),
    ]
    with pytest.raises(MigrationError, match="more than one business"):
        build_ownership_map(products)


def test_ownership_map_default_business_accepted_when_single():
    """Explicit default accepted when ALL legacy products belong to it."""
    products = [
        _prod("A", "a", "acme"),
        _prod("B", "b", "acme"),
    ]
    om = build_ownership_map(products, default_business_id="acme")
    assert {e.business_id for e in om.values()} == {"acme"}


def test_ownership_map_default_business_refused_when_multi():
    """Explicit default refused when products span multiple businesses."""
    products = [
        _prod("A", "a", "acme"),
        _prod("B", "b", "beta"),
    ]
    with pytest.raises(MigrationError, match="multiple businesses"):
        build_ownership_map(products, default_business_id="acme")


# ── dry-run / plan ───────────────────────────────────────────────────────

def _seed_actions(conn, projects):
    for i, p in enumerate(projects, start=1):
        conn.execute(
            "INSERT INTO actions (platform, action_type, account, project, target_id) "
            "VALUES ('reddit','comment','acct', ?, ?)",
            (p, f"t3_{i}"),
        )
    conn.commit()


def test_dry_run_lists_unmappable_rows(tmp_sqlite_path):
    db = Database(str(tmp_sqlite_path))
    try:
        conn = db.conn
        _seed_actions(conn, ["MyProduct", "Orphan"])
        ownership = {"MyProduct": OwnershipEntry("my_product", "acme")}
        report = plan_migration(conn, ownership)
        # 'MyProduct' maps; 'Orphan' does not -> unmapped.
        assert report.unmapped_count == 1
        tbl, row_id, legacy = report.unmapped[0]
        assert tbl == "actions"
        assert legacy == "Orphan"
        assert isinstance(row_id, int)
    finally:
        db.close()


def test_dry_run_reports_no_work_when_already_migrated(tmp_sqlite_path):
    db = Database(str(tmp_sqlite_path))
    try:
        conn = db.conn
        _seed_actions(conn, ["MyProduct"])
        # Backfill by hand to simulate an already-migrated DB.
        conn.execute("UPDATE actions SET business_id = 'acme'")
        conn.commit()
        ownership = {"MyProduct": OwnershipEntry("my_product", "acme")}
        report = plan_migration(conn, ownership)
        assert report.mapped_rows == 0
        assert report.unmapped_count == 0
    finally:
        db.close()


# ── apply: transactional, backed up, idempotent ──────────────────────────

def test_apply_backs_up_and_backfills(tmp_path):
    db_path = tmp_path / "milo.db"
    backup_root = tmp_path / "backups"
    db = Database(str(db_path))
    try:
        conn = db.conn
        _seed_actions(conn, ["MyProduct"])
        ownership = {"MyProduct": OwnershipEntry("my_product", "acme")}
        report = apply_migration(
            conn, ownership, str(db_path), [], backup_root
        )
        assert report.backed_up_db is not None
        assert report.backed_up_yaml_dir is not None
        from pathlib import Path
        assert Path(report.backed_up_db).exists()
        # Every row now has a non-empty business_id.
        row = conn.execute(
            "SELECT business_id FROM actions WHERE project = 'MyProduct'"
        ).fetchone()
        assert row["business_id"] == "acme"
    finally:
        db.close()


def test_apply_is_idempotent(tmp_path):
    db_path = tmp_path / "milo.db"
    backup_root = tmp_path / "backups"
    db = Database(str(db_path))
    try:
        conn = db.conn
        _seed_actions(conn, ["MyProduct"])
        ownership = {"MyProduct": OwnershipEntry("my_product", "acme")}
        first = apply_migration(conn, ownership, str(db_path), [], backup_root)
        assert first.mapped_rows >= 1
        # Second run: nothing left to backfill.
        second = apply_migration(conn, ownership, str(db_path), [], backup_root)
        assert second.mapped_rows == 0
        assert second.unmapped_count == 0
    finally:
        db.close()


def test_apply_refuses_on_unmappable_rows(tmp_path):
    db_path = tmp_path / "milo.db"
    backup_root = tmp_path / "backups"
    db = Database(str(db_path))
    try:
        conn = db.conn
        _seed_actions(conn, ["MyProduct", "Orphan"])
        ownership = {"MyProduct": OwnershipEntry("my_product", "acme")}
        with pytest.raises(MigrationError, match="Refusing --apply"):
            apply_migration(conn, ownership, str(db_path), [], backup_root)
        # Ambiguity abort must leave NO partial writes.
        row = conn.execute(
            "SELECT business_id FROM actions WHERE project = 'MyProduct'"
        ).fetchone()
        assert row["business_id"] in (None, "")
    finally:
        db.close()


def test_apply_backfills_fk_derived_tables(tmp_path):
    """analytics/ab_results/conversations get business_id from their parent."""
    db_path = tmp_path / "milo.db"
    backup_root = tmp_path / "backups"
    db = Database(str(db_path))
    try:
        conn = db.conn
        _seed_actions(conn, ["MyProduct"])
        action_id = conn.execute(
            "SELECT id FROM actions WHERE project='MyProduct'"
        ).fetchone()[0]
        # An analytics row tied to that action.
        conn.execute(
            "INSERT INTO analytics (action_id, metric_type, value) VALUES (?, 'clicks', 3)",
            (action_id,),
        )
        conn.commit()
        ownership = {"MyProduct": OwnershipEntry("my_product", "acme")}
        apply_migration(conn, ownership, str(db_path), [], backup_root)
        row = conn.execute(
            "SELECT business_id FROM analytics WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert row["business_id"] == "acme"
    finally:
        db.close()

