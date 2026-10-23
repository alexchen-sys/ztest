def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ == "test_newaxis":
        metafunc.parametrize("x", [1, 2])


def test_newaxis(x):
    assert x in (1, 2)


def test_plain():
    pass
