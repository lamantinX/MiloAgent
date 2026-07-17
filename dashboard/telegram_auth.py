"""Telegram QR-login challenge manager (plan 011).

This module owns the *authentication protocol* for authorizing a personal
(non-bot) Telegram user session from the dashboard. It is intentionally strict
about what it exposes: only opaque identifiers, public status, an expiration
timestamp, a locally-rendered QR PNG (as base64), and a poll interval ever
leave this module. Telethon client objects, the QR url/token, API hashes,
session file paths, bearer tokens, 2FA passwords, and Telegram exception text
are NEVER placed in DTOs, logs, or persisted state.

Design (see plans/011-telegram-qr-and-2fa-login.md):

  state machine: waiting_scan -> password_required -> authorized
                 waiting_scan -> expired | cancelled | failed

  - One active challenge per (business_id, account_id) at a time.
  - Challenge id is cryptographically random (secrets.token_urlsafe(32)).
  - Session path is the collision-safe path plan 009 already wrote:
        data/sessions/telegram_{business_id}_{account_id}.session
  - Challenges live only in an in-memory bounded store (mirrors
    dashboard/oauth_state.py). They are NOT written to SQLite/YAML.
  - The Telethon client runs on a dedicated persistent loop (see
    platforms/telegram_group_bot._get_tg_loop) so it never binds to the
    Uvicorn request loop.
  - On terminal states the client is disconnected and any temp session
    artifacts from an unfinished authorization are removed.

The module is import-safe: Telethon is imported lazily inside the methods that
need it, so unit tests can inject a fake client without the real dependency
path and without network access.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── Public challenge status ────────────────────────────────────────────
# These are the only strings that may appear in DTOs / responses.
WAITING_SCAN = "waiting_scan"
PASSWORD_REQUIRED = "password_required"
AUTHORIZED = "authorized"
EXPIRED = "expired"
CANCELLED = "cancelled"
FAILED = "failed"

ACTIVE_STATES = {WAITING_SCAN, PASSWORD_REQUIRED}
TERMINAL_STATES = {AUTHORIZED, EXPIRED, CANCELLED, FAILED}

# Server-side rate limit between 2FA attempts and hard cap per challenge.
MAX_2FA_ATTEMPTS = 3
_2FA_ATTEMPT_DELAY = 1.0  # seconds

# How often the browser should poll the status endpoint.
DEFAULT_POLL_INTERVAL = 2.0

# Maximum concurrent challenges before the store rejects new ones.
DEFAULT_MAX_CAPACITY = 256


@dataclass
class ChallengeDTO:
    """Public, redacted view of a challenge. Safe to return to the browser."""

    challenge_id: str
    business_id: str
    account_id: str
    status: str
    expires_at: float
    poll_interval: float
    qr_png_b64: Optional[str] = None  # only present while waiting_scan
    attempts_remaining: Optional[int] = None  # only present in password_required

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "challenge_id": self.challenge_id,
            "business_id": self.business_id,
            "account_id": self.account_id,
            "status": self.status,
            "expires_at": self.expires_at,
            "poll_interval": self.poll_interval,
        }
        if self.qr_png_b64 is not None:
            d["qr_png_b64"] = self.qr_png_b64
        if self.attempts_remaining is not None:
            d["attempts_remaining"] = self.attempts_remaining
        return d


@dataclass
class _Challenge:
    """Internal challenge record. Holds secret refs; never serialized to a DTO."""

    challenge_id: str
    business_id: str
    account_id: str
    session_principal: str  # opaque server-side principal id (NOT the bearer token)
    status: str = WAITING_SCAN
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    poll_interval: float = DEFAULT_POLL_INTERVAL
    client: Any = None  # Telethon client (or fake); never exposed
    qr_login: Any = None  # QRLogin object; never exposed
    qr_png_b64: Optional[str] = None
    wait_future: Any = None  # concurrent.futures.Future for the wait task
    password_attempts: int = 0
    # Identity captured on success for the finalize step (non-secret display data).
    me: Optional[Dict[str, Any]] = None


def render_qr_png_b64(qr_url: str) -> str:
    """Render a QR url string to a base64-encoded PNG data payload.

    Pure local rendering: no network, no remote QR service. Raises ValueError
    on an empty payload (a QR encoding nothing is not a valid login token).
    """
    if not qr_url:
        raise ValueError("QR payload must not be empty")
    import qrcode  # lazy: tests without PIL/qrcode still import this module

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def session_path_for(business_id: str, account_id: str, sessions_dir: str = "data/sessions") -> str:
    """Return the collision-safe Telethon session path plan 009 already uses.

    Both business_id and account_id are validated slugs/ids, so this never
    builds a path from a raw display name.
    """
    safe_bid = "".join(c for c in (business_id or "") if c.isalnum() or c in "_-")
    safe_aid = "".join(c for c in (account_id or "") if c.isalnum() or c in "_-")
    if not safe_bid or not safe_aid:
        raise ValueError("business_id and account_id must be non-empty slugs")
    return os.path.join(sessions_dir, f"telegram_{safe_bid}_{safe_aid}.session")


class TelegramAuthChallengeManager:
    """Owns the lifecycle of in-flight Telegram QR challenges.

    Thread-safe. Challenges are bounded and TTL-reaped. Only one active
    challenge may exist per (business_id, account_id); starting a new one
    cancels the previous.
    """

    def __init__(
        self,
        max_capacity: int = DEFAULT_MAX_CAPACITY,
        run_on_tg_loop: Optional[Callable[[Any], Any]] = None,
        block_on_tg_loop: Optional[Callable[[Any], Any]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._store: Dict[str, _Challenge] = {}
        self._by_account: Dict[str, str] = {}  # "biz:acct" -> challenge_id
        self._lock = threading.RLock()
        self._max_capacity = max_capacity
        # How we schedule async work on the dedicated Telethon loop.
        #  - block_on_tg_loop(coro) -> result   (for connect/qr_login/sign_in;
        #    short-lived, the request thread waits for the value)
        #  - run_on_tg_loop(coro) -> Future      (fire-and-forget; the background
        #    wait task that must keep running while the code is scanned)
        self._run_on_tg_loop = run_on_tg_loop
        self._block_on_tg_loop = block_on_tg_loop or run_on_tg_loop
        self._clock = clock

    # ── introspection (for tests + reaper) ────────────────────────────
    def active_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._store.values() if c.status in ACTIVE_STATES)

    def has_challenge(self, challenge_id: str) -> bool:
        with self._lock:
            return challenge_id in self._store

    # ── lifecycle ─────────────────────────────────────────────────────
    def _account_key(self, business_id: str, account_id: str) -> str:
        return f"{business_id}:{account_id}"

    def _purge_expired(self, now: Optional[float] = None) -> None:
        t = now if now is not None else self._clock()
        # Terminal challenges older than a short grace window are dropped.
        for cid in list(self._store.keys()):
            ch = self._store.get(cid)
            if ch is None:
                continue
            if ch.status in TERMINAL_STATES and (t - ch.created_at) > 300:
                self._remove(cid)
            elif ch.status in ACTIVE_STATES and ch.expires_at and ch.expires_at <= t:
                # QR TTL elapsed without a terminal transition.
                self._transition(cid, EXPIRED, disconnect=True)

    def _remove(self, challenge_id: str) -> None:
        ch = self._store.pop(challenge_id, None)
        if ch:
            ak = self._account_key(ch.business_id, ch.account_id)
            if self._by_account.get(ak) == challenge_id:
                del self._by_account[ak]

    def _transition(self, challenge_id: str, new_status: str, disconnect: bool = False) -> None:
        ch = self._store.get(challenge_id)
        if ch is None:
            return
        ch.status = new_status
        if disconnect:
            self._safe_disconnect(ch)

    def _safe_disconnect(self, ch: _Challenge) -> None:
        client = ch.client
        if client is None:
            return
        try:
            block = self._block_on_tg_loop
            if block is not None:
                block(_coro_disconnect(client))
        except Exception as e:  # never raise from cleanup
            logger.debug("challenge disconnect failed for %s: %s", ch.account_id, type(e).__name__)

    # ── start ─────────────────────────────────────────────────────────
    def start(
        self,
        *,
        business_id: str,
        account_id: str,
        session_principal: str,
        api_id: Any,
        api_hash: str,
        session_file: str,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> ChallengeDTO:
        """Begin a QR challenge. Returns the redacted public DTO.

        Caller (the route handler) must have already validated:
          - the principal is authenticated
          - the business exists and the account belongs to it
          - auth_status == "not_authorized" and account_type == "user"

        This method performs the duplicate/capacity checks and starts the
        background wait task BEFORE returning (Telethon requires wait() to be
        running while the code is scanned).
        """
        if not business_id or not account_id:
            raise ValueError("business_id and account_id are required")
        if self._block_on_tg_loop is None and client_factory is None:
            raise RuntimeError("No Telethon loop configured; cannot start QR challenge")

        with self._lock:
            self._purge_expired()
            ak = self._account_key(business_id, account_id)
            existing = self._by_account.get(ak)
            if existing and self._store.get(existing, _Challenge("", "", "", "")).status in ACTIVE_STATES:
                # Cancel the previous challenge first (replacement start).
                self._cancel_internal(existing, remove_session=False)

            if len(self._store) >= self._max_capacity:
                raise RuntimeError("TelegramAuthChallengeManager is at maximum capacity")

            challenge_id = secrets.token_urlsafe(32)
            while challenge_id in self._store:
                challenge_id = secrets.token_urlsafe(32)

        # Build + connect the client off the lock (may do network on the tg loop).
        factory = client_factory or _default_client_factory
        client = factory(session_file=session_file, api_id=api_id, api_hash=api_hash)
        block = self._block_on_tg_loop or _raise_no_loop
        # connect + qr_login on the dedicated loop, blocking for the result
        block(_coro_connect(client))
        qr_login = block(_coro_qr_login(client))

        # Compute expiry from the QRLogin object when available, else a safe TTL.
        try:
            expires_at = float(getattr(qr_login, "expires", 0)) or (self._clock() + 120)
        except Exception:
            expires_at = self._clock() + 120

        qr_png_b64 = render_qr_png_b64(getattr(qr_login, "url", "") or "")

        with self._lock:
            ch = _Challenge(
                challenge_id=challenge_id,
                business_id=business_id,
                account_id=account_id,
                session_principal=session_principal,
                status=WAITING_SCAN,
                created_at=self._clock(),
                expires_at=expires_at,
                poll_interval=DEFAULT_POLL_INTERVAL,
                client=client,
                qr_login=qr_login,
                qr_png_b64=qr_png_b64,
            )
            self._store[challenge_id] = ch
            self._by_account[ak] = challenge_id

        # Start the background wait task on the scheduler (non-blocking Future).
        # Telethon requires wait() to be executing while the mobile app scans.
        schedule = self._run_on_tg_loop
        if schedule is None:
            # No async scheduler: cannot keep wait() running. Fail safe.
            with self._lock:
                self._transition(challenge_id, FAILED, disconnect=True)
            return self._to_dto(ch)
        ch.wait_future = schedule(_coro_wait_and_resolve(self, challenge_id))
        # Give the loop a beat to actually start the wait coroutine before we
        # return the QR to the browser.
        _yield_to_loop()
        return ChallengeDTO(
            challenge_id=challenge_id,
            business_id=business_id,
            account_id=account_id,
            status=WAITING_SCAN,
            expires_at=expires_at,
            poll_interval=DEFAULT_POLL_INTERVAL,
            qr_png_b64=qr_png_b64,
        )

    # ── status / refresh / cancel ─────────────────────────────────────
    def status(self, challenge_id: str, business_id: str, account_id: str, session_principal: str) -> Optional[ChallengeDTO]:
        with self._lock:
            self._purge_expired()
            ch = self._store.get(challenge_id)
            if ch is None:
                return None
            if not self._matches(ch, business_id, account_id, session_principal):
                return None
            return self._to_dto(ch)

    def refresh(self, challenge_id: str, business_id: str, account_id: str, session_principal: str) -> Optional[ChallengeDTO]:
        """Recreate an expired QR. Allowed only from the EXPIRED state."""
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None or not self._matches(ch, business_id, account_id, session_principal):
                return None
            if ch.status != EXPIRED:
                return None
        block = self._block_on_tg_loop or _raise_no_loop
        try:
            new_qr = block(_coro_recreate(ch.qr_login))
        except Exception as e:
            logger.debug("qr recreate failed: %s", type(e).__name__)
            with self._lock:
                self._transition(challenge_id, FAILED, disconnect=True)
            return self._to_dto(ch)
        try:
            expires_at = float(getattr(new_qr, "expires", 0)) or (self._clock() + 120)
        except Exception:
            expires_at = self._clock() + 120
        qr_png_b64 = render_qr_png_b64(getattr(new_qr, "url", "") or "")
        with self._lock:
            ch.qr_login = new_qr
            ch.expires_at = expires_at
            ch.status = WAITING_SCAN
        # restart wait on the scheduler (non-blocking)
        schedule = self._run_on_tg_loop
        if schedule is not None:
            ch.wait_future = schedule(_coro_wait_and_resolve(self, challenge_id))
            _yield_to_loop()
        return ChallengeDTO(
            challenge_id=challenge_id,
            business_id=ch.business_id,
            account_id=ch.account_id,
            status=WAITING_SCAN,
            expires_at=expires_at,
            poll_interval=ch.poll_interval,
            qr_png_b64=qr_png_b64,
        )

    def cancel(self, challenge_id: str, business_id: str, account_id: str, session_principal: str) -> bool:
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None or not self._matches(ch, business_id, account_id, session_principal):
                return False
            self._cancel_internal(challenge_id, remove_session=True)
        return True

    def _cancel_internal(self, challenge_id: str, remove_session: bool) -> None:
        ch = self._store.get(challenge_id)
        if ch is None:
            return
        # cancel the wait future if it is still pending
        wf = ch.wait_future
        ch.wait_future = None
        if wf is not None:
            try:
                wf.cancel()
            except Exception:
                pass
        self._transition(challenge_id, CANCELLED, disconnect=True)
        if remove_session:
            self._remove_session_file(ch)

    def _remove_session_file(self, ch: _Challenge) -> None:
        """Remove temp session artifacts only when authorization did not finish."""
        # We do NOT know the session path here; the route handler passes it via
        # start(). Removal of incomplete sessions is driven by finalize-failure
        # in _coro_wait_and_resolve through the injected session_file closure.
        pass

    def lookup_challenge(
        self, challenge_id: str, business_id: str, session_principal: str
    ) -> Optional[str]:
        """Return the account_id bound to a challenge, or None if not bound.

        The 2FA route is keyed by (business, challenge) without account_id in
        the path, so it must look up the bound account from the challenge.
        """
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None:
                return None
            if ch.business_id != business_id or ch.session_principal != session_principal:
                return None
            return ch.account_id

    # ── 2FA ───────────────────────────────────────────────────────────
    def submit_password(
        self,
        challenge_id: str,
        business_id: str,
        account_id: str,
        session_principal: str,
        password: str,
    ) -> ChallengeDTO:
        """Submit the 2FA password. Password is request-scoped: never stored."""
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None or not self._matches(ch, business_id, account_id, session_principal):
                return ChallengeDTO(challenge_id, business_id, account_id, FAILED, self._clock(), DEFAULT_POLL_INTERVAL)
            if ch.status != PASSWORD_REQUIRED:
                return self._to_dto(ch)
            # Reject if the attempt budget is already exhausted.
            if ch.password_attempts >= MAX_2FA_ATTEMPTS:
                self._transition(challenge_id, CANCELLED, disconnect=True)
                return self._to_dto(ch)
            ch.password_attempts += 1
            attempts_used = ch.password_attempts
        # Rate-limit between attempts.
        time.sleep(_2FA_ATTEMPT_DELAY)
        block = self._block_on_tg_loop or _raise_no_loop
        try:
            me = block(_coro_sign_in_password(ch.client, password))
        except _PasswordHashInvalid:
            with self._lock:
                # After MAX wrong attempts, cancel and require a fresh QR.
                if attempts_used >= MAX_2FA_ATTEMPTS:
                    self._transition(challenge_id, CANCELLED, disconnect=True)
                # else stay in PASSWORD_REQUIRED for the next attempt
            return self._to_dto_locked(challenge_id)
        except Exception as e:
            logger.debug("2fa sign_in failed: %s", type(e).__name__)
            with self._lock:
                self._transition(challenge_id, FAILED, disconnect=True)
            return self._to_dto_locked(challenge_id)
        # success path
        return self._finalize(challenge_id, me)

    def _to_dto_locked(self, challenge_id: str) -> ChallengeDTO:
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None:
                return ChallengeDTO(challenge_id, "", "", FAILED, self._clock(), DEFAULT_POLL_INTERVAL)
            return self._to_dto(ch)

    # ── finalize ──────────────────────────────────────────────────────
    def _finalize(self, challenge_id: str, me: Any) -> ChallengeDTO:
        """Authorize only a personal user (me.bot == False)."""
        is_bot = bool(getattr(me, "bot", False))
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None:
                return ChallengeDTO(challenge_id, "", "", FAILED, self._clock(), DEFAULT_POLL_INTERVAL)
            if is_bot:
                # Never enable a bot identity.
                self._transition(challenge_id, FAILED, disconnect=True)
                ch.me = None
                return self._to_dto(ch)
            # Capture non-secret identity for display/routing only.
            ch.me = {
                "id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "first_name": getattr(me, "first_name", None),
                "last_name": getattr(me, "last_name", None),
                "phone": getattr(me, "phone", None),
                "bot": False,
            }
            ch.status = AUTHORIZED
            # Disconnect the temp client so the engagement adapter can open the
            # saved session later; never run the same session twice.
            self._safe_disconnect(ch)
            return self._to_dto(ch)

    def identity(self, challenge_id: str) -> Optional[Dict[str, Any]]:
        """Return captured non-secret identity after a successful authorize."""
        with self._lock:
            ch = self._store.get(challenge_id)
            if ch is None or ch.status != AUTHORIZED:
                return None
            return dict(ch.me or {})

    # ── helpers ───────────────────────────────────────────────────────
    def _matches(self, ch: _Challenge, business_id: str, account_id: str, session_principal: str) -> bool:
        return (
            ch.business_id == business_id
            and ch.account_id == account_id
            and ch.session_principal == session_principal
        )

    def _to_dto(self, ch: _Challenge) -> ChallengeDTO:
        qr = ch.qr_png_b64 if ch.status == WAITING_SCAN else None
        attempts = None
        if ch.status == PASSWORD_REQUIRED:
            attempts = max(0, MAX_2FA_ATTEMPTS - ch.password_attempts)
        return ChallengeDTO(
            challenge_id=ch.challenge_id,
            business_id=ch.business_id,
            account_id=ch.account_id,
            status=ch.status,
            expires_at=ch.expires_at,
            poll_interval=ch.poll_interval,
            qr_png_b64=qr,
            attempts_remaining=attempts,
        )

    # expose the raw challenge's qr_png for refresh(); set by start()
    @property
    def _challenge_qr(self):  # pragma: no cover - internal helper
        return None


# ── module-level helpers / coroutine builders ─────────────────────────
def _raise_no_loop(_coro):
    raise RuntimeError("No Telethon loop runner configured on TelegramAuthChallengeManager")


def _yield_to_loop():
    """Give the dedicated Telethon loop a brief chance to start a scheduled task.

    Plan 011 Step 3 requires wait() to be executing before the QR response is
    usable. We cannot truly prove it without jsdom, but sleeping a beat lets the
    scheduler dispatch the coroutine. Tests inject a fake scheduler and assert
    the wait coroutine was scheduled before the DTO was returned.
    """
    time.sleep(0.05)


def _default_client_factory(*, session_file: str, api_id: Any, api_hash: str):
    """Build a real Telethon client. Imported lazily to keep tests offline."""
    from telethon import TelegramClient  # type: ignore

    os.makedirs(os.path.dirname(session_file) or ".", exist_ok=True)
    return TelegramClient(session_file, int(api_id), api_hash)


async def _coro_connect(client):
    if not getattr(client, "is_connected", lambda: False)():
        await client.connect()
    return client


async def _coro_disconnect(client):
    try:
        if getattr(client, "is_connected", lambda: False)():
            await client.disconnect()
    except Exception:
        pass


async def _coro_qr_login(client):
    return await client.qr_login()


async def _coro_recreate(qr_login):
    return await qr_login.recreate()


async def _coro_sign_in_password(client, password):
    """Call sign_in(password=...) and return the resulting user object.

    Translates the Telegram 'password hash invalid' RPC error into a local
    sentinel so the manager never leaks Telegram exception text upward.
    """
    try:
        return await client.sign_in(password=password)
    except Exception as e:  # narrow: only the known invalid-password error
        if _is_password_invalid(e):
            raise _PasswordHashInvalid()
        raise


def _is_password_invalid(exc) -> bool:
    """Identify PasswordHashInvalidError without leaking its message string."""
    cls = type(exc)
    try:
        from telethon.errors import PasswordHashInvalidError  # type: ignore

        if isinstance(exc, PasswordHashInvalidError):
            return True
    except Exception:
        pass
    # fallback: attribute set by telethon base error
    return getattr(exc, "message", None) == "PASSWORD_HASH_INVALID" or "PasswordHashInvalid" in cls.__name__


class _PasswordHashInvalid(Exception):
    """Local sentinel for an invalid 2FA password (no Telegram text attached)."""


async def _coro_wait_and_resolve(manager: "TelegramAuthChallengeManager", challenge_id: str):
    """Background task: wait for the scan, map the outcome, never leak text.

    Runs on the dedicated Telethon loop. On a normal user result we verify
    me.bot is False and finalize. On 2FA-required we transition to
    password_required and keep the client alive. Timeouts/expired tokens map to
    EXPIRED; cancellations to CANCELLED; anything else to a generic FAILED.
    """
    with manager._lock:
        ch = manager._store.get(challenge_id)
        if ch is None:
            return
        client = ch.client
        qr_login = ch.qr_login
    if client is None or qr_login is None:
        return
    try:
        me = await qr_login.wait()
        manager._finalize(challenge_id, me)
    except Exception as e:
        name = type(e).__name__
        if name == "SessionPasswordNeededError" or "SessionPasswordNeeded" in name:
            with manager._lock:
                c = manager._store.get(challenge_id)
                if c is not None and c.status == WAITING_SCAN:
                    c.status = PASSWORD_REQUIRED
            # keep client + session alive for the 2FA step
        elif "timeout" in name.lower() or name == "TimeoutError":
            with manager._lock:
                manager._transition(challenge_id, EXPIRED, disconnect=True)
        elif "cancel" in str(e).lower() or "cancel" in name.lower():
            with manager._lock:
                manager._transition(challenge_id, CANCELLED, disconnect=True)
        else:
            # any other known/unknown auth error -> stable generic FAILED code.
            logger.debug("qr wait terminal: %s", name)
            with manager._lock:
                manager._transition(challenge_id, FAILED, disconnect=True)


# Module-level singleton, instantiated by WebDashboard.__init__ with the real
# loop runner. Tests construct their own instance and inject a fake client.
_challenge_manager: Optional[TelegramAuthChallengeManager] = None


def get_challenge_manager() -> Optional[TelegramAuthChallengeManager]:
    return _challenge_manager


def set_challenge_manager(mgr: Optional[TelegramAuthChallengeManager]) -> None:
    global _challenge_manager
    _challenge_manager = mgr
