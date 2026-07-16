import re
with open("tests/test_reddit_oauth.py", "r", encoding="utf-8") as f:
    text = f.read()

# Instead of FakePath, just mock yaml.safe_load and yaml.dump
# But there's a simpler way: just patch builtin open
import inspect
