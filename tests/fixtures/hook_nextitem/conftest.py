CALLS = []


def pytest_runtest_teardown(item, nextitem):
    CALLS.append((item.name, nextitem))
