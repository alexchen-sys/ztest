import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor


def _square(x):
    return x * x


def _child(q):
    q.put(os.getpid())


def test_process_pool_executor():
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_square, [1, 2, 3]))
    assert results == [1, 4, 9]


def test_raw_process():
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(q,))
    p.start()
    pid = q.get(timeout=10)
    p.join(timeout=10)
    assert p.exitcode == 0
    assert pid != os.getpid()
