"""A fixture finalizer failure is a teardown-phase failure as well."""
import pytest


@pytest.fixture
def leaking():
    yield "value"
    raise RuntimeError("fixture finalizer boom")


@pytest.mark.xfail(reason="expected to fail")
def test_uses_fixture(leaking):
    assert leaking == "value"
