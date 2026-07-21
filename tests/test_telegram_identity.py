import pytest
import time
from unittest.mock import AsyncMock, patch, MagicMock
from platforms.telegram_group_bot import TelegramGroupBot

@pytest.fixture
def base_config():
    return {
        "account_id": "test_tg",
        "business_id": "b1",
        "account_type": "user",
        "api_id": 12345,
        "api_hash": "abc",
        "session_file": "data/sessions/test.session",
        "phone": "+12345",
        "max_messages_per_hour": 10,
    }

@pytest.fixture
def mock_db():
    """In-memory database for Telegram tests."""
    from core.database import Database
    db = Database(":memory:")
    yield db
    db.close()

@pytest.mark.asyncio
async def test_telegram_reject_bot(base_config):
    """TelegramGroupBot must reject bot identities."""
    bot = TelegramGroupBot(MagicMock(), MagicMock(), base_config)
    bot.client = AsyncMock()
    bot.client.connect = AsyncMock()
    me_mock = MagicMock()
    me_mock.bot = True
    bot.client.get_me = AsyncMock(return_value=me_mock)

    with pytest.raises(ValueError, match="Telegram user engagement cannot be run with a bot identity"):
        await bot.authenticate()

@pytest.mark.asyncio
async def test_telegram_personal_user_send(base_config, mock_db):
    """_act_async sends a message via Telethon client."""
    bot = TelegramGroupBot(mock_db, MagicMock(), base_config)
    bot.client = AsyncMock()
    bot.client.connect = AsyncMock()
    me_mock = MagicMock()
    me_mock.bot = False
    me_mock.username = "real_user"
    me_mock.id = 123456
    bot.client.get_me = AsyncMock(return_value=me_mock)

    await bot.authenticate()
    assert bot._authenticated

    # Mock get_entity to return a mock entity
    mock_entity = MagicMock()
    mock_entity.id = 12345
    bot.client.get_entity = AsyncMock(return_value=mock_entity)
    bot.client.send_message = AsyncMock()

    bot.content_gen = MagicMock()
    bot.content_gen._should_be_promotional.return_value = False
    bot.content_gen.generate_telegram_reply.return_value = "hello from user"

    # Reset rate limiter timestamps
    bot._send_timestamps = []

    opportunity = {
        "group_id": 12345,
        "target_id": "tg:b1:test_tg:12345:123",
        "message_id": 123,
        "text": "hello",
        "group_name": "TestGroup",
        "author_name": "someone",
    }
    project = {
        "project": {"name": "test", "business_id": "b1"},
        "telegram": {"enabled": True, "persona": "helpful_casual"},
    }

    action = await bot._act_async(opportunity, project)
    assert action is True

    bot.client.send_message.assert_called_once()
    args, kwargs = bot.client.send_message.call_args
    # First arg is the entity object, second is the reply text
    assert args[0] == mock_entity
    assert args[1] == "hello from user"
    assert kwargs.get("reply_to") == 123

def test_telegram_admin_boundary():
    """telegram_group_bot.py must not reference admin bot internals."""
    with open("platforms/telegram_group_bot.py", "r", encoding="utf-8") as f:
        content = f.read()
    assert "bot_token" not in content, "telegram_group_bot.py MUST NOT read bot_token"
    assert "telegram.ext" not in content, "telegram_group_bot.py MUST NOT use python-telegram-bot"
    assert "admin_chat_ids" not in content, "telegram_group_bot.py MUST NOT manage admin stuff"

def test_account_key_canonical():
    """AccountManager._account_key must produce consistent keys."""
    from safety.account_manager import AccountManager
    assert AccountManager._account_key("biz1", "acct1") == "biz1:acct1"
    assert AccountManager._account_key("", "acct1") == ":acct1"

def _setup_biz_env(tmp_path, monkeypatch):
    """Set up a minimal business/product environment in tmp_path."""
    import yaml, os
    monkeypatch.chdir(tmp_path)
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    biz_dir = tmp_path / "businesses"
    biz_dir.mkdir(exist_ok=True)
    # BusinessManager expects: data["business"]["id"]
    (biz_dir / "test_biz.yaml").write_text(yaml.dump({
        "business": {
            "id": "test_biz",
            "name": "Test Business",
            "enabled": True,
        }
    }))
    prod_dir = tmp_path / "projects"
    prod_dir.mkdir(exist_ok=True)
    (prod_dir / "test_prod.yaml").write_text(yaml.dump({
        "project": {
            "id": "test_prod",
            "business_id": "test_biz",
            "name": "Test Product",
            "enabled": True,
            "weight": 1.0,
        }
    }))

    # Empty defaults for other platforms
    for fname in ("reddit_accounts.yaml", "twitter_accounts.yaml"):
        p = os.path.join(config_dir, fname)
        if not os.path.exists(p):
            with open(p, "w") as f:
                yaml.dump({"accounts": []}, f)

    # Reset BusinessManager singleton
    from core.business_manager import BusinessManager
    BusinessManager._instance = None
    return config_dir


def test_load_accounts_include_disabled(mock_db, tmp_path, monkeypatch):
    """load_accounts with include_disabled=True returns disabled accounts."""
    import yaml, os
    config_dir = _setup_biz_env(tmp_path, monkeypatch)

    tg_config = {
        "accounts": [{
            "account_id": "tg_disabled",
            "business_id": "test_biz",
            "account_type": "user",
            "api_id": "12345",
            "api_hash": "abc",
            "phone": "+111",
            "session_file": "data/sessions/test.session",
            "auth_status": "not_authorized",
            "enabled": False,
            "assigned_products": ["test_prod"],
        }]
    }
    with open(os.path.join(config_dir, "telegram_user_accounts.yaml"), "w") as f:
        yaml.dump(tg_config, f)

    from safety.account_manager import AccountManager
    mgr = AccountManager(mock_db, config_dir + "/")

    # Without include_disabled: should not see the account
    accounts = mgr.load_accounts("telegram")
    assert len(accounts) == 0

    # With include_disabled: should see it
    accounts = mgr.load_accounts("telegram", include_disabled=True, include_unauthorized=True)
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == "tg_disabled"


def test_get_telegram_account_finds_disabled(mock_db, tmp_path, monkeypatch):
    """get_telegram_account must find disabled/unauthorized accounts."""
    import yaml, os
    config_dir = _setup_biz_env(tmp_path, monkeypatch)

    tg_config = {
        "accounts": [{
            "account_id": "tg_new",
            "business_id": "test_biz",
            "account_type": "user",
            "api_id": "12345",
            "api_hash": "abc",
            "phone": "+222",
            "auth_status": "not_authorized",
            "enabled": False,
            "assigned_products": ["test_prod"],
        }]
    }
    with open(os.path.join(config_dir, "telegram_user_accounts.yaml"), "w") as f:
        yaml.dump(tg_config, f)

    from safety.account_manager import AccountManager
    mgr = AccountManager(mock_db, config_dir + "/")

    # Should find even though disabled and not authorized
    acc = mgr.get_telegram_account("test_biz", "tg_new")
    assert acc is not None
    assert acc["account_id"] == "tg_new"
    assert acc["auth_status"] == "not_authorized"


def test_get_all_health_uses_canonical_keys(mock_db, tmp_path, monkeypatch):
    """get_all_health must use business_id:account_id keys to find cooldown state."""
    import yaml, os
    config_dir = _setup_biz_env(tmp_path, monkeypatch)

    reddit_config = {
        "accounts": [{
            "account_id": "reddit_1",
            "business_id": "test_biz",
            "username": "testuser",
            "password": "pass",
            "assigned_projects": ["test_prod"],
            "enabled": True,
        }]
    }
    with open(os.path.join(config_dir, "reddit_accounts.yaml"), "w") as f:
        yaml.dump(reddit_config, f)

    from safety.account_manager import AccountManager
    mgr = AccountManager(mock_db, config_dir + "/")

    # Put account on cooldown
    mgr.mark_cooldown("reddit", "test_biz", "reddit_1", minutes=30)

    # get_all_health should reflect cooldown
    health = mgr.get_all_health()
    reddit_health = [h for h in health if h["platform"] == "reddit"]
    assert len(reddit_health) == 1
    assert reddit_health[0]["status"] == "cooldown"
    assert reddit_health[0]["cooldown_until"] is not None

def test_database_telegram_drafts(mock_db):
    """Test Telegram draft creation and status transitions."""
    draft_id = mock_db.create_telegram_draft(
        business_id="b1",
        product_id="p1",
        account_id="tg1",
        opportunity_id=1,
        group_id="g1",
        group_name="TestGroup",
        message_id=100,
        author_id="u1",
        author_name="User",
        original_text="Help me find a charger",
        generated_reply="Try ChargePoint!",
        relevance_score=7.5,
    )
    assert draft_id > 0

    # Get pending drafts
    drafts = mock_db.get_pending_drafts(business_id="b1")
    assert len(drafts) == 1
    assert drafts[0]["status"] == "pending"

    # Approve
    assert mock_db.update_draft_status(draft_id, "approved") is True
    draft = mock_db.get_telegram_draft(draft_id)
    assert draft["status"] == "approved"

    # Can't approve again (invalid transition)
    assert mock_db.update_draft_status(draft_id, "approved") is False

    # Send
    assert mock_db.update_draft_status(draft_id, "sent") is True
    draft = mock_db.get_telegram_draft(draft_id)
    assert draft["status"] == "sent"
    assert draft["sent_at"] is not None

def test_database_telegram_rate_limits(mock_db):
    """Test Telegram rate limit tracking."""
    mock_db.log_telegram_action("b1", "tg1", group_id="g1", action_type="message")
    mock_db.log_telegram_action("b1", "tg1", group_id="g2", action_type="message")
    mock_db.log_telegram_action("b1", "tg1", action_type="join")

    assert mock_db.get_telegram_action_count("tg1", "message", hours=1) == 2
    assert mock_db.get_telegram_action_count("tg1", "message", hours=1, group_id="g1") == 1
    assert mock_db.get_telegram_action_count("tg1", "join", hours=1) == 1

def test_database_flood_wait(mock_db):
    """Test FloodWait persistence."""
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    mock_db.set_flood_wait("b1", "tg1", future)
    assert mock_db.is_flood_wait_active("b1", "tg1") is True

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    mock_db.set_flood_wait("b1", "tg2", past)
    assert mock_db.is_flood_wait_active("b1", "tg2") is False

def test_database_telegram_groups(mock_db):
    """Test Telegram group tracking."""
    mock_db.upsert_telegram_group(
        "b1", "tg1", "g123",
        group_name="EV Chargers Discussion",
        member_count=5000,
    )
    groups = mock_db.get_telegram_groups("b1")
    assert len(groups) == 1
    assert groups[0]["group_name"] == "EV Chargers Discussion"

    mock_db.mark_group_scanned("b1", "tg1", "g123")
    groups = mock_db.get_telegram_groups("b1")
    assert groups[0]["last_scanned"] is not None

def test_database_account_state(mock_db):
    """Test Telegram account state persistence."""
    mock_db.update_telegram_account_state(
        "b1", "tg1",
        messages_1h=3,
        messages_24h=15,
    )
    state = mock_db.get_telegram_account_state("b1", "tg1")
    assert state["messages_1h"] == 3
    assert state["messages_24h"] == 15

    # Update existing
    mock_db.update_telegram_account_state("b1", "tg1", messages_1h=5)
    state = mock_db.get_telegram_account_state("b1", "tg1")
    assert state["messages_1h"] == 5
    assert state["messages_24h"] == 15  # unchanged
