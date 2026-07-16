# Plan 004: Enforce strict business/product/account routing

> **Executor instructions**: This is an isolation boundary. Follow all steps, run all tests, and stop on ambiguity. Update `plans/README.md` when complete.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- safety/account_manager.py core/orchestrator.py core/database.py miloagent.py config/reddit_accounts.yaml config/twitter_accounts.yaml config/telegram_user_accounts.yaml tests/test_account_routing.py tests/test_orchestrator_routing.py`

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `plans/003-business-domain-and-data-migration.md`
- **Category**: security
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

`AccountManager.get_next_account` currently prefers assigned accounts but falls back to every account when no assignment matches. CLI paths also choose the first enabled account. With multiple businesses this can publish the wrong product from the wrong identity. Routing must be strict, stable, and business-scoped everywhere.

## Current state

- `safety/account_manager.py:225-279` filters `assigned_projects`, then silently keeps all available accounts if the filtered list is empty.
- Rotation, health, cooldown, and bot caches use `platform:username` or only `platform`, so same-service accounts are not isolated by business.
- `core/orchestrator.py:918` selects with only platform and project name.
- `core/orchestrator.py:319-325` caches Telegram clients by username/phone only.
- `miloagent.py:280-302`, `331-341`, and `358-374` choose the first enabled service account in manual paths.
- Config templates use mutable `assigned_projects` names and have no `account_id` or `business_id`.

## Target invariants

- Each credential record has stable `account_id`, one `business_id`, and zero or more product IDs from that same business.
- Account lookup always requires `business_id`; write selection also requires product ID.
- No match returns `None` plus an auditable decision. It never falls back across business/product boundaries.
- Rotation/cooldown/health/client-cache keys include `business_id`, platform, and `account_id`.
- Multiple distinct accounts for one service and business rotate independently and fairly.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Routing tests | `python -m pytest -q tests\test_account_routing.py tests\test_orchestrator_routing.py` | all pass |
| Fallback scan | `rg -n "fall back to all|next\(\(a for a in accounts" safety\account_manager.py core\orchestrator.py miloagent.py` | no unsafe selection match |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `safety/account_manager.py`
- `core/orchestrator.py`
- `core/database.py`
- `miloagent.py`
- `config/reddit_accounts.yaml`
- `config/twitter_accounts.yaml`
- `config/telegram_user_accounts.yaml`
- `tests/test_account_routing.py` (create)
- `tests/test_orchestrator_routing.py` (create)

**Out of scope**:
- Dashboard forms/endpoints (plan 009).
- Moving secrets from ignored local YAML to a new secret manager.
- Allowing one credential record to serve multiple businesses.
- Changing platform rate limits or personas.

## Git workflow

- Branch: `codex/improve-004-account-routing`
- Commit: `Enforce business-scoped account routing`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Normalize stable account identity

Update safe templates to include `account_id`, `business_id`, and product-ID assignments. Add a normalization/validation function in `AccountManager`; never mutate the parsed YAML dict in place. Reject duplicate account IDs, unknown businesses/products, and cross-business product assignments.

**Verify**: `python -m pytest -q tests\test_account_routing.py -k validation` -> all invalid combinations reject with no secrets in messages.

### Step 2: Make all manager state business-scoped

Change load/get/list/select APIs to accept business ID. Key `_statuses`, `_cooldowns`, `_last_used`, `_rotation_index`, karma/account stats, and health rows by stable account identity. Display handle/phone is metadata, not a key. Preserve per-account rate limits.

**Verify**: `python -m pytest -q tests\test_account_routing.py -k "rotation or state"` -> two businesses with same platform do not share index/status/cooldown.

### Step 3: Remove every fallback

`get_next_account(platform, business_id, product_id)` filters in that order and returns `None` when no valid account exists. Empty assignment means “unassigned”, not “all products”. Add an explicit `all_products: true` opt-in only if product-wide assignment is needed, and validate it remains inside one business.

**Verify**: `python -m pytest -q tests\test_account_routing.py -k no_fallback` -> wrong-business and unassigned cases return `None`.


### Step 3.5: Fix schema omissions from Plan 003
The previous executor missed adding `business_id TEXT` to the `CREATE TABLE` statements for `actions`, `account_health`, and `opportunities` in `core/database.py`.
Manually add `business_id TEXT,` to the `CREATE TABLE IF NOT EXISTS` blocks in `_init_tables` for those tables. Note: do not use `ALTER TABLE`, just update the `CREATE TABLE` and ensure it's placed after `id INTEGER PRIMARY KEY AUTOINCREMENT,`.

### Step 4: Propagate identity through orchestrator and database audit rows

For every scan, act, warm-up, seed, research, relationship, hub, manual, and health path, derive `business_id` and product ID from the selected product and pass them to account selection and database writes. Cache platform clients by `(business_id, platform, account_id)`. Log an explicit skipped decision when no assigned account exists.

**Verify**: `python -m pytest -q tests\test_orchestrator_routing.py` -> each platform receives only the chosen business/product account; cache collision regression passes.

### Step 5: Fix manual CLI selection

Manual commands must require or resolve a business and product, then use the same `AccountManager` API. If more than one candidate exists and the user did not specify `--account`, choose through the scoped rotation policy, not list order. If business context is ambiguous, exit with a CMD-compatible example rather than guessing.

**Verify**: CLI tests with two businesses prove an omitted business fails and an explicit business never selects the other tenant.

## Test plan

- Two businesses, two products each, and multiple Reddit/Telegram accounts per business.
- Strict no-match behavior and explicit all-products behavior.
- Independent rotation, cooldown, status, and client caches.
- Manual and scheduled paths share the same selection contract.
- Audit rows contain business, product, platform, and stable account identity.

## Done criteria

- [ ] No write path selects by “first enabled” or falls back to all accounts.
- [ ] Multiple accounts of one service rotate inside one business.
- [ ] No state/cache key relies on display username alone.
- [ ] All scoped tests and `python scripts\verify.py` pass.
- [ ] Only in-scope files and plan index changed.

## STOP conditions

- A runtime path cannot determine its business and product without user input.
- Existing local account entries are shared across products in different businesses.
- A database API cannot accept stable account identity without a migration not completed in plan 003.

## Maintenance notes

Treat business routing like authorization, not a preference. Future services must implement the same strict account selection contract before being exposed in the dashboard.

