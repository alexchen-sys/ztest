"""capsys stays Python level: fd writes are not captured by it."""
import os


def test_capsys_ignores_fd_writes(capsys):
    print("SYS_LEVEL_MARK")
    os.write(1, b"FD_LEVEL_MARK\n")
    out, err = capsys.readouterr()
    assert "SYS_LEVEL_MARK" in out
    assert "FD_LEVEL_MARK" not in out
    assert err == ""
