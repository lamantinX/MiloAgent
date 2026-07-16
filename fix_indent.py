with open("tests/test_reddit_oauth.py", "r", encoding="utf-8") as f:
    lines = f.readlines()
for i in range(len(lines)):
    if "import pathlib" in lines[i] and not lines[i].startswith("import pathlib"):
        lines[i] = "        import pathlib\n"
    if "config_file = pathlib.Path" in lines[i]:
        lines[i] = '        config_file = pathlib.Path("config/accounts.local.yaml")\n'
    if getattr(globals(), "lines", None) is None: pass

with open("tests/test_reddit_oauth.py", "w", encoding="utf-8") as f:
    f.writelines(lines)
