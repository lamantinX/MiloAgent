# Plan 008: Replace unsafe dynamic inline dashboard handlers

> **Executor instructions**: Limit this plan to event wiring. Run static and browser-free tests, then update the plan index.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- dashboard/static/app.js dashboard/static/index.html tests/test_dashboard_static.py`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: `plans/001-verification-baseline.md`
- **Category**: bug
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

Dynamic project/account buttons embed `JSON.stringify` values inside double-quoted `onclick` attributes. Quoted names can break the HTML attribute and create an injection surface. The business switcher work needs a reusable safe event pattern before it renders more server data.

## Current state

- `dashboard/static/app.js:1039` creates Edit/Delete buttons with `onclick="editProject(${JSON.stringify(p.name)})"`.
- `dashboard/static/app.js:1072` creates Remove with serialized platform/username in an inline handler.
- `dashboard/static/index.html` also has static inline handlers, but those contain developer-authored constants, not server values.
- `esc()` protects visible text but does not make a JavaScript value safe inside an HTML attribute.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Static tests | `python -m pytest -q tests\test_dashboard_static.py` | all pass |
| JS syntax | `node --check dashboard\static\app.js` | exit 0 |
| Dynamic-handler scan | `rg -n "onclick=.*JSON\.stringify" dashboard\static` | no matches |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `dashboard/static/app.js`
- `dashboard/static/index.html` only if stable container hooks are needed
- `tests/test_dashboard_static.py` (create)

**Out of scope**:
- Removing every static inline handler in the application.
- Visual redesign or business switcher implementation.
- Changing API response shapes.

## Git workflow

- Branch: `codex/improve-008-dashboard-events`
- Commit: `Replace dynamic inline dashboard handlers`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Render action controls without executable attributes

In `renderManageProjects` and `renderManageAccounts`, create server-derived text and buttons with DOM APIs (`createElement`, `textContent`, `addEventListener`) or delegated events where identifiers are assigned through DOM properties. Do not concatenate identifiers into HTML/JavaScript strings.

**Verify**: `rg -n "onclick=.*JSON\.stringify" dashboard\static` -> no matches.

### Step 2: Preserve behavior and accessibility

Keep button labels/styles and call existing `editProject`, `deleteProject`, and `removeAccount` with exact original values. Use real `button` elements with `type="button"`. Empty and error states remain unchanged.

**Verify**: static tests assert action buttons are wired through listeners and a fixture name containing quotes/angle brackets appears only via text/data property, never executable markup.

### Step 3: Add regression source checks

Add a focused test that reads `app.js` and rejects dynamic `onclick` plus `JSON.stringify`. Keep it narrow enough not to ban safe JSON serialization elsewhere.

**Verify**: `python scripts\verify.py` -> exit 0.

## Test plan

- Project name with double quote, apostrophe, angle brackets, and ampersand.
- Account display handle with the same characters.
- Correct function receives exact identifier.
- JavaScript parses after refactor.

## Done criteria

- [ ] No server value is embedded into an executable HTML attribute.
- [ ] Edit/delete/remove behavior is preserved.
- [ ] Focused and full gates pass.
- [ ] Only in-scope files and plan index changed.

## STOP conditions

- Preserving behavior requires changing endpoint identity semantics; defer to plan 009.
- The dashboard has a build/test framework not visible at planned SHA; use it only after reporting drift.

## Maintenance notes

Plan 010 must reuse this event pattern for business/product/account controls. Prefer DOM properties and stable IDs over escaping data into executable strings.

