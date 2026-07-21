"""Account rotation and health management."""

import os
import stat
import logging
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
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

    @staticmethod
    def _account_key(business_id: str, account_id: str) -> str:
        """Canonical account key used across all status/cooldown maps."""
        return f"{business_id}:{account_id}"

    def _restore_state_from_db(self):
        """Restore cooldown/health state from account_health table on startup."""
        try:
            rows = self.db.conn.execute(
                """SELECT platform, business_id, account, status, timestamp
                   FROM account_health
                   WHERE id IN (
                       SELECT MAX(id) FROM account_health
                       GROUP BY platform, account
                   )"""
            ).fetchall()
            for row in rows:
                biz = row["business_id"] if row["business_id"] else ""
                key = self._account_key(biz, row["account"])
                status = row["status"]
                if status == self.COOLDOWN:
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                        # Restore with original cooldown window preserved
                        expires = ts + timedelta(minutes=30)
                        if expires > datetime.now(timezone.utc):
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

    def resolve_config_path(self, platform: str) -> Optional[str]:
        """Resolve the YAML config path for a platform, preferring .local.yaml.

        Returns None if the platform is unknown.
        """
        if platform == "reddit":
            base = "reddit_accounts.yaml"
        elif platform == "twitter":
            base = "twitter_accounts.yaml"
        elif platform == "telegram":
            base = "telegram_user_accounts.yaml"
        else:
            return None
        path = os.path.join(self.config_dir, base)
        if path.endswith(".yaml"):
            local_path = path[:-5] + ".local.yaml"
            if os.path.exists(local_path):
                return local_path
        return path

    def load_accounts(
        self,
        platform: str,
        *,
        include_disabled: bool = False,
        include_unauthorized: bool = False,
    ) -> List[Dict]:
        """Load accounts for a platform from config.

        Prefers .local.yaml override (gitignored) so git pull never
        overwrites real credentials on the server.

        Args:
            platform: "reddit", "twitter", or "telegram"
            include_disabled: If True, include accounts with enabled=False.
                Used by dashboard onboarding and list/edit endpoints.
            include_unauthorized: If True, include Telegram accounts with
                auth_status != "authorized". Used by QR/2FA auth flow.
                Only applies when platform == "telegram".
        """
        path = self.resolve_config_path(platform)
        if not path:
            return []
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}

            from core.business_manager import BusinessManager
            biz_mgr = BusinessManager()

            accounts = data.get("accounts", [])
            result = []
            seen_ids = set()

            for raw_a in accounts:
                # Filter disabled accounts unless explicitly requested
                if not raw_a.get("enabled", True) and not include_disabled:
                    continue

                # Filter unauthorized Telegram accounts unless explicitly requested
                if (platform == "telegram"
                        and raw_a.get("auth_status", "not_authorized") != "authorized"
                        and not include_unauthorized
                        and not include_disabled):
                    continue

                identifier = raw_a.get("username") or raw_a.get("phone", "")
                if identifier.startswith("YOUR_") or identifier.startswith("your_"):
                    continue
                # Skip obvious placeholder accounts
                if identifier in ("your_reddit_username", "your_twitter_username", "second_account"):
                    continue

                a = dict(raw_a)

                # Validation checks
                account_id = a.get("account_id")
                business_id = a.get("business_id")

                if not account_id:
                    logger.warning("Account missing account_id, skipped")
                    continue

                if account_id in seen_ids:
                    logger.warning(f"Duplicate account_id {account_id}, skipped")
                    continue

                if not business_id:
                    logger.warning(f"Account {account_id} missing business_id, skipped")
                    continue

                biz = biz_mgr.get_business(business_id)
                if not biz:
                    logger.warning(f"Account {account_id} references unknown business {business_id}")
                    continue

                # Validate products
                assigned_products = a.get("assigned_products", []) or a.get("assigned_projects", [])
                valid_prods = []
                prod_error = False
                for pid in assigned_products:
                    prod = biz_mgr.get_project(pid)
                    if not prod:
                        logger.warning(f"Account {account_id} references unknown product {pid}")
                        prod_error = True
                        break

                    prod_biz = prod.get("project", {}).get("business_id")
                    if not prod_biz:
                        prod_biz = prod.get("business_id")
                    if prod_biz != business_id:
                        logger.warning(f"Account {account_id} references cross-business product {pid}")
                        prod_error = True
                        break
                    valid_prods.append(pid)

                if prod_error:
                    continue

                seen_ids.add(account_id)
                a["assigned_products"] = valid_prods

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

    def get_next_account(self, platform: str, business_id: str, product_id: Optional[str] = None) -> Optional[Dict]:
        """Get the next available (healthy, not on cooldown) account.
        Uses round-robin rotation: never picks the same account twice in a row,
        then falls back to LRU (fewest actions in 4h) as tiebreaker.
        Thread-safe via self._lock.
        """
        accounts = self.load_accounts(platform)
        if not accounts:
            return None
        with self._lock:
            self._cleanup_expired()
            available = []
            for acc in accounts:
                if acc.get("business_id") != business_id:
                    continue
                # Enforce product routing
                if product_id:
                    has_prod = product_id in acc.get("assigned_products", [])
                    all_prod = acc.get("all_products", False)
                    if not has_prod and not all_prod:
                        continue
                
                key = f"{business_id}:{acc['account_id']}"
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

            # Skip accounts without cookie files or sessions
            if platform == "reddit":
                with_cookies = [a for a in available if os.path.exists(a.get("cookies_file", ""))]
                if with_cookies:
                    available = with_cookies
                
            if not available:
                logger.warning(f"No available {platform} accounts for business '{business_id}' and product '{product_id}'")
                return None

            # Sort by least recently used
            available.sort(key=lambda a: self._last_used.get(a["account_id"], datetime.min))

            # Pick the least recently used, or round robin
            best = available[0]
            for a in available:
                if self._rotation_index.get(f"{platform}:{business_id}") != a["account_id"]:
                    best = a
                    break

            best_key = best["account_id"]
            self._last_used[best["account_id"]] = datetime.utcnow()
            self._rotation_index[f"{platform}:{business_id}"] = best["account_id"]
            return best
    def get_account(self, platform: str, business_id: str, account_id: str) -> Optional[Dict]:
        """Get a specific account by account_id and business_id (if healthy and not on cooldown)."""
        accounts = self.load_accounts(platform)
        for acc in accounts:
            if acc.get("account_id") == account_id and acc.get("business_id") == business_id:
                key = f"{business_id}:{account_id}"
                status = self._statuses.get(key, self.HEALTHY)
                if status == self.BANNED:
                    logger.warning(f"Account {account_id} is banned — skipping")
                    return None
                if key in self._cooldowns and datetime.utcnow() < self._cooldowns[key]:
                    logger.warning(f"Account {account_id} is on cooldown — skipping")
                    return None
                return acc
        return None

    def update_karma_cache(self, account_id: str, karma: int):
        """Store fresh karma value for an account."""
        self._karma_cache[account_id] = (karma, time.time())
        logger.debug(f"Karma cache updated: {account_id} = {karma}")

    def get_cached_karma(self, account_id: str) -> Optional[int]:
        """Return cached karma if fresh (< 12h), else None."""
        entry = self._karma_cache.get(account_id)
        if entry and (time.time() - entry[1]) < self.KARMA_CACHE_TTL:
            return entry[0]
        return None

    def is_karma_sufficient(self, account_id: str) -> bool:
        """Check if account has enough karma for write operations.

        Returns True if karma is sufficient OR if karma is unknown (cache miss).
        Unknown karma = don't block, let the action proceed and learn from result.
        """
        karma = self.get_cached_karma(account_id)
        if karma is None:
            return True  # Unknown karma: don't block
        return karma >= self.MIN_KARMA_WRITE

    def get_account_tier(self, account_id: str) -> dict:
        """Return tier info dict for an account based on cached karma.

        Returns dict with keys: tier (int 0-3), name, daily_cap, can_post, karma.
        Defaults to tier 0 (most conservative) when karma unknown.
        """
        karma = self.get_cached_karma(account_id)
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

    def get_daily_cap(self, account_id: str) -> int:
        """Return the write-action daily cap for this account based on karma tier."""
        return self.get_account_tier(account_id)["daily_cap"]

    def can_post(self, account_id: str) -> bool:
        """Return True if account karma tier allows posting (not just commenting)."""
        return self.get_account_tier(account_id)["can_post"]

    def mark_cooldown(
        self,
        platform: str,
        business_id: str,
        account_id: str,
        minutes: int = 30,
    ):
        """Put an account on cooldown."""
        key = f"{business_id}:{account_id}"
        with self._lock:
            self._cooldowns[key] = datetime.utcnow() + timedelta(minutes=minutes)
            self._statuses[key] = self.COOLDOWN
        self.db.update_account_health(platform, business_id, account_id, self.COOLDOWN,
            notes=f"Cooldown for {minutes}min",
        )
        logger.info(f"Account {account_id} on {platform}: cooldown {minutes}min")

    def mark_warned(self, platform: str, business_id: str, account_id: str, reason: str):
        """Mark account as warned (suspicious activity detected)."""
        key = f"{business_id}:{account_id}"
        with self._lock:
            self._statuses[key] = self.WARNED
        self.db.update_account_health(
            platform, business_id, account_id, self.WARNED, notes=reason,
        )
        logger.warning(f"Account {account_id} on {platform}: warned — {reason}")

    def mark_banned(self, platform: str, business_id: str, account_id: str, reason: str):
        """Mark account as banned."""
        key = f"{business_id}:{account_id}"
        with self._lock:
            self._statuses[key] = self.BANNED
        self.db.update_account_health(
            platform, business_id, account_id, self.BANNED, notes=reason,
        )
        logger.error(f"Account {account_id} on {platform}: BANNED — {reason}")

    def mark_healthy(self, platform: str, business_id: str, account_id: str):
        """Mark account as healthy."""
        key = f"{business_id}:{account_id}"
        with self._lock:
            self._statuses[key] = self.HEALTHY
            if key in self._cooldowns:
                del self._cooldowns[key]
        self.db.update_account_health(platform, business_id, account_id, self.HEALTHY)

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

    def get_all_health(self, business_id: Optional[str] = None) -> List[Dict]:
        """Get health status for all configured accounts.

        Uses canonical ``business_id:account_id`` keys so that status set
        by :meth:`mark_cooldown` / :meth:`mark_warned` / etc. is reflected.
        """
        results = []
        for platform in ("reddit", "twitter", "telegram"):
            # include_disabled=True so dashboard can show health for all accounts
            accounts = self.load_accounts(platform, include_disabled=True, include_unauthorized=True)
            for acc in accounts:
                biz = acc.get("business_id", "")
                if business_id and biz != business_id:
                    continue
                username = acc.get("username") or acc.get("phone", "?")
                acct_id = acc.get("account_id", username)
                key = self._account_key(biz, acct_id)
                status = self._statuses.get(key, self.HEALTHY)
                action_count_24h = self.db.get_action_count(
                    hours=24, account=acct_id, platform=platform, business_id=biz,
                )
                cooldown_until = None
                with self._lock:
                    if key in self._cooldowns:
                        cooldown_until = self._cooldowns[key].isoformat()
                results.append({
                    "platform": platform,
                    "username": username,
                    "account_id": acct_id,
                    "business_id": biz,
                    "status": status,
                    "actions_24h": action_count_24h,
                    "cooldown_until": cooldown_until,
                })
        return results

    # ── Account Management ────────────────────────────────────────────

    def add_account(
        self,
        platform: str,
        account_id: str,
        business_id: str,
        account_data: dict,
    ) -> dict:
        """Add a new account to the platform config YAML.
        Persists safely to .local.yaml using atomic write and restrictive permissions.
        Returns a redacted DTO.
        """
        if platform == "reddit":
            base = f"{self.config_dir}/reddit_accounts"
        elif platform == "twitter":
            base = f"{self.config_dir}/twitter_accounts"
        elif platform == "telegram":
            # Must match load_accounts('telegram') / telegram_group_bot, which
            # both read config/telegram_user_accounts(.local).yaml. Writing to
            # telegram_accounts.local.yaml would persist records nothing loads.
            base = f"{self.config_dir}/telegram_user_accounts"
        else:
            raise ValueError(f"Unknown platform: {platform}")

        local_path = f"{base}.local.yaml"
        # Since it says strictly persist ONLY to ignored local YAML:
        path = local_path 

        import yaml, os, stat, tempfile
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            data = {}
        accounts = data.setdefault("accounts", [])
        
        # Check if account_id already exists
        for acc in accounts:
            if acc.get("account_id") == account_id:
                raise ValueError(f"Account id {account_id} already exists")

        # Create account record
        record = {
            "account_id": account_id,
            "business_id": business_id,
            "persona": account_data.get("persona", "helpful_casual"),
            "assigned_projects": account_data.get("projects", []),
            "enabled": account_data.get("enabled", True),
        }
        
        if platform == "reddit":
            username = account_data["username"]
            record.update({
                "username": username,
                "password": account_data["password"],
                "email": account_data.get("email", ""),
                "client_id": "",
                "client_secret": "",
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "cookies_file": f"data/cookies/reddit_{account_id}.json",
            })
        elif platform == "telegram":
            record.update({
                "api_id": account_data["api_id"],
                "api_hash": account_data["api_hash"],
                "account_type": "user",
                "phone": account_data.get("phone"),
                "auth_status": "not_authorized",
                "session_file": f"data/sessions/telegram_{business_id}_{account_id}.session",
                "enabled": False, # Plan 009: keep enabled = False
            })
        elif platform == "twitter":
            username = account_data["username"]
            record.update({
                "username": username,
                "password": account_data["password"],
                "email": account_data.get("email", ""),
                "cookies_file": f"data/cookies/twitter_{account_id}.json",
            })

        accounts.append(record)
        
        # Atomic replace and restrictive permissions
        fd, temp_path = tempfile.mkstemp(dir=str(self.config_dir), suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            
        try:
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
        os.replace(temp_path, path)

        # Build redacted DTO
        dto = {k: v for k, v in record.items() if k not in ["password", "api_hash", "client_secret"]}
        dto["has_cookies"] = False
        return dto

    def remove_account(self, platform: str, account_id_or_username: str, business_id: str = None) -> str:
        """Disable an account in the config (sets enabled: false)."""
        if platform == "reddit":
            base = f"{self.config_dir}/reddit_accounts"
        elif platform == "twitter":
            base = f"{self.config_dir}/twitter_accounts"
        elif platform == "telegram":
            # Match add_account / load_accounts('telegram'): telegram_user_accounts.
            base = f"{self.config_dir}/telegram_user_accounts"
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
            # Check by account_id first
            match = False
            if "account_id" in acc and acc["account_id"] == account_id_or_username:
                if not business_id or acc.get("business_id") == business_id:
                    match = True
            # Fallback to username if legacy
            elif acc.get("username", "").lower() == account_id_or_username.lower():
                if not business_id or acc.get("business_id") == business_id:
                    match = True
            
            if match:
                acc["enabled"] = False
                found = True
                break

        if not found:
            return f"Account {account_id_or_username} not found on {platform}"

        data["accounts"] = accounts
        # Atomic write to prevent corruption on crash
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            try:
                os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
            os.replace(temp_path, path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

        logger.info(f"Disabled {platform} account: {account_id_or_username}")
        return f"Disabled {account_id_or_username} on {platform}"

    def set_telegram_authorized(
        self,
        business_id: str,
        account_id: str,
        identity: dict,
    ) -> bool:
        """Atomically flip a Telegram account to authorized + enabled (plan 011).

        Called only after Telethon confirms a personal user (me.bot == False)
        and the session is usable. Persists non-secret identity metadata only
        (username/first_name/phone). Never writes api_hash/password/session bytes.
        Returns True on success, False if the account was not found.
        """
        base = f"{self.config_dir}/telegram_user_accounts"
        local_path = f"{base}.local.yaml"
        default_path = f"{base}.yaml"
        path = local_path if os.path.exists(local_path) else default_path
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return False
        accounts = data.get("accounts", [])
        found = False
        for acc in accounts:
            if (
                acc.get("account_id") == account_id
                and acc.get("business_id") == business_id
                and acc.get("account_type") == "user"
            ):
                acc["auth_status"] = "authorized"
                acc["enabled"] = True
                # Non-secret identity only, for display/routing.
                if identity:
                    if identity.get("username"):
                        acc["username"] = identity["username"]
                    if identity.get("phone"):
                        acc["phone"] = identity["phone"]
                    if identity.get("first_name"):
                        acc["display_name"] = identity["first_name"]
                found = True
                break
        if not found:
            return False
        data["accounts"] = accounts
        # Atomic write to avoid a torn read by the watcher.
        tmpfd, tmpname = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tg_auth.", suffix=".tmp")
        try:
            with os.fdopen(tmpfd, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            try:
                os.chmod(tmpname, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(tmpname, path)
        finally:
            if os.path.exists(tmpname):
                try:
                    os.unlink(tmpname)
                except OSError:
                    pass
        logger.info("Telegram account %s authorized for business %s", account_id, business_id)
        return True

    def get_telegram_account(self, business_id: str, account_id: str) -> Optional[dict]:
        """Return the raw telegram account record (incl. secrets) for auth checks.

        Used by the QR/2FA route handlers to validate auth_status/account_type
        and read api_id/api_hash/session_file. **Never returned to the browser.**

        Searches disabled and unauthorized accounts so the QR auth flow can
        find accounts that are pending authorization.
        """
        accounts = self.load_accounts("telegram", include_disabled=True, include_unauthorized=True)
        for acc in accounts:
            if acc.get("account_id") == account_id and acc.get("business_id") == business_id:
                return acc
        return None

    def list_all_accounts(self) -> List[Dict]:
        """List all accounts across platforms (including disabled ones).

        Uses resolve_config_path so .local.yaml overrides are picked up.
        """
        results = []
        for platform in ("reddit", "twitter", "telegram"):
            path = self.resolve_config_path(platform)
            if not path:
                continue
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
                        "account_id": acc.get("account_id", ""),
                        "business_id": acc.get("business_id", ""),
                        "enabled": acc.get("enabled", True),
                        "auth_status": acc.get("auth_status", "authorized"),
                        "account_type": acc.get("account_type", ""),
                        "persona": acc.get("persona", "?"),
                        "projects": acc.get("assigned_projects", []),
                        "cookies_file": session_or_cookies,
                        "has_cookies": os.path.exists(session_or_cookies) if session_or_cookies else False,
                    })
            except FileNotFoundError:
                pass

        return results
