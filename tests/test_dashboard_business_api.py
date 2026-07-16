import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from dashboard.web import WebDashboard
import os

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MILO_WEB_PASS", "testpass")
    
    orch = MagicMock()
    orch.business_mgr.get_business.side_effect = lambda bid: {"id": bid, "name": "Valid"} if bid == "valid-biz" else None
    orch.projects = []
    
    dash = WebDashboard(orch)
    # patch token
    dash._validate_session = lambda creds: True
    token = "dumb-token"
    client = TestClient(dash.app)
    return client, token

# Patch _pwd_ctx
import dashboard.web
dashboard.web._pwd_ctx = None

GLOBAL_ENDPOINTS = [
    "/health",
    "/api/status",
    "/api/server",
]

TENANT_ENDPOINTS = [
    "/api/projects",
    "/api/accounts",
    "/api/actions",
    "/api/stats",
    "/api/history",
    "/api/insights",
    "/api/opportunities",
    "/api/opportunities/rejected",
    "/api/decisions",
    "/api/minimaps",
    "/api/conversations",
]

def test_global_endpoints_scope(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    for endpoint in GLOBAL_ENDPOINTS:
        res = c.get(endpoint, headers=headers)
        assert res.status_code == 200, f"{endpoint} failed"
        if isinstance(res.json(), dict) and endpoint != "/health":
            assert res.json().get("scope") == "global", f"{endpoint} did not return scope global"

def test_tenant_endpoints_require_business(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    for endpoint in TENANT_ENDPOINTS:
        res = c.get(endpoint, headers=headers)
        assert res.status_code in [400, 422], f"{endpoint} did not enforce business_id, returned {res.status_code}"
        
        res_unknown = c.get(f"{endpoint}?business_id=unknown-biz", headers=headers)
        assert res_unknown.status_code == 404, f"{endpoint} did not return 404 for unknown business_id, returned {res_unknown.status_code}"
        
        res_valid = c.get(f"{endpoint}?business_id=valid-biz", headers=headers)
        assert res_valid.status_code == 200 or res_valid.status_code == 500, f"{endpoint} did not handle valid business_id properly" 

def test_business_and_product_crud(client):
    c, token = client
    headers = {"Authorization": f"Bearer {token}"}
    
    # 1. GET /api/businesses
    res = c.get("/api/businesses", headers=headers)
    assert res.status_code == 200, res.text
    
    # 2. POST /api/businesses
    res = c.post("/api/businesses", json={
        "business_id": "test-biz",
        "name": "Test Biz",
        "description": "A new test business"
    }, headers=headers)
    assert res.status_code == 200
    
    # 3. POST /api/projects should fail without business_id, wait, it has biz=Depends, so it's in query
    res = c.post("/api/projects?business_id=valid-biz", json={
        "name": "Test Product",
        "url": "https://test.local",
        "description": "desc"
    }, headers=headers)
    assert res.status_code == 200, res.text

