def pytest_report_teststatus(report):
    return None


def pytest_runtest_logreport(report):
    pass


def pytest_runtest_makereport(item, call):
    return None


def pytest_runtest_logstart(nodeid, location):
    pass


def pytest_runtest_logfinish(nodeid, location):
    pass


def pytest_assertrepr_compare(op, left, right):
    return None


def pytest_deselected(items):
    pass


def pytest_collectreport(report):
    pass
