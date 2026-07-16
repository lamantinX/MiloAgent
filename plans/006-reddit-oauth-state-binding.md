# Plan 006: Bind Reddit OAuth to one business account with one-time state

> **Executor instructions**: Treat OAuth state as a security boundary. Follow the plan, run every test, and update `plans/README.md`. Never print or commit credentials.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- dashboard/web.py dashboard/oauth_state.py safety/account_manager.py tests/test_reddit_oauth.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: HIGH
- **Depends on**: `plans/004-strict-business-account-routing.md`
- **Category**: security
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

The Reddit OAuth callback currently treats `state` as a URL-encoded username. It is predictable, reusable, not tied to an authenticated start request, and cannot distinguish same-service accounts by business. Replace it with a short-lived opaque one-time state mapped server-side to stable business/account identity.

## Current state

- `dashboard/web.py:765-786` builds the authorization URL with `state = quote(username)`.
- `dashboard/web.py:788-870` exposes a public callback, reads that state as username, exchanges the code, and writes a refresh token.
- The start endpoint is authenticated, but the callback's only correlation is the predictable username.
- After plan 004 the durable identity is `(business_id, account_id)`, not username.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| OAuth tests | `python -m pytest -q tests\test_reddit_oauth.py` | all pass |
| Legacy-state scan | `rg -n "state = quote\(username\)|unquote\(state\)" dashboard\web.py` | no matches |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `dashboard/web.py`
- `dashboard/oauth_state.py` (create)
- `safety/account_manager.py`
- `tests/test_reddit_oauth.py` (create)

**Out of scope**:
- Changing Reddit scopes, app registration, or callback URL.
- Supporting multiple dashboard users/roles.
- Returning refresh tokens to the browser.
- A distributed state store; the current deployment is one process.

## Git workflow

- Branch: `codex/improve-006-reddit-oauth-state`
- Commit: `Secure Reddit OAuth state and account binding`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Add a bounded one-time state store

Create a thread-safe `OAuthStateStore` using `secrets.token_urlsafe(32)`. Store only purpose, `business_id`, `account_id`, redirect destination, creation time, and expiry. Default TTL is 10 minutes; cap entries and purge expired items. `consume` must remove before returning so concurrent callbacks cannot reuse it.

**Verify**: focused unit tests cover uniqueness, expiry, capacity, and exactly-one successful concurrent consume.

### Step 2: Bind the authenticated start request

Change the OAuth start endpoint to accept stable account ID plus business ID, load that exact Reddit account through the strict manager, and create opaque state. Reject missing/wrong-business/disabled accounts. The authorization URL must contain no username, phone, business name, or secret.

**Verify**: start-endpoint test inspects the redirect and confirms state is opaque and account data is absent.

### Step 3: Consume state before code exchange

The callback consumes state once, verifies purpose and TTL, then exchanges the code. Unknown, expired, or reused state returns 400 before any outbound request. On success, write the refresh token only to the ignored local config record matching both IDs. Preserve unrelated YAML keys and use an atomic temp-file replace.

**Verify**: mocked exchange tests cover success, unknown state, expired state, replay, Reddit error, and wrong account. No test writes outside `tmp_path`.

### Step 4: Sanitize logs and responses

Log stable non-secret IDs and outcome only. Do not log code, state, client secret, refresh token, password, or entire account config. Redirect the browser to a fixed success/error route with a generic status code, not token data.

**Verify**: tests capture logs/responses and assert seeded secret values are absent.

## Test plan

- Opaque random state, ten-minute TTL, one-time consume, replay rejection.
- Correct binding to business/account and rejection across businesses.
- Exchange is never called for invalid state.
- Atomic config update modifies exactly one account and preserves Unicode.
- Secrets absent from URL after callback, response, and logs.

## Done criteria

- [ ] Username is never used as OAuth state.
- [ ] State is random, short-lived, single-use, and business/account-bound.
- [ ] Token writes target exactly one ignored local account record.
- [ ] `python scripts\verify.py` passes.
- [ ] Only in-scope files and plan index changed.

## STOP conditions

- Deployment is actually multi-process or horizontally scaled; an in-memory state store would be inconsistent, so report the required shared store.
- Account persistence cannot atomically target stable IDs.
- Reddit requires a callback behavior that conflicts with one-time state; provide primary documentation before changing design.

## Maintenance notes

If the dashboard later gains user sessions, add the dashboard principal ID to state. If deployment scales out, replace the in-memory store with an atomic shared TTL store without changing endpoint semantics.

