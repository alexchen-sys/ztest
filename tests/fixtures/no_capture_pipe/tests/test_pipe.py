import sys


def test_marker():
    print("STDOUT_MARKER_PIPE")
    print("STDERR_MARKER_PIPE", file=sys.stderr)
    assert True
