import threading
import time
import pytest
import datetime
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from dashboard.oauth_state import OAuthStateStore

def test_oauth_state_store_uniqueness():
    store = OAuthStateStore()
    state1 = store.generate_state("test", "b1", "a1", "/")
    state2 = store.generate_state("test", "b1", "a1", "/")
    assert state1 != state2
    assert len(store._store) == 2

def test_oauth_state_store_expiry():
    store = OAuthStateStore()
    state = store.generate_state("test", "b1", "a1", "/", ttl_seconds=0.1)
    
    assert len(store._store) == 1
    time.sleep(0.15)
    
    result = store.consume(state)
    assert result is None
    assert len(store._store) == 0

def test_oauth_state_store_capacity():
    store = OAuthStateStore(max_capacity=2)
    store.generate_state("test", "b1", "a1", "/")
    store.generate_state("test", "b1", "a2", "/")
    
    with pytest.raises(RuntimeError):
        store.generate_state("test", "b1", "a3", "/")

def test_oauth_state_store_concurrent_consume():
    store = OAuthStateStore()
    state = store.generate_state("test", "b1", "a1", "/")
    
    results = []
    def worker():
        results.append(store.consume(state))
        
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    successes = [r for r in results if r is not None]
    nones = [r for r in results if r is None]
    
    assert len(successes) == 1
    assert len(nones) == 9
    assert successes[0]["business_id"] == "b1"


# --- Endpoint tests ---

def get_test_dashboard():
    import dashboard.web
    if dashboard.web._pwd_ctx:
        dashboard.web._pwd_ctx = MagicMock()
        dashboard.web._pwd_ctx.hash.return_value = "mock_hash"
        dashboard.web._pwd_ctx.verify.return_value = True
    from dashboard.web import WebDashboard
    from core.orchestrator import Orchestrator
    orch = MagicMock(spec=Orchestrator)
    orch.business_mgr = MagicMock()
    orch.business_mgr.get_business.return_value = {'id': 'b1', 'name': 'Biz 1'}
    orch.settings = MagicMock()
    orch.settings.get.return_value = {}
    orch.settings.get.return_value = {}
    orch.account_mgr = MagicMock()
    dashboard = WebDashboard(orch)
    return dashboard, orch

def test_oauth_start_endpoint_valid(monkeypatch):
    dashboard, orch = get_test_dashboard()
    orch.account_mgr.get_account.return_value = {"business_id": "b1", "account_id": "a1", "platform": "reddit"}
    
    client = TestClient(dashboard.app)
    token = dashboard._create_session("test_user", "127.0.0.1")
    
    monkeypatch.setattr("dashboard.web._load_reddit_api_config", lambda: {"client_id": "test_id", "redirect_uri": "http://test"})
    
    response = client.post(
        "/api/reddit/oauth/start?business_id=b1", 
        json={"business_id": "b1", "account_id": "a1"},
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "test_id" in data["auth_url"]
    assert "business_id" not in data["auth_url"]
    assert "account_id" not in data["auth_url"]
    assert "state=" in data["auth_url"]
    assert "state=b1" not in data["auth_url"]

def test_oauth_start_endpoint_no_account(monkeypatch):
    dashboard, orch = get_test_dashboard()
    orch.account_mgr.get_account.return_value = None
    
    client = TestClient(dashboard.app)
    token = dashboard._create_session("test_user", "127.0.0.1")
    
    monkeypatch.setattr("dashboard.web._load_reddit_api_config", lambda: {"client_id": "test_id"})
    
    response = client.post(
        "/api/reddit/oauth/start?business_id=b1", 
        json={"business_id": "b1", "account_id": "a1"},
        headers={"Authorization": f"Bearer {token}"}
    )
    
    data = response.json()
    assert data["ok"] is False
    assert "Account not found" in data["error"]

def test_oauth_callback_invalid_state():
    dashboard, _ = get_test_dashboard()
    client = TestClient(dashboard.app)
    response = client.get("/api/reddit/oauth/callback?code=123&state=invalid", follow_redirects=False)
    assert response.status_code == 303
    assert "invalid_state" in response.headers["location"]

@patch("requests.post")
def test_oauth_callback_success(mock_post, monkeypatch, tmp_path):
    dashboard, _ = get_test_dashboard()
    client = TestClient(dashboard.app)
    
    from dashboard.oauth_state import oauth_store
    state = oauth_store.generate_state("reddit_oauth", "b1", "a1", "http://test")
    
    import yaml
    import pathlib
    config_file = pathlib.Path("config/accounts.local.yaml")
    config_file.write_text(yaml.dump({
        "accounts": [
            {"business_id": "b1", "account_id": "a1", "platform": "reddit"},
            {"business_id": "b2", "account_id": "a2", "platform": "reddit"}
        ],
        "other_key": "preserved"
    }))
    
    import pathlib
    original_path = pathlib.Path
    class FakePath(original_path):
        def __new__(cls, *args, **kwargs):
            p = str(args[0])
            if "accounts" in p:
                if "local" in p:
                    return original_path.__new__(cls, config_file)
                return original_path.__new__(cls, tmp_path / "not_exist")
            return original_path.__new__(cls, *args, **kwargs)
            
    monkeypatch.setattr("dashboard.web.Path", FakePath)
    monkeypatch.setattr("dashboard.web._load_reddit_api_config", lambda: {"client_id": "test_id", "client_secret": "test_secret"})
    
    mock_post.return_value = MagicMock()
    mock_post.return_value.json.return_value = {"access_token": "acc", "refresh_token": "ref_SECRET_123"}
    
    import logging
    with patch.object(logging.getLogger("dashboard.web"), "info") as mock_info, \
         patch.object(logging.getLogger("dashboard.web"), "error") as mock_error:
        
        response = client.get(f"/api/reddit/oauth/callback?code=CODE123&state={state}", follow_redirects=False)
        
        assert response.status_code == 303
        assert "success" in response.headers["location"], mock_error.call_args_list
        assert mock_post.called
        
        # Verify exactly once consume
        response2 = client.get(f"/api/reddit/oauth/callback?code=CODE123&state={state}", follow_redirects=False)
        assert "invalid_state" in response2.headers["location"]
        
        # Verify file update
        with open(config_file) as f:
            updated = yaml.safe_load(f)
            
        assert updated["other_key"] == "preserved"
        assert updated["accounts"][0]["refresh_token"] == "ref_SECRET_123"
        assert "refresh_token" not in updated["accounts"][1]
        
        # No secrets in logs
        for call in mock_info.call_args_list:
            arg_str = str(call)
            assert "ref_SECRET_123" not in arg_str
            assert "test_secret" not in arg_str
