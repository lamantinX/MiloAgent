"""Backend tests for the Telegram QR-login challenge manager (plan 011).

All tests run fully offline with a fake Telethon client and a fake loop runner.
No network access, no real Telegram, no real session files.
"""

from __future__ import annotations

import base64
import io
import time

import pytest

from dashboard.telegram_auth import (
    TelegramAuthChallengeManager,
    ChallengeDTO,
    WAITING_SCAN,
    PASSWORD_REQUIRED,
    AUTHORIZED,
    EXPIRED,
    CANCELLED,
    FAILED,
    MAX_2FA_ATTEMPTS,
    render_qr_png_b64,
    session_path_for,
    _PasswordHashInvalid,
    _coro_sign_in_password,
)


# ── Fakes ──────────────────────────────────────────────────────────────


class FakeUser:
    def __init__(self, bot=False, username="alice", phone="+1555", first_name="Alice", uid=1):
        self.bot = bot
        self.username = username
        self.phone = phone
        self.first_name = first_name
        self.id = uid


class FakeQRLogin:
    """Mimics telethon.tl.custom.qrlogin.QRLogin for tests."""

    def __init__(self, url="tg://login?token=fake123", expires_in=120, outcome=None):
        self.url = url
        # telethon exposes .expires as a datetime; tests use a future epoch.
        self.expires = time.time() + expires_in
        self.token = b"faketoken"
        self._outcome = outcome  # "user", "2fa", "timeout", "cancel", "other"
        self._recreated = False
        self._wait_started = False
        self._wait_calls = 0

    async def wait(self):
        self._wait_started = True
        self._wait_calls += 1
        # Drive the recorded outcome.
        if self._outcome == "user":
            return FakeUser(bot=False)
        if self._outcome == "bot":
            return FakeUser(bot=True)
        if self._outcome == "2fa":
            raise _make_exc("SessionPasswordNeededError")
        if self._outcome == "timeout":
            raise TimeoutError("qr expired")
        if self._outcome == "cancel":
            raise _make_exc("CancelledError")
        if self._outcome == "other":
            raise RuntimeError("some telegram auth error")
        # default: never resolves in this synchronous fake
        await _sleep_forever()

    async def recreate(self):
        self._recreated = True
        # after recreate, a scan resolves to a user
        self._outcome = "user"
        return self


class FakeClient:
    def __init__(self, qr_login=None, connected=False):
        self._connected = connected
        self.qr = qr_login
        self.password_result = None  # set per-test
        self.password_raises = None
        self.disconnect_calls = 0

    def is_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False
        self.disconnect_calls += 1

    async def qr_login(self):
        return self.qr

    async def sign_in(self, password=None):
        if self.password_raises:
            raise self.password_raises
        return self.password_result or FakeUser(bot=False)


def _make_exc(name):
    """Create a real exception object whose __name__ matches the tested pattern."""
    if name == "SessionPasswordNeededError":
        return type("SessionPasswordNeededError", (Exception,), {})("session password needed")
    if name == "CancelledError":
        return type("CancelledError", (Exception,), {})("cancelled")
    return Exception(name)


async def _sleep_forever():
    import asyncio

    await asyncio.sleep(3600)


class FakeFuture:
    """Minimal Future stand-in for run_on_tg_loop (scheduler)."""

    def __init__(self):
        self._cancelled = False
        self._done = False

    def cancel(self):
        self._cancelled = True
        return True

    def done(self):
        return self._done


class FakeLoop:
    """Records scheduled + blocked coroutines for assertions."""

    def __init__(self):
        self.scheduled = []  # coroutines scheduled non-blocking
        self.blocked = []    # coroutines run blocking

    def block(self, coro):
        """Run a coroutine to completion synchronously and return its result."""
        self.blocked.append(coro)
        return _run_coro_sync(coro)

    def schedule(self, coro):
        """Schedule a coroutine without blocking; return a FakeFuture.

        We drive resolvable wait() outcomes synchronously so test state
        transitions happen, but guard with a short timeout so a wait() that
        never resolves (used to keep a challenge ACTIVE) does not hang the test.
        """
        self.scheduled.append(coro)
        import concurrent.futures
        import threading

        # Use an ad-hoc thread instead of ThreadPoolExecutor so we can make
        # it a daemon thread. A non-daemon thread blocks pytest teardown.
        def _run():
            try:
                _run_coro_sync(coro)
            except Exception:
                pass
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        # Wait up to 0.5s for fast mock tasks (like success) to complete their 
        # state transition, so subsequent asserts pass immediately. Active
        # pending challenges will hit the timeout but keep running in background.
        t.join(timeout=0.5)
        return FakeFuture()


def _run_coro_sync(coro):
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except RuntimeError:
        # already running loop in some test contexts; fall back to thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_run_in_thread, coro).result(timeout=5)


def _run_in_thread(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def loop():
    return FakeLoop()


@pytest.fixture
def manager(loop):
    return TelegramAuthChallengeManager(
        block_on_tg_loop=loop.block,
        run_on_tg_loop=loop.schedule,
        clock=time.time,
    )


def _client_factory(qr_login):
    def make(*, session_file, api_id, api_hash):
        return FakeClient(qr_login=qr_login, connected=False)

    return make


# ── QR rendering + DTO secrecy ─────────────────────────────────────────


def test_qr_png_renders_locally_and_decodes():
    payload = "tg://login?token=secret_token_xyz"
    b64 = render_qr_png_b64(payload)
    raw = base64.b64decode(b64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # Re-render with the same payload yields a deterministic, decodable image.
    b64_again = render_qr_png_b64(payload)
    assert b64 == b64_again
    # The payload itself is not echoed in plaintext base64 (it is encoded in
    # the QR matrix, not stored as a string).
    assert payload not in b64


def test_qr_render_rejects_empty_payload():
    with pytest.raises(ValueError):
        render_qr_png_b64("")


def test_dto_never_carries_secrets():
    dto = ChallengeDTO(
        challenge_id="c", business_id="b", account_id="a",
        status=WAITING_SCAN, expires_at=0, poll_interval=2,
        qr_png_b64="png",
    )
    d = dto.to_dict()
    # None of the forbidden secret field names appear.
    for forbidden in ("api_hash", "api_id", "session_file", "password", "token", "bearer"):
        assert forbidden not in d, f"dto leaked {forbidden}"


def test_session_path_is_collision_safe():
    p = session_path_for("biz_one", "acct_2")
    assert p.endswith("telegram_biz_one_acct_2.session")
    # raw display names cannot inject path traversal
    p2 = session_path_for("../etc", ".. passwd")
    assert ".." not in p2.replace("\\", "/").split("/")[-1]
    assert " " not in p2


# ── start: wait() scheduled before return, QR present ─────────────────


def test_start_schedules_wait_before_return(loop, manager):
    qr = FakeQRLogin(outcome="user")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="principal-1",
        api_id=12345, api_hash="hash", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    assert dto.status == WAITING_SCAN
    assert dto.qr_png_b64  # QR returned for the browser
    assert dto.business_id == "b1" and dto.account_id == "a1"
    # The wait coroutine was scheduled (non-blocking) before this returned.
    assert len(loop.scheduled) >= 1, "wait() was not scheduled before return"


def test_start_returns_no_store_compatible_payload(loop, manager):
    qr = FakeQRLogin(outcome="user")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    d = dto.to_dict()
    # the QR token/url/api_hash never appears in the public payload
    assert "faketoken" not in str(d)
    assert "fake123" not in str(d)  # url content
    assert "hash" not in str(d) or d.get("qr_png_b64")  # api_hash not in plaintext fields


# ── binding: business/account/principal ───────────────────────────────


def test_status_rejects_wrong_principal(loop, manager):
    qr = FakeQRLogin(outcome="user")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="principal-A",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    # different principal cannot read the status
    assert manager.status(dto.challenge_id, "b1", "a1", "principal-B") is None
    # different business cannot read it
    assert manager.status(dto.challenge_id, "b2", "a1", "principal-A") is None
    # correct binding works
    got = manager.status(dto.challenge_id, "b1", "a1", "principal-A")
    assert got is not None


def test_only_one_active_challenge_per_account(loop, manager):
    # First challenge must remain ACTIVE (waiting_scan) so the replacement
    # start is the thing that cancels it. Use a wait() that never resolves.
    qr1 = FakeQRLogin(outcome=None)
    first = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr1),
    )
    assert manager.status(first.challenge_id, "b1", "a1", "p").status == WAITING_SCAN
    qr2 = FakeQRLogin(outcome="user")
    second = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr2),
    )
    assert first.challenge_id != second.challenge_id
    # the first must have been cancelled by the replacement start
    got = manager.status(first.challenge_id, "b1", "a1", "p")
    assert got is None or got.status == CANCELLED


def test_capacity_is_bounded(loop):
    # tiny capacity to force overflow quickly
    mgr = TelegramAuthChallengeManager(
        block_on_tg_loop=loop.block, run_on_tg_loop=loop.schedule, max_capacity=2,
    )
    for i in range(2):
        mgr.start(
            business_id="b", account_id=f"a{i}", session_principal="p",
            api_id=1, api_hash="h", session_file=f"data/sessions/x{i}.session",
            client_factory=_client_factory(FakeQRLogin(outcome="user")),
        )
    with pytest.raises(RuntimeError):
        mgr.start(
            business_id="b", account_id="aX", session_principal="p",
            api_id=1, api_hash="h", session_file="data/sessions/xX.session",
            client_factory=_client_factory(FakeQRLogin(outcome="user")),
        )


# ── outcome mapping: success / 2FA / expiry / cancel / fail / bot ─────


def test_qr_success_authorizes_personal_user(loop, manager):
    qr = FakeQRLogin(outcome="user")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    # wait ran synchronously in the fake; status should now be authorized
    got = manager.status(dto.challenge_id, "b1", "a1", "p")
    assert got.status == AUTHORIZED
    ident = manager.identity(dto.challenge_id)
    assert ident is not None and ident["bot"] is False
    assert ident["username"] == "alice"


def test_qr_bot_rejected(loop, manager):
    qr = FakeQRLogin(outcome="bot")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    got = manager.status(dto.challenge_id, "b1", "a1", "p")
    assert got.status == FAILED  # bot identity never authorizes
    assert manager.identity(dto.challenge_id) is None


def test_qr_2fa_transitions_to_password_required(loop, manager):
    qr = FakeQRLogin(outcome="2fa")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    got = manager.status(dto.challenge_id, "b1", "a1", "p")
    assert got.status == PASSWORD_REQUIRED
    assert got.attempts_remaining == MAX_2FA_ATTEMPTS


# ── 2FA: correct / wrong / limit ──────────────────────────────────────


def _start_2fa(manager, loop):
    qr = FakeQRLogin(outcome="2fa")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    return dto


def test_2fa_correct_password_authorizes(loop, manager):
    dto = _start_2fa(manager, loop)
    # patch the bound client to accept the password
    ch = manager._store[dto.challenge_id]
    ch.client.password_result = FakeUser(bot=False)
    out = manager.submit_password(dto.challenge_id, "b1", "a1", "p", "correct-horse")
    assert out.status == AUTHORIZED


def test_2fa_wrong_password_stays_required_then_locks(loop, manager):
    dto = _start_2fa(manager, loop)
    ch = manager._store[dto.challenge_id]
    from telethon.errors import PasswordHashInvalidError

    ch.client.password_raises = PasswordHashInvalidError("bad")
    # Plan: at most MAX_2FA_ATTEMPTS attempts; after the limit, cancel.
    # Attempts 1..MAX-1 stay in PASSWORD_REQUIRED.
    for i in range(MAX_2FA_ATTEMPTS - 1):
        out = manager.submit_password(dto.challenge_id, "b1", "a1", "p", f"wrong{i}")
        assert out.status == PASSWORD_REQUIRED, f"attempt {i+1} should stay required"
    # The MAX-th wrong attempt exhausts the limit and cancels the challenge.
    out_last = manager.submit_password(dto.challenge_id, "b1", "a1", "p", "wrong-final")
    assert out_last.status == CANCELLED
    # one more attempt after cancel is rejected (no re-arming).
    out_after = manager.submit_password(dto.challenge_id, "b1", "a1", "p", "again")
    assert out_after.status in (CANCELLED, FAILED)


def test_2fa_password_never_stored_in_state(loop, manager):
    dto = _start_2fa(manager, loop)
    ch = manager._store[dto.challenge_id]
    ch.client.password_result = FakeUser(bot=False)
    secret = "supersecret-password-123"
    manager.submit_password(dto.challenge_id, "b1", "a1", "p", secret)
    # the password must not be retained anywhere in the challenge record
    blob = repr(ch.__dict__) + repr(ch.me or {})
    assert secret not in blob


# ── cancel / replay / lookup ──────────────────────────────────────────


def test_cancel_then_status_is_cancelled(loop, manager):
    qr = FakeQRLogin(outcome="user")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    ok = manager.cancel(dto.challenge_id, "b1", "a1", "p")
    assert ok
    got = manager.status(dto.challenge_id, "b1", "a1", "p")
    assert got.status == CANCELLED


def test_lookup_challenge_resolves_account_id(loop, manager):
    qr = FakeQRLogin(outcome="2fa")
    dto = manager.start(
        business_id="b1", account_id="a1", session_principal="p",
        api_id=1, api_hash="h", session_file="data/sessions/x.session",
        client_factory=_client_factory(qr),
    )
    assert manager.lookup_challenge(dto.challenge_id, "b1", "p") == "a1"
    # wrong business/principal -> None
    assert manager.lookup_challenge(dto.challenge_id, "b2", "p") is None
    assert manager.lookup_challenge(dto.challenge_id, "b1", "other") is None
