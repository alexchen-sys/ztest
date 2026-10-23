import pytest


@pytest.fixture(params=(1, 2))
def x(request):
    raise RuntimeError("fixture must be masked by direct parametrize")


@pytest.mark.parametrize("x", [10, 20])
def test_direct_masks_fixture(x):
    assert x in (10, 20)
