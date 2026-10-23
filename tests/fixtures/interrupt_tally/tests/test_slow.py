import os
import time


def test_slow():
    # Signals the driver that this file has started, which under -j1 means the
    # fast file before it already finished — the interrupt then lands mid-run
    # with one complete file behind it.
    sentinel = os.environ.get("ZT_INTERRUPT_SENTINEL")
    if sentinel:
        with open(sentinel, "w", encoding="utf-8") as fh:
            fh.write("started\n")
    time.sleep(60)
    assert True
