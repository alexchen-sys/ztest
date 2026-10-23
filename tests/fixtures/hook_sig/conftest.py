SEEN = []


def pytest_runtest_teardown(item, /, nextitem):
    # positional-only item + a mandatory nextitem: only this way pluggy really
    # passes the next item; a defaulted parameter would always get None
    # (pluggy varnames -> kwargnames are not injected).
    SEEN.append((item.name, getattr(nextitem, "name", None)))


def pytest_sessionfinish(session, exitstatus):
    # pytest order: one->two, two->None. An error here means failed finalization.
    names = [n for n, _ in SEEN]
    assert names == ["test_one", "test_two"], SEEN
    assert SEEN[0][1] == "test_two", SEEN
    assert SEEN[1][1] is None, SEEN
