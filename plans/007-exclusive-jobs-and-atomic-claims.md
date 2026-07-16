# Plan 007: Make scheduled and manual jobs exclusive and opportunities atomic

> **Executor instructions**: Follow the concurrency design exactly. Run deterministic concurrency tests, not live platform operations. Update the plan index when complete.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- core/job_coordinator.py core/orchestrator.py core/database.py dashboard/web.py tests/test_job_coordinator.py tests/test_opportunity_claims.py`

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: `plans/003-business-domain-and-data-migration.md`
- **Category**: bug
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

The orchestrator labels thread-pool waits as hard timeouts, but cancelling a running `Future` does not stop its thread. Manual ACT starts another thread without an ACT guard, and pending opportunities are selected without an atomic claim. Overlap can duplicate comments or let a timed-out scan keep mutating state in the background.

## Current state

- `core/orchestrator.py:663-697` waits on futures, calls `future.cancel()`, and exits the executor context even though running work cannot be killed.
- Only `_scan_running` exists at `core/orchestrator.py:162`; there is no equivalent ACT/job coordinator.
- `dashboard/web.py:1462-1468` spawns an ACT thread for every request.
- `core/database.py:829-848` performs a plain `SELECT ... status = 'pending'`; status changes happen later.
- SQLite is WAL-enabled and serializes writes through `Database._execute_write`.

## Target invariants

- One active instance per job key; ACT is exclusive globally or by business according to an explicit constant.
- Manual endpoints return 409 with current job status when already running.
- Opportunity selection and claim are one SQLite transaction with a unique claim token.
- Timed-out work is cooperative: it receives cancellation and stops at defined safe checkpoints. No “hard timeout” claim remains for unkillable calls.
- Stale claims recover after a bounded lease and are auditable.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Coordinator tests | `python -m pytest -q tests\test_job_coordinator.py tests\test_opportunity_claims.py` | all pass |
| Old-pattern scan | `rg -n "Hard timeout|future\.cancel\(\)|get_pending_opportunities\(" core\orchestrator.py dashboard\web.py` | no unsafe ACT selection/timeout pattern |
| Full gate | `python scripts\verify.py` | exit 0 |

## Scope

**In scope**:
- `core/job_coordinator.py` (create)
- `core/orchestrator.py`
- `core/database.py`
- `dashboard/web.py`
- `tests/test_job_coordinator.py` (create)
- `tests/test_opportunity_claims.py` (create)

**Out of scope**:
- Killing Python threads or platform SDK calls forcibly.
- Replacing APScheduler or SQLite.
- Changing scoring, rate limits, or action counts per cycle.

## Git workflow

- Branch: `codex/improve-007-job-exclusivity`
- Commits: `Add exclusive job coordination`, then `Claim opportunities atomically`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Add a central job coordinator

Create `JobCoordinator` with thread-safe `try_start(key, business_id)`, status snapshot, cancellation event, finish outcome, and `finally`-safe lease release. Use stable keys such as `scan`, `act`, `learn`, and `engage`. Record start/end/error/cancelled timestamps without secrets.

**Verify**: concurrency test launches simultaneous starts; exactly one acquires each exclusive key and leases always release after exceptions.

### Step 2: Route scheduler and dashboard through the same guard

Wrap scheduled and manual entry points in the coordinator. Do not create an untracked thread before acquiring. `/api/control/act` and similar endpoints return 202 with job ID on start, or 409 with existing status. Web requests never block for the whole job.

**Verify**: endpoint/unit test issues two ACT requests and asserts one start, one 409, one orchestrator invocation.

### Step 3: Replace false hard timeouts with cooperative cancellation

Pass a cancellation token through scan/project loops and check it before/after each network unit, before DB writes, and between action attempts. On timeout, set the event and stop scheduling new work. Document that an in-flight blocking SDK call may finish, but it must not continue into another unit after cancellation.

**Verify**: fake blocking worker test times out, unblocks, observes cancellation, and performs no post-cancel DB write.

### Step 4: Add atomic opportunity claim/complete APIs

Add claim columns or a claim table with token, claimed time, job/business/account IDs, and lease expiry. `claim_best_opportunity` uses `BEGIN IMMEDIATE`, selects one eligible row for business/product/platform, changes it to `acting`, commits, and returns the row plus token. Complete/fail/release requires the matching token. Recover only expired leases.

**Verify**: 20 concurrent claim attempts for one opportunity produce exactly one winner; wrong tokens cannot complete/release; stale lease recovers once.

### Step 5: Use claims in ACT

Replace select-then-act with claim, then account/rate/content checks. Every exit path resolves the claim in `finally`: acted, skipped with reason, failed, or released only for retriable pre-action conditions. Dedup remains a second guard, not the concurrency primitive.

**Verify**: orchestrator tests cover success, validation rejection, rate limit, exception, and cancellation with no lingering non-stale `acting` row.

## Test plan

- Same/different job key acquisition and release under threads.
- Duplicate manual request behavior.
- Cooperative timeout prevents later side effects.
- Atomic claim race, token ownership, lease expiry, and crash recovery.
- Business ID is included in every claim query and audit record.

## Done criteria

- [ ] Manual and scheduled jobs share one exclusivity mechanism.
- [ ] No running thread is represented as cancelled when it can still mutate later units.
- [ ] An opportunity has at most one active claim.
- [ ] Every ACT exit resolves its claim.
- [ ] Focused tests and full gate pass.

## STOP conditions

- A platform call cannot be bounded or followed by a cancellation checkpoint.
- SQLite version/transaction semantics differ from the tested environment.
- Business-wide versus global ACT exclusivity cannot be decided from resource/rate-limit policy; report evidence before choosing.

## Maintenance notes

Reviewers should focus on `finally` blocks and transaction boundaries. Any new manual job endpoint must acquire a coordinator lease before starting background work.

