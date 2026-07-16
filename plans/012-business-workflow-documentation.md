# Plan 012: Document the business, product, account, and Telegram workflows

> **Executor instructions**: Document only behavior verified after plans 003-010. Commands shown to the user must be valid in CMD. Run documentation checks and update the plan index.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- README.md docs/business-workspaces.md config/reddit_accounts.yaml config/twitter_accounts.yaml config/telegram.yaml config/telegram_user_accounts.yaml projects/example_project.yaml businesses/example_business.yaml tests/test_documentation_examples.py`

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: `plans/011-telegram-qr-and-2fa-login.md`
- **Category**: docs
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

The repository currently uses “business” and “project” interchangeably and documents only one Reddit account/project. Operators need an unambiguous guide for separate businesses, products, multiple service accounts, business switching, and Telegram's personal-user versus admin-bot roles.

## Current state

- `README.md:9` claims multi-account/multi-project but does not explain ownership or isolation.
- `README.md:210-222` uses `assigned_projects: ["my_project"]`, while the example product display name is `MyProduct`; names/slugs are ambiguous.
- `README.md:321` mentions only `telegram.yaml` Bot API config and does not list `telegram_user_accounts.yaml`.
- `README.md:375` describes `business list|add|show` as project management.
- Example usage comments use bash commands (`cp`, `python3`), but user-facing commands for this workspace must be CMD-compatible.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Example checks | `python -m pytest -q tests\test_documentation_examples.py` | all pass |
| Terminology scan | `rg -n "business/project|Leave empty to assign to all|Telegram bot token" README.md docs config projects businesses` | no ambiguous/unsafe instruction |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `README.md`
- `docs/business-workspaces.md` (create)
- `config/reddit_accounts.yaml`
- `config/twitter_accounts.yaml`
- `config/telegram.yaml`
- `config/telegram_user_accounts.yaml`
- `projects/example_project.yaml`
- `businesses/example_business.yaml`
- `tests/test_documentation_examples.py` (create)

**Out of scope**:
- Changing shipped behavior or API contracts.
- Adding real credentials, business names, phone numbers, or session paths.
- POSIX-only user instructions.

## Git workflow

- Branch: `codex/improve-012-business-docs`
- Commit: `Document isolated business and account workflows`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Define the vocabulary once

At the top-level README and detailed guide, define: business/workspace, product, service account, assignment, and active UI business. State that one business can have multiple accounts on one service, an account belongs to one business, and products/accounts from another business are never used as fallback.

**Verify**: documentation test finds one canonical definition and links to it from setup/UI sections.

### Step 2: Document the dashboard workflow

Write a short path: create/select business -> add product(s) -> add multiple service accounts -> assign products -> authenticate -> verify status -> run manual action -> read business-filtered metrics. Explain Global CPU/RAM versus business data and that switching UI does not retarget background jobs.

**Verify**: every named button/field/endpoint exists at current HEAD; doc test checks stable selectors or API routes where practical.

### Step 3: Document Telegram's two independent identities

Create a prominent table:

- Personal Telegram user account: Telethon, phone/API/session, scans and comments in groups.
- Admin Telegram bot: BotFather token/admin chat IDs, alerts and remote control only.

State that bot accounts are rejected for commenting. Document QR authorization from the dashboard, the exact mobile navigation for scanning, QR expiration/refresh, and the conditional 2FA-password step. Explain that the 2FA password is submitted only to the operator's own MiloAgent backend over HTTPS and is never stored. Include safe troubleshooting without printing credentials.

**Verify**: `rg -n "Telethon|personal|BotFather|admin|QR|2FA" README.md docs\business-workspaces.md` -> both roles and the QR/2FA flow are described and separated.

### Step 4: Align safe config examples

Update all templates to use consistent fake IDs and cross-references. Show at least two distinct same-service account records under one business. Never imply an empty assignment means all products. Keep real configs local/ignored.

**Verify**: tests parse examples and validate all referenced business/product/account IDs and no placeholder resembles a real secret.

### Step 5: Make commands CMD-compatible

Use `copy`, `set`, backslash paths, and `python` commands suitable for CMD. Avoid `cp`, `export`, `python3`, shell continuations, and single-quote-dependent examples in user instructions. Commands inside explanatory Linux/Docker sections must be explicitly labeled as non-CMD.

**Verify**: documentation test rejects unlabelled `cp`, `export`, and `python3` in Windows setup/workflow sections.

## Test plan

- YAML examples parse and all IDs resolve.
- At least two accounts of one platform belong to the same example business.
- Telegram user/admin roles are both present and never share credential fields.
- QR scan, expiration/refresh, and conditional 2FA steps match plan 011's shipped UI.
- User workflow commands are CMD-compatible.
- No sensitive-looking values or local paths are introduced.

## Done criteria

- [ ] Vocabulary and ownership rules are unambiguous.
- [ ] End-to-end UI workflow is documented concisely.
- [ ] Telegram commenting is clearly personal-account-only.
- [ ] Telegram dashboard login documents QR scanning and optional 2FA without secret persistence.
- [ ] Examples demonstrate multiple same-service accounts per business.
- [ ] Commands intended for the user are CMD-compatible.
- [ ] Documentation tests and full gate pass.

## STOP conditions

- The implemented UI/API differs from the planned terminology or workflow.
- A documented step has not been verified in code/tests.
- A useful example would require a real credential or identifiable account.

## Maintenance notes

Update the guide whenever business scope, account onboarding, or Telegram authentication changes. Treat the config examples as validated fixtures, not prose snippets that can drift independently.
