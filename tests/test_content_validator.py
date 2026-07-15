"""Offline regression tests for content validation.

These tests never touch the network and never instantiate real platform
clients. Platform-adapter behavior is exercised through lightweight fakes
and by monkeypatching the validator to raise, proving each adapter fails
closed on an unexpected validator error.
"""

import pytest

from core.content_validator import ContentValidator


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _project():
    """Return a minimal project dict the validator accepts.

    Mirrors the project shape used by the adapters: a top-level ``project``
    key wrapping the business profile, pricing, and product facts.
    """
    return {
        "project": {
            "name": "MiloAgent",
            "url": "https://miloagent.example.com",
            "alt_names": [],
            "business_profile": {
                "rules": {
                    "never_say": ["we are the best"]
                },
                "pricing": {
                    "model": "paid",
                    "paid_plans": [{"price": "$10"}],
                },
            },
        }
    }


def _project_no_profile():
    """Return a project without a business_profile (no pricing/forbidden)."""
    return {"project": {"name": "MiloAgent", "url": "https://miloagent.example.com"}}


@pytest.fixture()
def validator():
    return ContentValidator()


# ---------------------------------------------------------------------------
# Score determinism and initialization order (Step 1)
# ---------------------------------------------------------------------------


def test_valid_organic_reply_is_accepted(validator):
    """A clean organic reply scores 1.0 and is accepted."""
    content = (
        "I ran into something similar last week and the config tweak you "
        "described fixed it for me too, thanks for writing this up."
    )
    is_valid, score, issues = validator.validate(
        content, _project(), "reddit", is_promotional=False
    )
    assert is_valid is True
    assert score == pytest.approx(1.0)
    assert issues == []


def test_rss_spam_deduction_is_not_overwritten(validator):
    """Regression: score = 1.0 used to run AFTER the RSS-spam deduction.

    Before the fix the RSS-spam ``score -= 0.5`` was overwritten by a later
    ``score = 1.0``, so spammy content scored a perfect 1.0. Now every
    deduction starts from 1.0, so RSS spam scores exactly 0.5.
    """
    # Long enough to clear reddit's 15-word minimum so the only deduction is
    # the RSS-spam penalty itself.
    content = (
        "found this earlier and it really has more than enough context words "
        "to pass the minimum length check easily without trouble"
    )
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    assert any("RSS spam" in i for i in issues)
    # 1.0 - 0.5, not the pre-fix 1.0 (deduction was overwritten).
    assert score == pytest.approx(0.5)
    assert is_valid is False


def test_combined_deductions_start_from_one(validator):
    """Multiple deductions compound from 1.0 deterministically."""
    # product name wrong case -> 0.15; one bot pattern ("game-changer") -> 0.15
    content = (
        "milOagent is honestly a real game-changer for my daily automation "
        "flow around the office and team workflows overall"
    )
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    # 1.0 - 0.15 (name case) - 0.15 (bot pattern) = 0.7
    assert score == pytest.approx(0.7)
    assert is_valid is True
    assert any("Product name case" in i for i in issues)
    assert any("marketing cliche" in i.lower() or "hype" in i.lower()
               or "bot-like" in i.lower() for i in issues)


def test_score_is_clamped_to_zero(validator):
    """Enough deductions clamp the score at 0.0, never negative."""
    # Many bot patterns stack up deductions well past zero.
    content = (
        "Leverage your workflow with this game-changer hidden gem!! "
        "It's worth noting this robust seamless solution is next level. "
        "Hope this helps! #a #b #c delve deeper into the synergy."
    )
    is_valid, score, issues = validator.validate(content, _project(), "twitter")

    assert score == 0.0
    assert score >= 0.0  # never negative
    assert is_valid is False


# ---------------------------------------------------------------------------
# Acceptance threshold and is_valid source of truth (Step 2)
# ---------------------------------------------------------------------------


def test_acceptance_threshold_constant_is_single_source(validator):
    """The threshold lives in one named class constant."""
    assert ContentValidator.ACCEPTANCE_THRESHOLD == 0.6
    assert validator.ACCEPTANCE_THRESHOLD == 0.6


def test_threshold_boundary_at_or_above_is_accepted(validator):
    """A score at or above the threshold (with no CRITICAL issue) is accepted."""
    # One product-name-case deduction -> 1.0 - 0.15 = 0.85, above 0.6.
    content = (
        "milOagent handled my automation queue without any trouble today and "
        "the whole setup went smoothly across the team"
    )
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    assert score >= ContentValidator.ACCEPTANCE_THRESHOLD
    assert is_valid is True


def test_threshold_boundary_below_is_rejected(validator):
    """Content below the threshold is rejected even without a CRITICAL flag.

    RSS spam (0.5) sits below the 0.6 acceptance threshold and carries no
    CRITICAL marker, so it must be rejected purely on score. The content is
    kept over 15 words so the length check does not also fire.
    """
    content = (
        "check this out: there is plenty of substance in the linked thread "
        "for anyone reading along right now in this subreddit"
    )
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    assert score == pytest.approx(0.5)
    assert score < ContentValidator.ACCEPTANCE_THRESHOLD
    assert is_valid is False


def test_critical_issue_rejects_even_when_score_is_high(validator):
    """A CRITICAL issue (e.g. forbidden phrase) rejects regardless of score."""
    content = (
        "we are the best option for this kind of automation setup today"
    )
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    assert any("CRITICAL" in i for i in issues)
    assert is_valid is False


# ---------------------------------------------------------------------------
# Individual policy deductions (Step 4 coverage)
# ---------------------------------------------------------------------------


def test_forbidden_phrase_deducts_and_flags_critical(validator):
    content = "Honestly we are the best at shipping this kind of feature"
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    assert any("forbidden phrase" in i.lower() for i in issues)
    assert is_valid is False


def test_organic_product_leakage_is_critical(validator):
    """Organic comments mentioning the product name are rejected."""
    content = (
        "I switched to MiloAgent last month and it has been smooth overall"
    )
    is_valid, score, issues = validator.validate(
        content, _project(), "reddit", is_promotional=False
    )

    assert any("CRITICAL" in i and "MiloAgent" in i for i in issues)
    assert is_valid is False


def test_organic_url_leakage_is_critical(validator):
    """Organic comments containing the product URL are rejected."""
    content = (
        "The docs at https://miloagent.example.com cover this in detail"
    )
    is_valid, score, issues = validator.validate(
        content, _project(), "reddit", is_promotional=False
    )

    assert any("CRITICAL" in i for i in issues)
    assert is_valid is False


def test_pricing_claim_mismatch_deducts(validator):
    """Claiming a paid product is 'free' triggers a pricing deduction."""
    content = "MiloAgent is free and you can try it without any setup cost"
    is_valid, score, issues = validator.validate(content, _project(), "reddit")

    assert any("free" in i.lower() and "pricing" in i.lower() for i in issues)
    assert is_valid is False


# ---------------------------------------------------------------------------
# Empty-content early return is a consistent 3-tuple (Step 1 deviation)
# ---------------------------------------------------------------------------


def test_empty_content_returns_three_tuple(validator):
    """The empty-content early return must match the normal 3-tuple shape.

    Regression: this branch previously returned a dict, which broke the
    adapters' tuple-unpacking. It now returns ``(False, 0.0, [issue])``.
    """
    is_valid, score, issues = validator.validate("", _project(), "reddit")

    assert isinstance(is_valid, bool)
    assert isinstance(score, float)
    assert isinstance(issues, list)
    assert is_valid is False
    assert score == 0.0
    assert issues and "empty_or_trivial" in issues[0]


def test_short_content_returns_three_tuple(validator):
    """Content under 15 chars hits the empty/trivial branch consistently."""
    result = validator.validate("too short", _project(), "reddit")
    # Must be a 3-tuple, not a dict.
    assert isinstance(result, tuple)
    assert len(result) == 3
    is_valid, score, issues = result
    assert is_valid is False
    assert score == 0.0


# ---------------------------------------------------------------------------
# Adapter fail-closed behavior (Step 3)
# ---------------------------------------------------------------------------


class _FakeContentValidator:
    """Stand-in for ContentValidator that always raises.

    Mounted via monkeypatch on the adapter's import target so the adapter
    never touches the real validator or any network client.
    """

    def validate(self, *args, **kwargs):
        raise RuntimeError("boom: simulated validator failure")


def _patch_validator(monkeypatch):
    """Replace core.content_validator.ContentValidator with the fake."""
    import core.content_validator as mod

    monkeypatch.setattr(
        mod, "ContentValidator", _FakeContentValidator
    )


def test_reddit_adapter_fails_closed_on_validator_error(monkeypatch):
    """Reddit must return None (not the original content) when validation raises."""
    from platforms import reddit_web

    _patch_validator(monkeypatch)

    bot = reddit_web.RedditWebBot.__new__(reddit_web.RedditWebBot)
    # The helper only reads these attributes off the instance; no client calls.
    class _RaisingGen:
        def generate_reddit_comment(self, **kwargs):
            raise AssertionError("retry must not be reached when validate raises")

    bot.content_gen = _RaisingGen()

    result = bot._validate_content(
        content="a perfectly normal generated comment body",
        opportunity={"title": "t", "subreddit": "s"},
        project=_project(),
        is_promo=False,
    )
    assert result is None


def test_telegram_adapter_fails_closed_on_validator_error(monkeypatch):
    """Telegram must return None when validation raises."""
    from platforms import telegram_group_bot

    _patch_validator(monkeypatch)

    bot = telegram_group_bot.TelegramGroupBot.__new__(
        telegram_group_bot.TelegramGroupBot
    )
    result = bot._validate_content(
        "a perfectly normal generated message body", _project()
    )
    assert result is None


def test_twitter_adapter_fails_closed_on_validator_error(monkeypatch):
    """Twitter must return None when validation raises."""
    from platforms import twitter_bot

    _patch_validator(monkeypatch)

    bot = twitter_bot.TwitterBot.__new__(twitter_bot.TwitterBot)
    result = bot._validate_and_retry(
        "a perfectly normal generated reply body", _project()
    )
    assert result is None


def test_adapters_never_return_original_after_exception(monkeypatch):
    """None of the scoped adapters may echo back unvalidated content on error."""
    from platforms import reddit_web, telegram_group_bot, twitter_bot

    _patch_validator(monkeypatch)

    reddit_bot = reddit_web.RedditWebBot.__new__(reddit_web.RedditWebBot)
    reddit_bot.content_gen = type(
        "Gen", (), {"generate_reddit_comment": lambda self, **kw: (_ for _ in ()).throw(AssertionError())}
    )()
    reddit_out = reddit_bot._validate_content(
        "ORIGINAL-REDDIT", {"title": "t", "subreddit": "s"}, _project(), False
    )

    tg_bot = telegram_group_bot.TelegramGroupBot.__new__(
        telegram_group_bot.TelegramGroupBot
    )
    tg_out = tg_bot._validate_content("ORIGINAL-TELEGRAM", _project())

    tw_bot = twitter_bot.TwitterBot.__new__(twitter_bot.TwitterBot)
    tw_out = tw_bot._validate_and_retry("ORIGINAL-TWITTER", _project())

    assert reddit_out is None
    assert tg_out is None
    assert tw_out is None
    # Explicit guard against the old fail-open behavior.
    assert "ORIGINAL" not in (reddit_out or "")
    assert "ORIGINAL" not in (tg_out or "")
    assert "ORIGINAL" not in (tw_out or "")
