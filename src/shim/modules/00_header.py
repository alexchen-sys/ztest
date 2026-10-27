# ztest shim source module: 00_header
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
"""
ztest_shim.py — Zero-pluggy Python 3.11+ test execution kernel for ztest (Zig 0.17.0).

The kernel delegates the test lifecycle to the standard library:
`unittest.TestCase.run` / `IsolatedAsyncioTestCase` drive setUp/body/tearDown/
doCleanups. The shim contributes:
  - ZtestResult(unittest.TestResult) — the event bridge to ZTST frames
    (outcome, reason, duration, capture buffers); the verdict is read from
    the public result lists, and `_Outcome` is used only (write-only) for the
    safety-net doCleanups;
  - _wrap_phase — one interceptor on the PUBLIC instance methods (setUp, test
    method, tearDown, doCleanups, asyncSetUp/asyncTearDown via async coroutine
    wrappers) giving per-phase budgets, re-arming disarm/consume timeout split,
    sticky timer flags, timestamps and the never-executed sentinel
    `_ztest_body_started`;
  - teardown-dominance: a timeout in tearDown/doCleanups sets
    `teardown_timed_out` and ZtestResult.stopTest() raises the final status;
  - pytest-style functions run through the same path via `_FunctionTestCase`;
  - capture stdout/stderr is a context manager OUTSIDE run().

Wire and containment contract: ZTST wire protocol (fd 199,
magic 0x5A545354, 20-byte header, CRC32); JSON FORK-protocol with the Zig
supervisor; worker lifecycle via setpgid/pdeathsig/IPC-channel rules. Full
pytest surface (approx/raises/warns/marks/MonkeyPatch/caplog/tmp_path/mocker);
assert-rewrite + AST cache; fixture DAG resolver with module/session scopes
finalized once per scope and partial finalization on failed setup; xfail
strict/ non-strict; expectedFailure; skip; subTest in body and teardown;
doCleanups LIFO; setUp-error => body never executed; per-phase budgets;
teardown-dominance; rejection sentinels (_ZtestNeverExecutedError for
coroutine-returning sync bodies, SYNC-COROUTINE-FAIL-CLOSED/ASYNC-RESULT-VALIDATED); Python 3.11+.

Comments tagged "Invariants:" name the invariants catalogued in docs/INVARIANTS.md.
"""
from __future__ import annotations

import ast
import asyncio
import collections
import contextlib
import ctypes
import enum
import errno
import faulthandler
import fnmatch
import functools
import gc
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import io
import itertools
import json
import logging
import marshal
import math
import os
import pathlib
import random
import re
import select
import shlex
import shutil
import signal
import socket
import struct
import sys
import tempfile
import time
import traceback
import types
import typing
import unittest
import unittest.mock
import warnings
import zlib
from pathlib import Path
from typing import Any, Callable

# Tests may monkeypatch time.monotonic/perf_counter with finite iterators
# The shim's internal timers/IPC must run on references captured
# at module start, otherwise StopIteration blows up the report mid-run
# (a crash inside send_event).
_REAL_MONOTONIC = time.monotonic
_REAL_MONOTONIC_NS = time.monotonic_ns
_REAL_PERF_COUNTER_NS = time.perf_counter_ns

SHIM_VERSION = "0.6.0-zig017.16"  # Part of the AST-cache key: a bump invalidates all cached modules (encode/audit semantics). Scope: unittest delegation (ZtestResult + _wrap_phase + teardown-dominance flag), scope-registry fixtures, buffered IPC transport, heartbeat rate-limit, zygote prewarm with opt-out project auto-preload, full AST assert rewriter (Compare chains, BoolOp, UnaryOp, Call, lazy Or repr), builtin fixture parity (doctest_namespace, capsysbinary, capfdbinary, capteesys, record_property), ExceptionInfo._excinfo, empty parametrize skip, --engine=pytest bridge, safe assert repr.
ZTST_MAGIC = 0x5A545354  # "ZTST"
IPC_TARGET_FD = 199
PR_SET_PDEATHSIG = 1

# A cached encoder + precompiled header Structs. Per-frame json.dumps
# rebuilds kwargs/re-creates the encoder; JSONEncoder.encode takes the same
# C path with the same flags (ensure_ascii=False, default=str, default
# separators) → payload bytes are identical. Header: "<IIII" + "<I" = "<IIIII"
# (magic, worker_pid, seq, len, crc32) — field order and sizes unchanged.
_JSON_ENCODER = json.JSONEncoder(ensure_ascii=False, default=str)
_HDR_PREFIX = struct.Struct("<IIII")
_HDR_CRC = struct.Struct("<I")

_ipc_active_fd: int | None = None
_real_stdout = sys.stdout
_real_stderr = sys.stderr


def _close_ipc_in_grandchild() -> None:
    """Registered via os.register_at_fork(after_in_child=...) in the worker.
    Unconditionally closes fd=199 in any grandchild process forked during setup, test, or teardown.
    """
    global _ipc_active_fd
    if _ipc_active_fd is not None:
        try:
            os.close(_ipc_active_fd)
        except OSError:
            pass
        _ipc_active_fd = None


def _arm_pdeathsig(expected_ppid: int, *, watch_thread: bool = True) -> None:
    """Parent-death arming (ADR-004-1).

    Linux: PR_SET_PDEATHSIG(SIGKILL) + getppid verification closes the classic
    fork/prctl TOCTOU window; a prctl failure is fatal (124), a parent that
    already died is fatal (125). Darwin has no prctl: the same contract is
    implemented by _darwin_parent_watch (kqueue EVFILT_PROC/NOTE_EXIT), with
    the identical getppid race guard. ``watch_thread=False`` is used by the
    zygote itself, which must stay single-threaded for fork safety; there the
    ctrl-socket EOF sweep and the supervisor's native kqueue batch watchdog
    carry parent-death containment instead of a Python thread.
    """
    if sys.platform == "darwin":
        if os.getppid() != expected_ppid:
            os._exit(125)
        if watch_thread:
            _darwin_parent_watch(expected_ppid)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(124)
    if os.getppid() != expected_ppid:
        os._exit(125)


# Darwin/XNU event constants (<sys/event.h>): EVFILT_PROC is -5, NOTE_EXIT is
# 0x80000000. struct kevent on 64-bit Darwin is 32 bytes: uintptr_t ident,
# int16_t filter, uint16_t flags, uint32_t fflags, intptr_t data, void *udata.
_DARWIN_EVFILT_PROC = -5
_DARWIN_NOTE_EXIT = 0x80000000
_DARWIN_EV_ADD = 0x0001
_DARWIN_EV_ENABLE = 0x0004
_DARWIN_EV_ONESHOT = 0x0010


class _DarwinKevent(ctypes.Structure):
    _fields_ = [
        ("ident", ctypes.c_uint64),
        ("filter", ctypes.c_int16),
        ("flags", ctypes.c_uint16),
        ("fflags", ctypes.c_uint32),
        ("data", ctypes.c_int64),
        ("udata", ctypes.c_void_p),
    ]


def _darwin_parent_watch(expected_ppid: int) -> None:
    """SIGKILL this process when ``expected_ppid`` exits (Darwin pdeathsig).

    A daemon thread blocks in kevent(2) on EVFILT_PROC/NOTE_EXIT. The kernel
    binds the note to the process, not to a reusable numeric pid, so the watch
    cannot fire on a recycled pid. Any kqueue setup/registration failure falls
    back to a 1 Hz getppid poll (reparenting proves the parent is gone); the
    ppid guard in _arm_pdeathsig already closed the pre-arm race. Internal
    errors never kill a healthy worker: only a confirmed parent exit does.
    """

    def _watch() -> None:
        dead = False
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            kq = libc.kqueue()
            if kq >= 0:
                try:
                    change = _DarwinKevent(
                        expected_ppid,
                        _DARWIN_EVFILT_PROC,
                        _DARWIN_EV_ADD | _DARWIN_EV_ENABLE | _DARWIN_EV_ONESHOT,
                        _DARWIN_NOTE_EXIT,
                        0,
                        None,
                    )
                    if libc.kevent(kq, ctypes.byref(change), 1, None, 0, None) == 0:
                        event = _DarwinKevent()
                        while not dead:
                            n = libc.kevent(kq, None, 0, ctypes.byref(event), 1, None)
                            if n == 1:
                                dead = True
                            elif n < 0 and ctypes.get_errno() == errno.EINTR:
                                continue
                            else:
                                break
                finally:
                    os.close(kq)
            while not dead:
                if os.getppid() != expected_ppid:
                    dead = True
                    break
                time.sleep(1.0)
        except Exception:
            return  # the native Zig watchdog + zygote EOF sweep still contain
        if not dead:
            return
        try:
            os.kill(os.getpid(), signal.SIGKILL)
        except Exception:
            pass
        os._exit(125)

    try:
        import threading

        threading.Thread(target=_watch, name="ztest-parent-watch", daemon=True).start()
    except Exception:
        pass  # no thread: the native containment layers still apply


# Return glibc free chunks to the OS before the fork fan-out. malloc_trim(0)
# releases only free chunks — no live object moves, the gc graph is identical
# before/after — so it is safe next to gc.collect()/gc.freeze() (the "trim
# before freeze" order is kept). glibc-only: on musl the symbol is absent ->
# getattr(None) -> a silent no-op, ~0.13 ms. On the real zygote the effect is
# ~0 (mallinfo2: fordblks 0.1-0.2 MB on a 17 MB arena; the residual ~22 MB RSS
# is pymalloc arenas, which trim never returns).
def _trim_free_arenas() -> None:
    """Return the allocator's free chunks to the OS (glibc malloc_trim), best-effort."""
    if sys.platform == "darwin":
        return  # libSystem's allocator has no malloc_trim(0) equivalent
    try:
        _trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
        if _trim is not None:
            _trim(0)
    except Exception:
        pass


def _close_extra_fds(keep: set[int]) -> None:
    """Close all open descriptors via /proc/self/fd (Linux) or /dev/fd (Darwin)."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            fds = [int(name) for name in os.listdir(fd_dir) if name.isdigit()]
        except Exception:
            continue
        for fd in fds:
            if fd not in keep:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return
    for fd in range(3, 1024):
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def _flush_user_stdio() -> None:
    """Flush the python-level stdio streams before the worker leaves.

    Every worker exit goes through os._exit() (no atexit, no interpreter
    teardown), so bytes sitting in a buffered text layer are simply lost.
    That is invisible when stdout is a terminal (line-buffered: each print
    lands at once) and fatal when stdout is a pipe or a file: CPython
    block-buffers it, so under `-s` the whole no-capture output of the file
    disappears while stderr (line-buffered) survives. Best-effort and
    non-raising: it runs on the exit path of every file.
    """
    for _stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            if _stream is not None:
                _stream.flush()
        except BaseException:
            pass


def _line_buffer_user_stdio() -> None:
    """Under -s, make the real stdio line-buffered (pytest -s behaves live).

    A block-buffered stdout keeps the test's print() in memory until the
    buffer fills or the process tears down, so a piped run shows nothing
    while the suite runs and a worker that dies (test-level os._exit, crash)
    drops the tail entirely. Line buffering restores the per-line behavior a
    terminal gives for free. Guarded: a replaced stream (StringIO and the
    like) has no reconfigure() and must not break the run.
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except BaseException:
            pass


