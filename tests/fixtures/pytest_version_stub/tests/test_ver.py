def test_pytest_stub_version():
    # target/core.py reads
    # sys.modules["_pytest"].__version__ - the stub must carry a version.
    import sys

    v = sys.modules["_pytest"].__version__
    assert tuple(int(x) for x in v.split(".")[:2]) >= (7, 0)
