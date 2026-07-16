# Plan 009: Add business-scoped dashboard APIs and multi-account CRUD

> **Executor instructions**: API scoping is an authorization boundary even for a single operator. Run all contract tests and update `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- dashboard/web.py safety/account_manager.py core/business_manager.py core/database.py tests/test_dashboard_business_api.py tests/test_dashboard_account_api.py`

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `plans/004-strict-business-account-routing.md`, `plans/005-telegram-personal-account-enforcement.md`, `plans/006-reddit-oauth-state-binding.md`, `plans/008-safe-dashboard-event-handlers.md`
- **Category**: security
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

Current dashboard endpoints return all projects, accounts, actions, opportunities, analytics, communities, and intelligence without business scope. `AccountCreate` claims Telegram support but only accepts username/password, while `AccountManager.add_account` rejects Telegram. Build a stable API contract that isolates business data and supports multiple same-service accounts with platform-specific credentials.

## Current state

- `dashboard/web.py:252-258` defines `AccountCreate` as Reddit/Telegram with only username/password/email/persona/projects.
- `dashboard/web.py:536-578` lists all Reddit/Telegram accounts.
- `dashboard/web.py:580-708` exposes global project CRUD and account create/delete by platform/username.
- Many metric/intel/export/community endpoints call unscoped database methods.
- `safety/account_manager.py:520-620` adds/removes only Reddit/Twitter and returns `Unknown platform: telegram`.
- API responses must never expose passwords, API hashes, tokens, cookie/session contents, or local secret file paths.

## Target API rules

- `GET /api/businesses` is the only unscoped tenant-discovery endpoint.
- Every tenant resource and manual control requires `business_id`; product resources also use stable product ID.
- If exactly one legacy business exists, compatibility may resolve it with a deprecation header. With multiple businesses, missing scope is 400, never “all”.
- Accounts are addressed by stable `account_id`, not username/phone path segments.
- Multiple distinct account records for the same platform/business are allowed.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| API tests | `python -m pytest -q tests\test_dashboard_business_api.py tests\test_dashboard_account_api.py` | all pass |
| Secret regression | `python -m pytest -q tests\test_dashboard_account_api.py -k secret` | all redaction/log-leak tests pass |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `dashboard/web.py`
- `safety/account_manager.py`
- `core/business_manager.py`
- `core/database.py`
- `tests/test_dashboard_business_api.py` (create)
- `tests/test_dashboard_account_api.py` (create)

**Out of scope**:
- Browser UI/CSS (plan 010).
- Dashboard multi-user roles.
- Returning aggregate data across businesses from tenant endpoints.
- Storing secrets in the main SQLite database.
- Interactive Telegram authorization, QR rendering, and 2FA submission (plan 011).

## Git workflow

- Branch: `codex/improve-009-business-api`
- Commits: `Add business-scoped dashboard API`, then `Add platform-specific account onboarding`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Add a reusable business-scope dependency

Implement a FastAPI dependency that validates authenticated `business_id` against `BusinessManager` and returns a business context. Apply it to products, accounts, actions, opportunities, stats, history, analytics, exports, communities, takeover, network, intel, and manual controls. Keep server/CPU/RAM/scheduler health explicitly global and label responses `scope: global`.

**Verify**: parameterized tests call every tenant endpoint without business ID (400), with unknown ID (404), and with valid ID (200/no foreign rows).

### Step 2: Add business and product CRUD contracts

Expose safe business DTOs and CRUD with stable IDs. Product CRUD is nested or requires business scope and validates ownership. Deletes are soft/disabled when tenant data or accounts exist unless a separate confirmed archival operation is used. Never build file paths from raw display names.

**Verify**: tests create two businesses/products, prove isolation, reject duplicate IDs/cross-owner updates, and preserve UTF-8 display names.

### Step 3: Replace generic account payloads with discriminated schemas

Use a shared base (`account_id`, `business_id`, platform, persona, product IDs, enabled) plus platform-specific fields:

- Reddit: username/password/email and cookie/OAuth status.
- Telegram user: API ID/API hash and session status; `account_type` must be `user`. Phone is optional before QR login and is populated from the authorized Telegram identity.
- Twitter: existing username/email/password shape if kept exposed.

Validate product ownership. Persist only to ignored local YAML with atomic replace and restrictive file permissions where supported. Return a redacted account DTO with status booleans, never secret fields.

**Verify**: tests add three Reddit and two Telegram accounts to one business, list all five, and assert secret seed strings never appear in JSON/logs.

### Step 4: Persist Telegram accounts in a non-authorized state

Creating a Telegram account record must generate a collision-safe ignored session path from `business_id` and `account_id`, set `auth_status = "not_authorized"`, and keep `enabled = false`. The account must not enter rotation until a later authorization flow both creates a valid session and confirms `me.bot == False`. Do not ask for an SMS code or 2FA password in this plan; plan 011 owns the QR and 2FA challenge lifecycle.

**Verify**: account API tests prove a newly created Telegram record is visible but disabled, has redacted credentials, cannot be selected by `AccountManager`, and exposes only the stable status needed by plan 011.

### Step 5: Address accounts by stable ID

Change health, performance, OAuth, cookie, disable/remove, and login endpoints to stable account IDs plus business scope. Keep username-only routes as temporary single-business compatibility shims with deprecation headers; reject ambiguous matches.

**Verify**: two accounts with similar display names remain independently addressable; wrong-business access is 404 and makes no file/DB change.

### Step 6: Scope database queries and exports

Extend database read APIs to require/filter `business_id` for tenant data. Audit any direct SQL in `dashboard/web.py`; no tenant response may fetch all then filter only in Python. Export filenames and content include business ID, and no export contains another tenant's rows.

**Verify**: seeded cross-business fixture proves every endpoint/export returns only selected business rows.

## Test plan

- Endpoint scope matrix across all tenant route families.
- Business/product CRUD ownership and archival safeguards.
- Multiple same-service accounts in one business.
- Platform-specific validation and redacted DTOs.
- Telegram account creation remains disabled with `auth_status = "not_authorized"` until plan 011 completes authorization.
- Stable-ID health/OAuth/cookie/remove operations.
- Cross-business database/export isolation.

## Done criteria

- [ ] Every tenant endpoint requires and enforces business scope.
- [ ] One business can CRUD multiple accounts for the same service.
- [ ] Telegram onboarding cannot enable a bot identity.
- [ ] No response/log exposes credential material.
- [ ] Contract tests and full gate pass.
- [ ] Only in-scope files and plan index changed.

## STOP conditions

- Any tenant endpoint cannot identify the owning business in persisted data.
- Secure local secret-file permissions cannot be preserved on the target OS; report platform behavior before weakening it.
- An endpoint requires returning a secret for the UI to function.

## Maintenance notes

New tenant endpoints must use the shared scope dependency and database predicate. Keep global operational endpoints explicit so “missing business filter” cannot be mistaken for intentional aggregation.
