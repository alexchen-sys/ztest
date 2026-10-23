import pytest

CALLS = []


def _freeze_class(klass):
    # target decorate_class pattern: fixtures are detected by
    # _pytestfixturefunction/__pytest_wrapped__ and rewritten through .obj;
    # test methods are wrapped wholesale.
    import functools

    for attr, val in list(vars(klass).items()):
        if attr.startswith("_"):
            continue
        if getattr(val, "__dict__", {}).get("_pytestfixturefunction"):
            orig = val.__pytest_wrapped__.obj

            @functools.wraps(orig)
            def frozen_fixture(*a, _o=orig, **kw):
                CALLS.append(("fixture-body", attr))
                return _o(*a, **kw) + "-frozen"

            val.__pytest_wrapped__.obj = frozen_fixture
        elif callable(val) and attr.startswith("test"):

            @functools.wraps(val)
            def frozen_test(*a, _v=val, **kw):
                CALLS.append(("test-body", attr))
                return _v(*a, **kw)

            setattr(klass, attr, frozen_test)
    return klass


@_freeze_class
class TestFrozen:
    @pytest.fixture
    def sample(self):
        CALLS.append(("raw-fixture", "sample"))
        return "raw"

    def test_uses_fixture(self, sample):
        # the fixture body must have been replaced through __pytest_wrapped__.obj
        assert sample == "raw-frozen", f"fixture body did not go through __pytest_wrapped__.obj: {sample!r}"
