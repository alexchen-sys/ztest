class _FakeLocalProxy:
    """target.local.LocalProxy pattern: any attribute access outside context raises RuntimeError."""
    def __getattr__(self, name):
        raise RuntimeError("Working outside of application context")

current_app = _FakeLocalProxy()
