import os
import sys


def test_preload_matches_opt_in():
    # The case driver sets ZTEST_FIXTURE_PRELOAD_EXPECT=probe for the arms that
    # request the preload (--preload / ZTEST_PRELOAD_PKGS) and leaves it unset
    # for the default arm. The module is importable in every arm — only the
    # preload decision may differ.
    expect = os.environ.get("ZTEST_FIXTURE_PRELOAD_EXPECT", "clean")
    if expect == "probe":
        assert "ztest_probe_mod" in sys.modules, "requested --preload module missing from the zygote"
    else:
        assert "ztest_probe_mod" not in sys.modules, "third-party module preloaded without an opt-in"
