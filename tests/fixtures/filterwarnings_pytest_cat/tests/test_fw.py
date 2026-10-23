import warnings


def test_filterwarnings_pytest_category_resolved():
    # categories resolve (no CollectionError); the pytest-side warning is ignored
    import pytest
    warnings.warn("cached", pytest.PytestCacheWarning)
    assert True


def test_plain_ok():
    assert True
