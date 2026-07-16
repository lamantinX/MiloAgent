with open("tests/test_reddit_oauth.py", "r", encoding="utf-8") as f:
    text = f.read()

import re
old_fakepath = """        class FakePath(original_path):
            def __new__(cls, *args, **kwargs):
                p = str(args[0])
                if "accounts" in p:
                    if "local" in p:
                        return original_path.__new__(cls, config_file)
                    return original_path.__new__(cls, tmp_path / "not_exist")
                return original_path.__new__(cls, *args, **kwargs)"""

new_fakepath = """        class FakePath(original_path):
            def __new__(cls, *args, **kwargs):
                p = str(args[0])
                if "reddit_accounts" in p:
                    return original_path.__new__(cls, config_file)
                if "accounts" in p:
                    if "local" in p:
                        return original_path.__new__(cls, config_file)
                    return original_path.__new__(cls, tmp_path / "not_exist")
                return original_path.__new__(cls, *args, **kwargs)"""

text = text.replace(old_fakepath, new_fakepath)
with open("tests/test_reddit_oauth.py", "w", encoding="utf-8") as f:
    f.write(text)
