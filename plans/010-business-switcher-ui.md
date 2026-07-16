# Plan 010: Add the business switcher and business-aware account UI

> **Executor instructions**: Build against plan 009's tested API only. Preserve safe DOM event wiring from plan 008. Run all gates and update the plan index.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- dashboard/static/index.html dashboard/static/app.js dashboard/static/cyber.css tests/test_dashboard_business_ui.py`

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: MED
- **Depends on**: `plans/009-business-scoped-dashboard-api.md`
- **Category**: direction
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

The dashboard currently presents one global list of projects and accounts. Operators need a visible business context, filtered products/data, and the ability to attach several accounts from the same service to one business. The switcher must be browser-local rather than changing global orchestrator state.

## Current state

- `dashboard/static/index.html:45-82` has a top bar/status/navigation but no business selector.
- The Config tab at `index.html:362-370` shows global Projects and Accounts cards.
- The Add Account modal at `index.html:435-462` always shows username/password/email and a comma-separated project field even for Telegram.
- `dashboard/static/app.js:1924-1935` fetches unscoped projects/accounts and renders them globally.
- Background orchestration processes all enabled products; changing UI selection must not pause or retarget that scheduler.

## UX contract

- The selected business is always visible in the top bar.
- Selection is stored in `localStorage` per browser under a versioned key; it is not server-global.
- Switching aborts/ignores stale requests, clears tenant panels, and refetches all business data.
- CPU/RAM/server controls remain visibly labeled Global; tenant actions/metrics use selected business.
- Products and service accounts are grouped within the selected business; duplicate platform cards are normal.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| UI source tests | `python -m pytest -q tests\test_dashboard_business_ui.py` | all pass |
| JS syntax | `node --check dashboard\static\app.js` | exit 0 |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `dashboard/static/index.html`
- `dashboard/static/app.js`
- `dashboard/static/cyber.css`
- `tests/test_dashboard_business_ui.py` (create)

**Out of scope**:
- API/backend changes beyond a documented mismatch that triggers STOP.
- Redesigning charts or navigation unrelated to business context.
- Setting an orchestrator-wide active business.

## Git workflow

- Branch: `codex/improve-010-business-switcher-ui`
- Commits: `Add dashboard business context switcher`, then `Add business account onboarding UI`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Add a persistent top-bar business switcher

Add a labeled select/control near the logo/status. On startup fetch `/api/businesses`, restore a still-valid local selection, otherwise select the only business or show an explicit “Choose business” state. For multiple businesses never auto-select based on list order after a stored ID becomes invalid.

**Verify**: source/DOM test covers zero, one, multiple, and removed stored business cases.

### Step 2: Centralize request scope

Create an immutable UI state object with selected business ID and generation counter/AbortController. All tenant API helpers append `business_id`; global helpers do not. On switch, cancel prior tenant requests, clear panels, increment generation, and ignore late responses from the old business.

**Verify**: fake-fetch test switches A->B while A is delayed and proves no A data renders after B selection.

### Step 3: Make every tenant view reflect selection

Refresh mission metrics, action feed, opportunities, projects/products, accounts, performance, communities, takeover, network, intelligence, exports, and manual controls with selected business. Add a compact business label to destructive/manual actions. Disable tenant controls when no business is selected.

**Verify**: static route inventory test asserts every tenant fetch/control uses the scoped helper; global CPU/RAM endpoints use the global helper.

### Step 4: Present products and multiple service accounts clearly

Rename UI “Projects” to “Products”. In Accounts, group by service but render every record with display identity, account status, assigned products, persona, last action, and enabled/session/OAuth state. All buttons use stable account ID and safe event listeners from plan 008.

**Verify**: fixture with three Reddit and two Telegram accounts shows all records without collapsing by platform/username.

### Step 5: Add platform-aware account onboarding

Replace the generic modal with a service selector and dynamic fields. Reddit shows its fields/OAuth/cookie next step. Telegram explicitly says “Personal account (Telethon), not a bot”, requests API ID/API hash, and creates a disabled `not_authorized` account through plan 009. Phone is optional because plan 011 authorizes by QR and reads the resulting identity from Telegram. Product assignment is a multi-select populated only from the current business; no comma-separated free text.

Never render existing secret values back into inputs. Render Telegram `auth_status` and a stable account-ID action hook/card location that plan 011 can extend; do not implement SMS, QR, or password submission in this plan.

**Verify**: UI tests prove Telegram never asks for a BotFather token or SMS code, newly created Telegram accounts remain disabled/not authorized, and changing platform clears hidden secret fields.

### Step 6: Add business CRUD and empty states

Provide create/edit/archive business controls with confirmation rules from plan 009. A new business starts with guided empty states: add product, then add personal/service accounts, then authenticate them. Archival explains blockers without offering force delete.

**Verify**: tests cover create, switch, archive blocked, and business with no products/accounts.

### Step 7: Make scope visually testable

Add CSS for switcher, business badge, grouped accounts, responsive layout, disabled states, and Telegram user/admin distinction. Maintain current design tokens and mobile usability.

**Verify**: `node --check dashboard\static\app.js` and `python scripts\verify.py` -> exit 0.

## Test plan

- Persist/restore selection and invalid stored ID.
- Request race on rapid switching.
- Every tenant panel/control/export uses current business.
- Multiple same-service accounts remain distinct.
- Reddit vs Telegram dynamic form and secret clearing.
- Telegram copy and fields prove personal-account intent, never Bot API; authorization remains explicitly pending plan 011.
- Empty/loading/error/mobile source states.

## Done criteria

- [ ] Business context is visible and browser-local.
- [ ] Switching replaces all tenant data without stale flashes.
- [ ] Multiple accounts per service are manageable inside one business.
- [ ] Telegram UI only onboards personal user sessions for commenting.
- [ ] Global vs business-scoped controls are clearly distinguished.
- [ ] UI tests and full gate pass.

## STOP conditions

- Any plan 009 endpoint is missing scope or returns foreign-business data.
- Switching would require mutating orchestrator global state.
- A secret must be re-rendered to edit an account.
- Stale response cancellation cannot be tested with the current plain-JS setup; report the smallest test harness addition before adding it.

## Maintenance notes

The selected business is view/control context only; background jobs continue across configured businesses. New panels must declare whether they are tenant or global and use the corresponding request helper.
