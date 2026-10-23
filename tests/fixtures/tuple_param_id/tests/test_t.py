import pytest


@pytest.mark.parametrize("v", [("a", 1)])
def test_t(v):
    assert v == ("a", 1)
