"""Regression test for the timestamp-cutoff format bug (Plan 001, Step 0).

Background: action/opportunity rows are stored with SQLite's
``datetime('now')`` → ``'YYYY-MM-DD HH:MM:SS'`` (space separator, no
microseconds). The read-side cutoffs previously used Python
``datetime.utcnow().isoformat()`` → ``'YYYY-MM-DDTHH:MM:SS.ffffff'`` ('T'
separator, microseconds). Compared as strings in ``WHERE timestamp > ?``,
``space(0x20) < 'T'(0x54)``, so a freshly-inserted row compared as *older*
than the cutoff and was filtered out — every time-windowed read returned 0.

These tests pin the fix: ``Database._cutoff`` reproduces SQLite's textual
datetime format, so windowed reads return the rows that were actually
written, and genuinely old rows are still excluded.
"""

from core.database import Database


def test_windowed_read_returns_just_written_row(tmp_sqlite_path):
    """A row written moments ago must be visible in a 1h window (was 0 before fix)."""
    db = Database(str(tmp_sqlite_path))
    try:
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="ts-account",
            project="ts-project",
            target_id="t3_ts1",
            content="timestamp regression",
            success=True,
        )
        actions = db.get_recent_actions(hours=1, platform="reddit", business_id="biz_test", limit=10)
        assert len(actions) == 1
        assert actions[0]["target_id"] == "t3_ts1"
    finally:
        db.close()


def test_action_count_returns_one_not_zero(tmp_sqlite_path):
    """get_action_count in a 1h window must be 1 (was 0 before fix)."""
    db = Database(str(tmp_sqlite_path))
    try:
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="ts-account",
            project="ts-project",
            target_id="t3_ts2",
            content="count me",
            success=True,
        )
        assert db.get_action_count(hours=1, platform="reddit") == 1
    finally:
        db.close()


def test_action_count_grows_with_second_row(tmp_sqlite_path):
    """A second row increments the windowed count to 2."""
    db = Database(str(tmp_sqlite_path))
    try:
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="ts-account",
            project="ts-project",
            target_id="t3_ts3a",
            content="first",
            success=True,
        )
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="ts-account",
            project="ts-project",
            target_id="t3_ts3b",
            content="second",
            success=True,
        )
        assert db.get_action_count(hours=1) == 2
    finally:
        db.close()


def test_recent_actions_by_type_returns_both(tmp_sqlite_path):
    """get_recent_actions_by_type for the written type returns both rows."""
    db = Database(str(tmp_sqlite_path))
    try:
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="ts-account",
            project="ts-project",
            target_id="t3_ts4a",
            content="first",
            success=True,
        )
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="ts-account",
            project="ts-project",
            target_id="t3_ts4b",
            content="second",
            success=True,
        )
        rows = db.get_recent_actions_by_type(action_type="comment", hours=1)
        assert len(rows) == 2
    finally:
        db.close()


def test_genuinely_old_row_is_excluded(tmp_sqlite_path):
    """A row backdated far into the past must NOT appear in a 1h window.

    This proves the window actually filters by time (not that it returns
    everything). We backdate by writing directly with an old timestamp in
    SQLite's own format, matching how datetime('now') stores rows.
    """
    db = Database(str(tmp_sqlite_path))
    try:
        # Insert a row, then backdate its timestamp to 10 days ago — well
        # outside the 1h query window. Uses SQLite's native strftime so the
        # stored value matches the DEFAULT (datetime('now')) on-disk format.
        db._execute_write(
            "INSERT INTO actions "
            "(platform, action_type, account, project, target_id, content, "
            " metadata, success, error_message, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 1, '', strftime('%Y-%m-%d %H:%M:%S', 'now', '-10 days'))",
            ("reddit", "comment", "old-account", "ts-project", "t3_old", "ancient"),
        )
        # A fresh row that SHOULD appear.
        db.log_action(
            platform="reddit", business_id="biz_test",
            action_type="comment",
            account="new-account",
            project="ts-project",
            target_id="t3_new",
            content="fresh",
            success=True,
        )
        actions = db.get_recent_actions(hours=1, platform="reddit", business_id="biz_test", limit=10)
        targets = {a["target_id"] for a in actions}
        assert "t3_new" in targets
        assert "t3_old" not in targets
        assert len(actions) == 1
    finally:
        db.close()
