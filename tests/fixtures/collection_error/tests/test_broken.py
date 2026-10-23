"""A file that dies at import: the collection error must be visible in the
summary, not folded into an anonymous failed count."""

import nonexistent_dependency_xyz  # noqa: F401  (ImportError at collection)


def test_never_runs():
    assert True
