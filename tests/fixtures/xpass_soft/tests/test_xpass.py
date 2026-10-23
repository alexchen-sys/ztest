import pytest


@pytest.mark.xfail(reason="shaky but passing")
def test_xpass_soft():
    assert True


@pytest.mark.xfail(reason="known broken")
def test_xfail_real():
    assert False
