import pytest


def test_excinfo_repr_contains_message():
    # str/repr(excinfo) carry the exception text
    with pytest.raises(ValueError) as e:
        raise ValueError("Outline Action Missing /D")
    assert "Outline Action Missing /D" in str(e)
    assert "Outline Action Missing /D" in repr(e)


def test_excinfo_exconly():
    # exconly() is "TypeName: message"
    with pytest.raises(RuntimeError) as exc:
        raise RuntimeError("boom happened")
    assert exc.exconly() == "RuntimeError: boom happened"


def test_legacypath_truediv(tmpdir):
    # the py.path.local `/` operator (tmpdir is a LegacyPath)
    sub = tmpdir / "nested.pdf"
    assert str(sub).endswith("nested.pdf")
    sub.write_binary(b"%PDF-1.4")
    assert sub.read_binary() == b"%PDF-1.4"
