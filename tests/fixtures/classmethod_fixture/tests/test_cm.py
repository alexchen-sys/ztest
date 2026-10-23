import pytest

class TestWithClassmethodFixture:
    @pytest.fixture(autouse=True, scope="class")
    @classmethod
    def needs_missing(cls):
        pytest.importorskip("definitely_missing_module_xyz")

    def test_a(self):
        assert False, "must be skipped via classmethod autouse fixture"

    def test_b(self):
        assert False, "must be skipped via classmethod autouse fixture"

class TestNoFixture:
    def test_c(self):
        assert True
