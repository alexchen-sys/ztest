SEEN = []


def pytest_runtest_teardown(item, nextitem=None):
    # pluggy does not pass parameters with defaults: a defaulted nextitem is
    # always None in pytest (the real next item receives only the required
    # parameter).
    SEEN.append((item.name, nextitem))


def pytest_sessionfinish(session, exitstatus):
    assert [n for n, _ in SEEN] == ["test_a", "test_b"], SEEN
    assert all(nx is None for _, nx in SEEN), SEEN
