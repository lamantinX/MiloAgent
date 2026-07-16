lines = []
with open("platforms/telegram_group_bot.py", "r", encoding="utf-8") as f:
    for line in f:
        # replace any single quote with unterminated newlines that look like this
        if "raise ValueError('Telegram api_id and" in line:
            lines.append("            raise ValueError('Telegram api_id and api_hash not configured.\n  1. Go to https://my.telegram.org\n  2. Create an app to get api_id and api_hash\n  3. Add them to config/telegram_user_accounts.yaml')\n")
            continue
        if "1. Go to" in line or "2. Create an" in line or "3. Add them" in line:
            continue
        lines.append(line)

with open("platforms/telegram_group_bot.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
