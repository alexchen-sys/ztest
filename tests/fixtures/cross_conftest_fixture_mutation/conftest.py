# The root conftest mutates a fixture of another conftest module via
# pytest_configure. Any introspection/fixture cache snapshot taken before this
# hook runs must yield to the live module - otherwise the tests would see
# 'initial' instead of 'configured'.
import pytest


def pytest_configure(config):
    import tests.conftest as child

    @pytest.fixture
    def value():
        return "configured"

    child.value = value
