import pytest


def _comp_id(v):
    if isinstance(v, str) and v.startswith("S"):
        return None  # pytest: fallback to value-id for this component
    return f"C{v}"


@pytest.mark.parametrize("a,b", [(1, "x"), (2, "S2")], ids=_comp_id)
def test_pairs(a, b):
    pass
