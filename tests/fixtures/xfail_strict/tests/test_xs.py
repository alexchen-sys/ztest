import pytest


@pytest.mark.xfail
def test_xpasses():
    pass


@pytest.mark.xfail
def test_xfails():
    assert False
