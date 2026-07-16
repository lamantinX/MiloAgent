# Plan 005: Guarantee Telegram commenting uses personal user sessions

> **Executor instructions**: Preserve the separation between engagement users and the admin bot. Run every verification and update the plan index. Stop if a live credential would be required.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- platforms/telegram_group_bot.py dashboard/telegram_bot.py core/orchestrator.py miloagent.py config/telegram.yaml config/telegram_user_accounts.yaml tests/test_telegram_identity.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: `plans/004-strict-business-account-routing.md`
- **Category**: security
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

The current group engagement implementation already uses Telethon and calls `TelegramClient.send_message`, while Bot API is used for the admin dashboard. That separation is correct but enforced only by configuration and naming. Add runtime invariants and tests so a bot identity or admin token can never be used for group commenting as account management grows.

## Current state

- `platforms/telegram_group_bot.py:1-5` explicitly says it uses a real user account and Telethon.
- `platforms/telegram_group_bot.py:100-137` creates `TelegramClient(session_file, api_id, api_hash)` and requires an authorized session.
- `platforms/telegram_group_bot.py:633-647` replies with `self.client.send_message(..., reply_to=message_id)`.
- `dashboard/telegram_bot.py:114-141` builds `python-telegram-bot` `Application` from `config/telegram.yaml:bot_token` for monitoring/control.
- `config/telegram_user_accounts.yaml` contains phone/API/session fields; `config/telegram.yaml` contains BotFather token/admin IDs.
- Current authentication does not assert `get_me().bot is False`, and the two roles are both casually called “bot”.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Identity tests | `python -m pytest -q tests\test_telegram_identity.py` | all pass with fake clients |
| Boundary scan | `rg -n "bot_token|telegram\.ext|Application\.builder" platforms\telegram_group_bot.py` | no matches |
| Send-path scan | `rg -n "send_message" platforms\telegram_group_bot.py dashboard\telegram_bot.py` | engagement sends only through Telethon client; admin sends only to admin IDs |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `platforms/telegram_group_bot.py`
- `dashboard/telegram_bot.py`
- `core/orchestrator.py`
- `miloagent.py`
- `config/telegram.yaml`
- `config/telegram_user_accounts.yaml`
- `tests/test_telegram_identity.py` (create)

**Out of scope**:
- Live login, sending a real message, or changing Telegram rate limits.
- Dashboard account forms and interactive authentication (plans 009-011).
- Using Bot API to join, scan, react, or comment in target groups.

## Git workflow

- Branch: `codex/improve-005-telegram-user-sessions`
- Commit: `Enforce Telegram user sessions for group engagement`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Make role names and config explicit

Document `account_type: user` in `telegram_user_accounts.yaml`. Rename internal comments/variables so `TelegramGroupBot` is clearly the user-engagement adapter and `TelegramDashboard` is the admin/control bot. A compatibility class name may remain to avoid a broad rename, but public logs must say “Telegram user engagement” versus “Telegram admin bot”.

**Verify**: config parsing test confirms user config cannot contain `bot_token` and admin config is never returned by `AccountManager`.

### Step 2: Reject bot identities before scanning or acting

Validate required user fields: stable account ID, business ID, numeric API ID, API hash, and session path. Phone may be absent before QR authorization and may be populated from `get_me()` after login. After `get_me`, raise a typed configuration/authentication error if `me.bot` is true. Do this before any group discovery, read, or send call.

**Verify**: `python -m pytest -q tests\test_telegram_identity.py -k reject_bot` -> fake bot identity is rejected and fake `send_message` is never called.

### Step 3: Prove the engagement send path

Add adapter tests with a fake authorized Telethon client. Assert `_act_async` replies through that client with the source message ID, and that neither Bot API config nor `TelegramDashboard` is imported/used. Patch delays and content generation so tests are deterministic.

**Verify**: `python -m pytest -q tests\test_telegram_identity.py -k personal_user_send` -> one Telethon send, zero admin-bot sends.

### Step 4: Protect the admin bot boundary

Keep admin sends restricted to configured `admin_chat_ids`. Add a module/class docstring warning that this component must never be passed to platform engagement code. Ensure orchestrator has two distinct attributes/factories and no generic Telegram send helper is used for comments.

**Verify**: static boundary test asserts `platforms/telegram_group_bot.py` has no `bot_token`, `telegram.ext`, or admin dashboard reference.

### Step 5: Make CLI wording unambiguous

Update login/test output so `telegram-groups` means Telethon personal user session and `telegram-admin` means BotFather dashboard bot. Preserve backward-compatible aliases if needed, but output must state which identity is being checked.

**Verify**: CLI unit tests assert the two checks load different config files and never exchange credential types.

## Test plan

- Missing user credentials reject before connection.
- Authorized human (`me.bot == False`) passes.
- Authorized bot (`me.bot == True`) rejects before scan/send.
- Group reply uses Telethon `reply_to`.
- Admin bot only sends alerts to admin IDs.
- No live phone, code, token, or session is needed.

## Done criteria

- [ ] Runtime asserts Telegram engagement identity is not a bot.
- [ ] Engagement code cannot load BotFather token/config.
- [ ] Admin/control code cannot be selected as a group engagement account.
- [ ] Focused and full verification pass.
- [ ] No credentials or session files are committed.

## STOP conditions

- A test or implementation requires a live Telegram code/session.
- Telethon reports an identity shape without a reliable bot flag; report the library/API evidence before weakening the check.
- Any existing feature intentionally comments through Bot API.

## Maintenance notes

Every new Telegram action (join, react, DM, comment) must pass through the same verified user adapter. Keep session files and `.local.yaml` ignored and never return API hashes or auth data from dashboard endpoints.
