"""zzz_bench_plugin — passive pytest outcome and collection recorder for bench100.

Does not alter pytest execution; records per-test outcomes and collected nodeids
to JSONL files specified by BENCH_JSONL and BENCH_COLLECT.
"""
import json
import os

_PATH = os.environ.get("BENCH_JSONL", "/tmp/out/pytest-events.jsonl")
_COLLECT = os.environ.get("BENCH_COLLECT", "")
_buf = []
_cbuf = []


def _flush(path, buf):
    if not buf:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            for obj in buf:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass
    buf.clear()


def pytest_sessionstart(session):
    for p in (_PATH, _COLLECT):
        if p and p != "/dev/null":
            try:
                with open(p, "w", encoding="utf-8"):
                    pass
            except Exception:
                pass


def pytest_runtest_logreport(report):
    rec = {"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome}
    wx = getattr(report, "wasxfail", None)
    if wx is not None:
        rec["wasxfail"] = str(wx)[:200]
    _buf.append(rec)
    if len(_buf) >= 500:
        _flush(_PATH, _buf)


def pytest_collection_finish(session):
    if not _COLLECT:
        return
    try:
        items = list(session.items)
    except Exception:
        return
    for item in items:
        _cbuf.append({"nodeid": item.nodeid})
    _flush(_COLLECT, _cbuf)


def pytest_sessionfinish(session, exitstatus):
    _flush(_PATH, _buf)
    _flush(_COLLECT, _cbuf)
