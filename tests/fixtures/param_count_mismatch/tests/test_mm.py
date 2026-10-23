import pytest


@pytest.mark.parametrize("a,b", [(1, 2, 3)])
def test_wide(a, b):
    pass


@pytest.mark.parametrize("a,b", [(1,)])
def test_narrow(a, b):
    pass


@pytest.mark.parametrize(("v",), [(1, 2)])
def test_ft_wide(v):
    pass
