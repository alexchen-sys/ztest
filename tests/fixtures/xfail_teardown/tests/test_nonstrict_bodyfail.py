"""Both the body failure and the finalizer failure are expected failures."""
import pytest


@pytest.mark.xfail(reason="expected to fail")
def test_body_fails():
    assert False, "body boom"


def teardown_function(function):
    raise RuntimeError("module teardown boom")
