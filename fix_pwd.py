import re
with open("tests/test_reddit_oauth.py", "r") as f:
    text = f.read()

text = text.replace("def get_test_dashboard():", """def get_test_dashboard():
    import dashboard.web
    if dashboard.web._pwd_ctx:
        dashboard.web._pwd_ctx = MagicMock()
        dashboard.web._pwd_ctx.hash.return_value = "mock_hash"
        dashboard.web._pwd_ctx.verify.return_value = True""")

with open("tests/test_reddit_oauth.py", "w") as f:
    f.write(text)
