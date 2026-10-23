import pytest


@pytest.mark.parametrize("v", [1, 2], ids="xy")
def test_str_ids(v):
    pass


@pytest.mark.parametrize("v", [10, 20, 30], ids=__import__("itertools").count())
def test_count_ids(v):
    pass
