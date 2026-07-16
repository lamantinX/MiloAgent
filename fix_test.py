with open("tests/test_telegram_identity.py", "r", encoding="utf-8") as f:
    text = f.read()

text = text.replace(
    'content_gen.generate_reply.return_value = {"reply": "This is a reply"}',
    'content_gen.generate_telegram_reply.return_value = "This is a reply"\n    db.claim_opportunity.return_value = True'
)

# And missing module patch
# _act_async calls get_business / get_project. I need to mock them, maybe pass them?
with open("tests/test_telegram_identity.py", "w", encoding="utf-8") as f:
    f.write(text)
