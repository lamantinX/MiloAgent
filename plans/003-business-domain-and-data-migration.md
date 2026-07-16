# Plan 003: Introduce first-class businesses and migrate tenant data

> **Executor instructions**: Follow this migration plan exactly and preserve backups. Stop rather than guessing when legacy ownership is ambiguous. Update the plan index when complete.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- core/business_manager.py core/database.py core/schema_migrations.py miloagent.py .gitignore businesses projects/example_project.yaml tests/test_business_manager.py tests/test_business_migration.py`

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `plans/001-verification-baseline.md`
- **Category**: migration
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

The current `BusinessManager` actually manages product/project YAML files, while persistent data stores only a project name. That cannot isolate two businesses, support multiple products under one business, or safely filter history. This plan introduces stable business and product identifiers and backfills tenant-owned data without silently guessing ownership.

## Current state

- `core/business_manager.py:1` says `Business (project) manager`; it only loads `projects/*.yaml` and keys products by `project.name`.
- `projects/example_project.yaml:15` has a `project` block but no stable `id` or `business_id`.
- `core/database.py:29` declares `SCHEMA_VERSION = 4`, but schema changes are ad-hoc `ALTER TABLE` calls.
- `actions`, `opportunities`, `performance`, learning/intel tables, relationships, decision logs, community tables, account health/stats, and hub tables contain tenant data but no `business_id`.
- Real `projects/*.yaml` and `*.local.yaml` are gitignored. Never commit or print their contents.

## Target invariants

- Business IDs and product IDs are immutable lowercase slugs matching `^[a-z0-9][a-z0-9_-]{1,63}$`.
- Each product has exactly one `business_id`.
- Historical rows have a nonempty `business_id`; ambiguous rows stop migration and are reported.
- Human-readable names may change without breaking references.
- A compatibility alias may still expose “project” internally, but UI/docs call it “product”.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Migration dry run | `python miloagent.py business migrate-legacy --dry-run` | exit 0; counts only, no writes |
| Migration | `python miloagent.py business migrate-legacy --apply` | exit 0; backup paths and migrated counts printed, no secrets |
| Focused tests | `python -m pytest -q tests\test_business_manager.py tests\test_business_migration.py` | all pass |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `core/business_manager.py`
- `core/database.py`
- `core/schema_migrations.py` (create)
- `miloagent.py`
- `.gitignore`
- `businesses/example_business.yaml` (create)
- `projects/example_project.yaml`
- `tests/test_business_manager.py` (create)
- `tests/test_business_migration.py` (create)

**Out of scope**:
- Runtime account selection (plan 004).
- Dashboard API/UI changes (plans 009-010).
- Deleting legacy `project` columns or renaming every internal symbol.
- Inferring ownership from account names, URLs, or content.

## Git workflow

- Branch: `codex/improve-003-business-domain`
- Commits: `Add first-class business and product identifiers`, then `Add tenant data migration`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Add business registry and stable product identity

Extend `BusinessManager` to load `businesses/*.yaml` plus products. Add `businesses`, `get_business`, `get_products(business_id)`, `add_business`, and validation methods. The committed example business contains only safe metadata. Add `project.id` and `project.business_id` to the product template. Update `.gitignore` so real business YAML is ignored while `businesses/example_business.yaml` is committed.

**Verify**: `python -m pytest -q tests\test_business_manager.py -k "load or validate"` -> valid relationships load; duplicate/unknown IDs fail.

### Step 2: Add an explicit schema migration runner

Create `core/schema_migrations.py` and make `Database` invoke ordered, transactional migrations from version 4 onward. Store version in a schema metadata table. Migration failures must roll back and leave the original database usable.

Add `business_id` to every tenant-owned table, including tables created in `core/database.py`, `core/community_manager.py`, and `core/subreddit_hub.py`. Tables that derive ownership only through a strict foreign key (`analytics`, `ab_results`, `conversations`) may retain that derivation if tests prove all joins are mandatory; otherwise add `business_id` directly.

**Verify**: `python -m pytest -q tests\test_business_migration.py -k schema` -> a v4 fixture migrates once, rerun is idempotent, rollback test passes.

### Step 3: Build a deterministic ownership map

Map legacy project names to product IDs and business IDs from the new YAML. The migrator accepts an explicit default business only when all legacy products belong to it. If a row's project cannot be mapped, list table/id/project in the dry-run report and refuse `--apply`.

Do not log row content, credentials, tokens, phone numbers, or generated replies.

**Verify**: `python miloagent.py business migrate-legacy --dry-run` -> every live legacy row is mapped or the command exits nonzero with identifiers only.

### Step 4: Back up and apply transactionally

Before `--apply`, copy the SQLite database using SQLite backup API and copy only the affected YAML files to a timestamped ignored backup directory. Add `business_id` and stable product ID values. Ensure zero NULL/empty `business_id` values across tenant tables after the transaction.

**Verify**: `python miloagent.py business migrate-legacy --apply` -> backup created and zero unmapped rows; a second run reports no work.

### Step 5: Keep one-business compatibility explicit

Legacy project files without IDs may load only in a single-business compatibility mode and must emit one clear warning. More than one business with any legacy product is a hard error. This compatibility is temporary and documented for removal after plan 011.

**Verify**: `python -m pytest -q tests\test_business_manager.py -k legacy` -> one-business compatibility passes; multi-business ambiguity raises.

## Test plan

- Business/product load, stable IDs, duplicate IDs, unknown owner, disabled entries.
- Empty v4 database and populated v4 fixture migration.
- Ambiguous project mapping aborts without partial writes.
- Backup exists before live mutation; migration is idempotent.
- Unicode display names survive round-trip as UTF-8 without BOM.

## Done criteria

- [ ] Business and product are separate persisted concepts.
- [ ] All tenant rows have a resolvable nonempty `business_id` after apply.
- [ ] Migration is transactional, backed up, and idempotent.
- [ ] No real business/project config or data backup is tracked by Git.
- [ ] `python scripts\verify.py` exits 0.
- [ ] Status row is DONE.

## STOP conditions

- Any project name maps to more than one business.
- Any tenant row cannot be assigned without inspecting content or credentials.
- The live database is open in a mode that prevents a consistent backup.
- An in-scope table has a uniqueness constraint that cannot include `business_id` without rebuilding it; report the table and proposed rebuild before proceeding.

## Maintenance notes

Plans 003 and 004 should ship together before enabling a second business. Keep display names out of foreign keys. A later cleanup may remove the compatibility alias only after all configs are migrated.

