import sys
import re

def fix_get_account():
    with open('C:/dev/MiloAgent/.worktrees/006/safety/account_manager.py', 'r') as f:
        content = f.read()

    # Find the start of get_account
    idx1 = content.find('def get_account(self, platform: str, business_id: str, account_id: str) -> Optional[Dict]:')
    
    # Find the start of update_karma_cache
    idx2 = content.find('def update_karma_cache(')
    
    # Replacement block
    replacement = '''def get_account(self, platform: str, business_id: str, account_id: str) -> Optional[Dict]:
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

    '''
    
    new_content = content[:idx1] + replacement + content[idx2:]
    
    # Also fix karma cache methods to use account_id instead of username
    new_content = new_content.replace('self._karma_cache[username]', 'self._karma_cache[account_id]')
    new_content = new_content.replace('logger.debug(f"Karma cache updated: {username}', 'logger.debug(f"Karma cache updated: {account_id}')
    new_content = new_content.replace('self._karma_cache.get(username)', 'self._karma_cache.get(account_id)')
    new_content = new_content.replace('self.get_cached_karma(username)', 'self.get_cached_karma(account_id)')
    new_content = new_content.replace('self.get_account_tier(username)', 'self.get_account_tier(account_id)')

    with open('C:/dev/MiloAgent/.worktrees/006/safety/account_manager.py', 'w') as f:
        f.write(new_content)

fix_get_account()
