import pytest


@pytest.mark.skipif
def test_bare_skipif():
    assert True


@pytest.mark.usefixtures
def test_bare_usefixtures():
    assert True
