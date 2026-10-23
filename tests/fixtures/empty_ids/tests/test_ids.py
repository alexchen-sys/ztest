import pytest


# Empty explicit ids are not a cardinality error; pytest treats num_ids == 0
# as "generate the id from the values" (#1849).
@pytest.mark.parametrize("v", [1, 2, 3], ids=[])
def test_empty_list_ids(v):
    assert True


@pytest.mark.parametrize("v", [10, 20], ids="")
def test_empty_str_ids(v):
    assert True


@pytest.fixture(params=["a", "b"], ids=[])
def fx_empty(request):
    return request.param


def test_fixture_empty_ids(fx_empty):
    assert fx_empty in ("a", "b")
