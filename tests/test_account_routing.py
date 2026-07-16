import pytest
from safety.account_manager import AccountManager
from core.database import Database
import logging
import yaml
import os

@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db

@pytest.fixture
def mock_biz_mgr(monkeypatch):
    class MockBizMgr:
        def get_business(self, bid):
            if bid == "biz_1": return {"id": "biz_1"}
            if bid == "biz_2": return {"id": "biz_2"}
            return None
        def get_project(self, pid):
            if pid == "prod_1": return {"project": {"business_id": "biz_1"}}
            if pid == "prod_2": return {"project": {"business_id": "biz_2"}}
            return None
    def mock_init(*args, **kwargs):
        pass
    import core.business_manager
    monkeypatch.setattr(core.business_manager, "BusinessManager", MockBizMgr)
    return MockBizMgr()

def test_validation_rejects_invalid(test_db, mock_biz_mgr, tmp_path, caplog):
    mgr = AccountManager(test_db, str(tmp_path))
    
    # We will mock the file directly for testing
    config_path = tmp_path / "reddit_accounts.yaml"
    
    accounts = [
        # Valid
        {"account_id": "valid_1", "business_id": "biz_1", "username": "some_user1", "assigned_products": ["prod_1"], "enabled": True},
        # Missing account_id
        {"business_id": "biz_1", "username": "bad_user1", "enabled": True},
        # Duplicate account_id
        {"account_id": "valid_1", "business_id": "biz_1", "username": "duplicate_id", "enabled": True},
        # Unknown business
        {"account_id": "valid_2", "business_id": "bad_biz", "username": "some_user2", "enabled": True},
        # Unknown product
        {"account_id": "valid_3", "business_id": "biz_1", "username": "some_user3", "assigned_products": ["bad_prod"], "enabled": True},
        # Cross-business product
        {"account_id": "valid_4", "business_id": "biz_1", "username": "some_user4", "assigned_products": ["prod_2"], "enabled": True},
    ]
    
    with open(config_path, "w") as f:
        yaml.dump({"accounts": accounts}, f)
        
    with caplog.at_level(logging.WARNING):
        loaded = mgr.load_accounts("reddit")
        
    assert len(loaded) == 1
    assert loaded[0]["account_id"] == "valid_1"
    
    log_text = caplog.text
    assert "bad_user1" not in log_text  # Secrets/usernames masked or omitted gracefully?
    assert "some_user2" not in log_text
    # We asserted logging.warning omitted usernames from rejects. (We just logged account_ids or generic)
    assert "Account valid_3 references unknown product" in log_text
    assert "Account valid_4 references cross-business product" in log_text

def test_no_fallback(test_db, mock_biz_mgr, tmp_path, caplog):
    mgr = AccountManager(test_db, str(tmp_path))
    config_path = tmp_path / "reddit_accounts.yaml"
    accounts = [
        {"account_id": "valid_1", "business_id": "biz_1", "username": "some_user1", "assigned_products": ["prod_1"], "enabled": True},
        {"account_id": "valid_2", "business_id": "biz_2", "username": "some_user2", "assigned_products": ["prod_2"], "enabled": True},
    ]
    with open(config_path, "w") as f:
        yaml.dump({"accounts": accounts}, f)
        
    # Valid assignments
    acc = mgr.get_next_account("reddit", business_id="biz_1", product_id="prod_1")
    assert acc and acc["account_id"] == "valid_1"
    
    # Wrong business returns None and logs warning
    acc = mgr.get_next_account("reddit", business_id="biz_2", product_id="prod_1")
    assert acc is None
    
    # Unassigned returns None because empty assignment means unassigned
    acc = mgr.get_next_account("reddit", business_id="biz_1", product_id="unassigned_prod")
    assert acc is None

def test_rotation_and_state(test_db, mock_biz_mgr, tmp_path):
    mgr = AccountManager(test_db, str(tmp_path))
    config_path = tmp_path / "reddit_accounts.yaml"
    accounts = [
        {"account_id": "acc1", "business_id": "biz_1", "username": "u1", "assigned_products": ["prod_1"], "enabled": True, "cookies_file": "dummy"},
        {"account_id": "acc2", "business_id": "biz_1", "username": "u2", "assigned_products": ["prod_1"], "enabled": True, "cookies_file": "dummy"},
        {"account_id": "acc3", "business_id": "biz_2", "username": "u3", "assigned_products": ["prod_2"], "enabled": True, "cookies_file": "dummy"},
    ]
    with open(config_path, "w") as f:
        yaml.dump({"accounts": accounts}, f)
    import os
    with open("dummy", "w") as f:
        f.write("dummy")
    
    # Test rotation
    a1 = mgr.get_next_account("reddit", business_id="biz_1")
    a2 = mgr.get_next_account("reddit", business_id="biz_1")
    assert a1["account_id"] != a2["account_id"], "Should rotate within same business"
    
    # Test state isolation
    mgr.mark_cooldown("reddit", a1["account_id"], 10)
    a1_again = mgr.get_next_account("reddit", business_id="biz_1")
    assert a1_again["account_id"] == a2["account_id"], "Should not return cooled down account"
    
    # Biz 2 should be unaffected
    b1 = mgr.get_next_account("reddit", business_id="biz_2")
    assert b1["account_id"] == "acc3", "Biz 2 should get its own account regardless of Biz 1"
    
    os.remove("dummy")
