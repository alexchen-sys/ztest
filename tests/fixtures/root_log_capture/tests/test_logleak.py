import io
import logging
import sys


def test_warning_not_leaked_to_stderr():
    # without the root capture handler
    # logging.lastResort writes the warning to the interceptor's sys.stderr.
    logger = logging.getLogger("fxprobe45")
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        logger.warning("must-not-leak %s", 42)
    finally:
        sys.stderr = old
    assert buf.getvalue() == "", f"leaked: {buf.getvalue()!r}"
