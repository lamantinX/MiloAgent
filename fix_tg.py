import re

with open('platforms/telegram_group_bot.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Validation block
text = re.sub(
    r'([ \t]+)if not self\._api_id or not self\._api_hash.*?(?=if self\.client is None:)',
    r'''\1if not self.account_config.get('account_id'):
\1    raise ValueError('telegram_user_accounts.yaml must contain a stable account_id')
\1if not self.account_config.get('business_id'):
\1    raise ValueError('telegram_user_accounts.yaml must contain a business_id')
\1if getattr(self, '_session_file', None) is None:
\1    raise ValueError('Telegram user session must have a session_file path')
\1
\1if not self._api_id or not self._api_hash:
\1    raise ValueError('Telegram api_id and api_hash not configured.\n  1. Go to https://my.telegram.org\n  2. Create an app to get api_id and api_hash\n  3. Add them to config/telegram_user_accounts.yaml')
\1try:
\1    self._api_id = int(self._api_id)
\1except ValueError:
\1    raise ValueError('Telegram api_id must be numeric')
\1
\1''',
    text,
    flags=re.DOTALL
)

# 2. bot check
text = re.sub(
    r'([ \t]+)me = await self\.client\.get_me\(\).*?_username\}\"\)',
    r'''\1me = await self.client.get_me()
\1if getattr(me, 'bot', False):
\1    raise ValueError('Telegram user engagement cannot be run with a bot identity.')
\1self._username = me.username or me.phone or self._phone
\1self._authenticated = True
\1logger.info(f"Telegram user engagement connected as @{self._username}")''',
    text,
    flags=re.DOTALL
)

with open('platforms/telegram_group_bot.py', 'w', encoding='utf-8') as f:
    f.write(text)
