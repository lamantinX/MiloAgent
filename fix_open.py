import re
with open("tests/test_reddit_oauth.py", "r", encoding="utf-8") as f:
    text = f.read()

# I will just write to an actual file in config/ to test it properly!
text = text.replace('config_file = tmp_path / "accounts.local.yaml"', 'config_file = pathlib.Path("config/accounts.local.yaml")')
from_str = '''        class FakePath(original_path):
            def __new__(cls, *args, **kwargs):
                p = str(args[0])
                if "accounts" in p:
                    if "local" in p:
                        return original_path.__new__(cls, config_file)
                    return original_path.__new__(cls, tmp_path / "not_exist")
                return original_path.__new__(cls, *args, **kwargs)
        monkeypatch.setattr("dashboard.web.Path", FakePath)'''
text = text.replace(from_str, "")

with open("tests/test_reddit_oauth.py", "w", encoding="utf-8") as f:
    f.write(text)
