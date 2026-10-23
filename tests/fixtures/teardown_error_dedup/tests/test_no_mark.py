"""Without an xfail mark a failing finalizer fails the test — and the failure
is reported once, not once per bookkeeping path.
"""


def test_body_passes():
    assert True


def teardown_function(function):
    raise RuntimeError("module teardown boom")
