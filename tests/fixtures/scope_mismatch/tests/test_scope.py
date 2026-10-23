import pytest


@pytest.fixture(scope="class")
def cls_dep(axis):
    return axis


def pytest_generate_tests(metafunc):
    if "axis" in metafunc.fixturenames:
        metafunc.parametrize("axis", [1, 2])


class TestScope:
    def test_a(self, cls_dep):
        pass
