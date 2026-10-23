def atest_test():
    raise AssertionError("must never be collected (pytest python_functions default)")

def _make_full_earley_test():
    raise AssertionError("factory, not a test")

class SomethingTests:
    def test_m(self):
        raise AssertionError("*Tests suffix is not a pytest collection rule")

class TestOk:
    def test_m(self):
        assert True

def test_real():
    assert True
