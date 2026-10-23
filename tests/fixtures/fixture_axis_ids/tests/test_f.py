import pytest


def _simulate_alpha():
    return 1


def _simulate_beta():
    return 2


@pytest.fixture(params=[_simulate_alpha, _simulate_beta, 42])
def scenario(request):
    return request.param


def test_scenario(scenario):
    assert scenario is not None
