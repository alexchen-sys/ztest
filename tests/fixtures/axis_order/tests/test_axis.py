import pytest


@pytest.fixture(params=["3.10"])
def ver(request):
    return request.param


# pytest puts the fixture axis before the decorator
# parametrize axis in the compound id: test_axis[3.10-a + a].
@pytest.mark.parametrize("expr", ["a + a"])
def test_axis(ver, expr):
    pass
