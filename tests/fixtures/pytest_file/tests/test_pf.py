def test_pytest_stub_has_file():
    # the _pytest stub carries __file__ (the real __init__.py path).
    import sys

    f = sys.modules["_pytest"].__file__
    assert f.endswith("__init__.py")
