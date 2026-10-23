def pytest_generate_tests(metafunc):
    if "x" in metafunc.fixturenames:
        metafunc.parametrize("x", [1, 2])
