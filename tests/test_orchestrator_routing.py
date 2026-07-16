import pytest
import os
from unittest.mock import patch, MagicMock
from core.orchestrator import Orchestrator
from core.database import Database
from core.business_manager import BusinessManager

@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db

def test_cache_collision_regression(test_db, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text("database:\n  path: ':memory:'\n")
    (config_dir / "llm.yaml").write_text("provider: openai\n")
    (config_dir / "reddit_accounts.yaml").write_text("auth_mode: web\n")

    orch = Orchestrator(config_dir=str(config_dir))

    account_a = {"username": "foo", "business_id": "b1", "account_id": "a1"}
    account_b = {"username": "foo", "business_id": "b2", "account_id": "a1"}

    bot_a = orch._get_reddit_bot(account_a)
    bot_b = orch._get_reddit_bot(account_b)

    assert bot_a is not bot_b
    assert ("b1", "reddit", "a1") in orch._clients
    assert ("b2", "reddit", "a1") in orch._clients
