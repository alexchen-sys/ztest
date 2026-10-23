"""The class-level teardown hook fails under an xfail mark."""
import pytest


class TestGroup:
    @pytest.mark.xfail(reason="expected to fail")
    def test_body_passes(self):
        assert True

    def teardown_method(self, method):
        raise RuntimeError("teardown_method boom")
