import pytest


@pytest.mark.parametrize("x", [1, 2])
@pytest.mark.parametrize("x", [3, 4])
def test_deco_deco_dup(x):
    pass
