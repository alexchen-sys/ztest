import pytest


@pytest.mark.slow
def test_alpha():
    assert True


def test_beta():
    assert True


@pytest.mark.slow
class TestBunch:
    def test_gamma(self):
        assert True

    def test_delta(self):
        assert True
