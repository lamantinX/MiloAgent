import re
with open("platforms/telegram_group_bot.py", "r", encoding="utf-8") as f:
    text = f.read()
text = re.sub(
    r"raise ValueError\('Telegram api_id.*yaml'\)",
    r"raise ValueError('Telegram config err')",
    text,
    flags=re.DOTALL
)
with open("platforms/telegram_group_bot.py", "w", encoding="utf-8") as f:
    f.write(text)
