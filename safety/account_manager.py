"""Account rotation and health management."""

import os
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import yaml

from core.database import Database

logger = logging.getLogger(__name__)


class AccountManager:
    """Manages account rotation and health tracking.

    - Rotates between multiple accounts per platform
    - Tracks account health status in database
    - Puts accounts on cooldown after errors
    - Selects least-recently-used healthy account
    """

    # Account statuses
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    WARNED = "warned"
    BANNED = "banned"

    # Karma-based tier system — accounts unlock more capacity as they warm up
    # Tier 0: new account  (<10 karma)  — 3 write actions/day, comments only
    # Tier 1: growing      (10-50)      — 7 write actions/day
    # Tier 2: established  (50-200)     — 12 write actions/day
    # Tier 3: veteran      (200+)       — 20 write actions/day, top priority
    KARMA_TIERS = [
        #  (min_karma, tier_name,    daily_cap, can_post)
        (200, "veteran",      20, True),
        ( 50, "established",  12, True),
        ( 10, "growing",       7, True),
        (  0, "new",           3, False),   # comment-only for new accounts
    ]
    MIN_KARMA_WRITE = -5    # Below this: skip entirely (shadowbanned risk)
    KARMA_CACHE_TTL = 43200  # 12 hours in seconds

    def __init__(self, db: Database, config_dir: str = "config/"):
        self.db = db
        self.config_dir = config_dir
        self._lock = threading.RLock()  # Protects _cooldowns, _statuses, _last_used
        self._cooldowns: Dict[str, datetime] = {}  # "platform:account" -> expires_at
        self._statuses: Dict[str, str] = {}
        self._last_used: Dict[str, str] = {}  # platform -> last used username
        self._rotation_index: Dict[str, int] = {}  # platform -> index for true round-robin
        # Karma cache: username -> (total_karma, fetched_at_timestamp)
        self._karma_cache: Dict[str, tuple] = {}
        # Hot-reload support
        self._file_mtimes: Dict[str, float] = {}
        self._on_reload_callbacks: List[Callable] = []
        self._watching = False
        self._watcher_thread: Optional[threading.Thread] = None
        self._snapshot_mtimes()
        self._restore_state_from_db()

    def _restore_state_from_db(self):
        """Restore cooldown/health state from account_health table on startup."""
        try:
            rows = self.db.conn.execute(
                """SELECT platform, account, status, timestamp
                   FROM account_health
                   WHERE id IN (
                       SELECT MAX(id) FROM account_health
                       GROUP BY platform, account
                   )"""
            ).fetchall()
            for row in rows:
                key = f"{row['platform']}:{row['account']}"
                status = row["status"]
                if status == self.COOLDOWN:
                    # Restore cooldown with platform-appropriate duration
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                        # Use shorter cooldown for Telegram (usually 3-5min)
                        platform = row["platform"]
                        if platform == "telegram":
                            cooldown_min = 5
                        else:
                            cooldown_min = 15
                        expires = ts + timedelta(minutes=cooldown_min)
                        if expires > datetime.utcnow():
                            self._cooldowns[key] = expires
                            self._statuses[key] = self.COOLDOWN
                        else:
                            self._statuses[key] = self.HEALTHY
                    except Exception:
                        self._statuses[key] = self.HEALTHY
                elif status == self.BANNED:
                    self._statuses[key] = self.BANNED
                elif status == self.WARNED:
                    self._statuses[key] = self.WARNED
                else:
                    self._statuses[key] = self.HEALTHY
            logger.debug(f"Restored account state: {len(rows)} entries")
        except Exception as e:
            logger.debug(f"Could not restore account state: {e}")

    # ── Hot-Reload Support ─────────────────────────────────────────

    def _snapshot_mtimes(self):
        """Record current mtimes of account YAML files."""
        for platform in ("reddit", "twitter", "telegram"):
            fname = (
                "telegram_user_accounts.yaml"
                if platform == "telegram"
                else f"{platform}_accounts.yaml"
            )
            path = os.path.join(self.config_dir, fname)
            try:
                self._file_mtimes[path] = os.path.getmtime(path)
            except FileNotFoundError:
                pass

    def reload(self):
        """Re-read account configs from disk and notify callbacks."""
        self._cleanup_expired()
        self._snapshot_mtimes()
        logger.info("Account configs reloaded from disk")
        for cb in self._on_reload_callbacks:
            try:
                cb()
            except Exception as e:
                logger.error(f"Account reload callback error: {e}")

    def start_watching(self, interval: float = 10.0):
        """Start polling account YAML files for changes."""
        if self._watching:
            return
        self._watching = True
        self._watcher_thread = threading.Thread(
            target=self._watch_loop, args=(interval,), daemon=True
        )
        self._watcher_thread.start()
        logger.debug(f"Account file watcher started (interval={interval}s)")

    def stop_watching(self):
        """Stop the file watcher thread."""
        self._watching = False

    def _watch_loop(self, interval: float):
        """Poll account YAML files for mtime changes."""
        while self._watching:
            time.sleep(interval)
            try:
                for path, old_mtime in list(self._file_mtimes.items()):
                    try:
                        current = os.path.getmtime(path)
                        if current != old_mtime:
                            logger.info(f"Account config changed: {path}")
                            self.reload()
                            break
                    except FileNotFoundError:
                        pass
            except Exception as e:
                logger.error(f"Account watcher error: {e}")

    def on_reload(self, callback: Callable):
        """Register a callback for account config changes."""
        self._on_reload_callbacks.append(callback)

    # ── Account Loading ────────────────────────────────────────────

    def load_accounts(self, platform: str) -> List[Dict]:
        """Load accounts for a platform from config.

        Prefers .local.yaml override (gitignored) so git pull never
        overwrites real credentials on the server.
        """
        if platform == "reddit":
            path = f"{self.config_dir}/reddit_accounts.yaml"
        elif platform == "twitter":
            path = f"{self.config_dir}/twitter_accounts.yaml"
        elif platform == "telegram":
            path = f"{self.config_dir}/telegram_user_accounts.yaml"
        else:
            return []

        # Check for .local.yaml override
        if path.endswith(".yaml"):
            local_path = path[:-5] + ".local.yaml"
            if os.path.exists(local_path):
                path = local_path

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            accounts = data.get("accounts", [])
            result = []
            for a in accounts:
                if not a.get("enabled", True):
                    continue
                identifier = a.get("username") or a.get("phone", "")
                if identifier.startswith("YOUR_") or identifier.startswith("your_"):
                    continue
                # Skip obvious placeholder accounts
                if identifier in ("your_reddit_username", "your_twitter_username"):
                    continue
                # Normalize: ensure 'username' key is set for telegram accounts
                if platform == "telegram" and not a.get("username"):
                    a["username"] = a.get("phone", "unknown")
                result.append(a)
            return result
        except FileNotFoundError:
            logger.warning(f"Config file not found: {path}")
            return []

    def _cleanup_expired(self):
        """Remove expired cooldown entries to prevent unbounded dict growth."""
        now = datetime.utcnow()
        expired = [k for k, v in self._cooldowns.items() if now >= v]
        for key in expired:
            del self._cooldowns[key]
            if self._statuses.get(key) == self.COOLDOWN:
                self._statuses[key] = self.HEALTHY

    def get_next_account(self, platform: str, project: Optional[str] = None) -> Optional[Dict]:
        """Get the next available (healthy, not on cooldown) account.

        Uses round-robin rotation: never picks the same account twice in a row,
        then falls back to LRU (fewest actions in 4h) as tiebreaker.
        Thread-safe via self._lock.

        If project is specified, prefers accounts assigned to that project.
        Falls back to all accounts if none are assigned to the project.
        """
        accounts = self.load_accounts(platform)
        if not accounts:
            return None

        with self._lock:
            # Periodically clean expired cooldowns
            self._cleanup_expired()

            # Filter out cooldown/banned accounts
            available = []
            for acc in accounts:
                key = f"{platform}:{acc['username']}"
                status = self._statuses.get(key, self.HEALTHY)

                if status == self.BANNED:
                    continue

                if key in self._cooldowns:
                    if datetime.utcnow() < self._cooldowns[key]:
                        continue
                    else:
                        del self._cooldowns[key]
                        self._statuses[key] = self.HEALTHY

                available.append(acc)

            if not available:
                logger.warning(f"No available {platform} accounts")
                return None

            # Filter by project if specified -- only consider accounts assigned to this project
            if project:
                project_accounts = [
                    a for a in available
                    if project in (a.get("assigned_projects") or [])
                ]
                if project_accounts:
                    available = project_accounts
                # else fall back to all available accounts

            # Karma gate: for write operations, prefer accounts with sufficient karma
            # (Reddit CAPTCHAs low-karma accounts aggressively for write ops)
            if platform == "reddit":
                total_before = len(available)
                karma_ok = [a for a in available if self.is_karma_sufficient(a["username"])]
                if karma_ok:
                    available = karma_ok
                    if len(karma_ok) < total_before:
                        logger.debug(f"Karma gate: {len(karma_ok)}/{total_before} accounts passed (karma>={self.MIN_KARMA_WRITE})")
                # else: all unknown karma, proceed (don't block if cache empty)

            # True round-robin: rotate through ALL available accounts fairly
            # Step 1: advance the rotation index
            idx = self._rotation_index.get(platform, -1) + 1
            if idx >= len(available):
                idx = 0
            self._rotation_index[platform] = idx

            # Step 2: pick account at current index
            best = available[idx]

            # Step 3: if this account has way more actions than others (4h),
            # prefer the least-used one instead (fairness correction)
            if len(available) > 1:
                counts = []
                for acc in available:
                    c = self.db.get_action_count(
                        hours=4, account=acc["username"], platform=platform
                    )
                    counts.append((c, acc))
                counts.sort(key=lambda x: x[0])
                min_count = counts[0][0]
                best_count = self.db.get_action_count(
                    hours=4, account=best["username"], platform=platform
                )
                # If selected account has 3+ more actions than the least-used,
                # switch to the least-used instead
                if best_count > min_count + 2:
                    best = counts[0][1]
                    # Update index to match the corrected pick
                    for i, acc in enumerate(available):
                        if acc["username"] == best["username"]:
                            self._rotation_index[platform] = i
                            break

            if best:
                self._last_used[platform] = best["username"]
                logger.debug(
                    f"Account rotation [{platform}]: picked {best['username']} "
                    f"(index {self._rotation_index.get(platform, 0)}/{len(available)})"
                )

        return best

    def get_account_by_username(self, platform: str, username: str) -> Optional[Dict]:
        """Get a specific account by username (if healthy and not on cooldown)."""
        accounts = self.load_accounts(platform)
        for acc in accounts:
            if acc.get("username", "").lower() == username.lower():
                key = f"{platform}:{acc.get('username', username)}"
                status = self._statuses.get(key, self.HEALTHY)
                if status == self.BANNED:
                    logger.warning(f"Account {username} is banned — skipping")
                    return None
                if key in self._cooldowns and datetime.utcnow() < self._cooldowns[key]:
                    logger.warning(f"Account {username} is on cooldown — skipping")
                    return None
                return acc
        return None

    def update_karma_cache(self, username: str, karma: int):
        """Store fresh karma value for an account."""
        self._karma_cache[username] = (karma, time.time())
        logger.debug(f"Karma cache updated: {username} = {karma}")

    def get_cached_karma(self, username: str) -> Optional[int]:
        """Return cached karma if fresh (< 12h), else None."""
        entry = self._karma_cache.get(username)
        if entry and (time.time() - entry[1]) < self.KARMA_CACHE_TTL:
            return entry[0]
        return None

    def is_karma_sufficient(self, username: str) -> bool:
        """Check if account has enough karma for write operations.

        Returns True if karma is sufficient OR if karma is unknown (cache miss).
        Unknown karma = don't block, let the action proceed and learn from result.
        """
        karma = self.get_cached_karma(username)
        if karma is None:
            return True  # Unknown karma: don't block
        return karma >= self.MIN_KARMA_WRITE

    def get_account_tier(self, username: str) -> dict:
        """Return tier info dict for an account based on cached karma.

        Returns dict with keys: tier (int 0-3), name, daily_cap, can_post, karma.
        Defaults to tier 0 (most conservative) when karma unknown.
        """
        karma = self.get_cached_karma(username)
        if karma is None:
            # Unknown karma -- use conservative tier 0 defaults but allow actions
            return {"tier": 0, "name": "new", "daily_cap": 3, "can_post": False, "karma": None}
        for min_k, tier_name, daily_cap, can_post in self.KARMA_TIERS:
            if karma >= min_k:
                tier_num = self.KARMA_TIERS.index((min_k, tier_name, daily_cap, can_post))
                return {
                    "tier": len(self.KARMA_TIERS) - 1 - tier_num,
                    "name": tier_name,
                    "daily_cap": daily_cap,
                    "can_post": can_post,
                    "karma": karma,
                }
        # Fallback (shouldn't happen)
        return {"tier": 0, "name": "new", "daily_cap": 3, "can_post": False, "karma": karma}

    def get_daily_cap(self, username: str) -> int:
        """Return the write-action daily cap for this account based on karma tier."""
        return self.get_account_tier(username)["daily_cap"]

    def can_post(self, username: str) -> bool:
        """Return True if account karma tier allows posting (not just commenting)."""
        return self.get_account_tier(username)["can_post"]

    def mark_cooldown(
        self,
        platform: str,
        account: str,
        minutes: int = 30,
    ):
        """Put an account on cooldown."""
        key = f"{platform}:{account}"
        with self._lock:
            self._cooldowns[key] = datetime.utcnow() + timedelta(minutes=minutes)
            self._statuses[key] = self.COOLDOWN
        self.db.update_account_health(
            platform, account, self.COOLDOWN,
            notes=f"Cooldown for {minutes}min",
        )
        logger.info(f"Account {account} on {platform}: cooldown {minutes}min")

    def mark_warned(self, platform: str, account: str, reason: str):
        """Mark account as warned (suspicious activity detected)."""
        key = f"{platform}:{account}"
        with self._lock:
            self._statuses[key] = self.WARNED
        self.db.update_account_health(
            platform, account, self.WARNED, notes=reason,
        )
        logger.warning(f"Account {account} on {platform}: warned — {reason}")

    def mark_banned(self, platform: str, account: str, reason: str):
        """Mark account as banned."""
        key = f"{platform}:{account}"
        with self._lock:
            self._statuses[key] = self.BANNED
        self.db.update_account_health(
            platform, account, self.BANNED, notes=reason,
        )
        logger.error(f"Account {account} on {platform}: BANNED — {reason}")

    def mark_healthy(self, platform: str, account: str):
        """Mark account as healthy."""
        key = f"{platform}:{account}"
        with self._lock:
            self._statuses[key] = self.HEALTHY
            if key in self._cooldowns:
                del self._cooldowns[key]
        self.db.update_account_health(platform, account, self.HEALTHY)

    def get_assigned_subreddits(self, account: Dict, platform: str) -> List[str]:
        """Get subreddits explicitly assigned to this account in the YAML config.

        Returns empty list if no assignment (meaning no restriction).
        """
        assigned = account.get("assigned_subreddits", [])
        if isinstance(assigned, list):
            return [s.lower() for s in assigned if s]
        return []

    def is_subreddit_assigned(
        self, account: Dict, platform: str, subreddit: str
    ) -> bool:
        """Check if a subreddit is assigned to this account.

        Returns True if:
        - No assigned_subreddits configured (no restriction)
        - The subreddit is in the account's assigned list
        """
        assigned = self.get_assigned_subreddits(account, platform)
        if not assigned:
            return True  # No restriction
        return subreddit.lower() in assigned

    def get_preferred_subreddits(
        self, account: str, platform: str, max_subs: int = 5
    ) -> List[str]:
        """Get focused subreddits for an account to build recognition.

        Returns subreddits where the account has most activity,
        enabling expertise concentration.
        """
        try:
            top = self.db.get_top_subreddits_for_account(
                account, platform, limit=max_subs
            )
            return [s["subreddit"] for s in top] if top else []
        except Exception:
            return []

    def get_all_health(self) -> List[Dict]:
        """Get health status for all configured accounts."""
        results = []
        for platform in ("reddit", "twitter", "telegram"):
            accounts = self.load_accounts(platform)
            for acc in accounts:
                username = acc.get("username") or acc.get("phone", "?")
                key = f"{platform}:{username}"
                status = self._statuses.get(key, self.HEALTHY)
                action_count_24h = self.db.get_action_count(
                    hours=24, account=username, platform=platform,
                )
                results.append({
                    "platform": platform,
                    "username": username,
                    "status": status,
                    "actions_24h": action_count_24h,
                    "cooldown_until": (
                        self._cooldowns[key].isoformat()
                        if key in self._cooldowns
                        else None
                    ),
                })
        return results

    # ── Account Management ────────────────────────────────────────────

    def add_account(
        self,
        platform: str,
        username: str,
        password: str,
        email: str = "",
        projects: Optional[List[str]] = None,
        persona: str = "helpful_casual",
    ) -> str:
        """Add a new account to the platform config YAML.

        Returns status message.
        """
        if platform == "reddit":
            base = f"{self.config_dir}/reddit_accounts"
        elif platform == "twitter":
            base = f"{self.config_dir}/twitter_accounts"
        else:
            return f"Unknown platform: {platform}"

        # Always write to .local.yaml when it exists (server config), else .yaml
        local_path = f"{base}.local.yaml"
        default_path = f"{base}.yaml"
        path = local_path if os.path.exists(local_path) else default_path

        # Load existing config
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}

        accounts = data.get("accounts", [])

        # Check if username already exists
        for acc in accounts:
            if acc.get("username", "").lower() == username.lower():
                return f"Account @{username} already exists on {platform}"

        # Build account entry
        cookie_index = len(accounts) + 1
        cookie_file = f"data/cookies/{platform}_account{cookie_index}.json"

        if platform == "reddit":
            new_account = {
                "username": username,
                "password": password,
                "client_id": "",
                "client_secret": "",
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                ),
                "cookies_file": cookie_file,
                "enabled": True,
                "persona": persona,
                "assigned_projects": projects or ["my_project"],
                "cooldown_minutes": 15,
                "max_actions_per_hour": 4,
            }
        else:  # twitter
            new_account = {
                "username": username,
                "email": email,
                "password": password,
                "totp_secret": "",
                "cookies_file": cookie_file,
                "enabled": True,
                "persona": persona,
                "assigned_projects": projects or ["my_project"],
                "cooldown_minutes": 20,
                "max_actions_per_hour": 3,
            }

        accounts.append(new_account)
        data["accounts"] = accounts

        # Preserve other top-level keys
        if platform == "reddit" and "auth_mode" not in data:
            data["auth_mode"] = "web"
        if platform == "reddit" and "default_cooldown_per_subreddit_minutes" not in data:
            data["default_cooldown_per_subreddit_minutes"] = 60

        # Ensure cookies directory exists
        os.makedirs("data/cookies", exist_ok=True)

        # Write back
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Added {platform} account: @{username}")
        return f"Added @{username} to {platform}. Cookie file: {cookie_file}"

    def remove_account(self, platform: str, username: str) -> str:
        """Disable an account in the config (sets enabled: false)."""
        if platform == "reddit":
            base = f"{self.config_dir}/reddit_accounts"
        elif platform == "twitter":
            base = f"{self.config_dir}/twitter_accounts"
        else:
            return f"Unknown platform: {platform}"

        local_path = f"{base}.local.yaml"
        default_path = f"{base}.yaml"
        path = local_path if os.path.exists(local_path) else default_path

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return "Config file not found"

        accounts = data.get("accounts", [])
        found = False
        for acc in accounts:
            if acc.get("username", "").lower() == username.lower():
                acc["enabled"] = False
                found = True
                break

        if not found:
            return f"Account @{username} not found on {platform}"

        data["accounts"] = accounts
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.info(f"Disabled {platform} account: @{username}")
        return f"Disabled @{username} on {platform}"

    def list_all_accounts(self) -> List[Dict]:
        """List all accounts across platforms (including disabled ones)."""
        results = []
        platform_paths = {
            "reddit": f"{self.config_dir}/reddit_accounts.yaml",
            "twitter": f"{self.config_dir}/twitter_accounts.yaml",
            "telegram": f"{self.config_dir}/telegram_user_accounts.yaml",
        }

        for platform, path in platform_paths.items():
            try:
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                for acc in data.get("accounts", []):
                    username = acc.get("username") or acc.get("phone", "?")
                    session_or_cookies = (
                        acc.get("session_file", "")
                        or acc.get("cookies_file", "")
                    )
                    results.append({
                        "platform": platform,
                        "username": username,
                        "enabled": acc.get("enabled", True),
                        "persona": acc.get("persona", "?"),
                        "projects": acc.get("assigned_projects", []),
                        "cookies_file": session_or_cookies,
                        "has_cookies": os.path.exists(session_or_cookies) if session_or_cookies else False,
                    })
            except FileNotFoundError:
                pass

        return results
