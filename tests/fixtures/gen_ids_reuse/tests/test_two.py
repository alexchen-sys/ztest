import pytest


@pytest.fixture(params=[10, 20, 30], ids=(f"g{i}" for i in range(3)))
def fx(request):
    return request.param


def test_a(fx):
    pass


def test_b(fx):
    pass
