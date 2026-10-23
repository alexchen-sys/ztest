import os
import sys


def test_stdout_fileno_is_real_fd():
    fd = sys.stdout.fileno()  # pytest fd-capture parity: a real fd, not UnsupportedOperation
    assert isinstance(fd, int) and fd >= 0
    assert os.isatty(fd) is False  # redirected to a temp file, not a tty


def test_stderr_fileno_is_real_fd():
    fd = sys.stderr.fileno()
    assert isinstance(fd, int) and fd >= 0
