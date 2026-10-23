import pytest


@pytest.mark.xfail
def test_bare_xfail_fails():
    assert False


@pytest.mark.xfail
def test_bare_xfail_passes():
    pass


def test_normal():
    pass
