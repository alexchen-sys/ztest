import pytest


@pytest.fixture
def cf_plugin_fix():
    return "cf-ok"


def pytest_collection_modifyitems(config, items):
    # black tests/optional.py pattern: skip items by marker via live add_marker
    for item in items:
        if item.get_closest_marker("opt_skip"):
            item.add_marker(pytest.mark.skip(reason="disabled optional"))
