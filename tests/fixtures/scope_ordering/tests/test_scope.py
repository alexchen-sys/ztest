import pytest


@pytest.fixture
def func_fx():
    return 1


@pytest.fixture(scope="class")
def cls_fx(func_fx):
    return func_fx


class TestScope:
    def test_a(self, cls_fx):
        pass
