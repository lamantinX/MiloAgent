"""Static regression tests for dashboard/static/app.js event wiring.

These tests guard against the reintroduction of the injection surface where
server-derived identifiers (project names, account platform/username) were
serialized via ``JSON.stringify`` into inline ``onclick`` attributes. They are
intentionally narrow: they reject dynamic ``onclick`` + ``JSON.stringify`` on
the same line but do NOT ban safe ``JSON.stringify`` usage elsewhere, nor do
they touch the developer-authored static handlers in index.html.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_JS = REPO_ROOT / "dashboard" / "static" / "app.js"


def _read_app_js() -> str:
    """Return app.js source, failing loudly if the file moved."""
    assert APP_JS.exists(), f"expected dashboard client at {APP_JS}"
    return APP_JS.read_text(encoding="utf-8")


def test_no_dynamic_onclick_with_json_stringify():
    """No line may combine an inline ``onclick=`` with ``JSON.stringify``.

    This is the exact injection surface: a server value serialized into an
    executable HTML/JS attribute. The check stays on a single line so safe
    ``JSON.stringify`` calls (e.g. fetch bodies) elsewhere remain allowed.
    """
    src = _read_app_js()
    offenders = [
        (i, ln)
        for i, ln in enumerate(src.splitlines(), 1)
        if "onclick=" in ln and "JSON.stringify" in ln
    ]
    assert not offenders, (
        "dynamic inline onclick with JSON.stringify found: " + repr(offenders)
    )


def test_no_inline_onclick_handlers_in_app_js():
    """app.js must not emit any inline ``onclick=`` handler at all.

    All server-derived action buttons are now wired through ``actionButton``
    (DOM ``addEventListener``), so no ``onclick=`` markup should remain in
    app.js. The static developer-authored handlers live in index.html and are
    out of scope.
    """
    src = _read_app_js()
    assert "onclick=" not in src, "inline onclick= handler present in app.js"


def test_action_button_helper_exists():
    """The safe DOM button factory must be present."""
    src = _read_app_js()
    assert "function actionButton(" in src, "actionButton helper missing"
    assert "addEventListener('click'" in src, "no addEventListener('click') wiring"


def test_action_functions_still_defined():
    """The four action handlers must keep their original names/signatures."""
    src = _read_app_js()
    for needle in (
        "async function editProject(name)",
        "async function deleteProject(name)",
        "async function removeAccount(platform, username)",
        "async function deleteCookies(platform, username)",
    ):
        assert needle in src, f"action function signature changed/missing: {needle!r}"


def test_action_buttons_wired_for_each_site():
    """Each of the four sites must wire its button through ``actionButton``.

    Plan 010 changed the account-action argument to prefer the stable
    ``account_id`` (with ``username`` as a legacy fallback); the safe DOM wiring
    pattern from plan 008 is unchanged. Project/cookie actions keep their form.
    """
    src = _read_app_js()
    for call in (
        "actionButton('Edit', 'btn btn-sm', () => editProject(p.name))",
        "actionButton('Delete', 'btn btn-sm danger', () => deleteProject(p.name))",
        "actionButton('Remove', 'btn btn-sm danger', () => removeAccount(a.platform, a.account_id || a.username))",
        "actionButton('Delete', 'btn btn-sm danger', () => deleteCookies(c.platform, c.username))",
    ):
        assert call in src, f"action button wiring missing: {call!r}"


def test_unsafe_identifier_not_in_executable_markup():
    """Server identifiers flow through closures/DOM properties, never markup.

    A fixture name containing quotes, apostrophes and angle brackets is the
    canonical break-in case for inline ``onclick``. After the refactor it must
    only ever appear as ``textContent``/``dataset`` style data passed into the
    handler closures, never interpolated into an executable HTML attribute via
    ``JSON.stringify`` next to ``onclick`` (already covered above) or a bare
    inline handler string.
    """
    src = _read_app_js()
    # The four action calls must receive the identifier as a JS expression
    # inside an arrow-function closure, not as an interpolated string.
    # Concretely, the identifier reference (e.g. p.name) must NOT appear
    # inside an onclick= attribute anywhere.
    assert "onclick=" not in src
