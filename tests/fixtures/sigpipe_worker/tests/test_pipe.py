import os


def test_broken_pipe_is_python_error():
    """The pytest worker lives with SIGPIPE=SIG_IGN (plain python): writing to
    a closed pipe raises BrokenPipeError, not death by signal 13
."""
    r, w = os.pipe()
    os.close(r)
    try:
        os.write(w, b"x")
    except BrokenPipeError:
        return
    raise AssertionError("expected BrokenPipeError")


def test_after_pipe():
    pass
