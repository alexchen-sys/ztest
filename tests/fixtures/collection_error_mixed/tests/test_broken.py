"""A file that dies at import, sitting next to a real failing test and a
passing one: the summary must keep both counters distinguishable."""

import nonexistent_dependency_xyz  # noqa: F401  (ImportError at collection)


def test_never_runs():
    assert True
