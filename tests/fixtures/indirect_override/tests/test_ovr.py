import pytest


@pytest.fixture(params=("2.0", "3.0.0"))
def spec(request):
    return request.param


@pytest.mark.parametrize("spec", ("3.0.0",), indirect=True)
def test_override(spec):
    # regression: indirect mark replaces the fixture's own params axis
    assert spec == "3.0.0"


def test_base(spec):
    assert spec in ("2.0", "3.0.0")
