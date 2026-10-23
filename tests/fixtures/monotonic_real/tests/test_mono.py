def test_finite_monotonic(monkeypatch):
    # the test replaces time.monotonic with a finite iterator; the
    # shim's internal timers/IPC must use saved references, otherwise
    # StopIteration blows up the report mid-run.
    ts = iter([100.0, 100.1, 100.2, 100.3])
    monkeypatch.setattr("time.monotonic", lambda: next(ts))
    assert True
