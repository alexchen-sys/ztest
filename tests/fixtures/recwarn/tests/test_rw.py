import pytest
import warnings

def test_recwarn_pop(recwarn):
    warnings.warn(UserWarning("u1"))
    warnings.warn(DeprecationWarning("d1"))
    assert len(recwarn) == 2
    w = recwarn.pop(UserWarning)
    assert isinstance(w.message, UserWarning)
    assert len(recwarn) == 1
    # .list attribute (pluggy pattern)
    assert isinstance(recwarn.list, list)
    assert recwarn.list[0].category is DeprecationWarning
    with pytest.raises(AssertionError):
        recwarn.pop(RuntimeWarning)

def test_recwarn_no_default_pop_typeerror(recwarn):
    warnings.warn(UserWarning("keep"))
    assert recwarn.pop(UserWarning).category is UserWarning
