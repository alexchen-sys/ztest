import pytest


def test_outcomes_functions():
    # from _pytest.outcomes import skip
    from _pytest.outcomes import Skipped
    from _pytest.outcomes import exit as o_exit
    from _pytest.outcomes import fail as o_fail
    from _pytest.outcomes import skip as o_skip
    from _pytest.outcomes import xfail as o_xfail

    with pytest.raises(Skipped):
        o_skip("via outcomes")


def test_after_skip():
    # the previous test ends up skipped, this one must pass
    assert True
