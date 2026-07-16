with open("dashboard/web.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
in_func = False
func_lines = []
for i, line in enumerate(lines):
    if line.startswith("        def _load_reddit_api_config() -> dict:"):
        in_func = True
        func_lines.append(line.replace("        ", "", 1))
    elif in_func:
        if line.strip() == "" or line.startswith("            "):
            func_lines.append(line.replace("        ", "", 1))
        else:
            in_func = False
            new_lines.append(line)
    else:
        new_lines.append(line)

final_lines = func_lines + new_lines
with open("dashboard/web.py", "w", encoding="utf-8") as f:
    f.writelines(final_lines)
