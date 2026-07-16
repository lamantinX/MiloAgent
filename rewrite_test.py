import re
with open("tests/test_reddit_oauth.py", "r", encoding="utf-8") as f:
    text = f.read()

# find def test_oauth_callback_success
pattern = r'    @patch\("requests\.post"\)\n    def test_oauth_callback_success\(mock_post.*?\n    @patch'
import re
match = re.search(pattern, text, re.DOTALL)
if match:
    pass # this is getting complicated...

