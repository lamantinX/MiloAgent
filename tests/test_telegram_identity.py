import pytest
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
        "phone": "+12345"
    }

@pytest.mark.asyncio
async def test_telegram_reject_bot(base_config):
    # Test that TelegramGroupBot rejects bot identities
    bot = TelegramGroupBot(MagicMock(), MagicMock(), base_config)
    bot.client = AsyncMock()
    bot.client.connect = AsyncMock()
    me_mock = MagicMock()
    me_mock.bot = True
    bot.client.get_me = AsyncMock(return_value=me_mock)
    
    with pytest.raises(ValueError, match="Telegram user engagement cannot be run with a bot identity"):
        await bot.authenticate()

@pytest.mark.asyncio
async def test_telegram_personal_user_send(base_config):
    # Test that _act_async uses the fake telethon client with the correct args
    bot = TelegramGroupBot(MagicMock(), MagicMock(), base_config)
    bot.client = AsyncMock()
    bot.client.connect = AsyncMock()
    me_mock = MagicMock()
    me_mock.bot = False
    me_mock.username = "real_user"
    bot.client.get_me = AsyncMock(return_value=me_mock)
    
    await bot.authenticate()
    assert bot._authenticated
    
    bot.client.send_message = AsyncMock()
    bot.content_gen = MagicMock()
    bot.content_gen._should_be_promotional.return_value = False
    bot.content_gen.generate_telegram_reply.return_value = "hello from user"
    
    opportunity = {
        "group_id": 12345,
        "target_id": "12345",
        "group_id": 12345,
        "message_id": 123,
        "context_text": "hello"
    }
    project = {
        "project": {"name": "test"}
    }
    
    action = await bot._act_async(opportunity, project)
    assert action is True  # _act_async returns a boolean in this codebase
    
    bot.client.send_message.assert_called_once()
    args, kwargs = bot.client.send_message.call_args
    assert args[0] == 12345
    assert args[1] == "hello from user"
    assert kwargs.get("reply_to") == 123

def test_telegram_admin_boundary():
    with open("platforms/telegram_group_bot.py", "r", encoding="utf-8") as f:
        content = f.read()
    assert "bot_token" not in content, "telegram_group_bot.py MUST NOT read bot_token"
    assert "telegram.ext" not in content, "telegram_group_bot.py MUST NOT use python-telegram-bot"
    assert "admin_chat_ids" not in content, "telegram_group_bot.py MUST NOT manage admin stuff"
