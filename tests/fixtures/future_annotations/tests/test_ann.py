def f(x: int) -> str:
    return str(x)


# `from __future__ import annotations` in ztest_shim.py must
# not leak through compile() (without dont_inherit) into user modules:
# annotations must stay real type objects.
def test_annotations_are_real_types():
    assert f.__annotations__["x"] is int
    assert f.__annotations__["return"] is str
