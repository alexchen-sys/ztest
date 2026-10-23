import pytest

def _boom():
    raise ValueError("kaboom")

def test_traceback_entries():
    with pytest.raises(ValueError) as ei:
        _boom()
    joined = "".join(str(e) for e in ei.traceback)
    assert "test_ei.py'" in joined
    assert "in _boom" in joined
    assert "raise ValueError" in joined
