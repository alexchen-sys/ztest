import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    # regression: an old-style wrapper hook; ztest runs hooks as plain
    # functions - the option must be ignored, not break the conftest.
    outcome = yield
    return outcome
