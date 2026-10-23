import pytest


# The cardinality of explicit ids must match the number of parametersets:
# loud collection error, pytest 9 parity.
@pytest.mark.parametrize("v", [1, 2], ids=["only-one"])
def test_ids_cardinality(v):
    assert True
