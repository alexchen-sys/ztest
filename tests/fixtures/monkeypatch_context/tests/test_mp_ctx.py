import os

from pytest import MonkeyPatch


def test_monkeypatch_context_classmethod():
    with MonkeyPatch.context() as m:
        m.setattr(os, "getcwd", lambda: "/frozen")
        assert os.getcwd() == "/frozen"
    # undo after leaving the context
    assert os.getcwd() != "/frozen"


def test_monkeypatch_context_setitem_delitem():
    d = {"a": 1}
    with MonkeyPatch.context() as m:
        m.setitem(d, "b", 2)
        assert d == {"a": 1, "b": 2}
    assert d == {"a": 1}
