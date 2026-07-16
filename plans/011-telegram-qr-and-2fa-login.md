# Plan 011: Add Telegram QR login with optional 2FA password

> **Executor instructions**: Treat the QR payload, Telegram session, and 2FA password as authentication secrets. Follow every step and verification exactly. Stop on any listed STOP condition; do not improvise. When done, update this plan's row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- requirements.txt dashboard/telegram_auth.py dashboard/web.py dashboard/static/index.html dashboard/static/app.js dashboard/static/cyber.css safety/account_manager.py tests/test_telegram_qr_auth.py tests/test_telegram_qr_ui.py`
> If any in-scope file changed, compare the current-state excerpts against live code before proceeding.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `plans/005-telegram-personal-account-enforcement.md`, `plans/009-business-scoped-dashboard-api.md`, `plans/010-business-switcher-ui.md`
- **Category**: security
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

Telegram accounts currently require a CLI phone/SMS login, which is unsuitable for the requested dashboard workflow. The dashboard must show a QR code that can be scanned from an already authorized Telegram mobile app and, when Telegram two-step verification is enabled, securely ask for the 2FA password after the scan. The account must remain disabled until Telethon confirms a non-bot user and persists the session.

## Current state

- `miloagent.py:1424-1508` implements CLI-only phone/SMS login and catches generic exception text to decide whether to ask for 2FA.
- `platforms/telegram_group_bot.py:100-137` can reuse an authorized Telethon session but cannot create one through the dashboard.
- `dashboard/web.py:411-418` validates a bearer token but returns only `True`; QR challenges therefore cannot yet bind to a stable dashboard session principal.
- `dashboard/static/app.js:6` stores the dashboard bearer token in `localStorage`; API helpers send it in the Authorization header.
- `requirements.txt` has Telethon and Pillow but no local QR image generator.
- At plan time, the local environment has Telethon 1.36.0. Official stable Telethon documentation confirms that `client.qr_login()` returns a `QRLogin`, `QRLogin.wait()` must already be running while the code is scanned, `QRLogin.recreate()` refreshes an expired code, and `wait()` raises `SessionPasswordNeededError` when 2FA is enabled.

## Target state machine

Each challenge has exactly one of these public states:

`waiting_scan -> password_required -> authorized`

or

`waiting_scan -> expired | cancelled | failed`

The challenge is bound to dashboard session principal, `business_id`, and `account_id`. Only one active challenge is allowed per Telegram account. The account remains `enabled = false` until `authorized`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Install | `python -m pip install -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Backend tests | `python -m pytest -q tests\test_telegram_qr_auth.py` | all pass with fake Telethon clients |
| UI tests | `python -m pytest -q tests\test_telegram_qr_ui.py` | all pass |
| JavaScript syntax | `node --check dashboard\static\app.js` | exit 0; no output |
| Full gate | `python scripts\verify.py` | exit 0 |

## Suggested executor toolkit

- Use the official Telethon client documentation for `qr_login`, `QRLogin.wait`, `QRLogin.recreate`, `SessionPasswordNeededError`, and `sign_in(password=...)`: `https://docs.telethon.dev/en/stable/modules/client.html` and `https://docs.telethon.dev/en/stable/modules/custom.html#telethon.tl.custom.qrlogin.QRLogin`.
- Use the API behavior present in both the repository's minimum Telethon 1.x line and the installed version. Do not upgrade to an incompatible major version as part of this plan.

## Scope

**In scope**:
- `requirements.txt`
- `dashboard/telegram_auth.py` (create)
- `dashboard/web.py`
- `dashboard/static/index.html`
- `dashboard/static/app.js`
- `dashboard/static/cyber.css`
- `safety/account_manager.py`
- `tests/test_telegram_qr_auth.py` (create)
- `tests/test_telegram_qr_ui.py` (create)

**Out of scope**:
- Telegram Bot API or BotFather tokens.
- Changing group-commenting behavior, rate limits, or target groups.
- Replacing the existing CLI phone/SMS fallback.
- Persisting QR challenges, SMS codes, or 2FA passwords in SQLite/YAML.
- Supporting multiple Uvicorn workers without a shared challenge/session design.

## Git workflow

- Branch: `codex/improve-011-telegram-qr-2fa`
- Commits: `Add secure Telegram QR authentication challenge`, then `Add Telegram QR and 2FA dashboard flow`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Add local QR rendering and explicit auth models

Add a bounded `qrcode[pil]` dependency compatible with the existing Pillow range. Create `dashboard/telegram_auth.py` with typed challenge state, typed public status DTOs, and a `TelegramAuthChallengeManager`. Never expose Telethon client objects, QR URLs/tokens, API hashes, session paths, or exception strings in DTOs.

Render `QRLogin.url` into PNG bytes locally. Return the PNG only as authenticated, no-store response data (for example a base64 data URL in the start/refresh response). Do not call a remote QR service or load a QR generator from a CDN.

**Verify**: `python -m pytest -q tests\test_telegram_qr_auth.py -k "render or dto"` -> the generated image decodes as PNG, represents the fake QR payload, and public DTO/log capture contains no raw payload or seeded secrets.

### Step 2: Bind challenges to dashboard and business identity

Change `_verify_token` or add a sibling dependency so authenticated routes receive a stable server-side session principal without exposing/storing the raw bearer token in challenge state. `start` must validate the selected business and Telegram account, require `auth_status = "not_authorized"`, and reject wrong-business, disabled/deleted, bot-config, or already-authorized records.

Use a cryptographically random challenge ID, bounded store, TTL derived from `QRLogin.expires`, and at most one active challenge per account. Session files must use the collision-safe server-generated path from plan 009. Starting a replacement challenge must cancel/disconnect the previous one first.

**Verify**: focused tests cover wrong business/principal, duplicate start, capacity, TTL, replacement, and absence of raw bearer tokens in manager state.

### Step 3: Start waiting before returning the QR

For `POST /api/businesses/{business_id}/accounts/{account_id}/telegram/qr-login`, create and connect one temporary `TelegramClient`, call `client.qr_login()`, then schedule `qr_login.wait()` on the same event loop. Yield control until the wait task has started before returning the QR response; the official API requires `wait()` to be executing while the mobile app scans.

The response contains only challenge ID, `waiting_scan`, expiration timestamp, QR PNG data, and polling interval. Add `Cache-Control: no-store`, `Pragma: no-cache`, and a restrictive referrer policy. For non-loopback clients, reject the entire QR/2FA flow unless the request is HTTPS. Trust forwarded scheme headers only from explicitly configured proxies.

**Verify**: a coordinated fake proves `wait()` starts before the endpoint returns; remote HTTP is rejected, loopback HTTP is allowed, and HTTPS succeeds.

### Step 4: Monitor completion and handle expiration safely

The background task maps outcomes without leaking Telegram error text:

- normal user result -> verify `user.bot is False`, finalize authorization;
- `SessionPasswordNeededError` -> `password_required` while keeping the same client/session alive;
- timeout/token expiry -> `expired`;
- cancellation -> `cancelled`;
- other known authentication errors -> stable generic `failed` code.

Expose an authenticated status endpoint scoped to the same principal/business/account. Add a refresh endpoint that is allowed only for `expired`, calls `QRLogin.recreate()`, replaces the PNG/expiry, and starts a new wait before returning. Add a cancel endpoint that cancels tasks, disconnects the client, removes temporary session artifacts when authorization did not finish, and makes replay impossible.

**Verify**: tests cover success, expiry, refresh, cancellation, replay, concurrent polling, task cleanup, and no orphan client/session on every terminal state.

### Step 5: Accept the 2FA password only when requested

Add `POST /api/businesses/{business_id}/telegram-login/{challenge_id}/2fa` with a Pydantic secret field in the JSON body. It is valid only in `password_required` and for the bound dashboard principal/business/account. Pass the request-scoped value directly to `client.sign_in(password=...)`; never store it in challenge state, YAML, SQLite, analytics, URLs, exception messages, or logs.

Catch `PasswordHashInvalidError` explicitly and return a generic invalid-password result. Allow at most three attempts per challenge with a short server-side delay/rate limit; after the limit, cancel the challenge and require a fresh QR. Python strings cannot be reliably zeroized, so make no zeroization claim: minimize lifetime, clear references in `finally`, and disconnect on terminal outcomes.

**Verify**: tests cover correct password, wrong password, fourth-attempt rejection, password sent in query (rejected), wrong state/principal/business, and seeded password absent from logs/responses/persisted files.

### Step 6: Finalize only a personal user session

After QR-only or QR+2FA success, call `get_me()`, reject `me.bot == True`, and persist only non-secret identity metadata needed for display/routing. Mark the exact account `auth_status = "authorized"` and `enabled = true` atomically only after the session is usable. Disconnect the temporary client so the normal engagement adapter can later open the saved session; do not run the same session simultaneously in two clients.

On bot rejection or finalization failure, keep the account disabled, delete the incomplete session, and return a stable failure code.

**Verify**: fake personal user enables exactly one account; fake bot and failed session check enable none; subsequent fake engagement client opens the saved session without duplicate-client use.

### Step 7: Add the QR and 2FA modal flow

On a Telegram account card with `not_authorized`, add `Authorize with Telegram`. The modal must:

1. Start the challenge and show the locally generated QR with an expiration countdown.
2. Explain: Telegram mobile app -> Settings -> Devices -> Link Desktop Device -> scan.
3. Poll the bound status endpoint at the server-provided interval.
4. On `password_required`, remove the QR image from the DOM and show one password input plus Submit.
5. On `expired`, show Refresh QR; on success, close and refresh the account card; on close/logout/business switch, cancel the challenge.

The 2FA input uses `type="password"`, is never copied to `localStorage`/`sessionStorage`/URL/console, and is cleared in `finally` after every submission. Clear timers, image `src`, response objects, and DOM nodes when the modal closes. All identifiers use safe event listeners from plan 008.

**Verify**: UI tests simulate QR success, 2FA success, wrong password, expiration/refresh, modal close, logout, and business switch. Assertions prove no QR/password data remains in storage or live modal DOM after cleanup.

### Step 8: Add lifecycle cleanup

Wire challenge manager startup/shutdown into FastAPI application lifespan. On shutdown cancel every wait task and disconnect every temporary client. Periodic cleanup removes expired terminal challenges. Never let the existing resource-sampler thread manipulate async Telethon clients.

**Verify**: lifecycle test shuts down with waiting/password-required challenges and reports zero pending tasks and zero connected fake clients.

## Test plan

- QR PNG generation is local, decodable, authenticated, and no-store.
- `wait()` is running before the QR response can be scanned.
- Business/account/dashboard-principal binding and one active challenge per account.
- QR success without 2FA; `SessionPasswordNeededError` transition; correct/incorrect 2FA.
- Three-attempt limit, expiration, recreation, cancellation, replay, logout, and shutdown.
- Remote HTTP rejection and loopback development exception.
- Personal-user assertion, bot rejection, atomic enablement, and session reuse.
- No QR URL/token, API hash, 2FA password, bearer token, or session path in logs/responses/persistence.
- Browser cleanup on close, switch, expiration, and success.

## Done criteria

- [ ] A Telegram account can be authorized by scanning an in-dashboard QR code.
- [ ] Telethon `wait()` starts before the QR is returned and expired QR codes can be safely refreshed.
- [ ] When Telegram requires 2FA, the dashboard accepts the password without storing or logging it.
- [ ] Only an authenticated personal user (`me.bot == False`) can enable the account.
- [ ] Challenges are business/account/principal-bound, one-time, bounded, and fully cleaned up.
- [ ] Remote QR/2FA authentication requires HTTPS.
- [ ] `python -m pytest -q tests\test_telegram_qr_auth.py tests\test_telegram_qr_ui.py` passes.
- [ ] `node --check dashboard\static\app.js` and `python scripts\verify.py` exit 0.
- [ ] No files outside the in-scope list and `plans/README.md` are modified.
- [ ] This plan's status row is DONE.

## STOP conditions

- The deployment uses more than one Uvicorn process/worker or multiple dashboard instances; in-memory Telethon client challenges will not be routable without a shared coordinator and sticky ownership.
- Production access is remote HTTP and HTTPS/trusted-proxy scheme cannot be established.
- The installed Telethon API no longer matches the documented `qr_login`/`wait`/`recreate`/2FA contract.
- A browser flow would require returning API hash, session file, raw bearer token, or 2FA password.
- The executor cannot prove that `wait()` has started before the QR response is usable.
- The same `.session` file is already connected by a running engagement client; stop and design an explicit handoff instead of opening it concurrently.

## Maintenance notes

QR login is an authentication protocol, not a generic modal. Review every future change for token caching, challenge binding, task cleanup, TLS enforcement, and secret logging. If the dashboard becomes multi-process, move challenge ownership to a dedicated single-instance auth service or a shared coordinator; do not serialize Telethon clients into a database.

