"""capfd captures at the file-descriptor level (pytest parity).

Everything here is invisible to capsys: os.write bypasses sys.stdout, and a
child process inherits fd 1/2 instead of the Python-level streams.
"""
import os
import subprocess
import sys


def test_capfd_captures_every_stream(capfd):
    print("PRINTED_MARK")
    os.write(1, b"FD_OUT_MARK\n")
    os.write(2, b"FD_ERR_MARK\n")
    subprocess.run(
        [sys.executable, "-c", "import sys; print('CHILD_OUT_MARK'); sys.stderr.write('CHILD_ERR_MARK\\n')"],
        check=True,
    )
    out, err = capfd.readouterr()
    assert "PRINTED_MARK" in out
    assert "FD_OUT_MARK" in out
    assert "CHILD_OUT_MARK" in out
    assert "FD_ERR_MARK" in err
    assert "CHILD_ERR_MARK" in err


def test_capfd_readouterr_truncates(capfd):
    os.write(1, b"FIRST\n")
    first, _ = capfd.readouterr()
    assert first == "FIRST\n"
    second, err = capfd.readouterr()
    assert second == ""
    assert err == ""


def test_capfd_stream_identity(capfd):
    # The streams are redirected to the captured file descriptors, so write
    # ordering is shared between print() and raw fd writes.
    print("ORDER_A")
    os.write(1, b"ORDER_B\n")
    out, _ = capfd.readouterr()
    assert out.index("ORDER_A") < out.index("ORDER_B")
    assert sys.stdout.fileno() > 0
