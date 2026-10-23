"""Supervision-gap suite: SLOW file of the gate suite.

The gate (the controlplane_delay test in tests/test_oss_regressions.py) runs
this suite with `-j2`/`-j4` and `--timeout 3`. The only worker delay inducible
here WITHOUT changing the protocol is `time.sleep(DELAY_S)` at module IMPORT
time, i.e. AFTER the worker has attached to a slot and sent `FILE_START`, but
BEFORE the first frame about this file.

Why here and not in a fixture/test: `--timeout T` (the worker's phase itimer)
is armed for the TEST duration, so `sleep(4)` inside setup/body is a real
test timeout, not a delayed control plane. Module import runs under the
FILE_START collection budget (600 s), so 4 s of slot silence is a legal
"worker attached, no frames yet" state: exactly what the supervisor sees when
a neighbor is busy with a synchronous REAP/FORK and not serving this slot.

What the gate proves: while slot S is silent for 4 s, the supervisor manages
to finalize (REAP + per-slot sweep + refill) the fast files F1/F2, and NOT A
SINGLE file gets a spurious inactivity-kill: run rc=0, summary.passed matches
the expectation, crashed=0, "CRASHED" is absent from the output.

The file is deliberately the largest in the suite: the rolling dispatcher
starts files in LPT order (by size), so S starts FIRST and its 4-second
silent window covers F1's finalization and F2's start/finish. The `test_a_*`
name gives the same property in batch mode (`--maxfail=1`), where LPT is off
and the order is lexicographic.
"""
import os
import time

# ~4 s of silence from an attached worker before the first frame about the file.
# The value can be raised via the FX_DELAY_S env var: the gate uses this for the
# "delay larger than the inactivity window (--timeout 3 => a 4.5 s window) - and
# still not inactivity" subcase, i.e. to pin the FILE_START collection-budget
# contract.
DELAY_S = float(os.environ.get("FX_DELAY_S", "4.0"))


# The delay point: module import (collection) - not a test, the itimer is not armed yet.
time.sleep(DELAY_S)


def test_slow_one():
    assert 1 + 1 == 2


def test_slow_two():
    assert "ztest" == "ztest"
