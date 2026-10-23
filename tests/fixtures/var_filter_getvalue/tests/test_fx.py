def test_fx(fx):
    assert fx == 1


def test_getvalue(pytestconfig):
    import pytest

    with pytest.raises(ValueError):
        pytestconfig.getvalue("anything")
    assert pytestconfig.getvalue("anything", default="d") == "d"
