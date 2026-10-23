"""Rate-limit heartbeat gate, part 2: a setup hanging AFTER a series of fast
tests, `--timeout 2`.

The real per-test worker timeout (phase itimer 2.0 s -> _TestTimeoutError ->
TEST_FAIL error="TestTimeoutError") must fire EARLIER than the supervisor's
inactivity-kill (3.5 s). The signature of the real timeout: the 50 fast tests
are already counted (passed=50), the file sent FILE_DONE, the status is not
CRASHED.

Even if rate-limit suppressed the last heartbeat, the deadline shift is bounded
by H <= 0.1 s (3.4 s > 2.0 s), so a supervisor kill remains impossible.
"""
import time

import pytest

TIMEOUT_S = 2.0   # --timeout 2 in the gate
HANG_S = 30.0     # far longer than both the timeout and the inactivity window


@pytest.fixture
def hangs_in_setup():
    time.sleep(HANG_S)
    yield None


@pytest.mark.parametrize("i", range(50))
def test_fast(i):
    assert i >= 0


def test_hung_setup(hangs_in_setup):
    assert True
