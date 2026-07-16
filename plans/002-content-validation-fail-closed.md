# Plan 002: Make content validation deterministic and fail closed

> **Executor instructions**: Execute every step and verification. Stop on a listed STOP condition. Update the plan row in `plans/README.md` when complete.
>
> **Drift check (run first)**: `git diff --stat d908d06..HEAD -- core/content_validator.py platforms/reddit_web.py platforms/telegram_group_bot.py platforms/twitter_bot.py tests/test_content_validator.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: `plans/001-verification-baseline.md`
- **Category**: bug
- **Planned at**: commit `d908d06`, 2026-07-15

## Why this matters

Validation currently computes at least one penalty before resetting `score` to 1.0, so the result depends on statement order rather than findings. Reddit also returns the original content when validation itself throws, while Telegram and Twitter accept borderline or errored content. A validator failure must skip a write action, not publish unverified content.

## Current state

- `core/content_validator.py:102-117` appends issues and performs `score -= 0.5` before `score = 1.0`.
- `platforms/reddit_web.py:974-976` logs a validation exception and returns the original content.
- `platforms/telegram_group_bot.py:697-705` accepts invalid content when `score >= 0.4` and returns content on validator exceptions.
- `platforms/twitter_bot.py:717-727` has the same `score >= 0.4` bypass and “continuing anyway” exception path.
- Callers already treat a falsey validated result as “skip”, so returning `None` is the established rejection signal.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused tests | `python -m pytest -q tests\test_content_validator.py` | all pass |
| Full gate | `python scripts\verify.py` | exit 0 |
| Fail-open scan | `rg -n "continuing anyway|return content" platforms\reddit_web.py platforms\telegram_group_bot.py platforms\twitter_bot.py` | no exception-path or score-bypass match |

## Scope

**In scope**:
- `core/content_validator.py`
- `platforms/reddit_web.py`
- `platforms/telegram_group_bot.py`
- `platforms/twitter_bot.py`
- `tests/test_content_validator.py` (create)

**Out of scope**:
- Changing copy generation prompts or promotional ratios.
- Relaxing/strengthening individual policy rules beyond making their existing penalties deterministic.
- Posting any test content to a live service.

## Git workflow

- Branch: `codex/improve-002-content-validation`
- Commit: `Fix content validation scoring and fail closed`
- Do not push or open a PR unless instructed.

## Steps

### Step 1: Initialize score before every check

Move `score = 1.0` to the beginning of `ContentValidator.validate`, before any branch can subtract from it. Keep issue collection and thresholds intact. Clamp the final score to `[0.0, 1.0]` once, immediately before calculating `is_valid` and returning.

**Verify**: `python -m pytest -q tests\test_content_validator.py -k score` -> deterministic score tests pass.

### Step 2: Define one acceptance rule

Add a named class constant for the acceptance threshold and make `validate` the single source of truth for `is_valid`. Platform adapters may regenerate once, but must not override `is_valid` with a separate `score >= 0.4` rule.

**Verify**: `rg -n "score >= 0.4" platforms` -> no matches in the three scoped adapters.

### Step 3: Fail closed in every write adapter

In Reddit web, Telegram, and Twitter validation helpers, catch unexpected validation exceptions, log with `logger.exception`, and return `None`. Preserve the callers' existing failed-action logging. Do not include generated content or credentials in exception logs.

**Verify**: `python -m pytest -q tests\test_content_validator.py -k exception` -> each adapter returns `None` when the validator raises.

### Step 4: Add regression coverage

Test: a valid organic reply; forbidden phrase deduction; organic product/URL leakage; pricing mismatch; combined deductions; empty content; and injected validator exception for every adapter. Assert no adapter returns the original content after an exception.

**Verify**: `python scripts\verify.py` -> exit 0.

## Test plan

- Exact score for one and multiple deductions proves initialization order.
- Threshold boundary is accepted/rejected consistently.
- Reddit, Telegram, and Twitter fail closed on unexpected validator errors.
- Tests use fakes and never call platform clients.

## Done criteria

- [ ] Every deduction starts from 1.0 and final score is clamped.
- [ ] No platform-specific `score >= 0.4` bypass remains.
- [ ] Validator exceptions return `None` in all scoped adapters.
- [ ] `python scripts\verify.py` exits 0.
- [ ] Only in-scope files and the plan index changed.

## STOP conditions

- A caller publishes content without checking the helper's return value; report the exact path before extending scope.
- Existing product policy intentionally requires a platform-specific threshold not represented in configuration.
- Tests require a real platform client.

## Maintenance notes

Future platforms must consume `is_valid`; they must not reinterpret scores. Reviewers should scrutinize every exception path for accidental fail-open behavior.

