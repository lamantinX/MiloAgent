with open("platforms/telegram_group_bot.py", "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    "raise ValueError('Telegram api_id and api_hash not configured.\n  1. Go to https://my.telegram.org\n  2. Create an app to get api_id and api_hash\n  3. Add them to config/telegram_user_accounts.yaml')",
    "raise ValueError('Telegram api_id and api_hash not configured.\n  1. Go to https://my.telegram.org\n  2. Create an app to get api_id and api_hash\n  3. Add them to config/telegram_user_accounts.yaml')"
)

with open("platforms/telegram_group_bot.py", "w", encoding="utf-8") as f:
    f.write(text)
