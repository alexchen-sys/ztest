"""Rate-limit heartbeat gate, part 1.

Scenario (the gate runs the suite with `--timeout 2`, see the corresponding
test in tests/test_oss_regressions.py):

1) a series of 50 fast tests - exactly here the TEST_START / PHASE_START /
   TEARDOWN_START / TEST_END pulses fall under rate-limit
   (H = min(0.1, (window - 1.5) / 8));
2) `test_sleep_just_under_timeout` sleeps T-0.2 = 1.8 s (< the phase itimer
   2.0 s) and must PASS: the supervisor window margin (3.5 s) shrinks by at
   most H <= 0.1 s when the pulse is suppressed;
3) `test_multiphase_progress` is three 1.5 s phases (setup/body/teardown).
   Each phase is shorter than the itimer (2.0 s), but the whole test runs
   4.5 s > the 3.5 s window: the suite stays alive ONLY because the
   phase-boundary pulses (no more often than once per 1.5 s > H) actually go
   on the wire and re-arm the inactivity deadline. That is the discriminating
   part: if suppression eats the phase pulses, the supervisor kills the
   worker -> CRASHED instead of PASS.
"""
import time

import pytest

TIMEOUT_S = 2.0            # --timeout 2 in the gate
SLEEP_S = TIMEOUT_S - 0.2  # 1.8 s
PHASE_S = 1.5              # < itimer 2.0 s; 3 phases = 4.5 s > the 3.5 s window


@pytest.fixture
def slow_phases():
    time.sleep(PHASE_S)          # setup phase
    yield None
    time.sleep(PHASE_S)          # teardown phase


@pytest.mark.parametrize("i", range(50))
def test_fast(i):
    assert i >= 0


def test_sleep_just_under_timeout():
    time.sleep(SLEEP_S)


def test_multiphase_progress(slow_phases):
    time.sleep(PHASE_S)          # body phase
