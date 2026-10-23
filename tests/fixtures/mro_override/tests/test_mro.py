import pytest


class Base:
    @pytest.fixture(scope="class")
    @classmethod
    def f(cls):
        return "base"

    @pytest.fixture
    def g(self):
        return "base-g"


class Sub(Base):
    @pytest.fixture(scope="class")
    @classmethod
    def f(cls):
        return "sub"

    @pytest.fixture
    def g(self):
        return "sub-g"


class TestOverride(Sub):
    def test_f(self, f):
        assert f == "sub"

    def test_g(self, g):
        assert g == "sub-g"
