"""Tenant data migration: backfill business_id from legacy project names.

Deterministic ownership map:
- The legacy ``project`` column on each tenant row is a display name.
- BusinessManager now exposes, per product, both the display name and the
  immutable (product id, business_id). The migrator maps each distinct legacy
  ``project`` value to a (product_id, business_id).
- An explicit ``default_business_id`` may be supplied ONLY when every legacy
  product resolves to that one business. Otherwise unmappable rows stop the
  migration (never guess).

Safety:
- ``--apply`` first backs up the SQLite DB (online backup API) and copies the
  affected YAML files into a timestamped, gitignored backup directory.
- The backfill runs in a single transaction; on any error it rolls back.
- After apply, no tenant row may have NULL/empty business_id (asserted).
- Idempotent: a second run reports zero work because rows already have
  non-empty business_id.

NEVER logs row content, credentials, tokens, phone numbers, or generated text.
Only structural identifiers (table name, row id, legacy project name) appear in
the dry-run report.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.schema_migrations import TENANT_TABLES_WITH_BUSINESS_ID

logger = logging.getLogger(__name__)

# Tables that store the legacy tenant key directly in a ``project`` column.
# (FK-derived tables analytics/ab_results/conversations have business_id added
# by the schema migration but are backfilled by joining their owner row; here we
# only enumerate tables with a direct ``project`` column.)
DIRECT_PROJECT_TABLES: Tuple[str, ...] = tuple(
    t for t in TENANT_TABLES_WITH_BUSINESS_ID
    # account_subreddit_stats has no project column (keyed by account/subreddit);
    # analytics/ab_results/conversations derive via FK and have no project col.
    if t != "account_subreddit_stats"
)

# Tables whose business_id is derived via a foreign key rather than a direct
# project column. We backfill these by joining the parent's business_id.
# (Map child -> (parent_table, child_fk_col))
FK_DERIVED_TABLES: Dict[str, Tuple[str, str]] = {
    "analytics": ("actions", "action_id"),
    "ab_results": ("ab_experiments", "experiment_id"),
    "conversations": ("relationships", "relationship_id"),
}


@dataclass
class MigrationReport:
    """Result of a dry-run or apply. Carries counts only — never row content."""
    mapped_rows: int = 0
    unmapped_rows: int = 0
    # List of (table, row_id, legacy_project) for unmappable rows.
    unmapped: List[Tuple[str, int, str]] = field(default_factory=list)
    backed_up_db: Optional[str] = None
    backed_up_yaml_dir: Optional[str] = None
    per_table_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def unmapped_count(self) -> int:
        return len(self.unmapped)


@dataclass
class OwnershipEntry:
    """One legacy project name -> product/business resolution."""
    product_id: str
    business_id: str


class MigrationError(Exception):
    """Raised when the migration cannot proceed safely."""


def build_ownership_map(
    products: List[Dict],
    default_business_id: Optional[str] = None,
) -> Dict[str, OwnershipEntry]:
    """Map legacy project display names -> OwnershipEntry.

    ``products`` is the list of product dicts from BusinessManager.projects.

    Rules:
    - Each product's ``project.name`` maps to its (project.id, business_id).
    - If a legacy project name resolves to more than one business, raise
      MigrationError (STOP condition).
    - ``default_business_id`` is accepted ONLY when every legacy product belongs
      to that single business; otherwise raise MigrationError.
    """
    # name -> {business_id: product_id}
    by_name: Dict[str, Dict[str, str]] = {}
    for p in products:
        proj = p.get("project", {})
        name = proj.get("name")
        pid = proj.get("id")
        bid = proj.get("business_id")
        if not name:
            continue
        by_name.setdefault(name, {})
        if bid:
            by_name[name][bid] = pid

    ownership: Dict[str, OwnershipEntry] = {}
    all_businesses: set = set()
    for name, bids in by_name.items():
        if len(bids) > 1:
            raise MigrationError(
                f"Legacy project name '{name}' maps to more than one business: "
                f"{sorted(bids)}"  # ids only, no content
            )
        if not bids:
            # No resolved business; cannot map without guessing.
            continue
        bid = next(iter(bids))
        pid = bids[bid]
        ownership[name] = OwnershipEntry(product_id=pid, business_id=bid)
        all_businesses.add(bid)

    if default_business_id is not None:
        if not all_businesses:
            # Nothing to map; default is accepted but inert.
            return ownership
        offending = all_businesses - {default_business_id}
        if offending:
            raise MigrationError(
                "Explicit default business refused: legacy products resolve to "
                f"multiple businesses {sorted(all_businesses)} (default was "
                f"'{default_business_id}'). All legacy products must belong to "
                "the default business."
            )
        # All products share the default business — safe.

    return ownership


def _quote_ident(name: str) -> str:
    """Quote a SQL identifier defensively."""
    return '"' + name.replace('"', '""') + '"'


def _legacy_project_values(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Return [(table, legacy_project_value)] for tables with a project column.

    Only distinct non-empty legacy project values whose rows still have
    NULL/empty business_id are candidates (the migrator only touches those).
    """
    candidates: List[Tuple[str, str]] = []
    for table in DIRECT_PROJECT_TABLES:
        # Confirm the table and its project/business_id columns exist.
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()}
        if "project" not in cols:
            continue
        if "business_id" not in cols:
            # Schema migration hasn't run for this table yet.
            continue
        rows = conn.execute(
            f"SELECT DISTINCT project FROM {_quote_ident(table)} "
            f"WHERE project IS NOT NULL AND project != '' "
            f"AND (business_id IS NULL OR business_id = '')"
        ).fetchall()
        for r in rows:
            candidates.append((table, r[0]))
    return candidates


def plan_migration(
    conn: sqlite3.Connection,
    ownership: Dict[str, OwnershipEntry],
) -> MigrationReport:
    """Compute what would change without writing. Returns a report.

    Lists every tenant row whose legacy ``project`` cannot be mapped under
    (table, row_id, legacy_project). Never inspects row content.
    """
    report = MigrationReport()
    seen_unmapped: set = set()

    for table in DIRECT_PROJECT_TABLES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()}
        if "project" not in cols or "business_id" not in cols:
            continue
        rows = conn.execute(
            f"SELECT id, project FROM {_quote_ident(table)} "
            f"WHERE project IS NOT NULL AND project != '' "
            f"AND (business_id IS NULL OR business_id = '')"
        ).fetchall()
        mapped_here = 0
        for r in rows:
            row_id = r[0]
            legacy = r[1]
            entry = ownership.get(legacy)
            if entry is None:
                key = (table, row_id, legacy)
                if key not in seen_unmapped:
                    report.unmapped.append(key)
                    seen_unmapped.add(key)
            else:
                mapped_here += 1
        if mapped_here:
            report.per_table_counts[table] = mapped_here
            report.mapped_rows += mapped_here

    # FK-derived rows: count how many would be backfilled via their parent.
    for child, (parent, fk_col) in FK_DERIVED_TABLES.items():
        child_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(child)})").fetchall()}
        parent_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(parent)})").fetchall()}
        if "business_id" not in child_cols or "business_id" not in parent_cols:
            continue
        if fk_col not in child_cols:
            continue
        row = conn.execute(
            f"SELECT COUNT(*) FROM {_quote_ident(child)} c "
            f"JOIN {_quote_ident(parent)} p ON c.{fk_col} = p.id "
            f"WHERE (c.business_id IS NULL OR c.business_id = '') "
            f"AND p.business_id IS NOT NULL AND p.business_id != ''"
        ).fetchone()
        cnt = row[0] if row else 0
        if cnt:
            report.per_table_counts[child] = report.per_table_counts.get(child, 0) + cnt
            report.mapped_rows += cnt

    report.unmapped_rows = len(report.unmapped)
    return report


def _backup_database(db_path: str, backup_dir: Path) -> Path:
    """Copy the live DB via SQLite's online backup API into backup_dir.

    Returns the backup file path. The backup API produces a consistent copy
    even while the DB is in WAL mode.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dest_path = backup_dir / f"miloagent-{ts}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(dest_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_path


def _backup_yaml_files(
    project_yaml_paths: List[Path], backup_dir: Path
) -> Path:
    """Copy the given YAML files into a timestamped subdir of backup_dir."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    yaml_dir = backup_dir / f"yaml-{ts}"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    for src in project_yaml_paths:
        if src.exists():
            shutil.copy2(src, yaml_dir / src.name)
    return yaml_dir


def apply_migration(
    conn: sqlite3.Connection,
    ownership: Dict[str, OwnershipEntry],
    db_path: str,
    project_yaml_paths: List[Path],
    backup_root: Path,
) -> MigrationReport:
    """Back up, then backfill business_id transactionally.

    Raises MigrationError if any row would remain unmapped (refuses to apply).
    Returns a report with backup paths and per-table counts.
    """
    report = plan_migration(conn, ownership)
    if report.unmapped:
        raise MigrationError(
            f"Refusing --apply: {report.unmapped_rows} row(s) cannot be mapped "
            "to a business without guessing. See the dry-run report for "
            "(table, id, project)."
        )

    # ── Backups first ──────────────────────────────────────────────────
    report.backed_up_db = str(_backup_database(db_path, backup_root))
    report.backed_up_yaml_dir = str(
        _backup_yaml_files(project_yaml_paths, backup_root)
    )

    # ── Transactional backfill ─────────────────────────────────────────
    try:
        conn.execute("BEGIN IMMEDIATE")
        total = 0
        for table in DIRECT_PROJECT_TABLES:
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()}
            if "project" not in cols or "business_id" not in cols:
                continue
            for legacy, entry in ownership.items():
                cur = conn.execute(
                    f"UPDATE {_quote_ident(table)} SET business_id = ? "
                    f"WHERE project = ? "
                    f"AND (business_id IS NULL OR business_id = '')",
                    (entry.business_id, legacy),
                )
                total += cur.rowcount if cur.rowcount > 0 else 0

        # FK-derived: copy business_id from parent via join.
        for child, (parent, fk_col) in FK_DERIVED_TABLES.items():
            child_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(child)})").fetchall()}
            parent_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(parent)})").fetchall()}
            if "business_id" not in child_cols or "business_id" not in parent_cols:
                continue
            if fk_col not in child_cols:
                continue
            cur = conn.execute(
                f"UPDATE {_quote_ident(child)} SET business_id = "
                f"(SELECT p.business_id FROM {_quote_ident(parent)} p "
                f"WHERE p.id = {child}.{fk_col}) "
                f"WHERE (business_id IS NULL OR business_id = '')"
            )
            total += cur.rowcount if cur.rowcount > 0 else 0

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        logger.exception("business_id backfill failed; rolled back")
        raise

    # ── Post-condition: zero NULL/empty business_id where resolvable ──
    # Direct tables: every row with a non-empty project must now have a
    # non-empty business_id.
    violations = _count_business_id_violations(conn)
    if violations:
        raise MigrationError(
            f"Post-condition failed: {violations} row(s) still have NULL/empty "
            "business_id after apply. Backup is at: " + str(report.backed_up_db)
        )

    report.mapped_rows = total
    return report


def _count_business_id_violations(conn: sqlite3.Connection) -> int:
    """Count rows with a non-empty project but empty business_id (direct tables)."""
    total = 0
    for table in DIRECT_PROJECT_TABLES:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()}
        if "project" not in cols or "business_id" not in cols:
            continue
        row = conn.execute(
            f"SELECT COUNT(*) FROM {_quote_ident(table)} "
            f"WHERE project IS NOT NULL AND project != '' "
            f"AND (business_id IS NULL OR business_id = '')"
        ).fetchone()
        total += row[0] if row else 0
    return total


def affected_yaml_paths(products: List[Dict], projects_dir: Path) -> List[Path]:
    """Return YAML file paths for products that resolved a business_id."""
    paths: List[Path] = []
    for p in products:
        proj = p.get("project", {})
        if proj.get("business_id"):
            name = proj.get("name", "")
            if name:
                slug = name.lower().replace(" ", "_")
                candidate = projects_dir / f"{slug}.yaml"
                if candidate.exists():
                    paths.append(candidate)
    return paths
