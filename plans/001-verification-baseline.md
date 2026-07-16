# Plan 001: Establish a repeatable verification baseline

> **Executor instructions**: Follow this plan step by step. Run every verification command and confirm the expected result before moving on. If a STOP condition occurs, stop and report; do not improvise. When done, update this plan's row in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- core/database.py requirements-dev.txt pytest.ini scripts tests`
> If an in-scope file changed, compare the current-state excerpts with live code before proceeding.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug, tests
- **Planned at**: commit `d908d06`, 2026-07-15
- **Revision**: r2 (2026-07-15) — adds Step 0, the timestamp-normalization bug fix. The first executor run discovered that the database smoke test could not pass because of a latent production bug in `core/database.py`; the original plan forbade touching production code, so that rule is relaxed *only* for the narrowly-scoped Step 0. Steps 1–4 are otherwise unchanged.

## Why this matters

The repository has 37 Python source files and a large single-file dashboard but no checked-in tests or test runner configuration. Every later safety, tenancy, OAuth, and UI change needs a deterministic offline verification command. This plan creates that baseline without calling live social networks or requiring credentials.

While writing the baseline, a **latent production bug** was discovered in `core/database.py`: timestamp columns are stored via SQLite's `DEFAULT (datetime('now'))`, which produces the text format `YYYY-MM-DD HH:MM:SS` (ASCII space between date and time, no microseconds), but the read-side cutoffs are computed with Python `datetime.utcnow().isoformat()`, producing `YYYY-MM-DDTHH:MM:SS.ffffff` (ASCII `T` separator, microseconds). Because these values are compared as plain strings in `WHERE timestamp > ?`, and ASCII space (0x20) sorts before `T` (0x54), a row inserted moments ago compares as *older* than the cutoff and is silently filtered out. Empirically, every time-windowed read returns **0** instead of the just-written rows. This bug is reproduced and must be fixed before the baseline test can pass.

## Current state

- `requirements.txt` contains runtime packages only; there is no `pytest`, `pytest.ini`, `requirements-dev.txt`, or `tests/` directory.
- `dashboard/static/app.js` is plain browser JavaScript; `node --check dashboard/static/app.js` currently exits 0.
- Importing some platform modules can initialize third-party dependencies, so baseline tests must not authenticate or access the network.
- Commit messages are imperative, for example `Fix: add ban_account_from_sub alias ...`. Match that style.
- **Bug (Step 0 target)**: `core/database.py` has 23 sites computing a cutoff via `(datetime.utcnow() - timedelta(...)).isoformat()` (lines 560, 593, 609, 670, 695, 753, 779, 869, 923, 937, 1025, 1079, 1147, 1173, 1316, 1393, 1434, 1480, 1623, 1624, 1836, 1902, 1903) and feeding it to a string comparison against columns populated by `datetime('now')` / `strftime` (space-separated). Lines 1623/1624 are a `since`/`until` pair; 1902/1903 are a 30-day/7-day pair; the rest are single cutoffs. All are broken in the same way.
- All timestamp columns across the schema use `DEFAULT (datetime('now'))` (confirmed at `core/database.py:67-493`), so they share the space-separated format and a single helper fixes all reads.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Install | `python -m pip install -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Tests | `python -m pytest -q` | exit 0; all tests pass |
| Python syntax | `python -m compileall -q core dashboard platforms safety miloagent.py` | exit 0; no output |
| JavaScript syntax | `node --check dashboard/static/app.js` | exit 0; no output |
| Full local gate | `python scripts/verify.py` | exit 0; prints a PASS line for each gate |
| No isoformat cutoff remains | `python - <<'PY'` … see Step 0 verify | 0 matches of `.isoformat()` used as a timestamp cutoff |

## Scope

**In scope**:
- `core/database.py` — **only** the timestamp-cutoff normalization described in Step 0 (the helper plus the 23 call-site replacements). Do not refactor anything else in this file.
- `requirements-dev.txt` (create)
- `pytest.ini` (create)
- `scripts/verify.py` (create)
- `tests/conftest.py` (create)
- `tests/test_database_smoke.py` (create)
- `tests/test_config_smoke.py` (create)
- `tests/test_database_timestamps.py` (create — regression coverage for the Step 0 fix)

**Out of scope**:
- Production behavior changes *other than* the Step 0 timestamp normalization.
- Any data migration: existing rows are already in the space-separated format, so normalizing the read-side cutoff is sufficient and backward compatible. No rewrite of stored values.
- Live Reddit, Telegram, Twitter, LLM, browser, or OAuth tests.
- CI provider configuration; the local command is the first deliverable.
- Switching from `datetime.utcnow()` to timezone-aware datetimes app-wide (only the *format string* changes; `utcnow` deprecation warnings are pre-existing noise and out of scope).

## Git workflow

- Branch: `codex/improve-001-verification-baseline`
- Commits (two, in order):
  1. `Fix timestamp cutoffs to match SQLite datetime format`
  2. `Add repeatable offline verification baseline`
- Do not push or open a PR unless instructed.

## Steps

### Step 0: Normalize timestamp cutoffs to SQLite-native format

Add a **single** private helper near the top of the `Database` class (after `__init__`/`_execute_write`, before the first method that needs it) or as a `@staticmethod`:

```python
@staticmethod
def _cutoff(hours: float = 0.0, days: float = 0.0, minutes: float = 0.0) -> str:
    """Return a UTC cutoff in SQLite's textual datetime format.

    Columns are populated by ``datetime('now')`` (or ``strftime``), which
    yields ``YYYY-MM-DD HH:MM:SS`` — ASCII space separator, no microseconds.
    Comparing that against Python's ``isoformat()`` ('T' separator, with
    microseconds) as a string breaks the filter, because space (0x20) sorts
    before 'T' (0x54). This helper reproduces SQLite's format so string
    comparisons in ``WHERE timestamp > ?`` are correct.
    """
    return (datetime.utcnow() - timedelta(hours=hours, days=days, minutes=minutes)) \
        .strftime("%Y-%m-%d %H:%M:%S")
```

Then replace **every one** of the 23 occurrences of `(datetime.utcnow() - timedelta(<units>=<amount>)).isoformat()` with the equivalent `self._cutoff(<units>=<amount>)` call, preserving each site's exact units and amount. The full replacement map (line numbers are approximate; verify by reading each site):

| Old (approx. line) | New |
|---|---|
| 560 `(datetime.utcnow() - timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 593 `(… timedelta(days=days)).isoformat()` | `self._cutoff(days=days)` |
| 609 `(… timedelta(days=days)).isoformat()` | `self._cutoff(days=days)` |
| 670 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 695 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 753 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 779 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 869 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 923 `(… timedelta(minutes=minutes)).isoformat()` | `self._cutoff(minutes=minutes)` |
| 937 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 1025 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 1079 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 1147 `(… timedelta(days=days)).isoformat()` | `self._cutoff(days=days)` |
| 1173 `(… timedelta(days=days)).isoformat()` | `self._cutoff(days=days)` |
| 1316 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 1393 `(… timedelta(hours=hours)).isoformat()` | `self._cutoff(hours=hours)` |
| 1434 `(… timedelta(hours=max_age_hours)).isoformat()` | `self._cutoff(hours=max_age_hours)` |
| 1480 `(… timedelta(days=days)).isoformat()` | `self._cutoff(days=days)` |
| 1623 `(… timedelta(days=days_ago_start)).isoformat()` | `self._cutoff(days=days_ago_start)` |
| 1624 `(… timedelta(days=days_ago_end)).isoformat()` | `self._cutoff(days=days_ago_end)` |
| 1836 `(… timedelta(days=days)).isoformat()` | `self._cutoff(days=days)` |
| 1902 `(… timedelta(days=30)).isoformat()` | `self._cutoff(days=30)` |
| 1903 `(… timedelta(days=7)).isoformat()` | `self._cutoff(days=7)` |

Do **not** touch lines 37, 1890, 1893 (the `self._last_cleanup = datetime.utcnow()` bookkeeping — those are real `datetime` objects compared with `.total_seconds()`, not string cutoffs; they are correct as-is and out of scope).

Confirm no replacement changed the semantics: each new call must produce a value strictly less than (older than) the rows it should match.

**Verify**:
1. `python -m compileall -q core/database.py` -> exit 0.
2. Grep returns no remaining cutoff-shaped `isoformat()`:
   `python -c "import re,sys; s=open('core/database.py').read(); m=re.findall(r'datetime\.utcnow\(\)\s*-\s*timedelta\([^)]*\)\)\.isoformat\(\)', s); print(len(m)); sys.exit(1 if m else 0)"` -> prints `0`, exit 0.
3. `python -m pytest -q tests/test_database_timestamps.py` -> all pass (this test file is created in Step 3; if you run this verify before Step 3, run the inline reproduction in Step 3's verify instead).

### Step 1: Add development-only test dependencies

Create `requirements-dev.txt` with bounded compatible ranges for `pytest` and `httpx` (FastAPI `TestClient` support). Do not move them into `requirements.txt`. Example: `pytest>=8.0,<9.0` and `httpx>=0.27,<1.0`.

**Verify**: `python -m pip install -r requirements.txt -r requirements-dev.txt` -> exit 0.

### Step 2: Configure isolated pytest discovery

Create `pytest.ini` with `testpaths = tests`, concise output, and markers for any future live/integration tests (e.g. `live`, `integration`). In `tests/conftest.py`, provide fixtures for a temporary SQLite path and temporary config/project/business directories. Do not change the process working directory globally without restoring it. The SQLite-path fixture yields a unique path under `tmp_path` and cleans up the file in teardown.

**Verify**: `python -m pytest --collect-only -q` -> exit 0 and only files under `tests/` are collected.

### Step 3: Add offline smoke + timestamp-regression suites

In `tests/test_database_smoke.py`: create `Database` against `tmp_path`, insert one action and one opportunity, read them back, and close the database. Use the real method signatures from `core/database.py` (`log_action`, `get_recent_actions`, `log_opportunity`, `get_pending_opportunities`). This test failed before Step 0 and must pass after.

In `tests/test_config_smoke.py`: parse every committed YAML template (everything under `config/` and `projects/` and `businesses/` that is tracked and does **not** end in `.local.yaml`) with `yaml.safe_load` and assert it is a mapping. Never load `.local.yaml` files; skip them with a collected-item check rather than a hard error.

In `tests/test_database_timestamps.py`: regression coverage for the Step 0 fix. Against a fresh temp `Database`:
- Insert an action with `log_action(success=True)`; assert `get_recent_actions(hours=1, platform=..., limit=10)` returns exactly that 1 row.
- Assert `get_action_count(hours=1, platform=...)` returns 1 (not 0).
- Insert a second action, then assert `get_action_count(hours=1)` returns 2.
- Assert `get_recent_actions_by_type(action_type=<that type>, hours=1)` returns 2.
- Assert an old cutoff excludes rows: insert one row, then query `get_recent_actions(hours=...)` with a window so small the row is excluded — confirm it returns 0 (proves the comparison is genuinely time-based, not "always returns everything").

These tests must never call a real client, the network, or a real account.

**Verify**: `python -m pytest -q` -> all new tests pass with no network access.

### Step 4: Add the one-command verifier

Create `scripts/verify.py`. It must run, in order, Python compileall (on `core dashboard platforms safety miloagent.py`), pytest, and `node --check dashboard/static/app.js`. Use `subprocess.run` with argument lists and the current Python executable (`sys.executable`). Print a `PASS <name>` line per gate on success and exit nonzero with a clear message on the first failure. If Node is absent (the `node` executable is not found), fail with an actionable message; do not silently skip the dashboard gate.

**Verify**: `python scripts/verify.py` -> exit 0 and PASS lines for each gate.

## Test plan

- Fresh temp database initializes and round-trips action/opportunity rows (this is also the regression for Step 0).
- Timestamp windowed reads return the rows they wrote (1 and then 2), and a too-small window excludes the row — proving time-based filtering now works.
- All committed YAML templates parse without secrets or local overrides.
- The verification script propagates a child command failure as a nonzero exit (unit-test the command runner by injecting a failing command if kept separable).

## Done criteria

- [ ] `core/database.py` has no remaining `(datetime.utcnow() - timedelta(...)).isoformat()` cutoff pattern.
- [ ] `python -m pytest -q tests/test_database_timestamps.py` passes (windowed reads return written rows).
- [ ] `python scripts/verify.py` exits 0.
- [ ] `python -m pytest -q` passes without credentials or network.
- [ ] `requirements.txt` is unchanged.
- [ ] No files outside the in-scope list and `plans/README.md` are modified.
- [ ] The status row is DONE.

## STOP conditions

- A smoke test requires a real account, token, browser, or network call.
- Normalizing the cutoff requires rewriting stored data or changing schema (it must not — the read-side format change is sufficient and backward compatible).
- Node is intentionally unsupported in the deployment environment; report this before changing the gate.
- The `_cutoff` helper does not reproduce SQLite's `datetime('now')` format exactly on this platform; report the observed formats before proceeding.

## Maintenance notes

All later plans must add focused tests under `tests/` and keep `python scripts/verify.py` green. Live service checks belong behind an explicit marker and must never run in the default gate. Any future timestamp cutoff in the database layer must use `self._cutoff(...)` (or compare in SQLite directly via `strftime`/`datetime('now','-N hours')`); never reintroduce `.isoformat()` for this purpose.
