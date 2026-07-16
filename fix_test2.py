import re
with open("tests/test_telegram_identity.py", "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    'db.claim_opportunity.return_value = True',
    'db.update_opportunity_status.return_value = True'
)
# add target_id to the opportunity dict
text = text.replace(
    '"group_id": -100123456,',
    '"group_id": -100123456,\n        "target_id": "opp1",'
)

text = text.replace(
    'assert result is True',
    'assert result is True\n    assert getattr(FakeClient, "send_message", None) is not None'
)

with open("tests/test_telegram_identity.py", "w", encoding="utf-8") as f:
    f.write(text)
