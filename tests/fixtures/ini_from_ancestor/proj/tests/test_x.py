import pytest


@pytest.mark.xfail(reason="expected to fail")
def test_unexpectedly_passes():
    assert True
