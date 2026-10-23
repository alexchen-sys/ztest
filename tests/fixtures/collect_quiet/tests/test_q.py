import pytest


def test_one():
    assert True


def test_two():
    assert True


@pytest.mark.parametrize("value", [1, 2])
def test_three(value):
    assert value in (1, 2)
