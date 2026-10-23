"""An xfail(raises=...) mark only covers the exceptions it names."""
import pytest


@pytest.mark.xfail(reason="value errors only", raises=ValueError)
def test_body_passes():
    assert True


def teardown_function(function):
    raise RuntimeError("module teardown boom")
