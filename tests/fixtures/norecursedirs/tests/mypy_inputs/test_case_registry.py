# Input for static mypy - not a pytest test.
# The file is invalid as a pytest module (it fails on import); norecursedirs
# must exclude the whole directory from collection.
raise RuntimeError("mypy-only input, must never be collected")


def test_never_collected():
    raise AssertionError("norecursedirs violated")
