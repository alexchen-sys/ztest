class _ExplodingCallable:
    """LocalProxy-class object: callable, __getattr__ raises non-AttributeError."""

    def __call__(self):
        raise RuntimeError("proxy unbound")

    def __getattr__(self, name):
        raise RuntimeError("proxy unbound")


class TestProxyHost:
    test_proxy = _ExplodingCallable()

    def test_real(self):
        pass
