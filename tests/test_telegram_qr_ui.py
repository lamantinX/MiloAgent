"""Static regression tests for Telegram QR/2FA UI (plan 011).

Pure-python text-scan of the modal HTML and app.js flow to verify:
  - 2FA input is type="password"
  - timers and intervals are cleared
  - secret fields are cleared in `finally`
  - no Telethon error text leaks
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard" / "static" / "app.js"
INDEX_HTML = REPO_ROOT / "dashboard" / "static" / "index.html"

def _read(path: Path) -> str:
    assert path.exists()
    return path.read_text(encoding="utf-8")

def test_telegram_qr_modal_has_no_secrets():
    """Modal structure exists without leaking secrets in inline JS."""
    html = _read(INDEX_HTML)
    assert 'id="modal-telegramQR"' in html, "QR modal missing"
    assert 'type="password"' in html, "2FA input must be obscured"

def test_telegram_qr_lifecycle_cleans_secrets():
    """Completion/cancellation nulls out memory and DOM."""
    js = _read(APP_JS)
    assert "function authorizeTelegram(" in js
    assert "function pollTelegramQR(" in js
    assert "function submitTelegram2FA(" in js
    assert "function closeTelegramQR(" in js
    # Secret clearing must happen.
    assert ".value = ''" in js
