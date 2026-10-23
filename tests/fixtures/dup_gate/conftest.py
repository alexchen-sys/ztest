def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_hook_hook_dup":
        return
    metafunc.parametrize("y", [1, 2])
    metafunc.parametrize("y", [3, 4])
