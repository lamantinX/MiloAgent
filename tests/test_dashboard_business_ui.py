"""Static regression tests for the business switcher UI (plan 010).

These mirror the plain-text scan style of ``test_dashboard_static.py``: the repo
has no jsdom/DOM test harness, so we assert against the static source of
``index.html``, ``app.js`` and ``cyber.css``. They guard the plan 010 contract:

  * the business switcher is visible, browser-local, and emits a change event;
  * a scoped request helper (``apiTenant``) appends ``business_id`` while the
    global helper stays unscoped;
  * every tenant fetch site uses the scoped helper, globals use the unscoped one;
  * accounts are grouped by service and no longer hard-filter twitter;
  * Telegram onboarding is personal-account-only (no BotFather/SMS), creates a
    disabled not_authorized record, and clears secrets on platform change;
  * business create/archive controls exist and no new inline ``onclick=`` is
    introduced in app.js (the plan 008 safe-event invariant stays intact).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = REPO_ROOT / "dashboard" / "static" / "index.html"
CYBER_CSS = REPO_ROOT / "dashboard" / "static" / "cyber.css"


def _read(path: Path) -> str:
    assert path.exists(), f"expected dashboard asset at {path}"
    return path.read_text(encoding="utf-8")


# ── Switcher markup + browser-local selection ──────────────────────────

def test_business_switcher_present_in_header():
    """A labeled business <select> lives in the mission-control header."""
    html = _read(INDEX_HTML)
    assert 'id="businessSelect"' in html, "business switcher select missing"
    assert "mc-business-switcher" in html, "switcher wrapper class missing"
    assert "Choose business" in html, "explicit empty/choose state missing"


def test_business_switcher_emits_change_event():
    """Selecting a business dispatches a business:changed CustomEvent."""
    js = _read(APP_JS)
    assert "business:changed" in js, "no business:changed event dispatched"
    assert "function onBusinessSelectChange" in js
    assert "applyBusinessSelection" in js


def test_business_selection_is_browser_local():
    """Selection is stored in localStorage under a versioned key, never global."""
    js = _read(APP_JS)
    assert "BUSINESS_KEY" in js
    assert "localStorage" in js and "milo_business_v1" in js
    # Orchestrator global state must not be mutated by switching.
    assert "uiState.businessId" in js
    assert "uiState.generation" in js


# ── Scoped vs global request helpers ───────────────────────────────────

def test_tenant_helper_appends_business_id():
    """apiTenant must append ?business_id=<sel> (merging existing query)."""
    js = _read(APP_JS)
    assert "function withBusinessScope" in js
    assert "business_id=" in js
    for helper in ("apiTenant(", "apiTenantPost(", "apiTenantPut(", "apiTenantDelete("):
        assert helper in js, f"scoped helper missing: {helper}"


def test_global_helper_unchanged_and_unscoped():
    """The original api()/apiPost()/apiPut()/apiDelete() stay unscoped."""
    js = _read(APP_JS)
    assert "async function api(path)" in js
    assert "async function apiPost(path, body)" in js
    assert "async function apiPut(path, body)" in js
    assert "async function apiDelete(path)" in js


# ── Tenant vs global fetch classification ──────────────────────────────

TENANT_FETCHES = [
    "/api/stats", "/api/minimaps", "/api/history", "/api/accounts/reddit/performance",
    "/api/heatmap", "/api/funnel", "/api/actions", "/api/conversations",
    "/api/brain", "/api/performance", "/api/insights", "/api/opportunities",
    "/api/decisions", "/api/intel/trends", "/api/intel/knowledge",
    "/api/intel/discoveries", "/api/intel/failures", "/api/intel/sentiment",
    "/api/intel/radar", "/api/network", "/api/communities",
    "/api/takeover/targets", "/api/takeover/requests",
    "/api/projects", "/api/accounts", "/api/cookies",
]

GLOBAL_FETCHES = ["/api/status", "/api/server", "/api/schedule", "/api/businesses"]


def test_tenant_endpoints_use_scoped_helper():
    """Every tenant fetch site must route through apiTenant* (not bare api)."""
    js = _read(APP_JS)
    offenders = []
    for endpoint in TENANT_FETCHES:
        needle = f"api('{endpoint}'"
        # Allow the endpoint to appear unscoped only inside the apiTenant helper
        # definition block or comments; the simplest robust check is that no
        # bare `api('<endpoint>'` call remains.
        if needle in js:
            offenders.append(endpoint)
    assert not offenders, f"tenant endpoints still using unscoped api(): {offenders}"


def test_global_endpoints_stay_unscoped():
    """Global endpoints must NOT gain a business_id query via apiTenant."""
    js = _read(APP_JS)
    for endpoint in GLOBAL_FETCHES:
        assert f"apiTenant('{endpoint}'" not in js, (
            f"global endpoint {endpoint} must stay on unscoped api()"
        )


# ── Accounts grouped by service, twitter no longer filtered ────────────

def test_accounts_no_longer_filter_twitter():
    """renderManageAccounts must not hard-exclude the twitter platform."""
    js = _read(APP_JS)
    # The old `a.platform !== 'twitter'` filter inside renderManageAccounts
    # must be gone (the cookies renderer still filters twitter, which is fine).
    assert "a.platform !== 'twitter'" not in js, (
        "twitter hard-filter still present in renderManageAccounts"
    )


def test_accounts_grouped_by_platform():
    """Accounts render in per-service groups with distinct records."""
    js = _read(APP_JS)
    assert "acct-group-head" in js, "no per-service group heading"
    assert "groups[plat]" in js or "groups[" in js


def test_accounts_payload_exposes_stable_fields():
    """GET /api/accounts must surface account_id + auth_status + assigned_projects."""
    py = (REPO_ROOT / "dashboard" / "web.py").read_text(encoding="utf-8")
    assert '"account_id": acc.get("account_id")' in py
    assert '"auth_status": acc.get("auth_status")' in py
    assert '"assigned_projects"' in py


# ── Telegram onboarding: personal account only ─────────────────────────

def test_telegram_onboarding_personal_account_only():
    """Telegram modal must say personal account, never BotFather/SMS."""
    html = _read(INDEX_HTML)
    js = _read(APP_JS)
    assert "Personal account" in html and "Telethon" in html
    assert "not a bot" in html.lower() or "not a bot" in js.lower()
    # Bot API / SMS must never appear as a Telegram option.
    assert "BotFather" not in html and "BotFather" not in js
    assert "bot_token" not in js
    # SMS / phone-code flow is plan 011's QR, not this modal.
    assert "send_code_request" not in js


def test_telegram_account_created_disabled_not_authorized():
    """New Telegram accounts are created as user/not_authorized, enabled=False."""
    py = (REPO_ROOT / "safety" / "account_manager.py").read_text(encoding="utf-8")
    assert '"account_type": "user"' in py
    assert '"auth_status": "not_authorized"' in py
    # The create-account client must send account_type user.
    assert "account_type: 'user'" in js_read_or_empty()


def js_read_or_empty() -> str:
    return _read(APP_JS)


def test_platform_change_clears_secret_fields():
    """Switching platform must clear secret-bearing inputs (no cross-leak)."""
    js = _read(APP_JS)
    assert "function clearAccountSecretFields" in js
    assert "function onAccountPlatformChange" in js
    # The platform select must wire the change handler.
    html = _read(INDEX_HTML)
    assert "onAccountPlatformChange()" in html


def test_product_assignment_is_multi_select_not_comma_text():
    """Account→product assignment uses a checklist, not free-text comma input."""
    html = _read(INDEX_HTML)
    js = _read(APP_JS)
    assert 'id="aProjectsList"' in html, "product checklist container missing"
    assert "comma-separated" not in html or "comma-separated" not in _account_modal_only(html)
    assert "loadAccountProductChecklist" in js
    assert "selectedAccountProducts" in js


def _account_modal_only(html: str) -> str:
    """Return just the Add Account modal block, to scope the comma-text check."""
    start = html.find('id="modal-addAccount"')
    end = html.find("</div>\n</div>", start) if start >= 0 else -1
    if start < 0:
        return ""
    return html[start:start + 2000]


# ── Business CRUD controls ─────────────────────────────────────────────

def test_business_create_and_archive_controls_present():
    """Create + archive business endpoints and UI handlers exist."""
    html = _read(INDEX_HTML)
    js = _read(APP_JS)
    py = (REPO_ROOT / "dashboard" / "web.py").read_text(encoding="utf-8")
    mgr = (REPO_ROOT / "core" / "business_manager.py").read_text(encoding="utf-8")
    assert "openModal('addBusiness')" in html
    assert "function submitBusiness" in js
    assert "function archiveBusiness" in js
    assert "/api/businesses/{business_id}/archive" in py
    assert "def archive_business" in mgr
    assert "def update_business" in mgr


def test_business_archive_is_soft_not_delete():
    """Archival flips enabled=False; no hard delete endpoint is exposed."""
    mgr = (REPO_ROOT / "core" / "business_manager.py").read_text(encoding="utf-8")
    assert "def archive_business" in mgr
    assert "enabled=False" in mgr or "enabled=False" in mgr.replace(" ", "")
    py = (REPO_ROOT / "dashboard" / "web.py").read_text(encoding="utf-8")
    # No destructive delete-business route.
    assert '"/api/businesses/{business_id}"' not in "".join(
        line for line in py.splitlines() if "delete" in line.lower()
    )


# ── Plan 008 invariant: no inline onclick in app.js ────────────────────

def test_app_js_still_has_no_inline_onclick():
    """Plan 008 invariant preserved: app.js must not emit inline onclick."""
    js = _read(APP_JS)
    assert "onclick=" not in js, "inline onclick= reintroduced in app.js"


# ── CSS coverage for new components ────────────────────────────────────

def test_css_covers_switcher_and_grouped_accounts():
    """cyber.css styles the switcher, badge, grouped accounts, disabled state."""
    css = _read(CYBER_CSS)
    for selector in (
        ".mc-business-switcher",
        ".biz-badge",
        ".acct-group-head",
        ".checklist",
        ".btn[disabled]",
    ):
        assert selector in css, f"css missing selector: {selector}"
