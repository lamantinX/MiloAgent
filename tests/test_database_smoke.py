"""Offline smoke test for the SQLite persistence layer.

Uses a fresh temp database; no network or real account is touched.
"""

from core.database import Database


def test_database_round_trips_action_and_opportunity(tmp_sqlite_path):
    """A fresh Database initializes and can store/read actions + opportunities."""
    db = Database(str(tmp_sqlite_path))
    try:
        action_id = db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="smoke-account",
            project="smoke-project",
            target_id="t3_smoke",
            content="hello from the smoke test",
            metadata={"k": "v"},
            success=True,
        )
        assert isinstance(action_id, int)
        assert action_id > 0

        actions = db.get_recent_actions(hours=1, platform="reddit", business_id="biz_test", limit=10)
        assert len(actions) == 1
        row = actions[0]
        assert row["platform"] == "reddit"
        assert row["action_type"] == "comment"
        assert row["account"] == "smoke-account"
        assert row["project"] == "smoke-project"
        assert row["target_id"] == "t3_smoke"
        assert row["content"] == "hello from the smoke test"
        assert row["success"] == 1

        opp_id = db.log_opportunity(
            platform="reddit", business_id="biz_test",
            target_id="t3_opp",
            title="an opportunity",
            subreddit_or_query="r/smoke",
            score=0.75,
            project="smoke-project",
        )
        assert isinstance(opp_id, int)
        assert opp_id > 0

        opps = db.get_pending_opportunities(platform="reddit", business_id="biz_test", project="smoke-project")
        assert len(opps) == 1
        opp = opps[0]
        assert opp["target_id"] == "t3_opp"
        assert opp["score"] == 0.75
        assert opp["status"] == "pending"
    finally:
        db.close()


def test_database_closes_idempotently(tmp_sqlite_path):
    """close() is safe to call and does not raise on a fresh database."""
    db = Database(str(tmp_sqlite_path))
    db.close()
    # second close must be a no-op, not an error
    db.close()
