import pytest


@pytest.fixture
def tag(request):
    return getattr(request, "param", None)


@pytest.fixture(autouse=True)
def _au(tag):
    return tag


def pytest_generate_tests(metafunc):
    if "tag" in metafunc.fixturenames:
        metafunc.parametrize("tag", ["t1", "t2"])
