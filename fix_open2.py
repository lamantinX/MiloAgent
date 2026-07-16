import re
with open("tests/test_reddit_oauth.py", "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace('config_file = pathlib.Path("config/accounts.local.yaml")', 'import pathlib\n        config_file = pathlib.Path("config/accounts.local.yaml")')

with open("tests/test_reddit_oauth.py", "w", encoding="utf-8") as f:
    f.write(text)
