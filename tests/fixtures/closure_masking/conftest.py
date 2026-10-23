import pytest

_SUPPORTED = ["3.6", "3.7"]


@pytest.fixture(autouse=True)
def auto_tag():
    yield


@pytest.fixture
def works_in_py(each_version):
    # regression: fixture depends on a name that only the hook parametrizes
    return ("checker", each_version)


def pytest_generate_tests(metafunc):
    if "each_version" in metafunc.fixturenames:
        metafunc.parametrize("each_version", _SUPPORTED)
    if "tag_value" in metafunc.fixturenames and "auto_tag" in metafunc.fixturenames:
        # autouse fixtures are part of pytest's metafunc.fixturenames closure
        metafunc.parametrize("tag_value", ["tagged"])
