"""A failing test whose output explains the failure."""
import os
import sys


def helper(value):
    print(f"helper saw {value}")
    os.write(2, b"STDERR_DIAGNOSTIC\n")
    sys.stderr.write("STDERR_SYS_DIAGNOSTIC\n")
    return value


def test_fails_after_printing():
    print("STDOUT_BEFORE_FAILURE")
    assert helper(3) == 3
    assert helper(4) == 5, "values differ"
