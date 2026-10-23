import pytest


def test_errisinstance():
    with pytest.raises(ValueError) as ei:
        raise ValueError("boom")
    assert ei.errisinstance(ValueError)
    assert not ei.errisinstance(KeyError)


def test_pytestconfig_fixture(pytestconfig):
    assert pytestconfig is not None
    assert hasattr(pytestconfig, "rootpath") or hasattr(pytestconfig, "option")
