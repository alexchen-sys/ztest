"""An xfail mark covers the teardown phase too: the body unexpectedly passes
(xpassed) while the failing module finalizer is an expected failure (xfailed).
"""
import pytest


@pytest.mark.xfail(reason="expected to fail")
def test_body_passes():
    assert True


def teardown_function(function):
    raise RuntimeError("module teardown boom")
