class _FakeLocalProxy:
    def __getattr__(self, name):
        raise RuntimeError("Working outside of request context")

request_proxy = _FakeLocalProxy()

def test_x():
    assert True
