import traceback as tbmod
import warnings

import pytest


class W1(UserWarning):
    pass


class W2(W1):
    pass


def test_pop_sibling_rule():
    # pytest recwarn.pop: later non-subclass candidate displaces an earlier
    # more-specific one (exact pytest _pytest.recwarn.pop semantics)
    with pytest.WarningsRecorder(_ispytest=True) as rec:
        warnings.warn("a", W2)
        warnings.warn("b", W1)
        w = rec.pop(UserWarning)
        assert w.category is W1


def test_lineno_zero_based():
    # pytest TracebackEntry.lineno is 0-based
    with pytest.raises(ValueError) as ei:
        raise ValueError("x")
    fs = tbmod.extract_tb(ei.value.__traceback__)[-1]
    assert ei.traceback[-1].lineno == fs.lineno - 1


def test_legacypath_eq_str(tmpdir):
    # py.path.local equality against plain strings
    assert tmpdir == str(tmpdir)
    assert tmpdir != str(tmpdir) + ".other"
