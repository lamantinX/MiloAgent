import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from dashboard.web import WebDashboard

import dashboard.web
dashboard.web._pwd_ctx = None

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MILO_WEB_PASS", "testpass")
    
    orch = MagicMock()
    orch.business_mgr.get_business.side_effect = lambda bid: {"id": bid, "name": "Valid"} if bid == "valid-biz" else None
    orch.projects = []
    
    dash = WebDashboard(orch)
    # patch token
    dash._validate_session = lambda creds: True
    dash.app.dependency_overrides[dash._verify_token] = lambda: True
    token = "dumb-token"
    client = TestClient(dash.app)
    return client, token

def test_add_accounts(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    
    # Add 3 Reddit accounts
    for i in range(3):
        res = c.post("/api/accounts?business_id=valid-biz", json={
            "platform": "reddit",
            "account_id": f"red-{i}",
            "business_id": "valid-biz",
            "username": f"user{i}",
            "password": f"pass{i}",
        }, headers=headers)
        assert res.status_code == 200, res.text
    
    # Add 2 Telegram accounts
    for i in range(2):
        res = c.post("/api/accounts?business_id=valid-biz", json={
            "platform": "telegram",
            "account_id": f"tg-{i}",
            "business_id": "valid-biz",
            "api_id": f"api_id_{i}",
            "api_hash": f"supersecret_api_hash_{i}",
            "account_type": "user"
        }, headers=headers)
        assert res.status_code == 200, res.text
        
    # Get all accounts
    res = c.get("/api/accounts?business_id=valid-biz", headers=headers)
    assert res.status_code == 200
    # ensure no secrets
    txt = res.text
    assert "supersecret" not in txt
    assert "pass0" not in txt
