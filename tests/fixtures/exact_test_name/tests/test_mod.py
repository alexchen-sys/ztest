import pytest

@pytest.mark.parametrize("case", ["okay"], ids=["okay"])
def test(case):
    assert case == "okay"

def test_helper_alive():
    assert True
