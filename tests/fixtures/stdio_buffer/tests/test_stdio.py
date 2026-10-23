import io
import sys

import pytest


def test_stdout_has_buffer():
    # black pattern: sys.stdout.buffer.write under capture
    assert hasattr(sys.stdout, "buffer")
    sys.stdout.buffer.write(b"")
    sys.stdout.flush()


def test_stdin_is_dontreadfrominput():
    # target streams_are_fine() parity: stdin under capture is not io.IOBase
    assert not isinstance(sys.stdin, io.IOBase)
    with pytest.raises(OSError):
        sys.stdin.read()


def test_capsys_buffer_roundtrip(capsys):
    sys.stdout.buffer.write(b"bytes-via-buffer\n")
    print("text-via-write")
    out, _ = capsys.readouterr()
    assert "bytes-via-buffer" in out
    assert "text-via-write" in out
