import enum

import pytest


class Color(enum.Enum):
    RED = 1


@pytest.mark.parametrize("a,b", [(1, "x"), (2, "y")])
def test_pairs(a, b):
    pass


@pytest.mark.parametrize("v", ["x\r\ny", "dup", "dup"])
def test_str(v):
    pass


@pytest.mark.parametrize("v", [b"\x01\xff"])
def test_bytes(v):
    pass


@pytest.mark.parametrize("v", [Color.RED])
def test_enum(v):
    pass


@pytest.mark.parametrize("a", [1, 2])
@pytest.mark.parametrize("b", ["p", "q"])
def test_stacked(a, b):
    pass
