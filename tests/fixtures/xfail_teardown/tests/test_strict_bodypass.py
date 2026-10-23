"""A strict mark turns the unexpected pass into a failure; the finalizer
failure under the same mark stays an expected failure.
"""
import pytest


@pytest.mark.xfail(reason="expected to fail", strict=True)
def test_body_passes():
    assert True


def teardown_function(function):
    raise RuntimeError("module teardown boom")
