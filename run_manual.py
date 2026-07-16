import traceback
try:
    from tests.test_reddit_oauth import test_oauth_start_endpoint_valid
    class DummyPatch:
        def setattr(self, a, b): pass
    test_oauth_start_endpoint_valid(DummyPatch())
except Exception as e:
    traceback.print_exc()
