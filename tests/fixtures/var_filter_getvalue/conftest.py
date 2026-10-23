import pytest


@pytest.fixture
def fx(*args, **kwargs):
    return 1


def pytest_generate_tests(metafunc):
    # VAR_POSITIONAL/VAR_KEYWORD names must not leak into fixturenames
    assert "args" not in metafunc.fixturenames
    assert "kwargs" not in metafunc.fixturenames
