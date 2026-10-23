#!/usr/bin/env python3
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

SHIM_VERSION = "0.5.0-zig017.14"  # Part of the AST-cache key: a bump invalidates all cached modules (encode/audit semantics). Scope: unittest delegation (ZtestResult + _wrap_phase + teardown-dominance flag), scope-registry fixtures, buffered IPC transport, heartbeat rate-limit, zygote prewarm, full AST assert rewriter (Compare chains, BoolOp, UnaryOp, Call, lazy Or repr), builtin fixture parity (doctest_namespace, capsysbinary, capfdbinary, capteesys, record_property), ExceptionInfo._excinfo, empty parametrize skip, --engine=pytest bridge, safe assert repr.
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


def _arm_pdeathsig(expected_ppid: int) -> None:
    """PR_SET_PDEATHSIG(SIGKILL) + getppid verification (worker re-arm)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(124)
    if os.getppid() != expected_ppid:
        os._exit(125)


# Return glibc free chunks to the OS before the fork fan-out. malloc_trim(0)
# releases only free chunks — no live object moves, the gc graph is identical
# before/after — so it is safe next to gc.collect()/gc.freeze() (the "trim
# before freeze" order is kept). glibc-only: on musl the symbol is absent ->
# getattr(None) -> a silent no-op, ~0.13 ms. On the real zygote the effect is
# ~0 (mallinfo2: fordblks 0.1-0.2 MB on a 17 MB arena; the residual ~22 MB RSS
# is pymalloc arenas, which trim never returns).
def _trim_free_arenas() -> None:
    """Return the allocator's free chunks to the OS (glibc malloc_trim), best-effort."""
    try:
        _trim = getattr(ctypes.CDLL(None), "malloc_trim", None)
        if _trim is not None:
            _trim(0)
    except Exception:
        pass


def _close_extra_fds(keep: set[int]) -> None:
    """Close all open descriptors via /proc/self/fd regardless of RLIMIT_NOFILE."""
    try:
        fds = [int(name) for name in os.listdir("/proc/self/fd") if name.isdigit()]
        for fd in fds:
            if fd not in keep:
                try:
                    os.close(fd)
                except OSError:
                    pass
    except Exception:
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


# ============================================================================
# 1. Binary IPC Protocol on fd = 199 (20-byte header + CRC32 + NDJSON payload)
# ============================================================================

# User tests patch the stdlib (unittest.mock.patch of the global json.dumps
# and the like) — a MagicMock leaking into IPC kills the worker (crc32 over
# a MagicMock → TypeError → file crash). The IPC core's immunity is layered:
# _JSON_ENCODER/_HDR_PREFIX/_HDR_CRC are captured as objects while the shim
# loads (before any user code) and never go through the patchable module
# attrs json.dumps/struct.pack; zlib.crc32 and os.write are patchable —
# pristine references are kept.
_ZLIB_CRC32_PRISTINE = zlib.crc32
_OS_WRITE_PRISTINE = os.write

# The single IPC channel of the worker process (installed in IpcChannel.__init__).
_ACTIVE_IPC: "IpcChannel | None" = None


def _flush_ipc_now() -> None:
    """An unconditional flush barrier before a call into user code.

    Contract: no pending frame may cross the "shim → user code" boundary
    (the test body, setup/teardown fixtures and finalizers, conftest hooks,
    the xunit lifecycle, module exec at collection). Invoked from the
    centralized call helpers (_call_arity_aware, _call_runtest_hook,
    FixtureResolver._invoke_fixture/teardown_scope, the _wrap_phase
    wrappers, load_module_with_ast_cache), not from every emission point.

    A transport error here does not escape: afterwards the channel is
    terminal, frames are silently skipped, and the missing FILE_DONE gives
    a fail-closed CRASHED at the supervisor (the same dead-IPC semantics).
    """
    ch = _ACTIVE_IPC
    if ch is None:
        return
    try:
        ch.flush()
    except OSError:
        pass


class IpcChannel:
    # Absolute deadline for sending one frame under backpressure (seconds).
    SEND_DEADLINE_S = 30.0
    # Worker transport batching: a bounded out-buffer. A frame is encoded in
    # the same format (header+payload+CRC32) and gets its seq at enqueue; it
    # sits in the buffer until flush; flush = one coalesced write cycle over
    # the whole buffer with the same write discipline (sigmask held across the
    # nonblocking write loop, poll(POLLOUT) without the mask under the
    # absolute SEND_DEADLINE_S deadline). Auto-flush when: frames >= 64 or
    # bytes >= 64 KiB or oldest-frame age > min(50 ms, supervisor_window/4) — age is checked on every enqueue.
    MAX_BUFFERED_FRAMES = 64
    MAX_BUFFERED_BYTES = 65536
    MAX_BUFFER_AGE_S = 0.05
    # Heartbeat rate-limit: service pulse frames. A fast test emits ~11
    # frames, ~9 of them exactly these pulses (TEST_START, 5×PHASE_START,
    # 4×TEARDOWN_START, TEST_END). They do not mutate the supervisor summary
    # (main.zig::parseAndApplyFrameJson only re-arms the inactivity deadline),
    # so a pulse landing in an already "fresh" window (last successful write
    # < H ago) can be swallowed whole: the deadline is re-armed by that write.
    HEARTBEAT_EVENTS = frozenset({"TEST_START", "PHASE_START", "TEARDOWN_START", "TEST_END"})
    # H <= HB_MAX_GAP_S: the upper bound of the heartbeat shift (see set_supervisor_window).
    HB_MAX_GAP_S = 0.1

    def __init__(self, fd: int, worker_pid: int) -> None:
        global _ACTIVE_IPC
        self.fd = fd
        self.worker_pid = worker_pid
        self.seq_no = 0
        self.terminal = False  # an interrupted frame → the channel is irreversibly terminal
        self._obuf = bytearray()  # pending frames in original order
        self._oframes = 0
        self._ooldest_s = 0.0  # _REAL_MONOTONIC() at enqueue of the oldest frame
        self._age_limit_s = self.MAX_BUFFER_AGE_S
        # Heartbeat rate-limit: the suppression window. _last_write_s is the
        # moment of the last successful write (not enqueue): the supervisor
        # re-arms its inactivity deadline exactly on a frame write. 0.0 means
        # "no writes yet" → the first pulse always goes out; seq/CRC unchanged.
        self._last_write_s = 0.0
        self._hb_min_gap_s = self.HB_MAX_GAP_S
        # One channel per worker process — the entry point for flush barriers
        # (_flush_ipc_now) from helpers that hold no ipc reference (fixtures,
        # conftest hooks, module exec, the xunit lifecycle).
        _ACTIVE_IPC = self

    def set_supervisor_window(self, window_s: float) -> None:
        """The intentional frame-hold limit = min(50 ms, supervisor_window/4).

        window is _PhaseBudgets._supervisor_window_s() (3600 s without
        --timeout; max(T,1500)+1500 ms with it): a pending frame may not age
        past a quarter of the supervisor inactivity window.
        """
        try:
            win = float(window_s)
        except (TypeError, ValueError):
            win = 3600.0
        if not win > 0:
            win = 3600.0
        self._age_limit_s = min(self.MAX_BUFFER_AGE_S, win / 4.0)
        # H = min(0.1 s, (window − phase floor)/8). The worker's phase itimer
        # = max(T, 1.5 s) = window − _PHASE_BUDGET_FLOOR_S (invariant INACTIVITY-WINDOW-DOMINATES,
        # mirrors main.zig::workerReadTimeoutMs), so a phase starting right after
        # a suppressed pulse gets a window remainder ≥ window − H = itimer +
        # 1.4 s: the worker's SIGALRM fires before the supervisor kill for any --timeout T.
        try:
            self._hb_min_gap_s = min(
                self.HB_MAX_GAP_S, max(0.0, (win - _PHASE_BUDGET_FLOOR_S) / 8.0)
            )
        except Exception:
            self._hb_min_gap_s = self.HB_MAX_GAP_S

    def _encode_frame(self, event_type: str, data: dict[str, Any]) -> bytes:
        # Cheap encoding: a cached JSONEncoder + precompiled Structs
        # (instead of json.dumps/struct.pack, which re-match the format on
        # every call). Wire invariant: the frame bytes are byte-for-byte equal
        # to the plain json.dumps/struct.pack encoding of the same payload
        # (unicode, big ints, nested dict/list, objects under default=str).
        payload_dict = {"t": event_type, **data}
        payload = _JSON_ENCODER.encode(payload_dict).encode("utf-8")
        hdr_prefix = _HDR_PREFIX.pack(
            ZTST_MAGIC,
            self.worker_pid & 0xFFFFFFFF,
            self.seq_no,
            len(payload))
        # zlib.crc32 is patchable (module attr) — only a pristine reference;
        # _HDR_CRC.pack is a bound method of a C type, not patchable.
        crc = _ZLIB_CRC32_PRISTINE(payload, _ZLIB_CRC32_PRISTINE(hdr_prefix)) & 0xFFFFFFFF
        header = hdr_prefix + _HDR_CRC.pack(crc)
        # seq wraps explicitly over u32 (struct.pack("<I") raises on overflow,
        # it does not truncate); the supervisor mirrors the wrapping add (+%=).
        self.seq_no = (self.seq_no + 1) & 0xFFFFFFFF
        return header + payload

    def send_event(
        self, event_type: str, *, defer: bool = False, force: bool = False, **data: Any
    ) -> None:
        """The single ZTST frame emission point (buffered transport).

        defer=False (default): the frame is appended to the buffer and flushed
        IMMEDIATELY together with the accumulated prefix in original seq order
        (an empty buffer means exactly one write cycle).
        defer=True: the frame stays buffered until an auto-flush on caps/age
        or the nearest unconditional flush barrier. Contract: no pending
        frames remain when control passes to user code.
        force=True: disables heartbeat suppression — the pulse must reach the
        wire even in a "fresh" window (INVARIANT TIMER-CHECKS-INSIDE-TRY: a heartbeat right
        before the drain). Terminal channel: enqueue is silently skipped.

        Heartbeat rate-limit: a service pulse (HEARTBEAT_EVENTS) with an empty
        out-buffer and a fresh (< H) successful write is SWALLOWED — whole,
        BEFORE _encode_frame: seq, CRC, the wire protocol and the supervisor
        are unchanged. Suppression is by event TYPE, not by payload: the four
        service types may carry diagnostic fields (nodeid, file, phase) and are
        still rate-limited. Outcome and collection frames (FAIL/SKIP/PASS/
        COLLECTION_*/FILE_*) are never in HEARTBEAT_EVENTS and always go out;
        force=True bypasses the limiter (TIMER-CHECKS-INSIDE-TRY).
        """
        if self.fd < 0 or self.terminal:
            return
        if (
            not force
            and event_type in self.HEARTBEAT_EVENTS
            and not self._obuf
            and self._hb_min_gap_s > 0.0
            and (_REAL_MONOTONIC() - self._last_write_s) < self._hb_min_gap_s
        ):
            # Swallowed before encoding: the frame takes no seq and never enters the buffer.
            return
        data = _sanitize_frame_data(data)
        frame = self._encode_frame(event_type, data)
        now = _REAL_MONOTONIC()
        if not self._obuf:
            self._ooldest_s = now
        self._obuf += frame
        self._oframes += 1
        if defer and not (
            self._oframes >= self.MAX_BUFFERED_FRAMES
            or len(self._obuf) >= self.MAX_BUFFERED_BYTES
            or (now - self._ooldest_s) > self._age_limit_s
        ):
            return
        self.flush()

    def flush(self) -> None:
        """Send the whole buffer in one coalesced write cycle.

        After the flush the buffer is empty. A transport error (deadline/
        EAGAIN exhaustion/an exception mid-write) makes the channel terminal
        — the receiver sees a broken CRC/truncated suffix → a
        fail-closed file crash; further frames are silently skipped.
        """
        if not self._obuf:
            return
        chunk = bytes(self._obuf)
        self._obuf.clear()
        self._oframes = 0
        self._ooldest_s = 0.0
        if self.fd < 0 or self.terminal:
            return
        self._write_all(chunk)

    def _write_all(self, frame: bytes) -> None:
        view = memoryview(frame)
        total = 0
        # SIGALRM/SIGINT are blocked across the nonblocking write loop — a mid-frame break = a broken CRC = the
        # file is reported CRASHED. Invariants: FRAME-SIGNAL-MASK, SIGINT-MASKED-WITH-SIGALRM.
        # The write is nonblocking with poll(POLLOUT) under an absolute deadline;
        # the mask is lifted while waiting (signals are delivered — the per-test
        # timeout still works under backpressure), save/restore via SIG_SETMASK; a
        # deadline or an interruption mid-frame → the channel is marked terminal
        # (further sends are silently skipped), the receiver sees a broken CRC → the file is reported CRASHED.
        prev_mask: Any = None
        try:
            prev_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGINT})
        except (AttributeError, OSError, ValueError):
            prev_mask = None
        # The poll object is created lazily — only when actually stuck on EAGAIN.
        # On the normal path (the socket takes the frame at once) no allocation/register is needed.
        poller = None
        deadline = _REAL_MONOTONIC() + self.SEND_DEADLINE_S
        try:
            while total < len(frame):
                try:
                    written = _OS_WRITE_PRISTINE(self.fd, view[total:total + 262144])
                except BlockingIOError as _bke:
                    # characters_written safety net (a raw os.write EAGAIN
                    # reports 0 bytes written; characters_written carries the
                    # same 0, but a missing or unusual attribute must not
                    # break the retry loop).
                    written = getattr(_bke, "characters_written", 0) or 0
                if written > 0:
                    total += written
                    continue
                if written < 0:
                    raise OSError("IPC socket closed mid-frame")
                # Backpressure: wait for writability without the mask under the absolute deadline.
                if prev_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, prev_mask)
                try:
                    remaining = deadline - _REAL_MONOTONIC()
                    if remaining <= 0:
                        raise OSError("IPC send deadline exceeded under backpressure")
                    if poller is None:
                        poller = select.poll()
                        poller.register(self.fd, select.POLLOUT)
                    poller.poll(int(min(remaining, 0.25) * 1000))
                finally:
                    if prev_mask is not None:
                        prev_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGINT})
            # "Wire is fresh" mark — only after a full successful write (a partial
            # write/deadline goes to the BaseException below and the channel becomes
            # terminal; by then pulses are not sent anyway).
            self._last_write_s = _REAL_MONOTONIC()
        except BaseException:
            self.terminal = True
            raise
        finally:
            if prev_mask is not None:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, prev_mask)
                except (AttributeError, OSError, ValueError):
                    pass


# ============================================================================
# 2. Exceptions & Outcomes (pytest / _pytest.outcomes compatibility)
# ============================================================================

class OutcomeException(BaseException):
    def __init__(self, msg: str = "", pytrace: bool = True) -> None:
        super().__init__(msg)
        self.msg = msg
        self.pytrace = pytrace


class Skipped(OutcomeException):
    pass


class Failed(OutcomeException):
    pass


class XFailed(OutcomeException):
    pass


class _StrictXPassError(OutcomeException):
    """Raised when an @pytest.mark.xfail(strict=True) test unexpectedly passes."""
    pass


class _TestTimeoutError(BaseException):
    """Per-test timeout: BaseException — never swallowed by a bare `except Exception` and never matched by xfail/skip."""
    pass


def _cancel_pending_loop_tasks(case: Any, budgets: Any = None) -> None:
    """An async-body timeout caught deep inside the event loop
    (selector.poll) leaves the body task PENDING — on the next loop restart
    (asyncTearDown) it resumes. Cancel all pending runner tasks BEFORE
    continuing the lifecycle:
    - the current task (an in-loop call from the wrapper) is NOT canceled;
    - in-loop call: cancel only — the drain happens at the next runner.run (teardown);
    - out-of-loop call (the addError bridge): a bounded drain via asyncio.wait
      (bound = min(_ZT_DRAIN_BOUND_S, supervisor window/2)) — a cancel-resistant
      task does not hang the process. Best effort: never breaks the bridge."""
    try:
        runner = getattr(case, "_asyncioRunner", None)
        loop = getattr(runner, "_loop", None) if runner is not None else None
        if loop is None or loop.is_closed():
            return
        import asyncio as _aio

        current = None
        if loop.is_running():
            try:
                current = _aio.current_task(loop)
            except RuntimeError:
                current = None
        pending = [t for t in _aio.all_tasks(loop) if not t.done() and t is not current]
        for t in pending:
            t.cancel()
        if not pending or loop.is_running():
            return
        bound = _ZT_DRAIN_BOUND_S
        if budgets is not None:
            try:
                bound = min(_ZT_DRAIN_BOUND_S, budgets._supervisor_window_s() / 2.0)  # noqa: SLF001
            except Exception:
                pass

        async def _drain() -> None:
            await _aio.wait(pending, timeout=max(bound, 0.5))

        loop.run_until_complete(_drain())
    except Exception:
        pass


def _cancel_runner_tasks_nowait(runner: Any) -> None:
    """Cancel a foreign runner's pending tasks without waiting (drain is separate,
    via budgets.drain_runner with a budget and poison). Best effort."""
    try:
        loop = runner.get_loop() if runner is not None else None
        if loop is None or loop.is_closed():
            return
        for t in asyncio.all_tasks(loop):
            if not t.done():
                t.cancel()
    except Exception:
        pass


class _CleanupTimeoutError(RuntimeError):
    """Timeout of a SINGLE cleanup callback: an ordinary teardown error —
    stdlib doCleanups continues the LIFO stack; control exceptions (KI/EscExit) still abort."""
    pass


class _ZtestNeverExecutedError(BaseException):
    """Sentinel for a never-executed body: BaseException, xfail-immune — xfail predicts the failure of an EXECUTED body."""
    pass


class Exit(OutcomeException):
    def __init__(self, msg: str = "", returncode: int | None = None) -> None:
        super().__init__(msg)
        self.returncode = returncode


# The canonical control-exception tuple — do not replace, do not xfail-swallow, do not rename. Invariants: CONTROL-EXCEPTIONS-CANONICAL.
_ZT_CONTROL_TYPES = (KeyboardInterrupt, SystemExit, _TestTimeoutError, Exit, _ZtestNeverExecutedError)


class ZtestUnsupportedApiError(AttributeError):
    """Subclasses AttributeError so hasattr(pytest, 'x') and getattr(pytest, 'x', default) work."""
    pass


def _ztest_close_unexecuted(obj: Any) -> None:
    """Best-effort close/cancel of a never-executed generator/asyncgen/coroutine result; even the .close lookup is guarded."""
    try:
        if isinstance(obj, (asyncio.Task, asyncio.Future)):
            try:
                obj.cancel()
            except BaseException:
                pass
        closer = getattr(obj, "close", None)
        if callable(closer):
            closer()
    except BaseException:
        pass


# ============================================================================
# 3. Helpers: approx, raises, warns, param, Mark
# ============================================================================

class ApproxValue:
    def __init__(self, expected: Any, rel: float | None = None, abs: float | None = None, nan_ok: bool = False) -> None:
        self.expected = expected
        self.rel = rel
        self.abs = abs
        self.nan_ok = nan_ok

    def _check_scalar(self, a: Any, b: Any) -> bool:
        import numbers as _num

        # pytest ApproxScalar semantics:
        # A) rel scales by |expected|, not by max(|a|,|b|) (math.isclose is symmetric — wrong);
        # B) a non-numeric operand is not coerced to float ("1" != approx(1));
        # C) the default abs=1e-12 is kept even with an explicit rel (see the approx() factory).
        if not isinstance(a, _num.Number) or not isinstance(b, _num.Number):
            return False
        try:
            # an int pair is computed in EXACT int arithmetic —
            # float() merges distinct large ints (2**53 and 2**53+1).
            if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) and not isinstance(b, bool):
                rel_tol = self.rel if self.rel is not None else 1e-6
                abs_tol = self.abs if self.abs is not None else 1e-12
                return abs(a - b) <= max(rel_tol * abs(b), abs_tol)
            # Decimal/Fraction are computed in the EXACT arithmetic of their type
            # (tolerances via str — like pytest); float merging is forbidden.
            from decimal import Decimal
            from fractions import Fraction

            if isinstance(a, (Decimal, Fraction)) or isinstance(b, (Decimal, Fraction)):
                rel_tol = self.rel if self.rel is not None else 1e-6
                abs_tol = self.abs if self.abs is not None else 1e-12
                T = Decimal if (isinstance(a, Decimal) or isinstance(b, Decimal)) else Fraction
                try:
                    ca = a if isinstance(a, T) else T(str(a))
                    cb = b if isinstance(b, T) else T(str(b))
                    # inf/nan in the exact branch — before the formula
                    # (otherwise Inf <= max(rel*Inf, abs) fabricates a PASS for finite-vs-inf).
                    if ca == cb:
                        return True
                    _fin = lambda x: x.is_finite() if hasattr(x, "is_finite") else True  # noqa: E731
                    if not (_fin(ca) and _fin(cb)):
                        return False
                    return abs(ca - cb) <= max(T(str(rel_tol)) * abs(cb), T(str(abs_tol)))
                except Exception:
                    return False
            fa, fb = float(a), float(b)
            # inf/nan are handled before the formula — otherwise inf<=inf
            # fabricates a PASS for finite-vs-inf, and inf==inf sinks into NaN arithmetic.
            if fa == fb:
                return True
            if math.isnan(fa) and math.isnan(fb):
                return self.nan_ok
            if math.isnan(fa) or math.isnan(fb) or math.isinf(fa) or math.isinf(fb):
                return False
            rel_tol = self.rel if self.rel is not None else 1e-6
            abs_tol = self.abs if self.abs is not None else 1e-12
            return abs(fa - fb) <= max(rel_tol * abs(fb), abs_tol)
        except Exception:
            return bool(a == b)

    def __eq__(self, actual: Any) -> bool:
        if isinstance(self.expected, dict) and isinstance(actual, dict):
            if set(self.expected.keys()) != set(actual.keys()):
                return False
            return all(ApproxValue(self.expected[k], self.rel, self.abs, self.nan_ok) == actual[k] for k in self.expected)
        if hasattr(self.expected, "__iter__") and not isinstance(self.expected, (str, bytes, dict)) and hasattr(actual, "__iter__") and not isinstance(actual, (str, bytes, dict)):
            exp_list = list(self.expected)
            act_list = list(actual)
            if len(exp_list) != len(act_list):
                return False
            return all(ApproxValue(e, self.rel, self.abs, self.nan_ok) == a for e, a in zip(exp_list, act_list))
        return self._check_scalar(actual, self.expected)

    def __repr__(self) -> str:
        return f"approx({self.expected!r})"


class _TracebackEntry:
    """String form of a pytest TracebackEntry — "  File '<path>':<line> in <name>\n  <source>\n"."""

    def __init__(self, fs: traceback.FrameSummary) -> None:
        self._fs = fs
        self.frame = fs

    @property
    def lineno(self) -> int:
        # pytest TracebackEntry.lineno — 0-based.
        return self._fs.lineno - 1

    @property
    def path(self) -> str:
        return self._fs.filename

    def __str__(self) -> str:
        loc = f"  File '{self._fs.filename}':{self._fs.lineno} in {self._fs.name}\n"
        if self._fs.line:
            loc += f"  {self._fs.line.strip()}\n"
        return loc


class ExceptionInfo:
    def __init__(self, exc_type: type[BaseException] | None = None, value: BaseException | None = None, tb: Any = None) -> None:
        self.type = exc_type
        self.value = value
        self.tb = tb
        self._excinfo: tuple[type[BaseException], BaseException, Any] | None = (
            (exc_type, value, tb) if exc_type is not None and value is not None else None
        )

    @property
    def typename(self) -> str:
        return self.type.__name__ if self.type else ""

    @property
    def traceback(self) -> list[_TracebackEntry]:
        # A pytest-like iterable traceback (extract_tb over
        # the captured tb); an empty tb → an empty list.
        if self.tb is None:
            return []
        try:
            frames = traceback.extract_tb(self.tb)
        except Exception:
            return []
        return [_TracebackEntry(fs) for fs in frames]

    def errisinstance(self, exc: type[BaseException] | tuple[type[BaseException], ...]) -> bool:
        # pytest ExceptionInfo.errisinstance: isinstance(value, exc).
        return isinstance(self.value, exc)

    def match(self, regexp: str | re.Pattern[str]) -> bool:
        text = str(self.value or "")
        if not re.search(regexp, text):
            raise Failed(f"Regex pattern {regexp!r} does not match {text!r}.")
        return True

    def __repr__(self) -> str:
        # pytest's ExceptionInfo.__repr__ =
        # "<ExceptionInfo ValueError('msg') tblen=N>" — tests search for the
        # exception text in str(excinfo)/repr(excinfo); without __repr__ the
        # object printed as "<__main__.ExceptionInfo object at 0x...>".
        return f"<ExceptionInfo {self.value!r} tblen={len(self.traceback)}>"

    def exconly(self, tryshort: bool = False) -> str:
        # pytest ExceptionInfo.exconly() — a one-line
        # "TypeName: message" (the tryshort argument is deprecated/ignorable).
        return f"{self.typename}: {self.value}"


class RaisesContext:
    def __init__(
        self,
        expected_exception: type[BaseException] | tuple[type[BaseException], ...],
        match: str | re.Pattern[str] | None = None,
        check: Callable[[BaseException], bool] | None = None,
    ) -> None:
        if check is not None and not callable(check):
            raise TypeError(f"check must be a callable, got {type(check).__name__}")
        self.expected = expected_exception
        self.match_expr = match
        self.check = check
        self.excinfo = ExceptionInfo()

    def __enter__(self) -> ExceptionInfo:
        return self.excinfo

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is None:
            raise Failed(f"DID NOT RAISE {self.expected}")
        if not issubclass(exc_type, self.expected):
            return False
        self.excinfo.type = exc_type
        self.excinfo.value = exc_val
        self.excinfo.tb = exc_tb
        self.excinfo._excinfo = (exc_type, exc_val, exc_tb)
        if self.match_expr is not None:
            self.excinfo.match(self.match_expr)
        if self.check is not None:
            if not self.check(exc_val):
                raise Failed(f"Check {self.check!r} did not return True for {exc_val!r}")
        return True


class WarnsContext:
    def __init__(self, expected_warning: type[Warning] | tuple[type[Warning], ...] = Warning, match: str | re.Pattern[str] | None = None) -> None:
        self.expected = expected_warning
        self.match_expr = match
        self._catch = warnings.catch_warnings(record=True)
        self.list: list[warnings.WarningMessage] = []

    def __enter__(self) -> list[warnings.WarningMessage]:
        self.list = self._catch.__enter__()
        warnings.simplefilter("always", self.expected)
        return self.list

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self._catch.__exit__(exc_type, exc_val, exc_tb)
        if exc_type is not None:
            return False
        matching = [w for w in self.list if issubclass(w.category, self.expected)]
        if not matching:
            raise Failed(f"DID NOT WARN. No warnings of type {self.expected} were emitted.")
        if self.match_expr is not None:
            if not any(re.search(self.match_expr, str(w.message)) for w in matching):
                raise Failed(f"No warning matched {self.match_expr!r}")
        return False


class PytestWarning(UserWarning):
    """pytest.PytestWarning — the base pytest warning category.

    A bare usefixtures under pytest 9 warns with exactly this
    PytestWarning ("usefixtures() in <nodeid> without arguments has no
    effect"), so a selective error::pytest.PytestWarning filter must
    catch it. A UserWarning stub stayed silent under such a filter (a divergence)."""


# The standard pytest 9 warning taxonomy (_pytest.warning_types) —
# ini filterwarnings references pytest.* categories by name.
class PytestAssertRewriteWarning(PytestWarning):
    pass


class PytestCacheWarning(PytestWarning):
    pass


class PytestCollectionWarning(PytestWarning):
    pass


class PytestConfigWarning(PytestWarning):
    pass


class PytestDeprecationWarning(PytestWarning, DeprecationWarning):
    pass


class PytestExperimentalApiWarning(PytestWarning, FutureWarning):
    pass


class PytestRemovedIn9Warning(PytestDeprecationWarning):
    pass


class PytestUnknownMarkWarning(PytestWarning):
    pass


class PytestUnraisableExceptionWarning(PytestWarning):
    pass


class PytestUnhandledCoroutineWarning(PytestWarning):
    pass


class PytestReturnNotNoneWarning(PytestRemovedIn9Warning):
    pass


class WarningsRecorder:
    """pytest-compatible recwarn — NOT a plain list.

    _pytest.recwarn.WarningsRecorder semantics: .list, len/iter/getitem delegate
    to the recorded list under catch_warnings, .pop(cls) pops the first exact
    category match, else the first subclass match; no match —
    AssertionError (.list and recwarn.pop(Category)).
    """

    def __init__(self, _ispytest: bool = False) -> None:
        # pytest compatibility: direct construction requires _ispytest=True
        # (a marker of conscious use); the shim accepts and ignores it.
        self._catch = warnings.catch_warnings(record=True)
        self._list: list[warnings.WarningMessage] = []

    def __enter__(self) -> "WarningsRecorder":
        self._list = self._catch.__enter__()
        warnings.simplefilter("always")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self._catch.__exit__(exc_type, exc_val, exc_tb)
        return False

    @property
    def list(self) -> list[warnings.WarningMessage]:
        return self._list

    def __getitem__(self, i: int) -> warnings.WarningMessage:
        return self._list[i]

    def __iter__(self) -> Any:
        return iter(self._list)

    def __len__(self) -> int:
        return len(self._list)

    def pop(self, cls: type[Warning] = Warning) -> warnings.WarningMessage:
        # Exact _pytest.recwarn.WarningsRecorder.pop logic:
        # an exact match is returned immediately; among subclass candidates
        # a later candidate displaces the current best only if it is not a subclass of best
        # (i.e. a more specific/related one wins, a broad late one does not).
        best_idx: int | None = None
        for i, w in enumerate(self._list):
            if w.category == cls:
                return self._list.pop(i)  # exact match, stop looking
            if issubclass(w.category, cls) and (
                best_idx is None
                or not issubclass(w.category, self._list[best_idx].category)
            ):
                best_idx = i
        if best_idx is not None:
            return self._list.pop(best_idx)
        raise AssertionError(f"no warning of type {cls!r} found")

    def clear(self) -> None:
        self._list.clear()


class _PytestNodeBase:
    """A minimal pytest.Node for custom File/Item collectors.

    from_parent passes every argument the constructor declares: name/parent
    plus path/fspath plus the user kwargs — the same set pytest 9's
    NodeMeta._create ends up forwarding. A constructor that declares none of
    them (custom __init__(self, name, parent)) still gets the
    positional (name, parent) fallback. path/fspath are inherited from the
    parent unless passed explicitly (pytest: same).
    """

    def __init__(self, name: str = "", parent: Any = None, config: Any = None, session: Any = None, nodeid: Any = None, fspath: Any = None, path: Any = None, **kw: Any) -> None:
        # The positional order of a pytest Node is (name, parent,
        # config, session, nodeid); custom Item/Collector classes call
        # super().__init__(name, parent, config, session, nodeid) positionally.
        self.name = name
        self.parent = parent
        self.config = config if config is not None else getattr(parent, "config", None)
        self.session = session if session is not None else getattr(parent, "session", None)
        if path is None and fspath is not None:
            path = str(fspath)
        if path is None and parent is not None:
            path = getattr(parent, "path", None)
        self.path = Path(path) if path is not None else None
        self.fspath = LegacyPath(self.path) if self.path is not None else None
        _pnodeid = getattr(parent, "nodeid", "") if parent is not None else ""
        # pytest: nodeid = parent.nodeid + "::" + name even for an empty name
        # (custom items with name='' → "file.ext::"). Without
        # a parent the separator is not added.
        if nodeid:
            self.nodeid = str(nodeid)
        elif parent is None:
            self.nodeid = str(name or "")
        else:
            self.nodeid = f"{_pnodeid}::{name}"
        self.own_markers: list[Mark] = []
        self.keywords: dict[str, Any] = {self.name: True} if self.name else {}

    def add_marker(self, marker: Any, append: bool = True) -> None:
        m_obj: Mark | None = None
        if isinstance(marker, str):
            m_obj = Mark(marker)
        elif isinstance(marker, Mark):
            m_obj = marker
        elif hasattr(marker, "mark") and isinstance(getattr(marker, "mark"), Mark):
            m_obj = getattr(marker, "mark")
        elif hasattr(marker, "name"):
            m_obj = Mark(str(marker.name), tuple(getattr(marker, "args", ()) or ()), dict(getattr(marker, "kwargs", {}) or {}))
        if m_obj is None:
            raise ValueError(f"is not a string or pytest.mark.* Marker: {marker!r}")
        if append:
            self.own_markers.append(m_obj)
        else:
            self.own_markers.insert(0, m_obj)
        self.keywords[m_obj.name] = True

    def iter_markers(self, name: str | None = None) -> Any:
        cur: Any = self
        while cur is not None:
            for m in getattr(cur, "own_markers", None) or ():
                m_norm = getattr(m, "mark", m)
                if name is None or getattr(m_norm, "name", None) == name:
                    yield m_norm
            cur = getattr(cur, "parent", None)

    def get_closest_marker(self, name: str, default: Any = None) -> Any:
        for m in self.iter_markers(name):
            return m
        return default

    def setup(self) -> None:
        return None

    def teardown(self) -> None:
        return None

    @classmethod
    def from_parent(cls, parent: Any, **kw: Any) -> Any:
        path = kw.pop("path", None)
        fspath = kw.pop("fspath", None)
        name = kw.pop("name", None)
        if name is None:
            name = Path(path).name if path is not None else cls.__name__
        # Inspect __init__ once and forward only the parameters it declares
        # (matching pytest 9's NodeMeta._create without invoking __init__ twice,
        # so constructor side effects run once and a body TypeError is not
        # mistaken for a signature mismatch).
        try:
            _params = inspect.signature(cls.__init__).parameters
        except (TypeError, ValueError):
            return cls(name=name, parent=parent, path=path, fspath=fspath, **kw)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in _params.values()):
            return cls(name=name, parent=parent, path=path, fspath=fspath, **kw)
        _kw_names = {
            _n
            for _n, _p in _params.items()
            if _p.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        _call_kw: dict[str, Any] = {}
        if "path" in _kw_names:
            _call_kw["path"] = path
        if "fspath" in _kw_names:
            _call_kw["fspath"] = fspath
        for _k, _v in kw.items():
            if _k in _kw_names:
                _call_kw[_k] = _v
        if "name" in _kw_names and "parent" in _kw_names:
            return cls(name=name, parent=parent, **_call_kw)
        return cls(name, parent)

    def reportinfo(self) -> Any:
        return (self.fspath, None, self.name)

    def repr_failure(self, excinfo: Any) -> str:
        return str(excinfo.value)

    def _prunetraceback(self, excinfo: Any) -> None:
        return None


class PytestFile(_PytestNodeBase):
    """pytest.File — a collector: collect() yields Items."""

    def collect(self) -> Any:
        return ()


class PytestItem(_PytestNodeBase):
    """pytest.Item — an executable unit: runtest() raises AssertionError."""

    def setup(self) -> None:
        return None

    def teardown(self) -> None:
        return None

    def runtest(self) -> None:
        raise NotImplementedError("custom pytest.Item must define runtest()")


class _DirNodeStub:
    """Parent directory for pytest_collect_file(parent=...)."""

    def __init__(self, path: Path, nodeid: str, cfg: "ConfigStub") -> None:
        self.path = path
        self.fspath = LegacyPath(path)
        self.nodeid = nodeid
        self.config = cfg


def _norm_marks(raw: Any) -> list[Any]:
    if not raw:
        return []
    if isinstance(raw, Mark):
        return [raw]
    if hasattr(raw, "mark") and isinstance(getattr(raw, "mark", None), Mark):
        return [raw.mark]
    if hasattr(raw, "name") and not isinstance(raw, (list, tuple, set, dict, str, bytes)):
        return [raw]
    if isinstance(raw, (list, tuple, set)):
        out: list[Any] = []
        for m in raw:
            out.extend(_norm_marks(m))
        return out
    return []


def _run_custom_collect_items(
    ipc: "IpcChannel",
    filepath: Path,
    rel_file: str,
    cfg: "ConfigStub",
    conftest_mods: list[types.ModuleType],
    options: dict[str, Any] | None = None,
    target_filter: list[str] | None = None,
    py_fallback: bool = False,
    finalize_fn: Callable[[int], int] | None = None) -> int | None:
    """A non-.py file — a custom pytest_collect_file collector.

    firstresult semantics: a leaf-first chain, the first non-None wins; None
    along the whole chain — the file is not collected (NO_TESTS_SELECTED),
    and with py_fallback=True we return None — the caller continues normal
    .py collection (pytest calls pytest_collect_file first for ANY file;
    custom collectors may also claim *.py).
    Items run through their runtest(); pytest_runtest_teardown(item,
    nextitem) is called for each.
    """
    parent_stub = _DirNodeStub(filepath.parent, str(filepath.parent), cfg)
    file_node: Any = None
    for cf_mod in reversed(conftest_mods):
        _hook_cf = _invocable_conf_hook(cf_mod, "pytest_collect_file")
        if _hook_cf is None:
            continue
        _cf_kw = _bind_hook_kwargs(
            _hook_cf,
            {"parent": parent_stub, "file_path": filepath, "path": LegacyPath(filepath), "config": cfg},
            "pytest_collect_file")
        _flush_ipc_now()  # the flush barrier before the user's pytest_collect_file
        _res = _hook_cf(**_cf_kw)
        if _res is not None:
            file_node = _res
            break
    if file_node is None:
        if py_fallback:
            return None  # a .py file the hook did not take — normal collection continues
        fin_f = finalize_fn(0) if finalize_fn is not None else 0
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_f, skipped=0, deselected=0, status="NO_TESTS_SELECTED", duration_ms=0)
        return 0 if fin_f == 0 else 1
    # Item nodeids are built from the file nodeid — normalized to rel_file.
    try:
        file_node.nodeid = rel_file
    except Exception:
        pass
    try:
        _raw_items = file_node.collect()
        items = list(_raw_items) if _raw_items is not None else []
    except BaseException:
        tb_s = traceback.format_exc()
        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<collection>", loc=rel_file, error="CollectionError", msg=tb_s, tb=tb_s)
        fin_f = finalize_fn(1) if finalize_fn is not None else 1
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_f, skipped=0, deselected=0, status="COLLECTION_ERROR", duration_ms=0)
        return 1
    for _it in items:
        _raw_om = getattr(_it, "own_markers", None)
        if _raw_om is None:
            try:
                _it.own_markers = []
            except Exception:
                pass
        else:
            try:
                _it.own_markers = [getattr(m, "mark", m) for m in _norm_marks(_raw_om)]
            except Exception:
                pass
    _session_stub = types.SimpleNamespace(config=cfg, items=[], exitstatus=0)
    _mod_list = list(items)
    _session_stub.items = _mod_list
    try:
        for cf_mod in conftest_mods:
            _hook_cmi = _invocable_conf_hook(cf_mod, "pytest_collection_modifyitems")
            if _hook_cmi is not None:
                _sig = inspect.signature(_hook_cmi)
                _kw: dict[str, Any] = {}
                for _pn in _sig.parameters:
                    if _pn == "session":
                        _kw[_pn] = _session_stub
                    elif _pn == "config":
                        _kw[_pn] = cfg
                    elif _pn == "items":
                        _kw[_pn] = _mod_list
                _flush_ipc_now()
                _check_conf_hook_result("pytest_collection_modifyitems", _hook_cmi(**_kw))
    except BaseException:
        tb_s = traceback.format_exc()
        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<collection_modifyitems>", loc=rel_file, error="CollectionError", msg=tb_s, tb=tb_s)
        fin_f = finalize_fn(1) if finalize_fn is not None else 1
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_f, skipped=0, deselected=0, status="COLLECTION_ERROR", duration_ms=0)
        return 1
    items = list(_mod_list)
    for _it in items:
        try:
            _it.own_markers = [getattr(m, "mark", m) for m in _norm_marks(getattr(_it, "own_markers", []))]
        except Exception:
            pass

    def _collect_custom_marks(node_obj: Any) -> list[Any]:
        out_m: list[Any] = []
        cur_n: Any = node_obj
        seen_ids: set[int] = set()
        while cur_n is not None and id(cur_n) not in seen_ids:
            seen_ids.add(id(cur_n))
            raw_om = getattr(cur_n, "own_markers", None)
            if raw_om:
                for m_entry in _norm_marks(raw_om):
                    out_m.append(getattr(m_entry, "mark", m_entry))
            cur_n = getattr(cur_n, "parent", None)
        return out_m

    def _eval_custom_marks(node_obj: Any) -> tuple[str | None, Mark | None]:
        _marks_list = _collect_custom_marks(node_obj)
        _sk_r: str | None = None
        _xf_m: Mark | None = None
        _it_mod = getattr(node_obj, "module", None)
        for _m in _marks_list:
            _mname = getattr(_m, "name", "")
            _margs = getattr(_m, "args", ()) or ()
            _mkw = getattr(_m, "kwargs", {}) or {}
            if _mname == "skip":
                _sk_r = str(_margs[0]) if _margs else str(_mkw.get("reason", "skipped"))
                break
            if _mname == "skipif":
                if not _margs and "condition" not in _mkw:
                    _sk_r = str(_mkw.get("reason") or "unconditional skip")
                    break
                _conds = list(_margs) if _margs else [_mkw.get("condition", False)]
                _matched = False
                for _c in _conds:
                    if isinstance(_c, str):
                        _c = bool(eval(_c, _skipif_eval_ns(_it_mod)))
                    if bool(_c):
                        _matched = True
                        break
                if _matched:
                    _sk_r = str(_mkw.get("reason") or "skipif")
                    break
            if _mname == "xfail":
                _cond = _margs[0] if _margs else _mkw.get("condition", True)
                if isinstance(_cond, str):
                    _cond = bool(eval(_cond, _skipif_eval_ns(_it_mod)))
                if _cond:
                    _xf_m = _m if isinstance(_m, Mark) else Mark("xfail", tuple(_margs), dict(_mkw))
        return _sk_r, _xf_m

    # Selection matches regular items — ::target, -k (substring
    # of the nodeid) and -m (by marks).
    deselected = 0
    k_expr = str((options or {}).get("k_expr") or "").strip()
    m_expr = str((options or {}).get("m_expr") or "").strip()
    if target_filter or k_expr or m_expr:
        _sel: list[Any] = []
        for it in items:
            _nid_f = getattr(it, "nodeid", None) or f"{rel_file}::{getattr(it, 'name', '')}"
            _rel_part = _nid_f[len(rel_file) + 2 :] if _nid_f.startswith(f"{rel_file}::") else _nid_f
            if target_filter:
                _suffix = "::".join(target_filter)
                if not (
                    _rel_part == _suffix
                    or _rel_part.startswith(f"{_suffix}[")
                    or _rel_part.startswith(f"{_suffix}::")
                ):
                    deselected += 1
                    continue
            _it_marks = _collect_custom_marks(it)
            if k_expr:
                _k_hay = _k_haystack(_nid_f, _it_marks, getattr(it, "cls", None))
                if not _eval_bool_expr(k_expr, lambda tok, h=_k_hay: tok.lower() in h):
                    deselected += 1
                    continue
            if m_expr:
                _marks = {getattr(m, "name", "") for m in _it_marks}
                if not _eval_bool_expr(m_expr, lambda tok, ms=_marks: tok in ms):
                    deselected += 1
                    continue
            _sel.append(it)
        items = _sel
    _su_hooks = [
        h
        for m in conftest_mods
        if (h := _invocable_conf_hook(m, "pytest_runtest_setup")) is not None
    ]
    _td_hooks = [
        h
        for m in conftest_mods
        if (h := _invocable_conf_hook(m, "pytest_runtest_teardown")) is not None
    ]
    ipc.send_event("COLLECTION_DONE", file=rel_file, collected=len(items))
    if (options or {}).get("collect_only", False):
        _ids = [getattr(it, "nodeid", None) or f"{rel_file}::{getattr(it, 'name', '')}" for it in items]
        if not (options or {}).get("llm_mode", False):
            for i_b in range(0, len(_ids), 100):
                ipc.send_event("COLLECT_BATCH", nodeids=_ids[i_b : i_b + 100])
        fin_f = finalize_fn(0) if finalize_fn is not None else 0
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_f, skipped=0, deselected=deselected, collected=len(_ids), status="COLLECTED", duration_ms=0)
        return 0 if fin_f == 0 else 1
    if not items:
        fin_f = finalize_fn(0) if finalize_fn is not None else 0
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_f, skipped=0, deselected=deselected, status="NO_TESTS_SELECTED" if fin_f == 0 else "FAILED", duration_ms=0)
        return 0 if fin_f == 0 else 1
    try:
        _xs_raw = cfg.getini("xfail_strict")
        _xfail_strict = str(_xs_raw).strip().lower() in ("1", "true", "yes", "on") if not isinstance(_xs_raw, bool) else _xs_raw
    except Exception:
        _xfail_strict = False
    passed = failed = skipped = 0
    _maxfail = int((options or {}).get("maxfail") or 0)
    _t_opt = (options or {}).get("timeout_us")
    _timeout_sec = _t_opt / 1_000_000.0 if (isinstance(_t_opt, int) and not isinstance(_t_opt, bool) and _t_opt > 0) else 0.0
    _active_collectors: list[Any] = []
    _failed_collectors: dict[int, BaseException] = {}
    _last_nid: str = rel_file

    def _collector_chain(it_node: Any) -> list[Any]:
        chain: list[Any] = []
        cur_p = getattr(it_node, "parent", None)
        seen_p: set[int] = set()
        while cur_p is not None and id(cur_p) not in seen_p and not isinstance(cur_p, _DirNodeStub):
            seen_p.add(id(cur_p))
            chain.append(cur_p)
            if cur_p is file_node:
                break
            cur_p = getattr(cur_p, "parent", None)
        if file_node is not None and all(c is not file_node for c in chain):
            chain.append(file_node)
        chain.reverse()
        return chain

    for _i, item in enumerate(items):
        _nid = getattr(item, "nodeid", None) or f"{rel_file}::{getattr(item, 'name', '')}"
        _last_nid = _nid
        _skip_reason, _xfail_mark = _eval_custom_marks(item)
        if _skip_reason is not None:
            skipped += 1
            ipc.send_event("TEST_SKIP", nodeid=_nid, reason=_skip_reason, duration_ms=0)
            continue
        if _xfail_mark is not None and not bool(_xfail_mark.kwargs.get("run", True)):
            skipped += 1
            ipc.send_event("TEST_SKIP", nodeid=_nid, reason="xfail (run=False): not executed", duration_ms=0)
            continue
        _clean_pass = False
        _body_passed = False
        _item_setup_ok = False
        ipc.send_event("TEST_START", nodeid=_nid)
        _t0 = _REAL_MONOTONIC_NS()
        _flush_ipc_now()  # the flush barrier before the custom item body
        _prev_alrm = None
        if _timeout_sec > 0:
            def _cc_alrm(_s: int, _f: Any) -> None:
                raise _TestTimeoutError(f"Test exceeded timeout of {_timeout_sec:.3f}s")
            _prev_alrm = signal.signal(signal.SIGALRM, _cc_alrm)
            signal.setitimer(signal.ITIMER_REAL, _timeout_sec)
        try:
            _req_cols = _collector_chain(item)
            while _active_collectors and (
                len(_active_collectors) > len(_req_cols)
                or any(a is not b for a, b in zip(_active_collectors, _req_cols[: len(_active_collectors)]))
            ):
                _pop_col = _active_collectors.pop()
                _col_td = getattr(_pop_col, "teardown", None)
                if callable(_col_td):
                    _col_td()
            for _col_n in _req_cols[len(_active_collectors) :]:
                if id(_col_n) in _failed_collectors:
                    raise _failed_collectors[id(_col_n)]
                _col_su = getattr(_col_n, "setup", None)
                if callable(_col_su):
                    try:
                        _col_su()
                    except BaseException as _col_exc:
                        _failed_collectors[id(_col_n)] = _col_exc
                        raise
                _active_collectors.append(_col_n)
            for _sh in _su_hooks:
                _call_runtest_hook(_sh, item, hook_name="pytest_runtest_setup")
            _item_setup = getattr(item, "setup", None)
            if callable(_item_setup):
                _item_setup()
            _item_setup_ok = True
            _post_skip, _post_xfail = _eval_custom_marks(item)
            if _post_xfail is not None:
                _xfail_mark = _post_xfail
            if _post_skip is not None:
                raise Skipped(_post_skip)
            if _xfail_mark is not None and not bool(_xfail_mark.kwargs.get("run", True)):
                raise XFailed("xfail (run=False): not executed")
            _res_cc = item.runtest()
            if inspect.iscoroutine(_res_cc):
                _res_cc.close()
                raise _ZtestNeverExecutedError(f"CustomItem {_nid!r} runtest() returned an unawaited coroutine")
        except (Skipped, unittest.SkipTest) as _sk:
            skipped += 1
            ipc.send_event("TEST_SKIP", nodeid=_nid, reason=str(_sk))
        except XFailed as _xf:
            skipped += 1
            ipc.send_event("TEST_SKIP", nodeid=_nid, reason=f"xfail: {_xf}")
        except BaseException as _exc:
            if isinstance(_exc, (KeyboardInterrupt, SystemExit)) and not isinstance(_exc, _TestTimeoutError):
                raise
            if _xfail_mark is not None and not isinstance(_exc, _TestTimeoutError) and _xfail_accepts(_xfail_mark, _exc):
                skipped += 1
                ipc.send_event("TEST_SKIP", nodeid=_nid, reason=f"xfail: {_exc}")
            else:
                failed += 1
                try:
                    _msg = item.repr_failure(ExceptionInfo(type(_exc), _exc, _exc.__traceback__))
                    if not isinstance(_msg, str):
                        _msg = str(_msg)
                except Exception:
                    _msg = f"{type(_exc).__name__}: {_exc}"
                _tb = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=_nid, loc=rel_file, error=type(_exc).__name__, msg=_msg, tb=_tb)
        else:
            _body_passed = True
        finally:
            if _timeout_sec > 0:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if _prev_alrm is not None:
                    signal.signal(signal.SIGALRM, _prev_alrm)
        _td_failed = False
        if _item_setup_ok:
            _item_td = getattr(item, "teardown", None)
            if callable(_item_td):
                try:
                    _item_td()
                except BaseException as _td_exc:
                    if isinstance(_td_exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    _td_failed = True
                    failed += 1
                    _tb = traceback.format_exc()
                    ipc.send_event("TEST_FAIL", nodeid=f"{_nid}::<teardown>", loc=rel_file, error=type(_td_exc).__name__, msg=_tb, tb=_tb)
        _next = items[_i + 1] if _i + 1 < len(items) else None
        if _next is None:
            while _active_collectors:
                _pop_col = _active_collectors.pop()
                _col_td = getattr(_pop_col, "teardown", None)
                if callable(_col_td):
                    try:
                        _col_td()
                    except BaseException as _col_td_exc:
                        if isinstance(_col_td_exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        _td_failed = True
                        failed += 1
                        _tb = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{_nid}::<file_teardown>", loc=rel_file, error=type(_col_td_exc).__name__, msg=_tb, tb=_tb)
        for _h in _td_hooks:
            try:
                _call_runtest_hook(_h, item, nextitem=_next, hook_name="pytest_runtest_teardown")
            except Exception:
                _td_failed = True
                failed += 1
                _tb = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=f"{_nid}::<runtest_teardown>", loc=rel_file, error="TeardownError", msg=_tb, tb=_tb)
        if _body_passed:
            _dur = (_REAL_MONOTONIC_NS() - _t0) // 1_000_000
            if _xfail_mark is not None and _xfail_mark.kwargs.get("strict", _xfail_strict):
                failed += 1
                _xmsg = f"[XPASS(strict)] {_xfail_mark.kwargs.get('reason', '')}"
                ipc.send_event("TEST_FAIL", nodeid=_nid, loc=rel_file, error="StrictXPassError", msg=_xmsg, tb=_xmsg)
            else:
                passed += 1
                ipc.send_event("TEST_PASS", nodeid=_nid, duration_ms=_dur, defer=not _td_failed)
                _clean_pass = not _td_failed
        if not _clean_pass:
            ipc.send_event("TEST_END", nodeid=_nid)
        if _maxfail > 0 and failed >= _maxfail:
            while _active_collectors:
                _pop_col = _active_collectors.pop()
                _col_td = getattr(_pop_col, "teardown", None)
                if callable(_col_td):
                    try:
                        _col_td()
                    except BaseException as _col_td_exc:
                        if isinstance(_col_td_exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        failed += 1
                        _tb = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{_last_nid}::<file_teardown>", loc=rel_file, error=type(_col_td_exc).__name__, msg=_tb, tb=_tb)
            break
    fin_f = finalize_fn(failed) if finalize_fn is not None else failed
    ipc.send_event(
        "FILE_DONE",
        file=rel_file,
        passed=passed,
        failed=fin_f,
        skipped=skipped,
        deselected=deselected,
        status="PASSED" if fin_f == 0 else "FAILED",
        duration_ms=0)
    return 0 if fin_f == 0 else 1


# pytest _pytest/compat.py: _non_printable_ascii_translate_table — a 1:1 port.
_NON_PRINTABLE_ASCII_IDS: dict[int, str] = {
    i: f"\\x{i:02x}" for i in range(128) if i not in range(32, 127)
}
_NON_PRINTABLE_ASCII_IDS.update({ord("\t"): "\\t", ord("\r"): "\\r", ord("\n"): "\\n"})


def _escape_id_text(s: str) -> str:
    """pytest ascii_escaped for str = unicode_escape + the non-printable-ASCII
    translate table (no double effect: unicode_escape already escaped the
    controls; the table only fixes edge cases like DEL on exotic codecs)."""
    return s.encode("unicode_escape").decode("ascii").translate(_NON_PRINTABLE_ASCII_IDS)


def _escape_id_bytes(b: bytes) -> str:
    """pytest ascii_escaped for bytes: decode("ascii", "backslashreplace") +
    translate. ASCII bytes (backslash included) pass through as-is — a SINGLE
    backslash in the id; non-ASCII bytes → \\xNN; raw controls
    (0x01) → \\x01 via translate."""
    return b.decode("ascii", "backslashreplace").translate(_NON_PRINTABLE_ASCII_IDS)


def _pytest_param_id(val: Any, argname: str, idx: int) -> str:
    """pytest IdMaker._idval — value-based ids.

    str → unicode_escape; int/float/bool/None/complex → str(); bytes →
    ascii-escaped without a b'' wrapper; re.Pattern → pattern; Enum → str(val)
    (the default __str__ gives Class.MEMBER, a custom one is respected);
    __name__ holders → __name__; other objects → {argname}{idx}.
    """
    if isinstance(val, str):
        return _escape_id_text(val)
    if isinstance(val, bool) or val is None:
        return str(val)
    if isinstance(val, (int, float, complex)):
        return str(val)
    if isinstance(val, bytes):
        return _escape_id_bytes(val)
    if isinstance(val, re.Pattern):
        # the pattern is escaped too (ascii_escaped(pattern));
        # a bytes pattern — through the bytes branch.
        pat = val.pattern
        return _escape_id_bytes(pat) if isinstance(pat, bytes) else _escape_id_text(pat)
    if isinstance(val, enum.Enum):
        return str(val)
    if isinstance(getattr(val, "__name__", None), str):
        return str(val.__name__)
    return f"{argname}{idx}"


def _idval_supported(val: Any) -> str | None:
    """pytest _idval_from_value for the RESULTS of ids-callbacks/lists:
    supported types → their id; unsupported ones
    (lists, ad-hoc objects) → None, with no positional fallback."""
    if isinstance(val, str):
        return _escape_id_text(val)
    if isinstance(val, bytes):
        return _escape_id_bytes(val)
    if val is None or isinstance(val, (bool, int, float, complex)):
        return str(val)
    if isinstance(val, re.Pattern):
        pat = val.pattern
        return _escape_id_bytes(pat) if isinstance(pat, bytes) else _escape_id_text(pat)
    if isinstance(val, enum.Enum):
        return str(val)
    if isinstance(getattr(val, "__name__", None), str):
        return str(val.__name__)
    return None


class ParameterSet:
    def __init__(self, values: tuple[Any, ...], marks: list[Mark] | None = None, id: str | None = None) -> None:
        self.values = values
        self.marks = list(marks or [])
        self.id = id


class Mark:
    def __init__(self, name: str, args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> None:
        self.name = name
        self.args = args
        self.kwargs = kwargs or {}

    @property
    def mark(self) -> "Mark":
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # A Mark decorator must return the target. A bare @pytest.mark.xfail is legal in
        # pytest (reason=None, strict=False): attach it. Bare skipif/usefixtures are legal too (skipif → skipped, usefixtures
        # → no-op + PytestWarning, see the branch below). Invariants: MARK-DECORATOR-CONTRACT.
        if len(args) == 1 and not kwargs and (inspect.isfunction(args[0]) or inspect.isclass(args[0])):
            # Bare marks are legal in pytest 9: xfail → xfail(reason=None,
            # strict=False), skipif → skipped (condition=None), usefixtures →
            # a no-op + PytestWarning (the warning matters under
            # filterwarnings=error).
            if self.name == "usefixtures" and not self.args and not self.kwargs:
                import warnings as _warnings

                # pytest 9: "usefixtures() in <file>::<name> without arguments
                # has no effect" as a PytestWarning (a selective
                # error::pytest.PytestWarning filter must catch it).
                try:
                    _uf_file = os.path.basename(
                        inspect.getfile(args[0]) or args[0].__module__
                    )
                except TypeError:  # builtins/proxies without a file
                    _uf_file = getattr(args[0], "__module__", "?")
                _uf_name = getattr(args[0], "__qualname__", None) or getattr(
                    args[0], "__name__", "?")
                _warnings.warn(
                    f"usefixtures() in {_uf_file}::{_uf_name} without arguments "
                    "has no effect",
                    PytestWarning,
                    stacklevel=2)
            target = args[0]
            self._attach(target)
            return target
        return Mark(self.name, self.args + args, {**self.kwargs, **kwargs})

    def _attach(self, target: Any) -> Any:
        marks = list(getattr(target, "pytestmark", []))
        marks.append(self)
        try:
            setattr(target, "pytestmark", marks)
        except Exception:
            pass
        return target

    def __repr__(self) -> str:
        return f"Mark(name={self.name!r}, args={self.args!r}, kwargs={self.kwargs!r})"


class MarkGenerator:
    def __getattr__(self, name: str) -> Mark:
        if name.startswith("_"):
            raise AttributeError(name)
        return Mark(name)


mark_generator = MarkGenerator()


# ============================================================================
# 4. Builtin Fixtures: MonkeyPatch, CapLog, CapSys, Mocker, TmpPath, Request
# ============================================================================

class MonkeyPatch:
    _notset = object()

    def __init__(self) -> None:
        self._setattr: list[tuple[Any, str, Any]] = []
        self._setitem: list[tuple[Any, Any, Any]] = []
        self._cwd: str | None = None
        self._syspath: list[str] | None = None

    @classmethod
    @contextlib.contextmanager
    def context(cls) -> Any:
        # pytest implements MonkeyPatch.context as a classmethod —
        # the shim must accept a call on the class.
        m = cls()
        try:
            yield m
        finally:
            m.undo()

    @staticmethod
    def _resolve_dotted(target: str) -> tuple[Any, str]:
        """Resolves multi-level dotted paths like 'pkg.mod.Class.attr'."""
        if "." not in target:
            raise TypeError(f"Must provide dotted path in setattr({target!r})")
        parts = target.split(".")
        attr_name = parts[-1]
        prefix_parts = parts[:-1]
        # Find longest importable module prefix
        mod_obj = None
        split_idx = 0
        for i in range(len(prefix_parts), 0, -1):
            cand = ".".join(prefix_parts[:i])
            try:
                mod_obj = importlib.import_module(cand)
                split_idx = i
                break
            except Exception:
                continue
        if mod_obj is None:
            mod_obj = importlib.import_module(prefix_parts[0])
            split_idx = 1
        cur = mod_obj
        for sub in prefix_parts[split_idx:]:
            cur = getattr(cur, sub)
        return cur, attr_name

    def setattr(self, target: Any, name: Any = _notset, value: Any = _notset, raising: bool = True) -> None:
        if value is self._notset:
            if not isinstance(target, str):
                raise TypeError("use setattr(target, name, value) or setattr('module.attr', value)")
            value = name
            target_obj, attr_name = self._resolve_dotted(target)
            name = attr_name
        else:
            target_obj = importlib.import_module(target) if isinstance(target, str) else target
        oldval = getattr(target_obj, name, self._notset)
        if raising and oldval is self._notset:
            raise AttributeError(f"{target_obj!r} has no attribute {name!r}")
        self._setattr.append((target_obj, name, oldval))
        setattr(target_obj, name, value)

    def delattr(self, target: Any, name: Any = _notset, raising: bool = True) -> None:
        if name is self._notset:
            target_obj, attr_name = self._resolve_dotted(target)
            name = attr_name
        else:
            target_obj = importlib.import_module(target) if isinstance(target, str) else target
        oldval = getattr(target_obj, name, self._notset)
        if raising and oldval is self._notset:
            raise AttributeError(f"{target_obj!r} has no attribute {name!r}")
        self._setattr.append((target_obj, name, oldval))
        if oldval is not self._notset:
            delattr(target_obj, name)

    def setitem(self, dic: Any, name: Any, value: Any) -> None:
        self._setitem.append((dic, name, dic.get(name, self._notset)))
        dic[name] = value

    def delitem(self, dic: Any, name: Any, raising: bool = True) -> None:
        if name not in dic:
            if raising:
                raise KeyError(name)
            return
        self._setitem.append((dic, name, dic.get(name, self._notset)))
        del dic[name]

    def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
        if prepend and name in os.environ:
            value = f"{value}{prepend}{os.environ[name]}"
        self.setitem(os.environ, name, str(value))

    def delenv(self, name: str, raising: bool = True) -> None:
        self.delitem(os.environ, name, raising=raising)

    def syspath_prepend(self, path: Any) -> None:
        if self._syspath is None:
            self._syspath = list(sys.path)
        sys.path.insert(0, str(path))

    def chdir(self, path: Any) -> None:
        if self._cwd is None:
            self._cwd = os.getcwd()
        os.chdir(path)

    def undo(self) -> None:
        for obj, name, val in reversed(self._setattr):
            if val is self._notset:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, val)
        self._setattr.clear()
        for dic, name, val in reversed(self._setitem):
            if val is self._notset:
                dic.pop(name, None)
            else:
                dic[name] = val
        self._setitem.clear()
        if self._syspath is not None:
            sys.path[:] = self._syspath
            self._syspath = None
        if self._cwd is not None:
            os.chdir(self._cwd)
            self._cwd = None


class _CapLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []
        self.stream = io.StringIO()
        self.setFormatter(logging.Formatter("%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            self.stream.write(self.format(record) + "\n")
        except Exception:
            pass

    def clear(self) -> None:
        self.records.clear()
        self.stream = io.StringIO()


# pytest always keeps the root capture handler (a LogCaptureHandler on
# root, level=0) — without it logging.lastResort writes the warning to
# sys.stderr, which leaks into CliRunner.capture in CLI tests. Installed
# in run_test_file (the common entry of both modes: zygote-fork/clean-spawn).
_ROOT_CAP_HANDLER: "_CapLogHandler | None" = None


def _ensure_root_log_capture() -> None:
    global _ROOT_CAP_HANDLER
    if _ROOT_CAP_HANDLER is None:
        _ROOT_CAP_HANDLER = _CapLogHandler()
        _ROOT_CAP_HANDLER.setLevel(0)
        logging.getLogger().addHandler(_ROOT_CAP_HANDLER)


class LogCaptureFixture:
    def __init__(self) -> None:
        self._handler = _CapLogHandler()
        self._saved_levels: list[tuple[logging.Logger, int]] = []
        self._attached = False
        self._finalized = False

    def _attach(self) -> None:
        if not self._attached and not self._finalized:
            root = logging.getLogger()
            root.addHandler(self._handler)
            self._attached = True

    def _finalize(self) -> None:
        self._finalized = True
        for logger, lvl in reversed(self._saved_levels):
            logger.setLevel(lvl)
        self._saved_levels.clear()
        if self._attached:
            logging.getLogger().removeHandler(self._handler)
            self._attached = False

    @property
    def handler(self) -> _CapLogHandler:
        self._attach()
        return self._handler

    @property
    def records(self) -> list[logging.LogRecord]:
        self._attach()
        return self._handler.records

    @property
    def text(self) -> str:
        self._attach()
        return self._handler.stream.getvalue()

    @property
    def messages(self) -> list[str]:
        self._attach()
        return [r.getMessage() for r in self._handler.records]

    @property
    def record_tuples(self) -> list[tuple[str, int, str]]:
        self._attach()
        return [(r.name, r.levelno, r.getMessage()) for r in self._handler.records]

    def clear(self) -> None:
        self._handler.clear()

    def set_level(self, level: int | str, logger: str | None = None) -> None:
        self._attach()
        lvl = getattr(logging, level.upper()) if isinstance(level, str) else level
        target = logging.getLogger(logger)
        self._saved_levels.append((target, target.level))
        target.setLevel(lvl)

    @contextlib.contextmanager
    def at_level(self, level: int | str, logger: str | None = None) -> Any:
        self._attach()
        lvl = getattr(logging, level.upper()) if isinstance(level, str) else level
        target = logging.getLogger(logger)
        old = target.level
        target.setLevel(lvl)
        try:
            yield
        finally:
            target.setLevel(old)


CaptureResult = collections.namedtuple("CaptureResult", ["out", "err"])


class _CapturedTextIO(io.TextIOBase):
    """A text capture stream with a real .buffer (BytesIO-backed).

    io.StringIO has no .buffer — black reads sys.stdout.buffer and dies
    with AttributeError. The pytest CaptureIO analog (a TextIOWrapper over
    BytesIO): byte writes via .buffer and text writes via write() land in
    the same stream; getvalue() decodes utf-8/replace.

    fileno() returns the real stream's fd (1/stdout, 2/stderr) — fd-capture
    has already dup2'ed them into temp files; os.isatty(fileno()) and similar
    queries must work as under pytest fd-capture.
    """

    def __init__(self, fd: int = 1, tee: Any = None) -> None:
        self._raw = io.BytesIO()
        self._text = io.TextIOWrapper(self._raw, encoding="utf-8", errors="replace", write_through=True)
        self._fd = fd
        self._tee = tee

    def fileno(self) -> int:
        return self._fd

    @property
    def buffer(self) -> io.BytesIO:
        return self._raw

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def write(self, s: str) -> int:
        n = self._text.write(s)
        if self._tee is not None:
            try:
                self._tee.write(s)
                self._tee.flush()
            except Exception:
                pass
        return n

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        try:
            self._text.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def seek(self, pos: int, whence: int = 0) -> int:
        # CapSysFixture.readouterr() resets the stream via seek(0)/truncate(0) —
        # the operations hit the raw buffer (a text wrapper with write_through).
        self._text.flush()
        return self._raw.seek(pos, whence)

    def truncate(self, size: int | None = None) -> int:
        self._text.flush()
        return self._raw.truncate(size)

    def getvalue(self) -> str:
        self.flush()
        return self._raw.getvalue().decode("utf-8", "replace")


class _DontReadFromInput:
    """pytest DontReadFromInput — deliberately NOT io.IOBase (callers checking isinstance(sys.stdin, io.IOBase) skip under capture, as under pytest); read* raise
    OSError, .buffer returns self (pytest 9 behavior, probe-verified)."""

    @property
    def encoding(self) -> str:
        return getattr(sys.__stdin__, "encoding", None) or "utf-8"

    @property
    def buffer(self) -> "_DontReadFromInput":
        return self

    def read(self, size: int = -1) -> str:
        raise OSError("pytest: reading from stdin while output is captured!  Consider using `-s`.")

    readline = read

    def readlines(self, hint: int = -1) -> list[str]:
        raise OSError("pytest: reading from stdin while output is captured!  Consider using `-s`.")

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> str:
        return self.readline()

    # Parity with pytest DontReadFromInput (the TextIO surface): the
    # multiprocessing child bootstrap calls sys.stdin.close() (util._close_stdin)
    # — without it forked children die in _bootstrap.
    def close(self) -> None:
        pass

    @property
    def closed(self) -> Any:
        # pytest parity: DontReadFromInput.closed == None.
        return None

    # The exception type is io.UnsupportedOperation (both OSError and
    # ValueError), like pytest DontReadFromInput/TextIO.
    def flush(self) -> None:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, has no flush()")

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = 0) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not seekable")

    def tell(self) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not seekable")

    def truncate(self, size: Any = None) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not writable")

    def write(self, s: str) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not writable")

    def writelines(self, lines: Any) -> None:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not writable")

    def __enter__(self) -> "_DontReadFromInput":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def fileno(self) -> int:
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")


class CapSysFixture:
    # CaptureFixture[str] annotations run at
    # runtime — the class must be subscriptable.
    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(self, binary: bool = False, tee: bool = False) -> None:
        self._binary = binary
        self._prev_out = sys.stdout
        self._prev_err = sys.stderr
        self._out = _CapturedTextIO(fd=1, tee=self._prev_out if tee else None)
        self._err = _CapturedTextIO(fd=2, tee=self._prev_err if tee else None)
        sys.stdout = self._out
        sys.stderr = self._err

    def readouterr(self) -> CaptureResult:
        if self._binary:
            self._out.flush()
            self._err.flush()
            out_b = self._out._raw.getvalue()
            err_b = self._err._raw.getvalue()
            self._out.seek(0)
            self._out.truncate(0)
            self._err.seek(0)
            self._err.truncate(0)
            return CaptureResult(out_b, err_b)
        out = self._out.getvalue()
        err = self._err.getvalue()
        self._out.seek(0)
        self._out.truncate(0)
        self._err.seek(0)
        self._err.truncate(0)
        return CaptureResult(out, err)

    @contextlib.contextmanager
    def disabled(self) -> Any:
        """Restores the real underlying process stdout/stderr."""
        sys.stdout = _real_stdout
        sys.stderr = _real_stderr
        try:
            yield
        finally:
            sys.stdout = self._out
            sys.stderr = self._err

    def _finalize(self) -> None:
        sys.stdout = self._prev_out
        sys.stderr = self._prev_err


class CapFdFixture:
    """The ``capfd`` fixture: captures at the file-descriptor level.

    Output that never passes through ``sys.stdout``/``sys.stderr`` — raw
    ``os.write(1, ...)``, C extensions, child processes inheriting fd 1/2 —
    is captured here, which is exactly the difference from ``capsys``.

    The mechanism mirrors pytest's ``FDCapture``: the target fd is dup2'ed
    onto a temporary file, and ``sys.stdout``/``sys.stderr`` become text
    wrappers over that same file, so ``print()`` and fd writes land in one
    buffer in write order. ``readouterr()`` drains and truncates it. On
    finalize the unread tail is forwarded to the original fd — captured
    output of a failing test is not lost, it still reaches the failure
    report.
    """

    # CaptureFixture[str] annotations in suites run at runtime — the class
    # must be subscriptable.
    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(self, binary: bool = False) -> None:
        _flush_user_stdio()
        self._binary = binary
        self._prev_out = sys.stdout
        self._prev_err = sys.stderr
        self._files: dict[int, Any] = {}
        self._saved: dict[int, int | None] = {}
        self._streams: dict[int, Any] = {}
        for fd in (1, 2):
            try:
                os.fstat(fd)
            except OSError:
                # pytest parity: a closed target fd is pointed at /dev/null
                # instead of failing the test on an environment quirk.
                self._saved[fd] = None
                try:
                    os.dup2(os.open(os.devnull, os.O_RDWR), fd)
                except OSError:
                    pass
            else:
                self._saved[fd] = os.dup(fd)
            tf = tempfile.TemporaryFile(buffering=0)
            self._files[fd] = tf
            try:
                os.dup2(tf.fileno(), fd)
            except OSError:
                pass
            self._streams[fd] = io.TextIOWrapper(
                tf, encoding="utf-8", errors="replace", write_through=True, newline="")
        sys.stdout = self._streams[1]
        sys.stderr = self._streams[2]

    def _drain(self, fd: int) -> str:
        stream = self._streams.get(fd)
        tf = self._files.get(fd)
        if tf is None:
            return ""
        _flush_user_stdio()
        try:
            stream.flush()
        except Exception:
            pass
        try:
            # The fd and the file object share one open file description, so
            # seeking the object also rewinds the writing offset: after
            # truncate() new writes start at 0 instead of leaving a hole.
            tf.seek(0)
            data = tf.read()
            tf.seek(0)
            tf.truncate(0)
        except Exception:
            return b"" if getattr(self, "_binary", False) else ""
        if getattr(self, "_binary", False):
            return data if isinstance(data, bytes) else str(data).encode("utf-8", "replace")
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return str(data)

    def readouterr(self) -> CaptureResult:
        """Drain what has been captured so far and reset both buffers."""
        return CaptureResult(self._drain(1), self._drain(2))

    @contextlib.contextmanager
    def disabled(self) -> Any:
        """Temporarily hand fd 1/2 and the streams back to the process."""
        _flush_user_stdio()
        sys.stdout = self._prev_out
        sys.stderr = self._prev_err
        for fd in (1, 2):
            saved = self._saved.get(fd)
            if saved is not None:
                try:
                    os.dup2(saved, fd)
                except OSError:
                    pass
        try:
            yield
        finally:
            _flush_user_stdio()
            for fd in (1, 2):
                tf = self._files.get(fd)
                if tf is None:
                    continue
                try:
                    os.dup2(tf.fileno(), fd)
                except OSError:
                    pass
            sys.stdout = self._streams[1]
            sys.stderr = self._streams[2]

    def _finalize(self) -> None:
        # Collect before restoring: the unread tail is written back to the
        # original fd afterwards, so the item-level capture still sees it.
        rest = {fd: self._drain(fd) for fd in (1, 2)}
        if sys.stdout is self._streams[1]:
            sys.stdout = self._prev_out
        if sys.stderr is self._streams[2]:
            sys.stderr = self._prev_err
        for fd in (1, 2):
            saved = self._saved.get(fd)
            if saved is not None:
                self._saved[fd] = None
                try:
                    os.dup2(saved, fd)
                except OSError:
                    pass
                try:
                    os.close(saved)
                except OSError:
                    pass
            try:
                self._streams[fd].close()
            except Exception:
                pass
            tf = self._files.get(fd)
            if tf is not None:
                try:
                    tf.close()
                except Exception:
                    pass
            if rest.get(fd):
                try:
                    tail_val = rest[fd]
                    tail_bytes = tail_val if isinstance(tail_val, bytes) else str(tail_val).encode("utf-8", "replace")
                    os.write(fd, tail_bytes)
                except OSError:
                    pass


class LegacyPath:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def strpath(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __fspath__(self) -> str:
        return str(self._path)

    def __eq__(self, other: Any) -> bool:
        # py.path.local parity: equality by the string form with
        # LegacyPath, str and pathlib.Path.
        if isinstance(other, LegacyPath):
            return self._path == other._path
        if isinstance(other, (str, Path)):
            return str(self._path) == str(other)
        return NotImplemented

    def __hash__(self) -> int:
        # hash is consistent with __eq__ by the string form (lp == str(lp)).
        return hash(str(self._path))

    def join(self, *parts: str) -> LegacyPath:
        return LegacyPath(self._path.joinpath(*parts))

    def __truediv__(self, other: str) -> LegacyPath:
        # py.path.local supports the / operator — tests build paths as
        # fspath / "file.pdf".
        return LegacyPath(self._path / other)

    def dirpath(self) -> LegacyPath:
        # py.path.local.dirpath() — the parent as py.path (the result must stay LegacyPath, not PosixPath).
        return LegacyPath(self._path.parent)

    @property
    def basename(self) -> str:
        # py.path.local.basename — the last path component (py.path API).
        return self._path.name

    def __add__(self, other: str) -> LegacyPath:
        # py.path.local: string path concatenation (fspath + '.output').
        return LegacyPath(Path(str(self._path) + str(other)))

    def mkdir(self, *parts: str) -> LegacyPath:
        p = self._path.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return LegacyPath(p)

    def write(self, content: str | bytes, mode: str = "w", ensure: bool = False) -> None:
        # py.path.local: ensure=True creates parent directories.
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        if "b" in mode and isinstance(content, str):
            self._path.write_bytes(content.encode("utf-8"))
        elif isinstance(content, bytes):
            self._path.write_bytes(content)
        else:
            self._path.write_text(str(content), encoding="utf-8")

    def read(self, mode: str = "r") -> str | bytes:
        return self._path.read_bytes() if "b" in mode else self._path.read_text(encoding="utf-8")

    # --- py.path.local surface, confirmed by the test corpus ---
    # Standard py.path.local filesystem helpers (as_cwd, write_binary, ensure, dirname).
    @contextlib.contextmanager
    def as_cwd(self) -> Any:
        old = os.getcwd()
        os.chdir(str(self._path))
        try:
            yield self
        finally:
            os.chdir(old)

    def write_binary(self, data: bytes, ensure: bool = False) -> None:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(bytes(data))

    def read_binary(self) -> bytes:
        return self._path.read_bytes()

    def write_text(self, text: str, encoding: str = "utf-8", ensure: bool = False) -> None:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text, encoding=encoding)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._path.read_text(encoding=encoding)

    def ensure(self, *parts: str, dir: bool = False) -> LegacyPath:
        p = self._path.joinpath(*parts)
        if dir:
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
        return LegacyPath(p)

    def check(self, dir: bool = False, file: bool = False, exists: bool = False, **kwargs: Any) -> bool:
        if dir:
            return self._path.is_dir()
        if file:
            return self._path.is_file()
        return self._path.exists()

    @property
    def dirname(self) -> str:
        return str(self._path.parent)

    @property
    def basename(self) -> str:
        return self._path.name

    @property
    def purebasename(self) -> str:
        return self._path.stem

    @property
    def ext(self) -> str:
        return self._path.suffix

    def __getattr__(self, name: str) -> Any:
        return getattr(self._path, name)


class _LegacyTmpdirFactory:
    """tmpdir_factory (the py.path era): mktemp/getbasetemp return LegacyPath
    so user code can use .strpath/.write(ensure=...).
    tmp_path_factory stays pathlib-modern."""

    def __init__(self, inner: TmpPathFactory) -> None:
        self._inner = inner

    def mktemp(self, basename: str, numbered: bool = True) -> LegacyPath:
        return LegacyPath(self._inner.mktemp(basename, numbered=numbered))

    def getbasetemp(self) -> LegacyPath:
        return LegacyPath(self._inner.getbasetemp())


class TmpPathFactory:
    def __init__(self, base_dir: str | None = None) -> None:
        # Session temp trees under the supervisor's opts["sandbox"] — the exit sweep collects them even when a worker is SIGKILLed; standalone — /tmp. Invariants: TEMP-FILES-CONTAINED.
        if base_dir:
            parent = Path(base_dir) / "wtmp"
            parent.mkdir(parents=True, exist_ok=True)
            self._base = Path(tempfile.mkdtemp(prefix="ztest_session_", dir=parent))
        else:
            self._base = Path(tempfile.mkdtemp(prefix="ztest_session_"))

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        clean = re.sub(r"[^\w.-]", "_", basename) or "tmp"
        if not numbered:
            p = self._base / clean
            p.mkdir(parents=True, exist_ok=True)
            return p
        return Path(tempfile.mkdtemp(prefix=f"{clean}_", dir=self._base))

    def getbasetemp(self) -> Path:
        return self._base

    def cleanup(self) -> None:
        shutil.rmtree(self._base, ignore_errors=True)


class MockerFixture:
    def __init__(self) -> None:
        self._patches: list[Any] = []
        self.MagicMock = unittest.mock.MagicMock
        self.Mock = unittest.mock.Mock
        self.AsyncMock = unittest.mock.AsyncMock
        self.PropertyMock = unittest.mock.PropertyMock
        self.call = unittest.mock.call
        self.ANY = unittest.mock.ANY
        self.DEFAULT = unittest.mock.DEFAULT
        self.sentinel = unittest.mock.sentinel
        # pytest-mock proxies mock_open onto mocker.*
        self.mock_open = unittest.mock.mock_open
        self.patch = self._PatchProxy(self)

    class _PatchProxy:
        def __init__(self, owner: MockerFixture) -> None:
            self._owner = owner

        @staticmethod
        def _bind_ctx(mock_obj: Any) -> Any:
            """Ensures `with mocker.patch(...) as m:` yields `mock_obj` itself."""
            try:
                if isinstance(mock_obj, unittest.mock.Mock):
                    mock_obj.__enter__ = unittest.mock.MagicMock(return_value=mock_obj)
                    mock_obj.__exit__ = unittest.mock.MagicMock(return_value=False)
            except Exception:
                pass
            return mock_obj

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch(*args, **kwargs)
            self._owner._patches.append(p)
            return self._bind_ctx(p.start())

        def object(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch.object(*args, **kwargs)
            self._owner._patches.append(p)
            return self._bind_ctx(p.start())

        def dict(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch.dict(*args, **kwargs)
            self._owner._patches.append(p)
            return p.start()

        def multiple(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch.multiple(*args, **kwargs)
            self._owner._patches.append(p)
            return p.start()

    def spy(self, obj: Any, name: str) -> Any:
        orig = getattr(obj, name)
        return self.patch.object(obj, name, wraps=orig)

    def stub(self, name: str | None = None) -> unittest.mock.MagicMock:
        return unittest.mock.MagicMock(name=name)

    def resetall(self) -> None:
        for p in self._patches:
            try:
                target = getattr(p, "temp_original", None)
                if hasattr(target, "reset_mock"):
                    target.reset_mock()
            except Exception:
                pass

    def stopall(self) -> None:
        for p in reversed(self._patches):
            try:
                p.stop()
            except Exception:
                pass
        self._patches.clear()


# ============================================================================
# 5. AST Assert Rewriter + SHA-256 Cache (.ztest_cache/ast/)
# ============================================================================

def _ztest_assert_cmp(op: str, left: Any, right: Any) -> bool:
    # Bool-predicate rewrite delegating to bool(res). Invariants: REWRITE-PRESERVES-SCOPE, ARRAY-TRUTH-FALLBACK.
    res: Any
    if op == "==":
        res = left == right
    elif op == "!=":
        res = left != right
    elif op == "<":
        res = left < right
    elif op == "<=":
        res = left <= right
    elif op == ">":
        res = left > right
    elif op == ">=":
        res = left >= right
    elif op == "in":
        res = left in right
    elif op == "not in":
        res = left not in right
    elif op == "is":
        res = left is right
    elif op == "is not":
        res = left is not right
    else:
        res = False
    # Propagate ValueError on ambiguous multi-element array truth (pytest parity). Invariants: ALL-FALLBACK-SHAPE-GATED.
    return bool(res)


def _safe_assert_repr(val: Any) -> str:
    try:
        return repr(val)
    except Exception as exc:
        return f"<[{type(exc).__name__}({str(exc)!r}) raised in repr()] {type(val).__name__} object at 0x{id(val):x}>"


def _ztest_assert_format(idx: int, op: str, left: Any, right: Any, expr_src: str, msg: Any = None) -> str:
    # Called only on the failing branch — the message is lazy, like the native assert; idx is a diagnostic counter, not baked into the text. Invariants: REWRITE-PRESERVES-SCOPE.
    _ = idx
    lr = _safe_assert_repr(left)
    rr = _safe_assert_repr(right)
    if len(lr) > 300:
        lr = lr[:297] + "..."
    if len(rr) > 300:
        rr = rr[:297] + "..."
    text = f"assert {expr_src}\n  Left:  {lr}\n  Right: {rr} (op: {op})"
    if msg is not None:
        return f"{msg}\n{text}"
    return text


def _ztest_assert_bool(val: Any) -> bool:
    # Delegate truthiness to bool(val) so multi-element array truth raises ValueError (pytest parity). Invariants: ARRAY-TRUTH-FALLBACK, ALL-FALLBACK-SHAPE-GATED.
    return bool(val)


def _ztest_assert_val_format(val: Any, expr_src: str) -> str:
    vr = _safe_assert_repr(val)
    if len(vr) > 300:
        vr = vr[:297] + "..."
    return f"assert {expr_src}\n  where {expr_src} = {vr}"


def _ztest_assert_not_format(val: Any, operand_src: str, expr_src: str) -> str:
    vr = _safe_assert_repr(val)
    if len(vr) > 300:
        vr = vr[:297] + "..."
    return f"assert {expr_src}\n  where {operand_src} = {vr}"


def _ztest_assert_call_format(res: Any, fn_src: str, args_info: list[tuple[str, Any]], expr_src: str) -> str:
    rr = _safe_assert_repr(res)
    if len(rr) > 300:
        rr = rr[:297] + "..."
    if not args_info:
        return f"assert {expr_src}\n  where {rr} = {expr_src}"
    parts: list[str] = []
    for a_src, a_val in args_info:
        ar = _safe_assert_repr(a_val)
        if len(ar) > 200:
            ar = ar[:197] + "..."
        if not a_src:
            parts.append(ar)
        elif a_src.startswith("__kw__:"):
            parts.append(f"{a_src[7:]}={ar}")
        else:
            parts.append(f"{a_src}={ar}")
    sig = ", ".join(parts)
    return f"assert {expr_src}\n  where {rr} = {fn_src}({sig})"


def _ztest_assert_or_format(expr_src: str, branch_msgs: list[Any]) -> str:
    lines = [f"assert {expr_src}"]
    for bm in branch_msgs:
        if isinstance(bm, tuple) and bm and callable(bm[0]):
            bm = bm[0](*bm[1:])
        if isinstance(bm, str) and bm:
            for sub in bm.splitlines():
                lines.append("  " + sub)
    return "\n".join(lines)


def _ztest_assert_finalize(detail: str, top_src: str, msg: Any = None) -> str:
    header = f"assert {top_src}"
    if detail.startswith(header):
        text = detail
    else:
        text = f"{header}\n  ({detail})"
    if msg is not None:
        return f"{msg}\n{text}"
    return text


class _AssertRewriteTransformer(ast.NodeTransformer):
    _OP_MAP: dict[type[ast.cmpop], str] = {
        ast.Eq: "==",
        ast.NotEq: "!=",
        ast.Lt: "<",
        ast.LtE: "<=",
        ast.Gt: ">",
        ast.GtE: ">=",
        ast.In: "in",
        ast.NotIn: "not in",
        ast.Is: "is",
        ast.IsNot: "is not",
    }
    _MAGIC_CALLS: frozenset[str] = frozenset({"super", "locals", "globals", "vars", "dir", "eval", "exec"})

    def __init__(self) -> None:
        super().__init__()
        # REWRITE-PRESERVES-SCOPE: unique temp-name counter — __zt_l<N>/__zt_r<N> must never collide
        self._n = 0

    @staticmethod
    def _unparse(expr: ast.AST, fallback: str = "<expr>") -> str:
        try:
            return ast.unparse(expr)
        except Exception:
            return fallback

    def _new_temp(self, temps: list[str], prefix: str = "v") -> str:
        n = self._n
        self._n += 1
        name = f"__zt_{prefix}{n}"
        temps.append(name)
        return name

    def _can_rewrite_top(self, expr: ast.AST) -> bool:
        if isinstance(expr, ast.Compare):
            return len(expr.ops) >= 1 and len(expr.ops) == len(expr.comparators) and all(type(op) in self._OP_MAP for op in expr.ops)
        if isinstance(expr, ast.BoolOp):
            return isinstance(expr.op, (ast.And, ast.Or)) and len(expr.values) >= 2
        if isinstance(expr, ast.UnaryOp):
            return isinstance(expr.op, ast.Not)
        if isinstance(expr, ast.Call):
            return True
        return False

    @staticmethod
    def _make_fmt(lazy: bool, func_name: str, args: list[ast.expr]) -> ast.expr:
        fn = ast.Name(id=func_name, ctx=ast.Load())
        if lazy:
            return ast.Tuple(elts=[fn, *args], ctx=ast.Load())
        return ast.Call(func=fn, args=args, keywords=[])

    def _compile_cond(self, expr: ast.AST, fail_var: str, temps: list[str], lazy: bool = False) -> list[ast.stmt]:
        if isinstance(expr, ast.Compare) and len(expr.ops) >= 1 and len(expr.ops) == len(expr.comparators) and all(type(op) in self._OP_MAP for op in expr.ops):
            expr_src = self._unparse(expr, "<compare>")
            l_var = self._new_temp(temps, "l")
            r_vars = [self._new_temp(temps, "r") for _ in expr.ops]
            assign_l = ast.Assign(targets=[ast.Name(id=l_var, ctx=ast.Store())], value=expr.left)
            next_stmts: list[ast.stmt] = []
            for i in range(len(expr.ops) - 1, -1, -1):
                op_str = self._OP_MAP[type(expr.ops[i])]
                comp_node = expr.comparators[i]
                left_node_src = expr.left if i == 0 else expr.comparators[i - 1]
                step_src = expr_src if len(expr.ops) == 1 else f"{self._unparse(left_node_src)} {op_str} {self._unparse(comp_node)}"
                left_var = l_var if i == 0 else r_vars[i - 1]
                right_var = r_vars[i]
                assign_r = ast.Assign(targets=[ast.Name(id=right_var, ctx=ast.Store())], value=comp_node)
                pred = ast.Call(
                    func=ast.Name(id="__ztest_assert_cmp__", ctx=ast.Load()),
                    args=[
                        ast.Constant(value=op_str),
                        ast.Name(id=left_var, ctx=ast.Load()),
                        ast.Name(id=right_var, ctx=ast.Load()),
                    ],
                    keywords=[])
                fmt_call = self._make_fmt(
                    lazy,
                    "__ztest_assert_format__",
                    [
                        ast.Constant(value=0),
                        ast.Constant(value=op_str),
                        ast.Name(id=left_var, ctx=ast.Load()),
                        ast.Name(id=right_var, ctx=ast.Load()),
                        ast.Constant(value=step_src),
                        ast.Constant(value=None),
                    ])
                set_fail = ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt_call)
                if_step = ast.If(
                    test=ast.UnaryOp(op=ast.Not(), operand=pred),
                    body=[set_fail],
                    orelse=next_stmts)
                next_stmts = [assign_r, if_step]
            return [assign_l, *next_stmts]

        if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
            next_and: list[ast.stmt] = []
            for v in reversed(expr.values):
                cur = self._compile_cond(v, fail_var, temps, lazy=lazy)
                if next_and:
                    guard = ast.If(
                        test=ast.Compare(
                            left=ast.Name(id=fail_var, ctx=ast.Load()),
                            ops=[ast.Is()],
                            comparators=[ast.Constant(value=None)]),
                        body=next_and,
                        orelse=[])
                    cur = [*cur, guard]
                next_and = cur
            return next_and

        if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
            expr_src = self._unparse(expr, "<or>")
            sub_vars = [self._new_temp(temps, "or") for _ in expr.values]
            or_fail = ast.Assign(
                targets=[ast.Name(id=fail_var, ctx=ast.Store())],
                value=self._make_fmt(
                    lazy,
                    "__ztest_assert_or_format__",
                    [
                        ast.Constant(value=expr_src),
                        ast.List(elts=[ast.Name(id=sv, ctx=ast.Load()) for sv in sub_vars], ctx=ast.Load()),
                    ]))
            next_or: list[ast.stmt] = [or_fail]
            for i in range(len(expr.values) - 1, -1, -1):
                sv = sub_vars[i]
                init_sv = ast.Assign(targets=[ast.Name(id=sv, ctx=ast.Store())], value=ast.Constant(value=None))
                branch_stmts = self._compile_cond(expr.values[i], sv, temps, lazy=True)
                check_failed = ast.If(
                    test=ast.Compare(
                        left=ast.Name(id=sv, ctx=ast.Load()),
                        ops=[ast.IsNot()],
                        comparators=[ast.Constant(value=None)]),
                    body=next_or,
                    orelse=[])
                next_or = [init_sv, *branch_stmts, check_failed]
            return next_or

        if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
            expr_src = self._unparse(expr, "not <expr>")
            op_src = self._unparse(expr.operand, "<expr>")
            v_var = self._new_temp(temps, "v")
            assign_v = ast.Assign(targets=[ast.Name(id=v_var, ctx=ast.Store())], value=expr.operand)
            pred = ast.Call(
                func=ast.Name(id="__ztest_assert_bool__", ctx=ast.Load()),
                args=[ast.Name(id=v_var, ctx=ast.Load())],
                keywords=[])
            fmt = self._make_fmt(
                lazy,
                "__ztest_assert_not_format__",
                [
                    ast.Name(id=v_var, ctx=ast.Load()),
                    ast.Constant(value=op_src),
                    ast.Constant(value=expr_src),
                ])
            if_stmt = ast.If(
                test=pred,
                body=[ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt)],
                orelse=[])
            return [assign_v, if_stmt]

        if isinstance(expr, ast.Call):
            expr_src = self._unparse(expr, "<call>()")
            fn_src = self._unparse(expr.func, "<fn>")
            can_split = (
                not (isinstance(expr.func, ast.Name) and expr.func.id in self._MAGIC_CALLS)
                and not any(isinstance(a, ast.Starred) for a in expr.args)
                and all(kw.arg is not None for kw in expr.keywords)
            )
            stmts: list[ast.stmt] = []
            info_elts: list[ast.expr] = []
            if can_split:
                fn_var = self._new_temp(temps, "fn")
                stmts.append(ast.Assign(targets=[ast.Name(id=fn_var, ctx=ast.Store())], value=expr.func))
                call_args: list[ast.expr] = []
                call_kws: list[ast.keyword] = []
                for a in expr.args:
                    a_var = self._new_temp(temps, "a")
                    stmts.append(ast.Assign(targets=[ast.Name(id=a_var, ctx=ast.Store())], value=a))
                    call_args.append(ast.Name(id=a_var, ctx=ast.Load()))
                    a_label = "" if isinstance(a, ast.Constant) else self._unparse(a, "arg")
                    info_elts.append(ast.Tuple(elts=[ast.Constant(value=a_label), ast.Name(id=a_var, ctx=ast.Load())], ctx=ast.Load()))
                for kw in expr.keywords:
                    kw_var = self._new_temp(temps, "kw")
                    stmts.append(ast.Assign(targets=[ast.Name(id=kw_var, ctx=ast.Store())], value=kw.value))
                    call_kws.append(ast.keyword(arg=kw.arg, value=ast.Name(id=kw_var, ctx=ast.Load())))
                    info_elts.append(ast.Tuple(elts=[ast.Constant(value=f"__kw__:{kw.arg}"), ast.Name(id=kw_var, ctx=ast.Load())], ctx=ast.Load()))
                res_var = self._new_temp(temps, "res")
                new_call = ast.Call(func=ast.Name(id=fn_var, ctx=ast.Load()), args=call_args, keywords=call_kws)
                stmts.append(ast.Assign(targets=[ast.Name(id=res_var, ctx=ast.Store())], value=new_call))
            else:
                res_var = self._new_temp(temps, "res")
                stmts.append(ast.Assign(targets=[ast.Name(id=res_var, ctx=ast.Store())], value=expr))
            pred = ast.Call(
                func=ast.Name(id="__ztest_assert_bool__", ctx=ast.Load()),
                args=[ast.Name(id=res_var, ctx=ast.Load())],
                keywords=[])
            fmt = self._make_fmt(
                lazy,
                "__ztest_assert_call_format__",
                [
                    ast.Name(id=res_var, ctx=ast.Load()),
                    ast.Constant(value=fn_src),
                    ast.List(elts=info_elts, ctx=ast.Load()),
                    ast.Constant(value=expr_src),
                ])
            stmts.append(ast.If(
                test=ast.UnaryOp(op=ast.Not(), operand=pred),
                body=[ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt)],
                orelse=[]))
            return stmts

        expr_src = self._unparse(expr, "<expr>")
        v_var = self._new_temp(temps, "v")
        assign_v = ast.Assign(targets=[ast.Name(id=v_var, ctx=ast.Store())], value=expr)
        pred = ast.Call(
            func=ast.Name(id="__ztest_assert_bool__", ctx=ast.Load()),
            args=[ast.Name(id=v_var, ctx=ast.Load())],
            keywords=[])
        fmt = self._make_fmt(
            lazy,
            "__ztest_assert_val_format__",
            [
                ast.Name(id=v_var, ctx=ast.Load()),
                ast.Constant(value=expr_src),
            ])
        if_stmt = ast.If(
            test=ast.UnaryOp(op=ast.Not(), operand=pred),
            body=[ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=fmt)],
            orelse=[])
        return [assign_v, if_stmt]

    def visit_Assert(self, node: ast.Assert) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and len(node.test.comparators) == 1:
            op_type = type(node.test.ops[0])
            op_str = self._OP_MAP.get(op_type)
            if op_str is not None:
                expr_src = self._unparse(node.test, f"<compare {op_str}>")
                # Scope-preserving rewrite v2 without a lambda — the block lives in the enclosing scope, operands are evaluated once in source order, await/walrus are legal. Invariants: REWRITE-PRESERVES-SCOPE.
                n = self._n
                self._n += 1
                l_name = f"__zt_l{n}"
                r_name = f"__zt_r{n}"
                assign_l = ast.Assign(
                    targets=[ast.Name(id=l_name, ctx=ast.Store())],
                    value=node.test.left)
                assign_r = ast.Assign(
                    targets=[ast.Name(id=r_name, ctx=ast.Store())],
                    value=node.test.comparators[0])
                pred = ast.Call(
                    func=ast.Name(id="__ztest_assert_cmp__", ctx=ast.Load()),
                    args=[
                        ast.Constant(value=op_str),
                        ast.Name(id=l_name, ctx=ast.Load()),
                        ast.Name(id=r_name, ctx=ast.Load()),
                    ],
                    keywords=[])
                msg_arg: ast.AST = node.msg if node.msg is not None else ast.Constant(value=None)
                fmt_call = ast.Call(
                    func=ast.Name(id="__ztest_assert_format__", ctx=ast.Load()),
                    args=[
                        ast.Constant(value=n),
                        ast.Constant(value=op_str),
                        ast.Name(id=l_name, ctx=ast.Load()),
                        ast.Name(id=r_name, ctx=ast.Load()),
                        ast.Constant(value=expr_src),
                        msg_arg,
                    ],
                    keywords=[])
                raise_stmt = ast.Raise(
                    exc=ast.Call(
                        func=ast.Name(id="AssertionError", ctx=ast.Load()),
                        args=[fmt_call],
                        keywords=[]),
                    cause=None)
                if_stmt = ast.If(
                    test=ast.UnaryOp(op=ast.Not(), operand=pred),
                    body=[raise_stmt],
                    orelse=[])
                # Temporaries are cleaned up on the success path, otherwise they pin operands in the frame and falsely fail weakref/getrefcount tests. Invariants: REWRITE-TEMPORARIES-CLEARED.
                clear_stmt = ast.Assign(
                    targets=[
                        ast.Name(id=l_name, ctx=ast.Store()),
                        ast.Name(id=r_name, ctx=ast.Store()),
                    ],
                    value=ast.Constant(value=None))
                # Rewrite under `if __debug__:` — under -O operands are not evaluated, the walrus does not bind. Invariants: REWRITE-DEBUG-GATED.
                debug_guard = ast.If(
                    test=ast.Name(id="__debug__", ctx=ast.Load()),
                    body=[assign_l, assign_r, if_stmt, clear_stmt],
                    orelse=[])
                for built in (assign_l, assign_r, if_stmt, clear_stmt, debug_guard):
                    ast.copy_location(built, node)
                return [debug_guard]

        # Compound assert rewrite (chained Compare, BoolOp and/or, UnaryOp not, Call).
        # When an explicit message is supplied on a non-Compare assert (e.g. `assert fn(), "msg"`),
        # keep the native assert if it has no Compare children so callers matching exact str(AssertionError)
        # in unit tests continue to see the verbatim message.
        if self._can_rewrite_top(node.test):
            if node.msg is not None and not any(isinstance(sub, ast.Compare) for sub in ast.walk(node.test)):
                return node
            top_src = self._unparse(node.test, "<assert>")
            temps: list[str] = []
            fail_var = self._new_temp(temps, "fail")
            init_fail = ast.Assign(targets=[ast.Name(id=fail_var, ctx=ast.Store())], value=ast.Constant(value=None))
            cond_stmts = self._compile_cond(node.test, fail_var, temps)
            msg_arg = node.msg if node.msg is not None else ast.Constant(value=None)
            fin_call = ast.Call(
                func=ast.Name(id="__ztest_assert_finalize__", ctx=ast.Load()),
                args=[
                    ast.Name(id=fail_var, ctx=ast.Load()),
                    ast.Constant(value=top_src),
                    msg_arg,
                ],
                keywords=[])
            raise_stmt = ast.Raise(
                exc=ast.Call(
                    func=ast.Name(id="AssertionError", ctx=ast.Load()),
                    args=[fin_call],
                    keywords=[]),
                cause=None)
            check_fail = ast.If(
                test=ast.Compare(
                    left=ast.Name(id=fail_var, ctx=ast.Load()),
                    ops=[ast.IsNot()],
                    comparators=[ast.Constant(value=None)]),
                body=[raise_stmt],
                orelse=[])
            clear_stmt = ast.Assign(
                targets=[ast.Name(id=t, ctx=ast.Store()) for t in temps],
                value=ast.Constant(value=None))
            debug_guard = ast.If(
                test=ast.Name(id="__debug__", ctx=ast.Load()),
                body=[init_fail, *cond_stmts, check_fail, clear_stmt],
                orelse=[])
            for sub in ast.walk(debug_guard):
                if isinstance(sub, (ast.stmt, ast.expr)):
                    ast.copy_location(sub, node)
            return [debug_guard]
        return node


def _package_qualified_name(filepath: Path, fallback: str) -> str:
    """Qualified name via the __init__.py chain upward to the first
    directory without one (import root → sys.path/PYTHONPATH, parents via importlib).
    Outside a package — fallback. Identical stems in different packages are distinguished."""
    pkg_parts: list[str] = []
    walk = filepath.parent
    while (walk / "__init__.py").is_file():
        pkg_parts.insert(0, walk.name)
        walk = walk.parent
    if not pkg_parts:
        return fallback
    import_root = str(walk)
    if import_root not in sys.path:
        sys.path.insert(0, import_root)
    # Do not touch os.environ['PYTHONPATH'] (pytest parity: CLI subprocesses
    # see a clean environment); the import path lives only in the worker's sys.path.
    for i in range(1, len(pkg_parts) + 1):
        importlib.import_module(".".join(pkg_parts[:i]))
    return ".".join([*pkg_parts, filepath.stem])


def load_module_with_ast_cache(filepath: Path, module_name: str, cache_dir: Path | None = None) -> types.ModuleType:
    abs_path = str(filepath.resolve())
    src_bytes = filepath.read_bytes()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}:{importlib.util.MAGIC_NUMBER.hex()}"
    # The -O level in the cache key and in compile() — otherwise collect-only under PYTHONOPTIMIZE caches code without asserts = fabricated PASS. Invariants: AST-CACHE-OPT-LEVEL.
    opt_level = str(sys.flags.optimize)
    h = hashlib.sha256()
    h.update(abs_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(src_bytes)
    h.update(b"\x00")
    h.update(SHIM_VERSION.encode("utf-8"))
    h.update(b"\x00")
    h.update(py_ver.encode("utf-8"))
    h.update(b"\x00")
    h.update(opt_level.encode("utf-8"))
    h.update(b"\x00")
    # Compile flags are part of the cache key — changing dont_inherit and the
    # like must invalidate the cache even without a SHIM_VERSION bump.
    h.update(b"dont_inherit=1")
    digest = h.hexdigest()

    code_obj = None
    cache_file = (cache_dir / "ast" / f"{digest}.pyc") if cache_dir else None
    if cache_file and cache_file.exists():
        try:
            code_obj = marshal.loads(cache_file.read_bytes())
        except Exception:
            code_obj = None

    if code_obj is None:
        tree = ast.parse(src_bytes, filename=abs_path)
        tree = _AssertRewriteTransformer().visit(tree)
        ast.fix_missing_locations(tree)
        code_obj = compile(tree, filename=abs_path, mode="exec", optimize=sys.flags.optimize, dont_inherit=True)  # the shim's CO_FUTURE_ANNOTATIONS must not leak into user modules
        if cache_file:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(marshal.dumps(code_obj))
            except Exception:
                pass

    # pkgutil.get_data(__name__, ...) on a module requires
    # __spec__.loader with get_data — SourceFileLoader, not loader=None
    # (otherwise pkgutil.get_data silently returns None).
    mod = types.ModuleType(module_name)
    mod.__file__ = abs_path
    mod.__package__ = module_name.rpartition(".")[0]
    _loader = importlib.machinery.SourceFileLoader(module_name, abs_path)
    mod.__spec__ = importlib.machinery.ModuleSpec(module_name, loader=_loader, origin=abs_path)
    mod.__loader__ = _loader
    mod.__dict__["__ztest_assert_cmp__"] = _ztest_assert_cmp
    mod.__dict__["__ztest_assert_format__"] = _ztest_assert_format
    mod.__dict__["__ztest_assert_bool__"] = _ztest_assert_bool
    mod.__dict__["__ztest_assert_val_format__"] = _ztest_assert_val_format
    mod.__dict__["__ztest_assert_not_format__"] = _ztest_assert_not_format
    mod.__dict__["__ztest_assert_call_format__"] = _ztest_assert_call_format
    mod.__dict__["__ztest_assert_or_format__"] = _ztest_assert_or_format
    mod.__dict__["__ztest_assert_finalize__"] = _ztest_assert_finalize
    sys.modules[module_name] = mod
    # The flush barrier: exec of a user module (test module/conftest) — a
    # long FILE_START/COLLECT window must not hide pending frames.
    _flush_ipc_now()
    try:
        exec(code_obj, mod.__dict__)
    except BaseException:
        # a half-initialized module must never linger in sys.modules — a later
        # import of that name would resurrect the broken object instead of re-executing it.
        sys.modules.pop(module_name, None)
        raise
    # The import machinery, on a normal import, registers the child module
    # as an attribute of the parent; the shim's manual loading skipped that
    # → logging.config resolve/ext:// (a getattr chain from the package)
    # could not find tests.test_x. Parity restored.
    if "." in module_name:
        _parent_name, _, _child_name = module_name.rpartition(".")
        _parent_mod = sys.modules.get(_parent_name)
        if _parent_mod is not None:
            try:
                setattr(_parent_mod, _child_name, mod)
            except Exception:
                pass
    return mod


# ============================================================================
# 6. Fixture Registry, Request & Execution Engine
# ============================================================================

class FixtureDef:
    def __init__(
        self,
        func: Callable[..., Any],
        name: str,
        scope: str = "function",
        autouse: bool = False,
        params: list[Any] | None = None,
        ids: Any = None) -> None:
        self.func = func
        self.name = name
        self.scope = "module" if scope == "package" else scope
        self.autouse = autouse
        self.params = list(params) if params is not None else None
        self.ids = ids
        # A declaration-unique token (lazily from _collect_parametrized_fixture_axes) — the cache never merges two declarations of one name. Invariants: DECL-TOKEN-KEY.
        self.decl_token: int | None = None


def _load_ini_options(rootpath: Path) -> dict[str, Any]:
    """Reading pytest ini configuration from disk.

    pytest priority: pytest.ini > pyproject.toml [tool.pytest.ini_options] >
    tox.ini [pytest] > setup.cfg [tool:pytest]. ONLY the values of
    keys are applied (filterwarnings/plugins/markers/custom addini); addopts/testpaths
    deliberately do not act — targets and options belong to the supervisor's
    CLI protocol (bench protocol: -o addopts=).
    """
    import configparser

    ini_path = rootpath / "pytest.ini"
    if ini_path.is_file():
        cp = configparser.ConfigParser()
        try:
            cp.read(ini_path, encoding="utf-8")
        except Exception:
            return {}
        return dict(cp.items("pytest")) if cp.has_section("pytest") else {}
    pyproject = rootpath / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            opts = data.get("tool", {}).get("pytest", {}).get("ini_options", None)
            if isinstance(opts, dict):
                return dict(opts)
        except Exception:
            pass
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        p = rootpath / name
        if p.is_file():
            cp = configparser.ConfigParser()
            try:
                cp.read(p, encoding="utf-8")
            except Exception:
                continue
            if cp.has_section(section):
                return dict(cp.items(section))
    return {}


# Cached ini options per rootdir. The value is the RAW dict from
# _load_ini_options; what goes out (ConfigStub.inicfg) is a shallow copy
# with copied lists, so consumers (getini/addini/addinivalue_line,
# ParserStub.addini, a user conftest via config.inicfg) never mutate the
# cached entry. Prewarmed in run_zygote_server before gc.freeze() (workers
# get the cache via COW; a per-file _load_ini_options costs ~16 ms: the
# configparser import + re.compile of its patterns in every worker).
_INI_OPTIONS_CACHE: dict[str, dict[str, Any]] = {}


def _ini_options_cached(rootpath: Path) -> dict[str, Any]:
    """Ini options of a rootdir from the module-level cache (computes transparently on a miss)."""
    try:
        key = str(rootpath.resolve())
    except OSError:
        key = str(rootpath)
    cached = _INI_OPTIONS_CACHE.get(key)
    if cached is None:
        cached = _load_ini_options(rootpath)
        _INI_OPTIONS_CACHE[key] = cached
    # A top-level copy + copies of list values: addinivalue_line (below) and
    # ParserStub.addini mutate inicfg in place, and ini options from
    # pyproject.toml may carry lists (filterwarnings/markers). Nested dicts
    # are contractually read-only (nothing in the shim writes them).
    return {k: (list(v) if isinstance(v, list) else v) for k, v in cached.items()}


def _warm_ini_options_cache(rootpath: Path) -> None:
    """Prewarm the ini-options cache in the zygote (before gc.freeze()); errors are silenced by the caller."""
    try:
        key = str(rootpath.resolve())
    except OSError:
        key = str(rootpath)
    if key not in _INI_OPTIONS_CACHE:
        _INI_OPTIONS_CACHE[key] = _load_ini_options(rootpath)


# pytest's config-file names, in the priority order it applies per directory.
_INI_CONFIG_NAMES = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
# Resolved config directory per starting directory (files of one suite share
# the answer; the walk is per file otherwise).
_MISSING_CACHE: Any = object()
_INI_DIR_CACHE: dict[str, Any] = {}


def _ini_has_pytest_config(path: Path) -> bool:
    """Whether the file really carries pytest configuration.

    A pyproject.toml without a pytest table, or a tox.ini/setup.cfg without
    the pytest section, is not a configuration file for pytest — the ancestor
    sweep continues above it.
    """
    if path.name == "pytest.ini":
        return True
    try:
        if path.name == "pyproject.toml":
            import tomllib

            data = tomllib.loads(path.read_text(encoding="utf-8"))
            tool_pytest = data.get("tool", {}).get("pytest", {})
            if not isinstance(tool_pytest, dict):
                return False
            if isinstance(tool_pytest.get("ini_options"), dict):
                return True
            # Native TOML mode: [tool.pytest] keys next to ini_options.
            return any(key != "ini_options" for key in tool_pytest)
        import configparser

        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        section = "pytest" if path.name == "tox.ini" else "tool:pytest"
        return cp.has_section(section)
    except Exception:
        return False


def _discover_ini_dir(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding a pytest config file.

    pytest resolves rootdir and the ini file from the target arguments, not
    from the process CWD: a suite run from an unrelated directory still
    applies the project's own pytest.ini/pyproject.toml settings.
    """
    try:
        cur = start.resolve()
    except OSError:
        cur = start
    key = str(cur)
    cached = _INI_DIR_CACHE.get(key, _MISSING_CACHE)
    if cached is not _MISSING_CACHE:
        return cached
    found: Path | None = None
    probe = cur
    while True:
        for name in _INI_CONFIG_NAMES:
            candidate = probe / name
            if candidate.is_file() and _ini_has_pytest_config(candidate):
                found = probe
                break
        if found is not None:
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    _INI_DIR_CACHE[key] = found
    return found


def _iter_plugin_names(raw: Any) -> list[str]:
    """plugins=/pytest_plugins: str ('a, b' or one per line) | list[str]."""
    if isinstance(raw, str):
        return [p.strip() for p in re.split(r"[,\n]", raw) if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    return []


def _parse_warn_filters(raw: Any) -> list[tuple[str, str, type, str, int]]:
    """Parsing of ini filterwarnings into warnings.filterwarnings tuples.

    The spec format is like -W: action:message:category:module:lineno
    (sections optional). The string is split per line (pytest linelist
    semantics). An unknown category is rejected (pytest: InvalidSpec at startup).
    """
    import builtins

    if isinstance(raw, str):
        specs = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    elif isinstance(raw, (list, tuple)):
        specs = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return []
    out: list[tuple[str, str, type, str, int]] = []
    for spec in specs:
        parts = spec.split(":")
        action = parts[0].strip()
        message = parts[1] if len(parts) > 1 else ""
        cat_s = parts[2].strip() if len(parts) > 2 else ""
        module = parts[3] if len(parts) > 3 else ""
        lineno = int(parts[4]) if len(parts) > 4 and parts[4].strip().isdigit() else 0
        if not action:
            continue
        cat: type = Warning
        if cat_s:
            if "." in cat_s:
                mod_s, _, cls_s = cat_s.rpartition(".")
                # Degrade only on ImportError (no module — pytest warns+skips);
                # AttributeError (module present, class missing) is fatal in pytest
                # — let it propagate.
                try:
                    cat = getattr(importlib.import_module(mod_s), cls_s)
                except ImportError:
                    warnings.warn(
                        f"Failed to resolve filterwarnings category {cat_s!r}; filter skipped"
                    )
                    continue
            else:
                cat = getattr(builtins, cat_s, None)
            if not (isinstance(cat, type) and issubclass(cat, Warning)):
                raise ZtestUnsupportedApiError(
                    f"ini filterwarnings: unknown warning category {cat_s!r} (rejected)"
                )
        out.append((action, message, cat, module, lineno))
    return out


class StashKey:
    """pytest.StashKey — a typed key for config.stash.

    The real StashKey is a marker class with __class_getitem__; in the shim
    stash is a plain dict, so subscriptability and identity suffice.
    """

    def __class_getitem__(cls, item: Any) -> Any:
        return cls


class ParserStub:
    """Stub for pytest_addoption(parser)."""
    def __init__(self, cfg: ConfigStub) -> None:
        self._cfg = cfg

    def addoption(self, *names: str, **kwargs: Any) -> None:
        default = kwargs.get("default", None)
        dest = kwargs.get("dest", None)
        for n in names:
            clean = n.lstrip("-").replace("-", "_")
            if clean not in self._cfg._options:
                self._cfg._options[clean] = default
            if n not in self._cfg._options:
                self._cfg._options[n] = default
        if dest and dest not in self._cfg._options:
            self._cfg._options[dest] = default
        self._cfg.option = types.SimpleNamespace(**self._cfg._options)

    def getgroup(self, name: str, description: str = "") -> ParserStub:
        return self

    def addini(self, name: str, help: str = "", type: str | None = None, default: Any = None) -> None:
        self._cfg.inicfg.setdefault(name, default if default is not None else "")


class _PluginManagerStub:
    """pytestconfig.pluginmanager — a minimal surface.
    unregister(name="logging-plugin") → None and the reverse
    register only if something was returned — None semantics are compatible."""

    def unregister(self, plugin: Any = None, name: str | None = None) -> Any:
        return None

    def register(self, plugin: Any, name: str | None = None) -> Any:
        return None

    def getplugin(self, name: str) -> Any:
        return None

    def hasplugin(self, name: str) -> bool:
        return False

    def get_plugins(self) -> list[Any]:
        return []


class ConfigStub:
    def __init__(self, rootpath: Path, options: dict[str, Any] | None = None, ini_root: Path | None = None) -> None:
        self.rootpath = rootpath
        # The directory whose config file pytest would read. It matches
        # rootpath in the usual layout and differs when the target lives
        # outside the invocation directory (config discovered upwards from
        # the target).
        self.ini_root = ini_root or rootpath
        # inicfg is read from the config file of the target's project.
        # Cached per dir (prewarmed in the zygote → COW-shared by workers).
        self.inicfg: dict[str, Any] = _ini_options_cached(self.ini_root)
        self._options = dict(options or {})
        self.option = types.SimpleNamespace(**self._options)
        # config.stash / config._store — a
        # plain dict, keys are StashKey.
        self._store: dict[Any, Any] = {}
        self.stash = self._store
        # pytestconfig.pluginmanager.
        self.pluginmanager = _PluginManagerStub()

    def getoption(self, name: str, default: Any = None, skip: bool = False) -> Any:
        clean = name.lstrip("-").replace("-", "_")
        if clean in self._options:
            return self._options[clean]
        if name in self._options:
            return self._options[name]
        if skip:
            raise Skipped(f"Option {name} not provided")
        return default

    _MISSING: Any = object()

    def getvalue(self, name: str, default: Any = _MISSING, skip: bool = False) -> Any:
        # pytest deprecated Config.getvalue == getoption semantics:
        # without a default an unknown name — ValueError, like pytest.
        clean = name.lstrip("-").replace("-", "_")
        if clean in self._options:
            return self._options[clean]
        if name in self._options:
            return self._options[name]
        if default is self._MISSING:
            raise ValueError(f"no option named {name!r}")
        return default

    def getini(self, name: str) -> Any:
        return self.inicfg.get(name, "")

    def addinivalue_line(self, name: str, line: str) -> None:
        val = self.inicfg.get(name)
        if isinstance(val, list):
            val.append(line)
        else:
            self.inicfg[name] = [line]


class NodeStub:
    def __init__(
        self,
        name: str,
        nodeid: str,
        fspath: Path,
        func: Callable[..., Any],
        cls: type | None = None,
        module: types.ModuleType | None = None,
        markers: list[Mark] | None = None,
        config: ConfigStub | None = None) -> None:
        self.name = name
        self.originalname = name.split("[")[0]
        self.nodeid = nodeid
        self.path = fspath
        self.fspath = LegacyPath(fspath)
        self.function = func
        self.obj = func
        self.cls = cls
        self.module = module
        self.own_markers: list[Mark] = list(markers or [])
        self.config = config
        # keywords — a live shared mapping of the node; fixtures write
        # (request.keywords['expected'] = ...) and read via other fixtures
        # of the same item. FixtureRequest.keywords references this same dict.
        self.keywords: dict[str, Any] = {m.name: m for m in self.own_markers}
        self.user_properties: list[tuple[str, Any]] = []

    def add_marker(self, marker: str | Mark, append: bool = True) -> None:
        m = Mark(marker) if isinstance(marker, str) else marker
        self.keywords[m.name] = m  # keywords are live: add_marker is visible through them too
        if append:
            self.own_markers.append(m)
        else:
            self.own_markers.insert(0, m)

    def iter_markers(self, name: str | None = None) -> Any:
        for m in self.own_markers:
            if name is None or m.name == name:
                yield m

    def get_closest_marker(self, name: str, default: Any = None) -> Mark | Any:
        for m in self.own_markers:
            if m.name == name:
                return m
        return default


class FixtureRequest:
    def __init__(
        self,
        node: NodeStub,
        resolver: FixtureResolver,
        scope: str = "function",
        param: Any = None,
        instance: Any = None,
        fixturename: str | None = None) -> None:
        self.node = node
        self._resolver = resolver
        self.scope = scope
        self.param = param
        self.instance = instance
        self._fixturename = fixturename
        self.function = node.function
        self.cls = node.cls
        self.module = node.module
        self.fspath = node.fspath
        self.path = node.path
        self.config = node.config
        # The live shared keywords mapping of the node, not a copy per FixtureRequest.
        self.keywords: dict[str, Any] = node.keywords

    @property
    def fixturename(self) -> str | None:
        return self._fixturename

    def addfinalizer(self, finalizer: Callable[[], Any]) -> None:
        self._resolver.add_finalizer(self.scope, finalizer)

    def getfixturevalue(self, argname: str) -> Any:
        return self._resolver.get_fixture_value(argname)


# A KI in teardown is recorded, the flag is armed, but not re-raised from the helper (the finally with restore/FILE_DONE is not torn down); the loop consumes the flag and stops the file. Invariants: INTERRUPT-SAFE-TEARDOWN.
_TEARDOWN_INTERRUPTED = False


# Per-unit budgets at module level (teardown_scope is not closed over run_file); each finalizer — its own budgeting unit. Invariants: FINALIZER-PER-UNIT-BUDGET.
_ZT_PHASE_ARM: Callable[[str], None] | None = None
_ZT_PHASE_DISARM: Callable[[], None] | None = None
# Sticky-flag check at the accounting boundary of each unit before disarm; module level — for teardown_scope. Invariants: DISARM-AND-CONSUME-SEPARATE.
_ZT_PHASE_RAISE: Callable[[str], None] | None = None
_ZT_PHASE_PURE_DISARM: Callable[[], None] | None = None
_ZT_PHASE_CONSUME: Callable[[], bool] | None = None
# Drain-hook — an async finalizer timeout goes to heartbeat + bounded drain + poison of run_file. Invariants: ASYNC-FINALIZER-TIMEOUT-PROTOCOL.
_ZT_TIMEOUT_DRAIN: Callable[[Any, str], None] | None = None


def _mark_teardown_interrupt() -> None:
    global _TEARDOWN_INTERRUPTED
    _TEARDOWN_INTERRUPTED = True


def _consume_teardown_interrupt() -> bool:
    global _TEARDOWN_INTERRUPTED
    was = _TEARDOWN_INTERRUPTED
    _TEARDOWN_INTERRUPTED = False
    return was


class FixtureResolver:
    def __init__(
        self,
        layers: list[dict[str, FixtureDef]],
        node: NodeStub,
        tmp_factory: TmpPathFactory,
        async_runner: asyncio.Runner | None,
        indirect_params: dict[str, Any] | None = None,
        instance: Any = None,
        direct_params: dict[str, Any] | None = None) -> None:
        self.layers = layers  # [conftest_root, ..., module_fixtures, class_fixtures]
        self.node = node
        self.tmp_factory = tmp_factory
        self.async_runner = async_runner
        self.indirect_params = indirect_params or {}
        # Direct (non-indirect) call_params shadow same-named
        # fixtures (pytest CallSpec2: _arg2fixturedefs[argname] = []) — a fixture
        # requesting a parameterized name
        # gets the axis value, not a LookupError.
        self.direct_params = direct_params or {}
        self.instance = instance
        self.cache: dict[str, dict[tuple[Any, ...], Any]] = {
            "session": {},
            "module": {},
            "class": {},
            "function": {},
        }
        self.finalizers: dict[str, list[Callable[[], Any]]] = {
            "session": [],
            "module": [],
            "class": [],
            "function": [],
        }
        self._resolving: set[tuple[str, int]] = set()
        self._caplog: LogCaptureFixture | None = None

    def share_higher_scopes(self, prev: FixtureResolver) -> None:
        for sc in ("session", "module", "class"):
            self.cache[sc] = prev.cache[sc]
            self.finalizers[sc] = prev.finalizers[sc]
        if prev.async_runner is not None and self.async_runner is None:
            self.async_runner = prev.async_runner

    def add_finalizer(self, scope: str, fn: Callable[[], Any]) -> None:
        sc = scope if scope in self.finalizers else "function"
        self.finalizers[sc].append(fn)

    def teardown_scope(self, scope: str) -> list[str]:
        errors: list[str] = []
        raw_excs: list[BaseException] = []
        self.last_teardown_excs = raw_excs
        stack = self.finalizers.get(scope, [])
        while stack:
            fn = stack.pop()
            # Each finalizer is its own budgeting unit: heartbeat + re-arm before, disarm after; a hung one → _TestTimeoutError in its record, the rest still run. Invariants: FINALIZER-PER-UNIT-BUDGET.
            arm = _ZT_PHASE_ARM
            disarm = _ZT_PHASE_DISARM
            pure_disarm = _ZT_PHASE_PURE_DISARM
            consume_hook = _ZT_PHASE_CONSUME
            raise_hook = _ZT_PHASE_RAISE
            # The drain hook — a timed-out async finalizer's cancel-resistant tasks do not survive the unit. Invariants: ASYNC-FINALIZER-TIMEOUT-PROTOCOL.
            drain_hook = _ZT_TIMEOUT_DRAIN
            # Heartbeat arm() swallows OSError on dead IPC — the worker is doomed, the finalizers must still run. Invariants: HEARTBEAT-DEAD-PEER-SWALLOWED.
            try:
                if arm is not None:
                    arm(f"teardown_scope:{scope}:finalizer")
            except OSError:
                pass
            # The flush barrier: arm() above put TEARDOWN_START into the buffer — it
            # must leave before the user finalizer (no pending frames across user code).
            _flush_ipc_now()
            try:
                res = fn()
                # A generator/awaitable result of an xunit finalizer is not driven (close/cancel, then a rejection); kind-guard before the call, result-guard after — the drive branch is only for async fixtures. Invariants: XUNIT-RETURN-CONTRACT, XUNIT-TEARDOWN-GUARDS.
                if inspect.iscoroutine(res):
                    runner = self._ensure_runner()
                    runner.run(res)
                else:
                    _ztest_lifecycle_result_guard(res, f"teardown finalizer ({getattr(fn, '__qualname__', fn)})")
                # The sticky-flag check before disarm — a swallowed SIGALRM = a recorded teardown error, not a phantom pass. Invariants: DISARM-AND-CONSUME-SEPARATE.
                if raise_hook is not None:
                    raise_hook(f"teardown_scope:{scope}:finalizer")
            except KeyboardInterrupt as ki_e:
                # The interrupt is recorded, the finalizers still run; disarm first — the recurring itimer is dead before formatting. Invariants: INTERRUPT-SAFE-TEARDOWN, DISARM-AND-CONSUME-SEPARATE.
                # A pure disarm (itimer off); the pop belongs to the finally clause.
                if pure_disarm is not None:
                    pure_disarm()
                raw_excs.append(ki_e)
                errors.append(traceback.format_exc())
                _mark_teardown_interrupt()
            except BaseException as ts_e:
                # Failures of finalizers of any kind are recorded, the rest still run; disarm first. Invariants: FINALIZER-FAILURES-RECORDED, DISARM-AND-CONSUME-SEPARATE.
                if isinstance(ts_e, _TestTimeoutError):
                    # The absorb pattern (pure disarm + consume) — the sticky flag does not leak further.
                    if pure_disarm is not None:
                        pure_disarm()
                    if consume_hook is not None:
                        consume_hook()
                elif pure_disarm is not None:
                    pure_disarm()
                raw_excs.append(ts_e)
                errors.append(traceback.format_exc())
                # After an async finalizer timeout — heartbeat → bounded drain → runner poisoning. Invariants: ASYNC-FINALIZER-TIMEOUT-PROTOCOL.
                if isinstance(ts_e, _TestTimeoutError) and drain_hook is not None:
                    try:
                        drain_hook(self, "timeout_drain:teardown_scope")
                    except BaseException:
                        pass
            finally:
                if disarm is not None:
                    disarm()
        self.cache.get(scope, {}).clear()
        return errors

    def run_autouse_fixtures(self) -> None:
        seen_names: set[str] = set()
        autouse_order: list[str] = []
        for layer in self.layers:
            for fname, fdef in layer.items():
                if fdef.autouse and fname not in seen_names:
                    seen_names.add(fname)
                    autouse_order.append(fname)
        # Execute session -> module -> class -> function autouse before test body / caplog
        for target_scope in ("session", "module", "class", "function"):
            for fname in autouse_order:
                fdef, _ = self._find_fixture(fname, None)
                if fdef and fdef.scope == target_scope:
                    self.get_fixture_value(fname)

    def _find_fixture(self, name: str, max_layer_idx: int | None) -> tuple[FixtureDef | None, int]:
        limit = len(self.layers) - 1 if max_layer_idx is None else max_layer_idx - 1
        for idx in range(limit, -1, -1):
            if name in self.layers[idx]:
                return self.layers[idx][name], idx
        return None, -1

    def get_fixture_value(self, name: str, max_layer_idx: int | None = None) -> Any:
        # A direct axis parameter shadows a fixture/request of the same name from
        # the fixture closure (pytest: the parameter wins over the fixturedef).
        if name in self.direct_params:
            return self.direct_params[name]
        fdef, layer_idx = self._find_fixture(name, max_layer_idx)
        if fdef is not None:
            # Key = (decl_token, idx, value) + the dependency signature of the parameterized closure — an instance from a foreign parameter is never served. Invariants: DECL-TOKEN-KEY.
            axis_key: Any = None
            if name in self.indirect_params:
                carrier = self.indirect_params[name]
                axis_key = (carrier[0], carrier[1])
            dep_sig = self._parametrized_dep_signature(fdef)
            key = (name, layer_idx, axis_key, dep_sig)
            rkey = (name, layer_idx)
            scope = fdef.scope
            if key in self.cache[scope]:
                return self.cache[scope][key]
            if rkey in self._resolving:
                raise RuntimeError(f"Circular fixture dependency detected for {name!r}")
            self._resolving.add(rkey)
            try:
                val = self._invoke_fixture(fdef, layer_idx)
                self.cache[scope][key] = val
                return val
            finally:
                self._resolving.discard(rkey)

        return self._get_builtin(name)

    def _parametrized_dep_signature(self, fdef: FixtureDef) -> tuple[tuple[str, int, int], ...]:
        """Signature = (dep_name, dep_decl_token, dep_idx) of all parameterized closure dependencies."""
        if not self.indirect_params:
            return ()
        merged: dict[str, FixtureDef] = {}
        for layer in self.layers:
            merged.update(layer)
        closure: set[str] = set()
        stack: list[FixtureDef] = [fdef]
        seen: set[int] = set()
        while stack:
            fd = stack.pop()
            if id(fd) in seen:
                continue
            seen.add(id(fd))
            try:
                sig = inspect.signature(fd.func)
            except Exception:
                continue
            for pname in sig.parameters:
                if pname in ("self", "request"):
                    continue
                closure.add(pname)
                dep = merged.get(pname)
                if dep is not None:
                    stack.append(dep)
        parts: list[tuple[str, int, int]] = []
        for dep_name in sorted(closure):
            if dep_name == fdef.name:
                continue  # the fixture's own axis is already part of the key
            carrier = self.indirect_params.get(dep_name)
            if carrier is not None:
                parts.append((dep_name, carrier[0], carrier[1]))
        return tuple(parts)

    def _get_builtin(self, name: str) -> Any:
        key = (f"__builtin__{name}", -1, None)
        if key in self.cache["function"]:
            return self.cache["function"][key]
        if key in self.cache["session"]:
            return self.cache["session"][key]

        if name == "request":
            req = FixtureRequest(self.node, self, scope="function", param=self.indirect_params.get("request"), instance=self.instance, fixturename=None)
            self.cache["function"][key] = req
            return req
        if name == "pytestconfig":
            # Builtin config fixture (session scope).
            cfg_obj = getattr(self.node, "config", None)
            self.cache["session"][key] = cfg_obj
            return cfg_obj
        if name == "monkeypatch":
            mp = MonkeyPatch()
            self.add_finalizer("function", mp.undo)
            self.cache["function"][key] = mp
            return mp
        if name == "tmp_path":
            p = self.tmp_factory.mktemp(self.node.originalname)
            self.cache["function"][key] = p
            return p
        if name == "tmp_path_factory":
            self.cache["session"][key] = self.tmp_factory
            return self.tmp_factory
        if name == "tmpdir":
            p = LegacyPath(self.tmp_factory.mktemp(self.node.originalname))
            self.cache["function"][key] = p
            return p
        if name == "tmpdir_factory":
            # The py.path era: mktemp returns a LegacyPath with strpath.
            lf = _LegacyTmpdirFactory(self.tmp_factory)
            self.cache["session"][key] = lf
            return lf
        if name == "caplog":
            cl = LogCaptureFixture()
            cl._attach()
            self.add_finalizer("function", cl._finalize)
            self.cache["function"][key] = cl
            self._caplog = cl
            return cl
        if name in ("capsys", "capteesys"):
            cs = CapSysFixture(binary=False, tee=(name == "capteesys"))
            self.add_finalizer("function", cs._finalize)
            self.cache["function"][key] = cs
            return cs
        if name == "capsysbinary":
            csb = CapSysFixture(binary=True)
            self.add_finalizer("function", csb._finalize)
            self.cache["function"][key] = csb
            return csb
        if name == "capfd":
            # capsys is Python level (sys.stdout/sys.stderr); capfd is fd
            # level — subprocess and os.write(1/2) output is captured too.
            cf = CapFdFixture(binary=False)
            self.add_finalizer("function", cf._finalize)
            self.cache["function"][key] = cf
            return cf
        if name == "capfdbinary":
            cfb = CapFdFixture(binary=True)
            self.add_finalizer("function", cfb._finalize)
            self.cache["function"][key] = cfb
            return cfb
        if name == "doctest_namespace":
            dn: dict[str, Any] = {}
            self.cache["session"][key] = dn
            return dn
        if name == "record_property":
            def _rec_prop(prop_name: str, prop_val: Any) -> None:
                props = getattr(self.node, "user_properties", None)
                if isinstance(props, list):
                    props.append((prop_name, prop_val))
            self.cache["function"][key] = _rec_prop
            return _rec_prop
        if name == "record_xml_attribute":
            def _rec_xml(attr_name: str, attr_val: Any) -> None:
                pass
            self.cache["function"][key] = _rec_xml
            return _rec_xml
        if name == "record_testsuite_property":
            def _rec_ts(prop_name: str, prop_val: Any) -> None:
                pass
            self.cache["session"][key] = _rec_ts
            return _rec_ts
        if name == "benchmark":
            class _BenchmarkPassthrough:
                def __init__(self) -> None:
                    self.stats: dict[str, Any] = {}
                    self.extra_info: dict[str, Any] = {}
                def __call__(self, func: Any, *args: Any, **kwargs: Any) -> Any:
                    return func(*args, **kwargs)
                def pedantic(self, target: Any, args: Any = (), kwargs: Any = None, setup: Any = None, rounds: int = 1, warmup_rounds: int = 0, iterations: int = 1) -> Any:
                    kw = dict(kwargs) if kwargs is not None else {}
                    if setup is not None:
                        res = setup()
                        if isinstance(res, tuple) and len(res) == 2:
                            args, kw = res
                    return target(*args, **kw)
                def weave(self, *args: Any, **kwargs: Any) -> Any:
                    return None
            bm = _BenchmarkPassthrough()
            self.cache["function"][key] = bm
            return bm
        if name == "mocker":
            mk = MockerFixture()
            self.add_finalizer("function", mk.stopall)
            self.cache["function"][key] = mk
            return mk
        if name == "recwarn":
            # WarningsRecorder (not a plain list) — .list/.pop(cls) like pytest;
            # the finalizer disables catch_warnings, does not assert matching warnings.
            rec = WarningsRecorder()
            rec.__enter__()
            self.add_finalizer("function", lambda: rec._catch.__exit__(None, None, None))
            self.cache["function"][key] = rec
            return rec
        if name == "event_loop":
            runner = self._ensure_runner()
            return runner.get_loop()

        raise LookupError(f"fixture '{name}' not found in {self.node.nodeid}")

    def _invoke_fixture(self, fdef: FixtureDef, layer_idx: int) -> Any:
        fn = fdef.func
        # The executable fixture body — via __pytest_wrapped__.obj
        # (class decorators replace it after decoration; pytest calls
        # exactly that — class-level time-freezing decorators).
        _pw = getattr(fn, "__pytest_wrapped__", None)
        if _pw is not None and hasattr(_pw, "obj") and callable(_pw.obj):
            fn = _pw.obj
        sig = inspect.signature(fn)
        kwargs: dict[str, Any] = {}
        # A parameterized fixture outside its axis — a collection refusal, not a silent fallback to params[0]. Invariants: AXIS-MEMBERSHIP-ENFORCED.
        if fdef.name in self.indirect_params:
            _decl_tok, _axis_idx, param_val = self.indirect_params[fdef.name]
        elif fdef.params:
            raise ZtestUnsupportedApiError(
                f"parametrized fixture {fdef.name!r} was requested outside its axis closure in "
                f"{self.node.nodeid}: the shim refuses to silently "
                f"fall back to params[0]"
            )
        else:
            param_val = None
        req = FixtureRequest(self.node, self, scope=fdef.scope, param=param_val, instance=self.instance, fixturename=fdef.name)

        # A classmethod fixture — the first positional parameter is
        # the owning class (not the fixture name); bind the owner, do not resolve.
        bind_cls_first = bool(getattr(fdef, "_bind_cls_first", False))
        owner_cls = type(self.instance) if self.instance is not None else getattr(self.node, "cls", None)
        for _pidx, (pname, p) in enumerate(sig.parameters.items()):
            # VAR_POSITIONAL/VAR_KEYWORD fixtures are not resolved (pytest skips
            # them — otherwise: fixture 'args' not found).
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if _pidx == 0 and bind_cls_first:
                kwargs[pname] = owner_cls
            elif pname == "self" and self.instance is not None:
                kwargs[pname] = self.instance
            elif pname == "request":
                kwargs[pname] = req
            elif pname == fdef.name:
                kwargs[pname] = self.get_fixture_value(pname, max_layer_idx=layer_idx)
            else:
                # A wider-scope fixture cannot depend on a
                # narrower one (function < class < module < package < session);
                # parametrized names are function-scoped.
                if fdef.scope != "function":
                    _SCOPE_ORDER = {"function": 0, "class": 1, "module": 2, "package": 3, "session": 4}
                    _dep_def, _ = self._find_fixture(pname, None)
                    _narrow = pname in self.direct_params or (
                        _dep_def is not None
                        and _SCOPE_ORDER.get(_dep_def.scope, 0) < _SCOPE_ORDER.get(fdef.scope, 0)
                    )
                    if _narrow:
                        raise RuntimeError(
                            f"ScopeMismatch: You tried to access the function scoped "
                            f"fixture {pname!r} with a {fdef.scope} scoped request object "
                            f"(fixture {fdef.name!r} in {self.node.nodeid})"
                        )
                try:
                    kwargs[pname] = self.get_fixture_value(pname)
                except LookupError:
                    if p.default is not inspect.Parameter.empty:
                        kwargs[pname] = p.default
                    else:
                        raise

        # The flush barrier: dependencies are resolved (the recursion above),
        # user fixture code follows — no pending frames may remain.
        _flush_ipc_now()

        if inspect.isasyncgenfunction(fn):
            agen = fn(**kwargs)
            runner = self._ensure_runner()
            val = runner.run(agen.__anext__())

            def _fin_agen() -> None:
                # A poisoned runner is not driven — skip with an explicit diagnostic instead of "Event loop is closed". Invariants: POISONED-RUNNER-SKIP-DIAGNOSTIC.
                if getattr(runner, "_ztest_drain_abandoned", False):
                    raise RuntimeError(
                        f"async fixture {fdef.name!r} finalizer skipped: runner poisoned by "
                        f"an earlier timeout (cancel-resistant tasks survived the bounded "
                        f"drain); the fixture's post-yield teardown never ran"
                    )
                try:
                    extra = runner.run(agen.__anext__())
                except StopAsyncIteration:
                    return
                # A second yield of an async fixture = a teardown error; aclose() must run, its error is chained. Invariants: MULTI-YIELD-IS-TEARDOWN-ERROR.
                close_err: BaseException | None = None
                close_ki: KeyboardInterrupt | None = None
                try:
                    runner.run(agen.aclose())
                except KeyboardInterrupt as ac_ki:
                    # A KI from aclose() first arms the teardown-interrupt, then is wrapped — the KI's identity is not erased. Invariants: INTERRUPT-IDENTITY-PRESERVED.
                    close_ki = ac_ki
                    _mark_teardown_interrupt()
                except BaseException as ac_e:
                    close_err = ac_e
                msg = f"fixture {fdef.name!r} yielded more than once (extra value {extra!r})"
                if close_ki is not None:
                    raise RuntimeError(f"{msg}; aclose() raised KeyboardInterrupt: {close_ki!r}") from close_ki
                if close_err is not None:
                    raise RuntimeError(f"{msg}; aclose() also raised: {close_err!r}") from close_err
                raise RuntimeError(msg)

            self.add_finalizer(fdef.scope, _fin_agen)
            return val

        if inspect.iscoroutinefunction(fn):
            runner = self._ensure_runner()
            return runner.run(fn(**kwargs))

        if inspect.isgeneratorfunction(fn):
            gen = fn(**kwargs)
            val = next(gen)

            def _fin_gen() -> None:
                try:
                    extra = next(gen)
                except StopIteration:
                    return
                # A second yield of a fixture = a teardown error; close() must run, its error is chained. Invariants: MULTI-YIELD-IS-TEARDOWN-ERROR.
                close_err: BaseException | None = None
                close_ki: KeyboardInterrupt | None = None
                try:
                    gen.close()
                except KeyboardInterrupt as c_ki:
                    # A KI from close() first arms the teardown-interrupt. Invariants: INTERRUPT-IDENTITY-PRESERVED.
                    close_ki = c_ki
                    _mark_teardown_interrupt()
                except BaseException as c_e:
                    close_err = c_e
                msg = f"fixture {fdef.name!r} yielded more than once (extra value {extra!r})"
                if close_ki is not None:
                    raise RuntimeError(f"{msg}; gen.close() raised KeyboardInterrupt: {close_ki!r}") from close_ki
                if close_err is not None:
                    raise RuntimeError(f"{msg}; gen.close() also raised: {close_err!r}") from close_err
                raise RuntimeError(msg)

            self.add_finalizer(fdef.scope, _fin_gen)
            return val

        return fn(**kwargs)

    def _ensure_runner(self) -> asyncio.Runner:
        if self.async_runner is None:
            _r = asyncio.Runner()
            self.async_runner = _r

            def _fin_runner_close() -> None:
                # A poisoned runner is never close()d (unbounded cancel-and-wait) — the finalizer is a no-op, the loop is closed best-effort on poison. Invariants: POISONED-RUNNER-NEVER-CLOSED.
                if getattr(_r, "_ztest_drain_abandoned", False):
                    return
                _r.close()

            self.add_finalizer("session", _fin_runner_close)
        return self.async_runner


# ============================================================================
# 7. sys.modules['pytest'] & '_pytest.*' Stubs (Fail-Loud Policy)
# ============================================================================

class _FixtureWrappedStub:
    """A minimal __pytest_wrapped__ — .obj points at the fixture body.

    pytest (FixtureFunctionDefinition) stores the user function in
    __pytest_wrapped__.obj; a class-level decorator may rewrite .obj,
    wrapping the body in a freeze context. The shim reads .obj in _invoke_fixture."""

    __slots__ = ("obj",)

    def __init__(self, obj: Any) -> None:
        self.obj = obj


def fixture(
    callable_or_scope: Any = None,
    *args: Any,
    scope: str = "function",
    params: Any = None,
    autouse: bool = False,
    ids: Any = None,
    name: str | None = None) -> Any:
    if isinstance(callable_or_scope, str):
        scope = callable_or_scope
        callable_or_scope = None

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        fname = name or fn.__name__
        fdef = FixtureDef(fn, fname, scope=scope, autouse=autouse, params=params, ids=ids)
        setattr(fn, "_ztest_fixture", fdef)
        # pytest 8.2+ marks fixture functions _pytestfixturefunction and
        # __pytest_wrapped__ (.obj — the executable body); class decorators
        # Class decorators that replace the callable body via .obj —
        # _invoke_fixture calls exactly that.
        try:
            fn.__dict__["_pytestfixturefunction"] = True
            fn.__dict__["__pytest_wrapped__"] = _FixtureWrappedStub(fn)
        except (AttributeError, TypeError):
            pass  # impure callable objects without __dict__ — skip the marks
        return fn

    if callable(callable_or_scope):
        return _wrap(callable_or_scope)
    return _wrap


def skip(reason: str = "", *, allow_module_level: bool = False) -> None:
    raise Skipped(reason)


def fail(reason: str = "", pytrace: bool = True) -> None:
    raise Failed(reason, pytrace=pytrace)


def xfail(reason: str = "") -> None:
    raise XFailed(reason)


def exit(reason: str = "", returncode: int | None = None) -> None:
    raise Exit(reason, returncode=returncode)


def importorskip(modname: str, minversion: str | None = None, reason: str | None = None, *, exc_type: type[BaseException] = ImportError) -> Any:
    try:
        mod = importlib.import_module(modname)
    except exc_type as e:
        raise Skipped(reason or f"could not import {modname!r}: {e}") from None
    if minversion is not None:
        ver = getattr(mod, "__version__", None)
        if ver is None or tuple(int(x) for x in re.findall(r"\d+", str(ver))) < tuple(int(x) for x in re.findall(r"\d+", minversion)):
            raise Skipped(reason or f"module {modname!r} has version {ver!r}, required >= {minversion!r}")
    return mod


def param(*values: Any, marks: Any = (), id: str | None = None) -> ParameterSet:
    if isinstance(marks, Mark):
        m_list = [marks]
    else:
        m_list = list(marks or [])
    return ParameterSet(values, marks=m_list, id=id)


_RAISES_NOTSET = object()


def raises(
    expected_exception: Any = _RAISES_NOTSET,
    *args: Any,
    **kwargs: Any,
) -> Any:
    if args and callable(args[0]):
        if expected_exception is _RAISES_NOTSET:
            raise TypeError("raises() missing required positional argument: 'expected_exception'")
        fn = args[0]
        ctx = RaisesContext(expected_exception)
        with ctx:
            fn(*args[1:], **kwargs)
        return ctx.excinfo
    match = kwargs.pop("match", None)
    check = kwargs.pop("check", None)
    if args or kwargs:
        if kwargs:
            bad = next(iter(kwargs))
            raise TypeError(f"raises() got an unexpected keyword argument {bad!r}")
        raise TypeError(f"raises() takes 1 positional argument in context-manager mode but {1 + len(args)} were given")
    if expected_exception is _RAISES_NOTSET:
        if check is None:
            raise ValueError("must provide either expected_exception or check")
        expected_exception = BaseException
    return RaisesContext(expected_exception, match=match, check=check)


def warns(expected_warning: type[Warning] | tuple[type[Warning], ...] = Warning, *args: Any, match: str | re.Pattern[str] | None = None, **kwargs: Any) -> Any:
    if args and callable(args[0]):
        if match is not None:
            raise TypeError("warns() callable form does not accept 'match' keyword argument")
        fn = args[0]
        ctx = WarnsContext(expected_warning)
        with ctx:
            fn(*args[1:], **kwargs)
        return ctx.list
    if args or kwargs:
        if kwargs:
            bad = next(iter(kwargs))
            raise TypeError(f"warns() got an unexpected keyword argument {bad!r}")
        raise TypeError(f"warns() takes at most 1 positional argument in context-manager mode but {1 + len(args)} were given")
    return WarnsContext(expected_warning, match=match)


def approx(expected: Any, rel: float | None = None, abs: float | None = None, nan_ok: bool = False) -> ApproxValue:
    # pytest semantics — an explicit abs without rel disables the relative
    # branch; an explicit rel does not disable the default abs=1e-12; nothing set → 1e-6/1e-12.
    if abs is not None and rel is None:
        rel = 0.0
    return ApproxValue(expected, rel=rel, abs=abs, nan_ok=nan_ok)


@contextlib.contextmanager
def deprecated_call(*, match: str | re.Pattern[str] | None = None) -> Any:
    with WarnsContext((DeprecationWarning, PendingDeprecationWarning, FutureWarning), match=match) as w:
        yield w


class _PytestModule(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise ZtestUnsupportedApiError(f"pytest.{name} is not implemented in ztest_shim (Fail-Loud policy)")


def _pytest_hookimpl_guard(*a: Any, **kw: Any) -> Any:
    """pytest.hookimpl: wrapper/ordering flags (hookwrapper/tryfirst/...) do not
    change execution in the shim (hooks are called as plain functions).
    Decorating with such flags must NOT kill the conftest —
    the options are accepted and ignored (a hard rejection here would drop the
    whole suite at conftest load). wrapper=True marks the function
    _ztest_hookimpl_wrapper — a generator hook is driven pre-yield+post-yield back-to-back at the call site (_invocable_conf_hook)."""
    wrapper_marked = bool(kw.get("hookwrapper") or kw.get("wrapper"))

    def _mark(f: Any) -> Any:
        if wrapper_marked:
            try:
                f._ztest_hookimpl_wrapper = True
            except (AttributeError, TypeError):
                pass
        return f

    if a and callable(a[0]):
        return _mark(a[0])
    return _mark


def _pytest_main_stub(args: Any = None, plugins: Any = None) -> int:
    """pytest.main inside a test is not drivable — it is refused, never a fabricated exit code 0."""
    raise ZtestUnsupportedApiError(
        "pytest.main() is not supported inside ztest workers: nested pytest sessions cannot be "
        "driven by the shim (refused)"
    )


def install_pytest_modules() -> None:
    pytest_mod = _PytestModule("pytest")
    pytest_mod.__version__ = "9.0.3+ztest"
    pytest_mod.fixture = fixture
    pytest_mod.mark = mark_generator
    pytest_mod.param = param
    pytest_mod.raises = raises
    pytest_mod.warns = warns
    pytest_mod.approx = approx
    pytest_mod.deprecated_call = deprecated_call
    pytest_mod.skip = skip
    pytest_mod.fail = fail
    pytest_mod.xfail = xfail
    pytest_mod.exit = exit
    pytest_mod.importorskip = importorskip
    pytest_mod.MonkeyPatch = MonkeyPatch
    pytest_mod.LogCaptureFixture = LogCaptureFixture
    pytest_mod.FixtureRequest = FixtureRequest
    # Public pytest names that suites actually import.
    # (TempPathFactory, CaptureFixture, WarningsRecorder).
    pytest_mod.TempPathFactory = TmpPathFactory
    pytest_mod.CaptureFixture = CapSysFixture
    pytest_mod.WarningsRecorder = WarningsRecorder
    pytest_mod.ExceptionInfo = ExceptionInfo
    pytest_mod.StashKey = StashKey  # config.stash[StashKey[...]()]
    # Custom collectors — File/Item for user
    # subclasses (from_parent/runtest/repr_failure). pytest.Item is the base
    # of custom items, not a NodeStub (NodeStub stays pytest.Function).
    pytest_mod.File = PytestFile
    pytest_mod.Item = PytestItem
    pytest_mod.Config = ConfigStub
    pytest_mod.Parser = ParserStub
    pytest_mod.Session = object
    pytest_mod.Module = object
    pytest_mod.Class = object
    pytest_mod.Function = NodeStub
    pytest_mod.hookimpl = _pytest_hookimpl_guard
    pytest_mod.hookspec = lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda f: f))
    pytest_mod.register_assert_rewrite = lambda *a, **kw: None
    pytest_mod.main = _pytest_main_stub
    # A real attribute (not a fail-loud __getattr__) — ini filterwarnings
    # error::pytest.PytestWarning resolves through
    # import_module('pytest') + getattr.
    pytest_mod.PytestWarning = PytestWarning
    # ini filterwarnings with pytest.* categories must resolve — register the
    # standard pytest 9 warning taxonomy (the hierarchy as in
    # _pytest.warning_types).
    for _wcls in (
        PytestAssertRewriteWarning, PytestCacheWarning, PytestCollectionWarning,
        PytestConfigWarning, PytestDeprecationWarning, PytestExperimentalApiWarning,
        PytestRemovedIn9Warning, PytestUnknownMarkWarning,
        PytestUnraisableExceptionWarning, PytestUnhandledCoroutineWarning,
        PytestReturnNotNoneWarning):
        setattr(pytest_mod, _wcls.__name__, _wcls)
    # A conftest may declare subclasses of pytest.Collector (custom data
    # collectors) — the class must exist and accept a cooperative
    # __init__(name=..., parent=..., **kw).
    pytest_mod.Collector = _PytestNodeBase

    sys.modules["pytest"] = pytest_mod

    pa_mod = types.ModuleType("pytest_asyncio")
    pa_mod.fixture = fixture
    sys.modules["pytest_asyncio"] = pa_mod
    pa_plugin = types.ModuleType("pytest_asyncio.plugin")
    sys.modules["pytest_asyncio.plugin"] = pa_plugin

    _pytest_root = types.ModuleType("_pytest")
    # Property-based libraries read sys.modules["_pytest"].__version__ to adapt to the
    # pytest version.
    _pytest_root.__version__ = "9.0.3"
    # The real _pytest package backs two delegations: `import py` (the py-lib
    # shim) touches _pytest.__file__ and lazily pulls _pytest._py.error/path;
    # those vendored modules are self-contained and pull no pytest machinery
    # Resolve the real package through sys.path once — the interpreter
    # may keep it in system dist-packages, the user site, or a venv, none of
    # which is guaranteed to equal sysconfig's purelib. A missing install
    # degrades to the bare stub (no __file__, empty __path__).
    try:
        import importlib.machinery as _machinery

        _spec = _machinery.PathFinder.find_spec("_pytest")
        if _spec is not None:
            if _spec.origin:
                _pytest_root.__file__ = _spec.origin
            _pytest_root.__path__ = list(_spec.submodule_search_locations or ())  # type: ignore[attr-defined]
        else:
            _pytest_root.__path__ = []  # type: ignore[attr-defined]
    except Exception:
        _pytest_root.__path__ = []  # type: ignore[attr-defined]
    outcomes_mod = types.ModuleType("_pytest.outcomes")
    outcomes_mod.OutcomeException = OutcomeException
    outcomes_mod.Skipped = Skipped
    outcomes_mod.Failed = Failed
    outcomes_mod.XFailed = XFailed
    outcomes_mod.Exit = Exit
    # The real _pytest.outcomes also exports the functions
    # skip/fail/xfail/exit — not only the exception classes.
    outcomes_mod.skip = skip
    outcomes_mod.fail = fail
    outcomes_mod.xfail = xfail
    outcomes_mod.exit = exit
    mp_mod = types.ModuleType("_pytest.monkeypatch")
    mp_mod.MonkeyPatch = MonkeyPatch
    mark_mod = types.ModuleType("_pytest.mark")
    mark_mod.Mark = Mark
    mark_mod.MarkGenerator = MarkGenerator
    mark_mod.ParameterSet = ParameterSet
    mark_struct_mod = types.ModuleType("_pytest.mark.structures")
    mark_struct_mod.Mark = Mark
    mark_struct_mod.MarkDecorator = Mark
    mark_struct_mod.ParameterSet = ParameterSet
    nodes_mod = types.ModuleType("_pytest.nodes")
    nodes_mod.Node = NodeStub
    nodes_mod.Item = NodeStub
    cfg_mod = types.ModuleType("_pytest.config")
    cfg_mod.Config = ConfigStub
    log_mod = types.ModuleType("_pytest.logging")
    log_mod.LogCaptureFixture = LogCaptureFixture

    sys.modules["_pytest"] = _pytest_root
    sys.modules["_pytest.outcomes"] = outcomes_mod
    sys.modules["_pytest.monkeypatch"] = mp_mod
    sys.modules["_pytest.mark"] = mark_mod
    sys.modules["_pytest.mark.structures"] = mark_struct_mod
    sys.modules["_pytest.nodes"] = nodes_mod
    sys.modules["_pytest.config"] = cfg_mod
    sys.modules["_pytest.logging"] = log_mod
    # Direct _pytest submodule imports (SubRequest, WarningsRecorder, Outcomes).
    fixtures_mod = types.ModuleType("_pytest.fixtures")
    fixtures_mod.SubRequest = FixtureRequest
    fixtures_mod.FixtureRequest = FixtureRequest

    class _FixtureFunctionDefinition:
        # Third-party decorators check isinstance(test, sys.modules["_pytest.fixtures"]
        # .FixtureFunctionDefinition) as an "@given on a fixture" guard. Only the
        # isinstance class is needed — our fixtures are never its instances
        # (semantics preserved).
        ...

    fixtures_mod.FixtureFunctionDefinition = _FixtureFunctionDefinition
    recwarn_mod = types.ModuleType("_pytest.recwarn")
    recwarn_mod.WarningsRecorder = WarningsRecorder
    tmpdir_mod = types.ModuleType("_pytest.tmpdir")
    tmpdir_mod.TempPathFactory = TmpPathFactory
    capture_mod = types.ModuleType("_pytest.capture")
    capture_mod.CaptureFixture = CapSysFixture
    sys.modules["_pytest.fixtures"] = fixtures_mod
    sys.modules["_pytest.recwarn"] = recwarn_mod
    sys.modules["_pytest.tmpdir"] = tmpdir_mod
    sys.modules["_pytest.capture"] = capture_mod


# ============================================================================
# 8. Collection, Parametrize + Fixture-Params Expansion, Filtering & Execution
# ============================================================================

# Only genuinely invocable conftest hooks are called; non-invocable ones — fail-closed ZtestUnsupportedApiError. Invariants: HOOK-EXPORT-FAIL-CLOSED.
# pytest_generate_tests and pytest_ignore_collect have real
# support and are on the invocable list.
SUPPORTED_CONFTEST_HOOKS = {
    "pytest_addoption",
    "pytest_configure",
    "pytest_unconfigure",
    "pytest_collection_modifyitems",
    "pytest_sessionstart",
    "pytest_sessionfinish",
    "pytest_runtest_setup",
    "pytest_runtest_teardown",
    "pytest_generate_tests",
    "pytest_ignore_collect",
    "pytest_collect_file",
    # Custom collection of module attributes — driven by _drive_pcmi_hook
    # with a pluggy.Result emulation (see pcmi_hooks in run_test_file).
    "pytest_pycollect_makeitem",
}

# Reporting metadata hooks are recognized and deliberately not called —
# ztest prints no pytest header, the hook has no observable effect; its
# presence in a conftest never fails collection. Invariants: OSS-RH.
RECOGNIZED_NOOP_CONFTEST_HOOKS = {
    "pytest_report_header",
    # pytest_terminal_summary — a reporting hook; under ztest the terminal is owned by the Zig supervisor.
    "pytest_terminal_summary",
    # The remaining purely reporting hooks — no effect on collection/outcomes,
    # they do not fail the conftest scan.
    "pytest_report_teststatus",
    "pytest_runtest_logreport",
    "pytest_runtest_makereport",
    "pytest_runtest_logstart",
    "pytest_runtest_logfinish",
    "pytest_assertrepr_compare",
    "pytest_deselected",
    "pytest_collectreport",
}


# pytest MarkEvaluator evaluates skipif/xfail strings in the namespace
# {os, sys, platform, sysconfig} + the test module's globals — the module may
# not import sys while the string uses it.
def _skipif_eval_ns(module: types.ModuleType) -> dict[str, Any]:
    import os as _os
    import platform as _platform
    import sys as _sys
    import sysconfig as _sysconfig

    ns: dict[str, Any] = {"os": _os, "sys": _sys, "platform": _platform, "sysconfig": _sysconfig}
    ns.update(getattr(module, "__dict__", {}) or {})
    return ns


class _MetafuncStub:
    """Metafunc for pytest_generate_tests.

    Exactly the callable surface is supported: fixturenames (for the
    `if 'x' in metafunc.fixturenames` gate), config (options registered
    via pytest_addoption), function/cls/module/definition and parametrize(),
    which materializes into a synthetic Mark and goes through the regular
    _expand_parametrize axis (indecl tokens are preserved: one call = one
    Mark object = one declaration).
    """

    def __init__(
        self,
        func: Callable[..., Any],
        cls: type | None,
        module: types.ModuleType,
        config: "ConfigStub",
        fixturenames: list[str],
        definition: "NodeStub | None" = None) -> None:
        self.function = func
        self.cls = cls
        self.module = module
        self.config = config
        self.fixturenames = list(fixturenames)
        self.definition = definition
        self._parametrize_marks: list[Mark] = []

    def parametrize(
        self,
        argnames: Any,
        argvalues: Any,
        indirect: Any = False,
        ids: Any = None,
        scope: Any = None,
        **kwargs: Any) -> None:
        if kwargs:
            raise ZtestUnsupportedApiError(
                f"metafunc.parametrize() got unsupported keyword(s) {sorted(kwargs)}: "
                f"the ztest shim supports argnames/argvalues/indirect/ids/scope only (rejected)"
            )
        # scope= is accepted and ignored: the shim does not model fixture-scope
        # granularity of parameterization (documented, INVARIANTS.md).
        # An ids= generator is materialized here: further down the
        # pipeline ids travel as list/callable, a generator would otherwise silently
        # degrade to an auto-id.
        if ids is not None and not callable(ids) and not isinstance(ids, (list, tuple)):
            ids = list(ids)
        self._parametrize_marks.append(
            Mark("parametrize", (argnames, list(argvalues)), {"indirect": indirect, "ids": ids})
        )


def _bind_hook_kwargs(fn: Callable[..., Any], available: dict[str, Any], hook_name: str) -> dict[str, Any]:
    """Name-aware conftest hook argument binding (pluggy name semantics);
    an unknown REQUIRED parameter is an error (silent substitution forbidden)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as sig_e:
        raise ZtestUnsupportedApiError(
            f"conftest hook {hook_name!r}: cannot inspect signature ({sig_e}); the hook is rejected"
        ) from sig_e
    kw: dict[str, Any] = {}
    for pname, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if pname in available:
            kw[pname] = available[pname]
        elif p.default is inspect.Parameter.empty:
            raise ZtestUnsupportedApiError(
                f"conftest hook {hook_name!r} requires argument {pname!r} which the ztest shim "
                f"cannot provide (have: {sorted(available)}); the hook is rejected"
            )
    return kw


def _fixturename_closure(direct: list[str], layers: list[dict[str, FixtureDef]]) -> list[str]:
    """Metafunc.fixturenames = direct arguments + the transitive
    fixture closure + autouse (parity with pytest getfixtureclosure).

    When a hook-generated axis feeds a fixture dependency — the hook gate
    `if 'each_version' in metafunc.fixturenames` must see the name from
    the fixture's signature, otherwise the axis never applies. Autouse
    fixtures are in the closure in pytest (verified empirically on 9.0.3).
    """
    out: list[str] = []
    seen: set[str] = set()
    queue: list[str] = list(direct)
    # autouse fixtures enter the
    # closure together with their transitive dependencies — names go into the
    # BFS queue, not only into the output (otherwise the hook gate is blind to the autouse dep chain).
    for layer in layers:
        for fname, fdef in layer.items():
            if fdef.autouse:
                queue.append(fname)
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        fdef: FixtureDef | None = None
        for idx in range(len(layers) - 1, -1, -1):  # innermost (last) wins, like _find_fixture
            if name in layers[idx]:
                fdef = layers[idx][name]
                break
        if fdef is None:
            continue
        try:
            params = inspect.signature(fdef.func).parameters
        except (TypeError, ValueError):
            continue
        for pname, p in params.items():
            if pname in ("self", "cls") or pname in seen:
                continue
            # VAR_POSITIONAL/VAR_KEYWORD from fixture signatures —
            # not fixture names (pytest does not show them).
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            queue.append(pname)
    return out


def _generate_tests_marks(
    hooks: list[tuple[types.ModuleType, Callable[..., Any]]],
    func: Callable[..., Any],
    cls: type | None,
    module: types.ModuleType,
    cfg: "ConfigStub",
    filepath: Path,
    rel_file: str,
    base_name: str,
    layers: list[dict[str, FixtureDef]] | None = None,
    extra_marks: list[Mark] | None = None) -> list[Mark]:
    """Run the pytest_generate_tests chain for one function/method.

    The hook is called once per function per conftest (the pluggy chain
    order), synthetic parametrize marks accumulate multiplicatively.
    fixturenames — the transitive closure (_fixturename_closure).
    """
    if not hooks:
        return []
    try:
        sig = inspect.signature(func)
        fixturenames = [
            p.name
            for p in sig.parameters.values()
            if p.name != "self"
            and p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
    except Exception:
        # including a RuntimeError from proxy objects: safe_getattr semantics.
        fixturenames = []
    try:
        _fn_pytestmark = getattr(func, "pytestmark", ()) or ()
    except Exception:
        _fn_pytestmark = ()
    for m in _fn_pytestmark:
        if isinstance(m, Mark) and m.name == "usefixtures":
            fixturenames.extend(str(a) for a in m.args)
    # Class/module-level usefixtures also enter
    # metafunc.fixturenames in pytest (accumulated marks are passed by the collector).
    for m in extra_marks or ():
        if isinstance(m, Mark) and m.name == "usefixtures":
            fixturenames.extend(str(a) for a in m.args)
    fixturenames = _fixturename_closure(fixturenames, layers or [])
    definition = NodeStub(
        base_name, f"{rel_file}::{base_name}", filepath, func, cls=cls, module=module, config=cfg
    )
    marks: list[Mark] = []
    for cf_mod, hook in hooks:
        stub = _MetafuncStub(func, cls, module, cfg, fixturenames, definition)
        _flush_ipc_now()  # the flush barrier before the user hook
        _check_conf_hook_result("pytest_generate_tests", hook(stub))
        marks.extend(stub._parametrize_marks)
    return marks


class _PluggyResultEmu:
    """A minimal pluggy.Result emulation for old-style wrapper hooks
    (hookwrapper=True): get_result/force_result. The shim's default result
    is None."""

    def __init__(self, result: Any = None) -> None:
        self._res = result
        self._exc: BaseException | None = None

    def get_result(self) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._res

    def force_result(self, result: Any) -> None:
        self._res = result
        self._exc = None

    def force_exception(self, exc: BaseException) -> None:
        self._exc = exc


def _drive_pcmi_hook(hook: Callable[..., Any], collector: Any, name: str, obj: Any) -> Any:
    """pytest_pycollect_makeitem — old-style wrapper
    (a generator with outcome = yield). The drive: pre-yield → send(Result)
    → force_result decides. Plain functions (the new style) are called directly.
    None → the hook did not take the attribute, default collection continues."""
    res = hook(collector=collector, name=name, obj=obj)
    if not inspect.isgenerator(res):
        return res
    gen = res
    try:
        next(gen)
    except StopIteration:
        return None
    emu = _PluggyResultEmu(None)
    try:
        gen.send(emu)
    except StopIteration:
        pass
    else:
        gen.close()  # more than one yield — not expected
    try:
        return emu.get_result()
    except BaseException:
        return None


def _wrap_custom_collected(
    custom: Any,
    rel_file: str,
    filepath: Path,
    test_mod: types.ModuleType,
    mod_marks: list["Mark"],
    fallback_name: str) -> list["TestItemSpec"]:
    """The pytest_pycollect_makeitem result (Item | Collector | list)
    → TestItemSpecs run by the standard pipeline via item.runtest().
    nodeid comes from item.nodeid (the parent chain gives the pytest form
    file::fn::data-name); without nodeid — the file::fn::item.name fallback."""
    if isinstance(custom, (list, tuple)):
        raw = list(custom)
    elif hasattr(custom, "collect") and callable(custom.collect):
        raw = list(custom.collect() or [])
    else:
        raw = [custom]
    specs: list[TestItemSpec] = []
    for it in raw:
        nid = getattr(it, "nodeid", None) or f"{rel_file}::{fallback_name}::{getattr(it, 'name', 'item')}"

        def _mk(_it: Any = it) -> Callable[[], None]:
            def _run() -> None:
                _it.runtest()

            return _run

        specs.append(
            TestItemSpec(
                nodeid=nid,
                name=getattr(it, "name", fallback_name),
                filepath=filepath,
                func=_mk(),
                cls=None,
                module=test_mod,
                markers=list(mod_marks),
                call_params={},
                indirect_params={})
        )
    return specs


class TestItemSpec:
    def __init__(
        self,
        nodeid: str,
        name: str,
        filepath: Path,
        func: Callable[..., Any],
        cls: type | None,
        module: types.ModuleType,
        markers: list[Mark],
        call_params: dict[str, Any],
        indirect_params: dict[str, Any],
        is_unittest: bool = False) -> None:
        self.nodeid = nodeid
        self.name = name
        self.filepath = filepath
        self.func = func
        self.cls = cls
        self.module = module
        self.markers = markers
        self.call_params = call_params
        self.indirect_params = indirect_params
        self.is_unittest = is_unittest
        self.node = NodeStub(name, nodeid, filepath, func, cls=cls, module=module, markers=markers)


def _extract_fixtures_from_obj(obj: Any) -> dict[str, FixtureDef]:
    res: dict[str, FixtureDef] = {}
    try:
        _attr_names = dir(obj)
    except Exception:
        # An exploding __dir__ (a proxy object) — an empty layer, not a CollectionError.
        return res
    for attr_name in _attr_names:
        try:
            val = getattr(obj, attr_name)
        except Exception:
            continue
        # Non-fixture attributes with an exploding __getattr__ (lazy proxies and descriptors) are skipped safely —
        # a proxy cannot carry a ztest fixture, and its exception must not
        # be allowed to crash the module's collection.
        try:
            fdef = getattr(val, "_ztest_fixture", None)
        except Exception:
            continue
        if isinstance(fdef, FixtureDef):
            res[fdef.name] = fdef
    if inspect.isclass(obj):
        # The @pytest.fixture + @classmethod/@staticmethod combination:
        # an autouse class-scope fixture with importorskip. The mark lives on
        # the descriptor (fixture-over-classmethod) or on __func__ (the reverse
        # order) — via bound access it is invisible/non-callable (a classmethod
        # is not callable). We walk the raw __dict__ along the MRO and rebind
        # func onto __func__ with a cls-binding flag.
        # The MRO is walked from base to subclass — the last
        # record (the most derived class) wins, override-semantics parity.
        for klass in reversed(inspect.getmro(obj)):
            for _an, raw in vars(klass).items():
                if not isinstance(raw, (classmethod, staticmethod)):
                    continue
                try:
                    fdef = getattr(raw, "_ztest_fixture", None)
                    if not isinstance(fdef, FixtureDef):
                        fdef = getattr(raw.__func__, "_ztest_fixture", None)
                except Exception:
                    continue
                if not isinstance(fdef, FixtureDef):
                    continue
                rebound = FixtureDef(raw.__func__, fdef.name, scope=fdef.scope, autouse=fdef.autouse, params=fdef.params, ids=fdef.ids)
                rebound.decl_token = fdef.decl_token
                rebound._bind_cls_first = isinstance(raw, classmethod)  # type: ignore[attr-defined]
                res[fdef.name] = rebound
    return res


def _parse_argnames(argnames: str | list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(argnames, str):
        return [x.strip() for x in argnames.split(",") if x.strip()]
    return [str(x).strip() for x in argnames]


# A global decl_token counter — two declarations of one fixture name never share a cache entry. Invariants: DECL-TOKEN-KEY.
_PARAM_DECL_COUNTER = itertools.count()


def _decl_token_for(mark_obj: Mark) -> int:
    """Stable declaration token for a parametrize Mark: one Mark object == one declaration."""
    tok = getattr(mark_obj, "_ztest_decl_token", None)
    if tok is None:
        tok = next(_PARAM_DECL_COUNTER)
        try:
            mark_obj._ztest_decl_token = tok  # type: ignore[attr-defined]
        except Exception:
            try:
                object.__setattr__(mark_obj, "_ztest_decl_token", tok)
            except Exception:
                pass
    return tok


def _collect_parametrized_fixture_axes(
    func: Callable[..., Any],
    fixture_layers: list[dict[str, FixtureDef]],
    markers: list[Mark] | None = None) -> list[list[tuple[dict[str, Any], dict[str, Any], str, list[Mark]]]]:
    """The full transitive closure of parameterized fixture axes; indirect = (decl_token, axis_index, value) (INVARIANT INDIRECT-CLOSURE-AXES, AXIS-MEMBERSHIP-ENFORCED, FIXTURE-KEY-UNIQUE, DECL-TOKEN-KEY; see INVARIANTS.md)."""
    merged_fixtures: dict[str, FixtureDef] = {}
    for layer in fixture_layers:
        merged_fixtures.update(layer)

    visited: set[str] = set()
    param_fdefs: list[FixtureDef] = []

    def _add_fixture_root(fname: str) -> None:
        if fname in visited or fname in ("self", "request"):
            return
        visited.add(fname)
        fdef = merged_fixtures.get(fname)
        if fdef is None:
            return
        if fdef.params:
            param_fdefs.append(fdef)
        _walk_deps(fdef.func)

    def _walk_deps(fn: Callable[..., Any]) -> None:
        try:
            sig = inspect.signature(fn)
        except Exception:
            return
        for pname in sig.parameters:
            if pname in ("self", "request"):
                continue
            _add_fixture_root(pname)

    # Signature of the test itself.
    _walk_deps(func)
    # usefixtures markers on the test (INDIRECT-CLOSURE-AXES: they are real fixture dependencies).
    for m in markers or []:
        if m.name == "usefixtures":
            for fn_name in m.args:
                _add_fixture_root(str(fn_name))
    # Autouse fixtures visible to this node, with their recursive dependencies (INDIRECT-CLOSURE-AXES).
    for fname, fdef in merged_fixtures.items():
        if fdef.autouse:
            _add_fixture_root(fname)

    axes: list[list[tuple[dict[str, Any], dict[str, Any], str, list[Mark]]]] = []
    for fdef in param_fdefs:
        # DECL-TOKEN-KEY: declaration-unique token (one FixtureDef == one params declaration; the
        # token is assigned once and stays stable for every test using this fixture).
        if fdef.decl_token is None:
            fdef.decl_token = next(_PARAM_DECL_COUNTER)
        axis_entries: list[tuple[dict[str, Any], dict[str, Any], str, list[Mark]]] = []
        # Generator fixture-ids are materialized once and persisted in
        # fdef.ids — otherwise the second consumer of the same fixture gets
        # an exhausted generator.
        if fdef.ids is not None and not callable(fdef.ids) and not isinstance(fdef.ids, (list, tuple, str, bytes)) and hasattr(fdef.ids, "__iter__"):
            fdef.ids = list(fdef.ids)
        fdef_ids_list = fdef.ids
        # pytest parity: str/bytes ids — sequences of characters/ints.
        if isinstance(fdef_ids_list, str):
            fdef_ids_list = list(fdef_ids_list)
        elif isinstance(fdef_ids_list, bytes):
            fdef_ids_list = list(fdef_ids_list)
        # ids cardinality == params, else a loud collection error (pytest);
        # empty ids (num_ids == 0) mean value-ids, not an error (pytest
        # #1849); str/bytes "" already became [] above.
        if isinstance(fdef_ids_list, (list, tuple)) and not fdef_ids_list:
            fdef_ids_list = None
        if isinstance(fdef_ids_list, (list, tuple)) and len(fdef_ids_list) != len(fdef.params or []):
            raise ValueError(
                f"fixture {fdef.name!r}: {len(fdef.params or [])} params specified, with different number of ids: {len(fdef_ids_list)}"
            )
        for idx, raw_val in enumerate(fdef.params or []):
            extra_marks: list[Mark] = []
            custom_id: str | None = None
            if isinstance(raw_val, ParameterSet):
                val = raw_val.values[0] if len(raw_val.values) == 1 else raw_val.values
                extra_marks = list(raw_val.marks)
                custom_id = raw_val.id
            else:
                val = raw_val
            if custom_id is not None:
                eid = _escape_id_text(str(custom_id))
            elif callable(fdef_ids_list):
                # pytest: ids=callable receives the value; None → a value-id;
                # an exception — a loud collection error (not a silent substitution).
                try:
                    _cid = fdef_ids_list(val)
                except Exception as exc:
                    raise ValueError(f"fixture ids callable raised for {fdef.name!r} index {idx}: {exc}") from exc
                if _cid is None:
                    eid = _pytest_param_id(val, fdef.name, idx)
                else:
                    # The result goes through _idval_from_value; an unsupported
                    # type → the value's value-id.
                    _rid = _idval_supported(_cid)
                    eid = _rid if _rid is not None else _pytest_param_id(val, fdef.name, idx)
            elif isinstance(fdef_ids_list, (list, tuple)) and idx < len(fdef_ids_list) and fdef_ids_list[idx] is not None:
                # A list entry goes through _idval_from_value (required).
                _sid = _idval_supported(fdef_ids_list[idx])
                if _sid is None:
                    raise ValueError(
                        f"fixture ids contains unsupported value {fdef_ids_list[idx]!r} at index {idx} "
                        f"for {fdef.name!r} (pytest: collection error)"
                    )
                eid = _sid
            else:
                # Functional objects → __name__;
                # Enum/bytes/Pattern — as in the decorator axis.
                eid = _pytest_param_id(val, fdef.name, idx)
            # (decl_token, axis_index, value) — the declaration-unique carrier of indirect params. Invariants: FIXTURE-KEY-UNIQUE, DECL-TOKEN-KEY.
            axis_entries.append(({}, {fdef.name: (fdef.decl_token, idx, val)}, eid, extra_marks))
        if axis_entries:
            axes.append(axis_entries)
    return axes


def _expand_parametrize(
    base_name: str,
    rel_file: str,
    cls_name: str | None,
    filepath: Path,
    func: Callable[..., Any],
    cls: type | None,
    module: types.ModuleType,
    all_marks: list[Mark],
    fixture_layers: list[dict[str, FixtureDef]] | None = None,
    extra_param_marks: list[Mark] | None = None,
    mark_split: tuple[int, int] | None = None) -> list[TestItemSpec]:
    # The canonical pytest order of composite-id axes: [hook
    # (pytest_generate_tests), fixture-params, method-marks, class-marks,
    # module-marks]. all_marks comes as [module, class, method] — unroll at
    # the mark_split boundaries.
    if mark_split is not None:
        _n_mod, _n_cls = mark_split
        _seg_mod = all_marks[:_n_mod]
        _seg_cls = all_marks[_n_mod:_n_mod + _n_cls]
        _seg_fn = all_marks[_n_mod + _n_cls:]
        marks_for_param = list(_seg_fn) + list(_seg_cls) + list(_seg_mod)
    else:
        marks_for_param = all_marks
    param_marks = [m for m in marks_for_param if m.name == "parametrize"]
    # Synthetic parametrize marks of the pytest_generate_tests chain.
    # Hook axes go before decorator axes (pytest parity).
    hook_param_marks = list(extra_param_marks or [])
    # force_tuple for list-form argnames — in the loop below.
    # The duplicate gate covers all pairs — decorator,
    # hook and module parametrize over one name (pytest: a loud error).
    _seen_argnames: dict[str, int] = {}
    for pm in list(param_marks) + hook_param_marks:
        if len(pm.args) < 2:
            continue
        for _an in _parse_argnames(pm.args[0]):
            _seen_argnames[_an] = _seen_argnames.get(_an, 0) + 1
    for _an, _cnt in _seen_argnames.items():
        if _cnt > 1:
            raise ValueError(f"duplicate parametrization of {_an!r}")
    fixture_axes = _collect_parametrized_fixture_axes(func, fixture_layers or [], markers=all_marks)
    # Parametrize over a name whose fixture has its own
    # params= replaces the fixture axis (pytest: the mark's axis fully
    # overrides the fixture's axis). The rule is the same
    # for indirect=True (the fixture is called with request.param) and for direct
    # parametrize (the fixture is shadowed, the value goes into the test directly) — therefore
    # the fixture axis is removed on any mention of the name in marks.
    mark_argnames: set[str] = set()
    for pm in list(param_marks) + hook_param_marks:
        if len(pm.args) < 2:
            continue
        mark_argnames.update(_parse_argnames(pm.args[0]))
    if mark_argnames:
        fixture_axes = [
            ax
            for ax in fixture_axes
            if not (ax and ax[0][1] and next(iter(ax[0][1])) in mark_argnames)
        ]

    if not param_marks and not fixture_axes and not hook_param_marks:
        prefix = f"{rel_file}::{cls_name}::" if cls_name else f"{rel_file}::"
        return [
            TestItemSpec(
                nodeid=f"{prefix}{base_name}",
                name=base_name,
                filepath=filepath,
                func=func,
                cls=cls,
                module=module,
                markers=all_marks,
                call_params={},
                indirect_params={},
                is_unittest=False)
        ]

    hook_axes: list[list[tuple[dict[str, Any], dict[str, Any], str, list[Mark]]]] = []
    dec_axes: list[list[tuple[dict[str, Any], dict[str, Any], str, list[Mark]]]] = []
    # Decorator marks in application order (the bottom decorator first).
    # Hook axes before fixture ones, fixture ones before decorator ones.
    for pm, _is_hook_axis in [(m, True) for m in hook_param_marks] + [(m, False) for m in param_marks]:
        if len(pm.args) < 2:
            continue
        # One parametrize Mark = one declaration = one token. Invariants: DECL-TOKEN-KEY.
        pm_token = _decl_token_for(pm)
        argnames = _parse_argnames(pm.args[0])
        # argnames given as a list/tuple (("value",)) enable the pytest
        # force_tuple — a sequence value is unpacked positionally even for a
        # single name; a string name — the value as-is (validators).
        force_tuple = isinstance(pm.args[0], (list, tuple))
        argvalues = list(pm.args[1])
        indirect = pm.kwargs.get("indirect", False)
        ids_arg = pm.kwargs.get("ids", None)
        # Iterable ids — pytest islice(ids, n): materialize exactly for the
        # number of parametersets (infinite generators like itertools.count()
        # are legal). str/bytes — per-char/int id sequences (pytest).
        if ids_arg is not None and not callable(ids_arg) and not isinstance(ids_arg, (list, tuple)):
            if isinstance(ids_arg, str):
                ids_arg = list(ids_arg)
            elif isinstance(ids_arg, bytes):
                ids_arg = list(ids_arg)
            elif hasattr(ids_arg, "__iter__"):
                import itertools as _it

                ids_arg = list(_it.islice(ids_arg, len(argvalues)))
        # ids cardinality == argvalues, else a loud collection error;
        # num_ids == 0 is not an error: pytest treats empty ids as "generate
        # ids from the values" (#1849).
        if isinstance(ids_arg, (list, tuple)) and not ids_arg:
            ids_arg = None
        if isinstance(ids_arg, (list, tuple)) and len(ids_arg) != len(argvalues):
            raise ValueError(
                f"parametrize({argnames}): {len(argvalues)} parameter sets specified, with different number of ids: {len(ids_arg)}"
            )
        indirect_set: set[str] = set()
        if indirect is True:
            indirect_set = set(argnames)
        elif isinstance(indirect, (list, tuple, set)):
            indirect_set = set(indirect)

        axis_entries: list[tuple[dict[str, Any], dict[str, Any], str, list[Mark]]] = []
        for idx, raw_val in enumerate(argvalues):
            extra_marks: list[Mark] = []
            custom_id: str | None = None
            if isinstance(raw_val, ParameterSet):
                vals = raw_val.values
                extra_marks = list(raw_val.marks)
                custom_id = raw_val.id
            elif force_tuple:
                vals = tuple(raw_val)
            elif len(argnames) == 1:
                vals = (raw_val,)
            elif isinstance(raw_val, (tuple, list)):
                vals = tuple(raw_val)
            else:
                vals = (raw_val,)

            # The number of values must match the number of names — pytest:
            # a loud collection error, not a silent zip truncation.
            if len(vals) != len(argnames):
                raise ValueError(
                    f"in 'parametrize' the number of names ({argnames}) does not match"
                    f" the number of values ({list(vals)})"
                )

            c_params: dict[str, Any] = {}
            i_params: dict[str, Any] = {}
            id_parts: list[str] = []
            for aname, aval in zip(argnames, vals):
                if aname in indirect_set:
                    # (decl_token, axis_index, value) tuple carrier (FIXTURE-KEY-UNIQUE + DECL-TOKEN-KEY):
                    # declaration-unique, collision-free cache keying.
                    i_params[aname] = (pm_token, idx, aval)
                else:
                    c_params[aname] = aval
                # pytest IdMaker value-based id (bytes/enum/escaping).
                id_parts.append(_pytest_param_id(aval, aname, idx))

            if custom_id is not None:
                # Explicit ids also go through ascii_escaped (pytest _resolve_ids).
                entry_id = _escape_id_text(str(custom_id))
            elif callable(ids_arg):
                # ids=callable is called by pytest PER component (not on the
                # whole tuple!): once per axis value, None → the component's
                # value-id; an exception — a loud collection error with context
                def _ids_one(comp: Any, comp_name: str) -> str:
                    try:
                        _cid = ids_arg(comp)
                    except Exception as exc:
                        raise ValueError(f"parametrize ids callable raised for index {idx} ({argnames}): {exc}") from exc
                    if _cid is None:
                        return _pytest_param_id(comp, comp_name, idx)
                    # The callable's result goes through _idval_from_value —
                    # str/bytes are escaped, an unsupported type → the
                    # component's value-id (not str()!).
                    _rid = _idval_supported(_cid)
                    return _rid if _rid is not None else _pytest_param_id(comp, comp_name, idx)

                if len(vals) == 1:
                    entry_id = _ids_one(vals[0], argnames[0])
                else:
                    entry_id = "-".join(_ids_one(v, n) for v, n in zip(vals, argnames))
            elif isinstance(ids_arg, (list, tuple)) and idx < len(ids_arg) and ids_arg[idx] is not None:
                # A list ids entry goes through _idval_from_value (required) —
                # an unsupported type = a loud collection error.
                _sid = _idval_supported(ids_arg[idx])
                if _sid is None:
                    raise ValueError(
                        f"ids contains unsupported value {ids_arg[idx]!r} at index {idx} "
                        f"for parametrize({argnames}) (pytest: collection error)"
                    )
                entry_id = _sid
            else:
                entry_id = "-".join(id_parts)
            axis_entries.append((c_params, i_params, entry_id, extra_marks))
        if not axis_entries and argnames:
            empty_mark = Mark("skip", (), {"reason": f"got empty parameter set for {argnames}"})
            axis_entries.append((
                {an: None for an in argnames if an not in indirect_set},
                {an: (pm_token, 0, None) for an in argnames if an in indirect_set},
                "NOTSET",
                [empty_mark],
            ))
        if axis_entries:
            (hook_axes if _is_hook_axis else dec_axes).append(axis_entries)

    axes = hook_axes + list(fixture_axes) + dec_axes
    if not axes:
        prefix = f"{rel_file}::{cls_name}::" if cls_name else f"{rel_file}::"
        return [TestItemSpec(f"{prefix}{base_name}", base_name, filepath, func, cls, module, all_marks, {}, {}, False)]

    specs: list[TestItemSpec] = []
    prefix = f"{rel_file}::{cls_name}::" if cls_name else f"{rel_file}::"
    combos = list(itertools.product(*axes))
    # pytest make_unique_ids — duplicate ids get a numeric
    # suffix on all occurrences (dup0, dup1), not "_1" from the second on.
    raw_ids: list[str] = []
    raw_counts: dict[str, int] = {}
    for combo in combos:
        _rid = "-".join(eid for _cp, _ip, eid, _em in combo)
        raw_ids.append(_rid)
        raw_counts[_rid] = raw_counts.get(_rid, 0) + 1
    seen_ids: dict[str, int] = {}
    _all_ids: set[str] = set(raw_ids)  # collisions are counted against the raw ids too
    for combo, raw_id in zip(combos, raw_ids):
        merged_c: dict[str, Any] = {}
        merged_i: dict[str, Any] = {}
        merged_m: list[Mark] = list(all_marks)
        for cp, ip, _eid, em in combo:
            merged_c.update(cp)
            merged_i.update(ip)
            merged_m.extend(em)
        if raw_counts[raw_id] > 1:
            # pytest suffixes '_' when the id ends in a digit (1_0/1_1),
            # otherwise with no separator (dup0/dup1), and spins the counter
            # while the candidate collides with any id of the set — raw or
            # already assigned (while new_id in resolved).
            _sep = "_" if raw_id[-1:].isdigit() else ""
            _i = seen_ids.get(raw_id, 0)
            while f"{raw_id}{_sep}{_i}" in _all_ids:
                _i += 1
            seen_ids[raw_id] = _i + 1
            final_id = f"{raw_id}{_sep}{_i}"
        else:
            final_id = raw_id
        _all_ids.add(final_id)
        item_name = f"{base_name}[{final_id}]"
        specs.append(
            TestItemSpec(
                nodeid=f"{prefix}{item_name}",
                name=item_name,
                filepath=filepath,
                func=func,
                cls=cls,
                module=module,
                markers=merged_m,
                call_params=merged_c,
                indirect_params=merged_i,
                is_unittest=False)
        )
    return specs


def _k_haystack(nodeid: str, own_markers: Any = None, cls: Any = None) -> str:
    """The -k haystack of one item: its nodeid plus its marker names.

    pytest evaluates -k over `item.keywords`, a dictionary that holds the
    nodeid chain (file, class, function, parametrized id) AND every marker
    name attached to the item. So `-k slow` selects a test whose only link to
    the word is `@pytest.mark.slow`, although the nodeid never contains it.
    Matching only the nodeid (the previous behavior) silently deselected
    those tests.

    Class marks are part of the item's keywords in pytest (a mark on the
    class applies to every method), so `pytestmark` of the enclosing class
    joins the haystack as well. Plain name/id matching is unchanged: the
    nodeid is still the first component of the returned string.
    """
    hay = nodeid.lower()
    names: list[str] = []
    for m in own_markers or ():
        _n = getattr(m, "name", None)
        if _n:
            names.append(str(_n))
    if cls is not None:
        try:
            pm = getattr(cls, "pytestmark", None)
        except Exception:
            # A proxy object with an exploding __getattr__ must not take the
            # whole selection down (same rule as the marker scan).
            pm = None
        if pm is not None:
            _pm_list = pm if isinstance(pm, (list, tuple)) else (pm,)
            for m in _pm_list:
                _n = getattr(m, "name", None)
                if _n:
                    names.append(str(_n))
    if names:
        hay = f"{hay} {' '.join(n.lower() for n in names)}"
    return hay


def _eval_bool_expr(expr: str, match_token: Callable[[str], bool]) -> bool:
    if not expr or not expr.strip():
        return True
    tokens = re.findall(r"\(|\)|\band\b|\bor\b|\bnot\b|[^\s()]+", expr)
    pos = 0

    def parse_or() -> bool:
        nonlocal pos
        val = parse_and()
        while pos < len(tokens) and tokens[pos] == "or":
            pos += 1
            rhs = parse_and()
            val = val or rhs
        return val

    def parse_and() -> bool:
        nonlocal pos
        val = parse_not()
        while pos < len(tokens) and tokens[pos] == "and":
            pos += 1
            rhs = parse_not()
            val = val and rhs
        return val

    def parse_not() -> bool:
        nonlocal pos
        if pos < len(tokens) and tokens[pos] == "not":
            pos += 1
            return not parse_not()
        if pos < len(tokens) and tokens[pos] == "(":
            pos += 1
            val = parse_or()
            if pos >= len(tokens) or tokens[pos] != ")":
                raise ValueError(f"Invalid filter expression (unclosed '('): {expr!r}")
            pos += 1
            return val
        if pos < len(tokens):
            tok = tokens[pos]
            if tok in ("and", "or", "not", ")"):
                raise ValueError(f"Invalid filter expression (unexpected token {tok!r}): {expr!r}")
            pos += 1
            return match_token(tok)
        raise ValueError(f"Invalid filter expression (missing operand): {expr!r}")

    res = parse_or()
    if pos != len(tokens):
        raise ValueError(f"Invalid filter expression (unexpected trailing token {tokens[pos]!r}): {expr!r}")
    return res


def _call_arity_aware(fn: Callable[..., Any], arg: Any) -> Any:
    """Call an xunit setup/teardown hook with 0 or 1 argument, decided by its signature; a TypeError raised inside fn is not caught."""
    # The flush barrier: user xunit/conftest code follows.
    _flush_ipc_now()
    try:
        params = [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in inspect.signature(fn).parameters.values())
        req_count = sum(1 for p in params if p.default is inspect.Parameter.empty)
    except Exception:
        return fn()
    if req_count >= 1 or (len(params) >= 1 and not has_varargs):
        return fn(arg)
    return fn()


def _invocable_conf_hook(cf_mod: types.ModuleType, name: str) -> Callable[..., Any] | None:
    """Conftest hooks must be plain callables; generator and coroutine hooks are
    rejected unless marked as hook wrappers:
    hookimpl(hookwrapper/wrapper=True) generator hooks are driven pre-yield
    +post-yield back-to-back at the call point — for ztest's setup/teardown
    hooks (no intermediate work between the halves) that is semantically the pluggy wrapper."""
    fn = getattr(cf_mod, name, None)
    if fn is None or not callable(fn):
        return None
    if inspect.isgeneratorfunction(fn) or inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn):
        if inspect.isgeneratorfunction(fn) and getattr(fn, "_ztest_hookimpl_wrapper", False):
            def _drive_wrapper(*args: Any, _fn: Callable[..., Any] = fn, **kwargs: Any) -> Any:
                gen = _fn(*args, **kwargs)
                next(gen)  # pre-yield
                try:
                    next(gen)  # post-yield → StopIteration is expected
                except StopIteration:
                    return None
                gen.close()  # guard against >1 yield
                return None
            return _drive_wrapper
        raise ZtestUnsupportedApiError(
            f"conftest hook {name!r} is a generator/coroutine function: the ztest shim invokes hooks "
            f"as plain callables and cannot drive its post-yield half (rejected)"
        )
    return fn


def _call_runtest_hook(fn: Callable[..., Any], item_node: Any, nextitem: Any = None, hook_name: str = "") -> Any:
    """Call a lifecycle conftest hook with pluggy-style injection by name.

    The set of allowed arguments follows the hook's hookspec:
    pytest_runtest_setup/call know only item; teardown —
    item+nextitem; a hook with an extra parameter is rejected by pytest with
    PluginValidationError already at conftest load; we raise TypeError at
    the call (loud, not a silent skip). POSITIONAL_ONLY parameters are passed
    positionally (pluggy does the same). Unknown required parameters — the
    positional fallback (a loud TypeError).
    pluggy varnames splits parameters: without a default → argnames
    (injected), with a default/KEYWORD_ONLY → kwargnames (never passed to
    non-wrapper impls). So a defaulted nextitem gets None/its default, and
    the real next item goes only to a required parameter (verified against
    live pytest 9.0.3 + pluggy 1.6.0).
    """
    # The flush barrier: no pending frames remain when control passes to the hook.
    _flush_ipc_now()
    allowed: dict[str, Any] = {"item": item_node}
    if hook_name == "pytest_runtest_teardown":
        allowed["nextitem"] = nextitem
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(item_node)
    params = [
        p
        for p in sig.parameters.values()
        if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if not params:
        return fn()
    # pluggy: only required positional parameters are injected
    # (POSITIONAL_ONLY/POSITIONAL_OR_KEYWORD without a default); parameters
    # with a default and keyword-only get their defaults (never passed).
    injectable = [
        p
        for p in params
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if all(p.name in allowed for p in injectable):
        pos = [allowed[p.name] for p in injectable if p.kind == inspect.Parameter.POSITIONAL_ONLY]
        kw = {p.name: allowed[p.name] for p in injectable if p.kind != inspect.Parameter.POSITIONAL_ONLY}
        return fn(*pos, **kw)
    return fn(item_node)


def _check_conf_hook_result(name: str, res: Any) -> None:
    """Reject hooks RETURNING generator/asyncgen/coroutine/awaitable — the _ZtestNeverExecutedError sentinel."""
    if inspect.isgenerator(res) or inspect.iscoroutine(res) or inspect.isasyncgen(res) or inspect.isawaitable(res):
        raise _ZtestNeverExecutedError(
            f"conftest hook {name!r} returned a {type(res).__name__} "
            f"(generator/asyncgen/coroutine/awaitable): the ztest shim cannot drive it "
            f"(rejected)"
        )


def _ztest_lifecycle_kind_guard(
    fn: Callable[..., Any], label: str, *, allow_coroutine: bool = False
) -> None:
    """Reject a generator or coroutine xunit lifecycle before invocation; the kind is classified on the OUTER callable (unwrapping would falsely cut asyncio.run adapters); allow_coroutine is set only where a driver exists."""
    if not callable(fn):
        return
    _is_coro = inspect.iscoroutinefunction(fn)
    if (
        (not allow_coroutine and _is_coro)
        or inspect.isgeneratorfunction(fn)
        or inspect.isasyncgenfunction(fn)
    ):
        _kind = (
            "async generator function" if inspect.isasyncgenfunction(fn)
            else "generator function" if inspect.isgeneratorfunction(fn)
            else "coroutine function"
        )
        raise _ZtestNeverExecutedError(
            f"xunit lifecycle {label!r} is an {_kind}: it is invoked as a plain callable "
            f"and its body never executes that way (rejected)"
        )


def _ztest_lifecycle_result_guard(res: Any, label: str) -> None:
    """Post-invocation result contract for xunit/cleanup: an undriven generator/coroutine — close/cancel + sentinel."""
    if isinstance(res, (types.GeneratorType, types.AsyncGeneratorType)) or inspect.isawaitable(res):
        _ztest_close_unexecuted(res)
        raise _ZtestNeverExecutedError(
            f"xunit lifecycle {label!r} returned a {type(res).__name__} "
            f"(generator/asyncgen/coroutine/awaitable): it was never driven and never "
            f"executed (rejected)"
        )


def _ztest_xunit_teardown_finalizer(fn_td: Callable[..., Any], fn_t: Any, label: str) -> Any:
    """Unified teardown_method/teardown_function contract: kind-guard before, result-guard after — symmetry with setup."""
    _ztest_lifecycle_kind_guard(fn_td, label)
    _res = _call_arity_aware(fn_td, fn_t)
    _ztest_lifecycle_result_guard(_res, label)
    return _res


_PRELOADED_CONFTESTS: dict[str, types.ModuleType] = {}

# Conftest fixtures are audited and extracted from the live module, per file:
# an introspection snapshot taken in the zygote can go stale when another
# conftest's pytest_configure mutates this module's attributes
# (cross-module fixture registration) — module identity does not prove
# attribute immutability. The per-file path below always runs a full audit +
# fixture extraction from the live module.
# Regression gate: cross-conftest fixture mutation.


def _audit_conftest_hooks(
    cf_mod: types.ModuleType,
    cf_path: Any,
    errors_out: list[str] | None = None,
    supported_out: list[str] | None = None) -> None:
    """Strict audit of a conftest module's pytest_* attributes
    (Invariants: HOOK-EXPORT-FAIL-CLOSED).
    errors_out is None — ZtestUnsupportedApiError is raised at the first
    error (the per-file path). errors_out given —
    errors accumulate AS DATA in dir() order (zygote cache prewarming: an
    audit failure must not kill the zygote; the per-file path raises the same error from the data).
    """
    for attr in dir(cf_mod):
        if not attr.startswith("pytest_"):
            continue
        # An exploding module-level __getattr__ (PEP 562) must not fail the
        # collection during the hook audit.
        try:
            hook_candidate = getattr(cf_mod, attr)
        except Exception:
            continue
        if not callable(hook_candidate):
            continue
        # Reporting metadata hooks are recognized no-ops; un-marked generator
        # forms are still rejected (HOOKIMPL-FLAGS-INERT).
        if attr in RECOGNIZED_NOOP_CONFTEST_HOOKS:
            if (inspect.isgeneratorfunction(hook_candidate) or inspect.isasyncgenfunction(hook_candidate)) \
                    and not getattr(hook_candidate, "_ztest_hookimpl_wrapper", False):
                _audit_msg = (
                    f"conftest hook {attr!r} in {cf_path} is a generator function: "
                    f"hook bodies never execute as generators (rejected, HOOKIMPL-FLAGS-INERT)"
                )
                if errors_out is None:
                    raise ZtestUnsupportedApiError(_audit_msg)
                errors_out.append(_audit_msg)
            continue
        if attr not in SUPPORTED_CONFTEST_HOOKS:
            _audit_msg = (
                f"Unsupported conftest hook {attr!r} in {cf_path}: the ztest shim only "
                f"invokes {sorted(SUPPORTED_CONFTEST_HOOKS)} (rejected)"
            )
            if errors_out is None:
                raise ZtestUnsupportedApiError(_audit_msg)
            errors_out.append(_audit_msg)
            continue
        if supported_out is not None:
            supported_out.append(attr)
        # Invocability validation (a generator/coroutine hook is an error).
        try:
            _invocable_conf_hook(cf_mod, attr)
        except ZtestUnsupportedApiError as _audit_exc:
            if errors_out is None:
                raise
            errors_out.append(str(_audit_exc))

# ============================================================================
# 9. ADR-005 execution engine: budgets, result bridge, phase wrappers
# ============================================================================

# The disarm/consume split + sticky flag — a swallowed SIGALRM surfaces as a timeout failure, not a phantom pass; one class owns the timer machine. Invariants: STICKY-TIMEOUT-FLAG, DISARM-AND-CONSUME-SEPARATE.
_PHASE_BUDGET_FLOOR_S = 1.5
_ZT_DRAIN_BOUND_S = 5.0


class _PhaseBudgets:
    """Per-unit budgets: arm/disarm/consume_fired/raise_if_fired/phase/timeout_dominance_exc/drain_runner — the timer automaton in one class (INVARIANT STICKY-TIMEOUT-FLAG, DISARM-AND-CONSUME-SEPARATE…POISONED-RUNNER-NEVER-CLOSED; see INVARIANTS.md)."""

    def __init__(self, ipc: IpcChannel, rel_file: str, per_test_timeout: float) -> None:
        self.ipc = ipc
        self.rel_file = rel_file
        self.per_test_timeout = per_test_timeout
        # The pending-frame age limit = min(50 ms, window/4); the window is
        # derived from --timeout (the single verifiable formula).
        ipc.set_supervisor_window(self._supervisor_window_s())
        self._fired = False
        self._armed = False
        self.installed = False
        # A stack of phases for phase-aware error routing —
        # the teardown origin is determined by the phase, not by exception order.
        self._phase_stack: list[str] = []

    def install(self) -> None:
        if self.per_test_timeout > 0:
            signal.signal(signal.SIGALRM, self._handler)
        global _ZT_PHASE_ARM, _ZT_PHASE_DISARM, _ZT_PHASE_RAISE, _ZT_TIMEOUT_DRAIN, _ZT_PHASE_PURE_DISARM, _ZT_PHASE_CONSUME
        _ZT_PHASE_ARM = self.phase
        _ZT_PHASE_DISARM = self.phase_off
        # A clean disarm (without pop) for the except branches of teardown_scope —
        # there the pop belongs to finally, otherwise a double pop steals a foreign phase frame.
        _ZT_PHASE_PURE_DISARM = self.disarm
        _ZT_PHASE_CONSUME = self.consume_fired
        _ZT_PHASE_RAISE = self.raise_if_fired
        _ZT_TIMEOUT_DRAIN = self.drain_runner
        self.installed = True

    def arm(self, seconds: float, *, reset_flag: bool = False) -> None:
        # arm never overwrites the sticky flag — a swallowed SIGALRM must
        # surface at the nearest raise_if_fired boundary (otherwise a timeout
        # between phases/cleanup units gives a phantom pass). Reset — only
        # at item start (begin_item) or via consume_fired/raise_if_fired.
        if reset_flag:
            self._fired = False
        self._armed = True
        try:
            signal.setitimer(signal.ITIMER_REAL, seconds)
        except Exception:
            pass

    def disarm(self) -> None:
        # Disarm stops the itimer but deliberately does not touch the sticky
        # flag (disarm/consume split): a swallowed timeout stays visible.
        self._armed = False
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.0, 0.0)
        except Exception:
            pass

    def consume_fired(self) -> bool:
        was = self._fired
        self._fired = False
        return was

    def raise_if_fired(self, phase_label: str) -> None:
        if self._fired:
            self._fired = False
            raise _TestTimeoutError(
                f"Test timed out after {self.per_test_timeout}s (sticky flag: SIGALRM raise was "
                f"swallowed by the event loop / an except-BaseException; phase={phase_label})"
            )

    def begin_item(self) -> None:
        # Per-item reset of the sticky flag — the single reset point
        # besides consume_fired/raise_if_fired; arm() does not touch the flag.
        self._fired = False

    def absorb_timeout(self) -> None:
        # A teardown handler that catches _TestTimeoutError must
        # quench the recurring storm (disarm) and consume the sticky flag before
        # formatting/IPC — otherwise the 50 ms SIGALRM queue fires into the
        # subsequent finalization phases.
        self.disarm()
        self.consume_fired()

    def timeout_dominance_exc(self, exit_kind: str) -> _TestTimeoutError:
        return _TestTimeoutError(
            f"Test timed out after {self.per_test_timeout}s (sticky flag: timeout dominates the "
            f"{exit_kind} exit of the test body; the SIGALRM raise was swallowed)"
        )

    def _handler(self, signum: int, frame: Any) -> None:
        # Sticky flag first, then recurring re-arm — the raise repeats until disarmed by the consumer. Invariants: DISARM-AND-CONSUME-SEPARATE.
        self._fired = True
        if self._armed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0.05, 0.05)
            except Exception:
                pass
        raise _TestTimeoutError(f"Test timed out after {self.per_test_timeout}s")

    def _phase_timeout_s(self) -> float:
        if self.per_test_timeout <= 0:
            return 0.0
        return max(self.per_test_timeout, _PHASE_BUDGET_FLOOR_S)

    def phase(self, label: str) -> None:
        # Heartbeat first (TEARDOWN_START re-arms the supervisor's inactivity
        # deadline), then the local itimer budget for the phase.
        self._phase_stack.append(label)
        # The heartbeat frame is deferred; the unconditional flush puts
        # _wrap_phase/teardown_scope before the user phase (no pending frames across user code).
        self.ipc.send_event("TEARDOWN_START", file=self.rel_file, phase=label, defer=True)
        _pt = self._phase_timeout_s()
        if _pt > 0:
            self.arm(_pt)

    def heartbeat(self, label: str) -> None:
        # A heartbeat at every phase boundary (setup/body included) — the
        # supervisor re-arms the inactivity deadline; otherwise phases legal by
        # the local budget get killed by a stale deadline (a false file CRASHED).
        try:
            self.ipc.send_event("PHASE_START", file=self.rel_file, phase=label, defer=True)
        except OSError:
            pass

    def phase_off(self) -> None:
        if self._phase_stack:
            self._phase_stack.pop()
        self.disarm()

    def in_teardown_phase(self) -> bool:
        # Errors from tearDown/asyncTearDown/doCleanups/finalizers/
        # conftest teardown — a teardown origin; xfail conversion is forbidden
        # for them, even if it is the item's first/only exception.
        if not self._phase_stack:
            return False
        cur = self._phase_stack[-1].lower()
        # Case-insensitively — "unittest:asyncTearDown" contains
        # "teardown" only in lower(). Otherwise a subtest failure in asyncTearDown is classified
        # as primary → xfail conversion → an exit-code lie.
        return "teardown" in cur or "docleanups" in cur

    def _supervisor_window_s(self) -> float:
        # Window = max(T*1000,1500)+1500 ms (mirrors workerReadTimeoutMs in main.zig); without --timeout — 1 h (3600 s, the 3_600_000 ms default). Invariants: INACTIVITY-WINDOW-DOMINATES.
        if self.per_test_timeout <= 0:
            return 3600.0
        return (max(self.per_test_timeout * 1000.0, 1500.0) + 1500.0) / 1000.0

    def drain_runner(self, res: Any, phase: str) -> None:
        # A heartbeat right before the drain — the supervisor re-arms the full window on TEARDOWN_START. Invariants: TIMER-CHECKS-INSIDE-TRY.
        try:
            # force: invariant TIMER-CHECKS-INSIDE-TRY requires this pulse to actually reach the
            # wire (the drain gets the full supervisor window, not the leftover after suppression).
            self.ipc.send_event("TEARDOWN_START", file=self.rel_file, phase=phase, force=True)
        except OSError:
            pass
        runner_obj = getattr(res, "async_runner", None)
        if runner_obj is None:
            return
        if not self._drain_runner_tasks(runner_obj):
            self._poison_runner(res, runner_obj)

    def _drain_runner_tasks(self, runner: Any) -> bool:
        # A bounded drain without cancel-and-wait — asyncio.wait within the budget, survivors → poison. Invariants: DRAIN-BOUNDED.
        try:
            loop = runner.get_loop()
        except BaseException:
            return False
        try:
            if loop is None or loop.is_closed():
                return False
        except BaseException:
            return False
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        except BaseException:
            return False
        if not pending:
            return True
        for _t in pending:
            try:
                _t.cancel()
            except BaseException:
                pass
        bound_s = min(_ZT_DRAIN_BOUND_S, self._supervisor_window_s() / 2.0)

        async def _drain_coro() -> None:
            await asyncio.wait(pending, timeout=bound_s)

        self.arm(bound_s, reset_flag=False)
        drained = False
        try:
            loop.run_until_complete(_drain_coro())
            drained = all(t.done() for t in pending)
        except BaseException:
            drained = False
        finally:
            self.disarm()
        return drained

    def _poison_runner(self, res: Any, runner_obj: Any) -> None:
        # Poison, not runner.close() — close = unbounded cancel-and-wait; the reference is dropped, the loop is closed best-effort. Invariants: POISONED-RUNNER-NEVER-CLOSED.
        try:
            res.async_runner = None
        except BaseException:
            pass
        try:
            runner_obj._ztest_drain_abandoned = True  # type: ignore[attr-defined]
        except BaseException:
            pass
        try:
            loop = runner_obj.get_loop()
        except BaseException:
            return
        try:
            if loop is not None and not loop.is_closed():
                loop.close()
        except BaseException:
            pass


class _FileRunCtx:
    """Mutable per-file execution state shared by the item runner helpers."""

    def __init__(
        self,
        rel_file: str,
        ipc: IpcChannel,
        options: dict[str, Any],
        budgets: _PhaseBudgets,
        conftest_mods: list[types.ModuleType],
        fixture_layers: list[dict[str, FixtureDef]],
        test_mod: types.ModuleType,
        tmp_factory: TmpPathFactory,
        per_test_timeout: float) -> None:
        self.rel_file = rel_file
        self.ipc = ipc
        self.options = options
        self.budgets = budgets
        self.conftest_mods = conftest_mods
        self.fixture_layers = fixture_layers
        self.test_mod = test_mod
        self.tmp_factory = tmp_factory
        self.per_test_timeout = per_test_timeout
        # pytest ini xfail_strict: the default strict for marks without an
        # explicit strict=; set per file from ini.
        self.xfail_strict: bool = False
        self.prev_resolver: FixtureResolver | None = None
        # The active item's resolver — its async runner is drained
        # by the bridge on timeout (a fixture setup timeout leaves pending tasks).
        self.active_resolver: FixtureResolver | None = None
        self.setup_classes: set[type] = set()
        # classes whose setUpClass was attempted (successfully or not) —
        # doClassCleanups must run for every attempted class exactly once.
        self.attempted_classes: set[type] = set()
        self.failed_classes: dict[type, BaseException] = {}
        self.cls_fixture_cache: dict[type, dict[str, FixtureDef]] = {}


def _close_class_lifecycle(ctx: _FileRunCtx, cls_to_close: type, failed: int) -> int:
    """Class-level lifecycle close: scope-("class") finalizers, tearDownClass,
    doClassCleanups — every unit budgeted, errors recorded, FILE_DONE preserved."""
    ipc = ctx.ipc
    rel_file = ctx.rel_file
    budgets = ctx.budgets
    if ctx.prev_resolver is not None:
        td_c_errs = ctx.prev_resolver.teardown_scope("class")
        for err_tb in td_c_errs:
            failed += 1
            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<class_teardown>", loc=rel_file, error="TeardownError", msg=err_tb, tb=err_tb)
    if cls_to_close in ctx.setup_classes:
        ctx.setup_classes.discard(cls_to_close)
        td_cls = getattr(cls_to_close, "tearDownClass", None) or getattr(cls_to_close, "teardown_class", None)
        if callable(td_cls):
            budgets.phase(f"teardown:tearDownClass:{cls_to_close.__name__}")
            try:
                _ztest_lifecycle_kind_guard(td_cls, f"{cls_to_close.__name__}.tearDownClass/teardown_class")
                # pytest-style teardown_class(cls) — arity-aware (see setup_class).
                _td_cls_res = _call_arity_aware(td_cls, cls_to_close)
                _ztest_lifecycle_result_guard(_td_cls_res, f"{cls_to_close.__name__}.tearDownClass/teardown_class")
                budgets.raise_if_fired(f"teardown:tearDownClass:{cls_to_close.__name__}")
            except KeyboardInterrupt:
                # absorb first — an armed recurring storm does not live inside the handler.
                budgets.absorb_timeout()
                failed += 1
                tb_s = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<tearDownClass>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                _mark_teardown_interrupt()
            except BaseException as _tdc_exc:
                # absorb unconditionally (not only on timeout) —
                # an armed storm does not live through the handler's format_exc/send_event.
                budgets.absorb_timeout()
                tb_s = traceback.format_exc()
                failed += 1
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<tearDownClass>", loc=rel_file, error="TeardownError", msg=tb_s, tb=tb_s)
            finally:
                budgets.phase_off()
    # doClassCleanups run for every attempted class — setUpClass failure
    # included (addClassCleanup may have registered before the failure).
    if cls_to_close in ctx.attempted_classes:
        ctx.attempted_classes.discard(cls_to_close)
        # CPython's pop-one loop is mirrored by per-unit budgets; a doClassCleanups override — one budgeting unit. Invariants: KIND-CLASSIFICATION-OUTER-ONLY.
        cc_stack = getattr(cls_to_close, "_class_cleanups", None)
        dc_cls = getattr(cls_to_close, "doClassCleanups", None)
        _dc_base = getattr(unittest.TestCase, "doClassCleanups", None)
        _dc_base_func = getattr(_dc_base, "__func__", _dc_base)
        _dc_func = getattr(dc_cls, "__func__", dc_cls)
        dc_is_override = callable(dc_cls) and _dc_func is not _dc_base_func
        if dc_is_override:
            budgets.phase(f"teardown:doClassCleanups:{cls_to_close.__name__}")
            try:
                _ztest_lifecycle_kind_guard(dc_cls, f"{cls_to_close.__name__}.doClassCleanups")
                _flush_ipc_now()  # the flush barrier before the user override
                _dc_cls_res = dc_cls()
                _ztest_lifecycle_result_guard(_dc_cls_res, f"{cls_to_close.__name__}.doClassCleanups")
                budgets.raise_if_fired(f"teardown:doClassCleanups:{cls_to_close.__name__}")
            except KeyboardInterrupt:
                budgets.absorb_timeout()
                failed += 1
                tb_s = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<doClassCleanups>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                _mark_teardown_interrupt()
            except BaseException as _dcc_exc:
                budgets.absorb_timeout()
                tb_s = traceback.format_exc()
                failed += 1
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<doClassCleanups>", loc=rel_file, error="TeardownError", msg=tb_s, tb=tb_s)
            finally:
                budgets.phase_off()
        else:
            try:
                cls_to_close.tearDown_exceptions = []
            except (AttributeError, TypeError):
                pass
            while cc_stack:
                _cc_func, _cc_args, _cc_kwargs = cc_stack.pop()
                budgets.phase(f"teardown:classCleanup:{cls_to_close.__name__}")
                try:
                    _ztest_lifecycle_kind_guard(_cc_func, f"{cls_to_close.__name__}.addClassCleanup callback")
                    _flush_ipc_now()  # the flush barrier before the user cleanup
                    _cc_res = _cc_func(*_cc_args, **_cc_kwargs)
                    _ztest_lifecycle_result_guard(_cc_res, f"{cls_to_close.__name__}.addClassCleanup callback")
                    budgets.raise_if_fired(f"teardown:classCleanup:{cls_to_close.__name__}")
                except KeyboardInterrupt:
                    budgets.absorb_timeout()
                    failed += 1
                    tb_s = traceback.format_exc()
                    ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<class-cleanup>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                    _mark_teardown_interrupt()
                except _TestTimeoutError as _cc_tte:
                    # A class-cleanup timeout — absorb + record, the stack still runs
                    budgets.absorb_timeout()
                    failed += 1
                    tb_s = traceback.format_exc()
                    ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<class-cleanup>", loc=rel_file, error="ClassCleanupTimeout", msg=str(_cc_tte), tb=tb_s)
                except Exception:
                    budgets.absorb_timeout()
                    try:
                        cls_to_close.tearDown_exceptions.append(sys.exc_info())
                    except Exception:
                        failed += 1
                        tb_s = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<class-cleanup>", loc=rel_file, error="TeardownError", msg=tb_s, tb=tb_s)
                except BaseException as _cc_exc:
                    budgets.absorb_timeout()
                    failed += 1
                    tb_s = traceback.format_exc()
                    ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<class-cleanup>", loc=rel_file, error="TeardownError", msg=tb_s, tb=tb_s)
                finally:
                    budgets.phase_off()
        for exc_rec in getattr(cls_to_close, "tearDown_exceptions", None) or []:
            try:
                exc_type, exc_val, exc_tb = exc_rec
                rec_tb = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
                rec_msg = str(exc_val) if exc_val is not None else str(exc_type)
            except Exception:
                rec_tb = repr(exc_rec)
                rec_msg = repr(exc_rec)
            failed += 1
            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::{cls_to_close.__name__}::<class-cleanup>", loc=rel_file, error="ClassCleanupError", msg=rec_msg, tb=rec_tb)
    return failed


class ZtestResult(unittest.TestResult):
    """ADR-005: the stdlib lifecycle bridge to ZTST; public TestResult API only; timeout dominance — _TestTimeoutError re-raised after recording (see INVARIANTS.md)."""

    def __init__(self, ctx: _FileRunCtx, item: TestItemSpec, outcome: _ItemOutcome) -> None:
        super().__init__()
        self.ctx = ctx
        self.item = item
        self.outcome = outcome
        self.started_monotonic = _REAL_MONOTONIC_NS()
        self.teardown_timed_out = False
        self._teardown_dom_done = False
        # A subtest before stopTest winds down unittest; after it (teardown/doCleanups) — a teardown diagnostic. Invariants: SUBTEST-AGGREGATION-PHASE-AWARE.
        self.sub_failures: list[BaseException] = []
        self.sub_skips: list[str] = []
        self._subtest_installed = False

    def install_subtest_bridge(self, case: unittest.TestCase) -> None:
        """case.subTest → a recording bridge: exceptions are recorded, control ones re-raised; the bridge lives for the whole instance."""

        @contextlib.contextmanager
        def _honest_subtest(msg: Any = "", **params: Any) -> Any:
            try:
                yield
            except (KeyboardInterrupt, SystemExit, _TestTimeoutError, Exit, _ZtestNeverExecutedError):
                raise
            except BaseException as sub_exc:
                # Subtest skip outcomes — into the skip channel, not failures.
                if isinstance(sub_exc, (Skipped, unittest.SkipTest, XFailed)):
                    self.sub_skips.append(str(sub_exc))
                elif self.ctx.budgets.in_teardown_phase():
                    # A subtest caught in the teardown phase
                    # (tearDown/doCleanups/addCleanup) — a teardown diagnostic, not
                    # a primary candidate: xfail conversion is unavailable to it. The phase on the stack
                    # at catch time — the wrapper pops it after tearDown returns.
                    if sub_exc not in self.outcome.teardown_errs:
                        self.outcome.teardown_errs.append(sub_exc)
                else:
                    self.sub_failures.append(sub_exc)

        try:
            case.subTest = _honest_subtest  # type: ignore[method-assign]
            self._subtest_installed = True
        except (AttributeError, TypeError):
            pass

    # -- control-exception leakage -------------------------------------------------
    def _reraise_control(self, exc: BaseException) -> None:
        # _TestTimeoutError does not exit control from the bridge — the stdlib lifecycle continues
        # (tearDown/doCleanups in a live asyncio runner); the timeout is already recorded as an
        # item error. KI/SystemExit/Exit/NeverExecuted still abort run() (global dominance). Invariants: CONTROL-EXCEPTIONS-CANONICAL.
        if isinstance(exc, _ZT_CONTROL_TYPES) and not isinstance(exc, _TestTimeoutError):
            raise exc

    # -- primary outcome sinks ------------------------------------------------------
    def startTest(self, test: Any) -> None:
        super().startTest(test)
        self.ctx.ipc.send_event("TEST_START", nodeid=self.item.nodeid)

    def addError(self, test: Any, err: Any) -> None:
        exc = err[1] if isinstance(err, tuple) else err
        if isinstance(exc, Skipped):
            # shim-Skipped is a BaseException (protection against
            # user `except Exception`); stdlib unittest does not recognize it and
            # routes it into the error channel. Translate it into the bridge's skip
            # channel; timeout dominance
            # inside addSkip is preserved.
            self.addSkip(test, str(exc) or "skipped")
            return
        if exc is not None:
            if exc in self.outcome.teardown_errs:
                # A teardown origin marked by the phase wrapper never
                # becomes primary (the wrapper pops the phase before the add* hook).
                pass
            elif self.ctx.budgets.in_teardown_phase():
                # A teardown origin never becomes
                # primary — xfail conversion is unavailable to it, even if it is the first
                # and only exception of the item.
                if exc is not self.outcome.exc:
                    self.outcome.teardown_errs.append(exc)
            elif self.outcome.exc is None:
                self.outcome.exc = exc
            elif exc is not self.outcome.exc and exc not in self.outcome.teardown_errs:
                # Non-primary lifecycle errors
                self.outcome.teardown_errs.append(exc)
            if isinstance(exc, _TestTimeoutError):
                # Stop the recurring SIGALRM storm (the handler
                # re-arms 0.05 s while _armed) and consume the sticky flag — the timeout
                # is reflected in the record; the post-run raise_if_fired must not fire again.
                self.ctx.budgets.disarm()
                self.ctx.budgets.consume_fired()
                # The pending body task is canceled before the lifecycle continues.
                _cancel_pending_loop_tasks(test, self.ctx.budgets)
                # The fixture resolver's runner is also a source of pending
                # tasks (an async fixture setup timeout inside runner.run): cancel + bounded drain
                # via the bounded-drain machinery; survivors → poison (teardown gets a fresh runner).
                _res = self.ctx.active_resolver
                if _res is not None:
                    _cancel_runner_tasks_nowait(getattr(_res, "async_runner", None))
                    self.ctx.budgets.drain_runner(_res, "timeout_drain:ipc_bridge")
            self._reraise_control(exc)
        super().addError(test, err)

    def addFailure(self, test: Any, err: Any) -> None:
        exc = err[1] if isinstance(err, tuple) else err
        if exc is not None:
            if exc in self.outcome.teardown_errs:
                # See addError.
                pass
            elif self.ctx.budgets.in_teardown_phase():
                # See addError.
                if exc is not self.outcome.exc:
                    self.outcome.teardown_errs.append(exc)
            elif self.outcome.exc is None:
                self.outcome.exc = exc
            elif exc is not self.outcome.exc and exc not in self.outcome.teardown_errs:
                # See addError.
                self.outcome.teardown_errs.append(exc)
            self._reraise_control(exc)
        super().addFailure(test, err)

    def addSkip(self, test: Any, reason: str) -> None:
        if self.ctx.budgets.consume_fired():
            # TIMEOUT dominance over a skip exit — the item is a timeout
            # failure, never a green skip.
            raise self.ctx.budgets.timeout_dominance_exc("skip")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: Any, err: Any) -> None:
        exc = err[1] if isinstance(err, tuple) else err
        if exc is not None:
            self._reraise_control(exc)
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: Any) -> None:
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        # A subtest failure is recorded (primary = failure), a pass — no-op; control exceptions are re-raised. Invariants: SESSION-FINALIZATION, SUBTEST-CONTROL-PROPAGATION.
        if err is not None:
            exc = err[1] if isinstance(err, tuple) else err
            if exc is not None:
                self._reraise_control(exc)
            if isinstance(exc, (Skipped, unittest.SkipTest, XFailed)):
                self.sub_skips.append(str(exc))
            else:
                self.sub_failures.append(exc)
        super().addSubTest(test, subtest, err)

    def addDuration(self, test: Any, elapsed: Any) -> None:
        pass

    def stopTest(self, test: Any) -> None:
        super().stopTest(test)
        if self._teardown_dom_done:
            return
        self._teardown_dom_done = True
        if self.teardown_timed_out:
            # Teardown dominance — the primary verdict is replaced by the timeout failure. Invariants: TEARDOWN-DOMINATES-XFAIL.
            self.outcome.timeout_dominant = True


class _ItemOutcome:
    """The primary verdict + diagnostics of one item: exactly one primary, each teardown error — a separate diagnostic."""

    def __init__(self, item: TestItemSpec) -> None:
        self.item = item
        self.verdict: str | None = None  # "pass" | "fail" | "skip"
        self.reason: str = ""
        self.exc: BaseException | None = None
        # The exception the reported failure was formatted from (the wrapper
        # itself only carries text).
        self.exc_source: BaseException | None = None
        # True when the test body ran to completion without raising; a
        # finalizer failure alone does not turn such an item into a failure.
        self.body_ok: bool | None = None
        self.duration_ms: int = 0
        self.timeout_dominant = False
        self.teardown_errs: list[BaseException] = []
        # Finalizer errors that a matching xfail mark predicts — reported as
        # skipped, never as failures.
        self.expected_teardown: list[BaseException] = []
        self.exited = False
        self.interrupted = False
        # The number of subTests skipped in a passed test's body
        # (pytest-subtests: every skipped subTest = a separate skipped report).
        self.sub_skip_count: int = 0
        self.sub_skip_reasons: list[str] = []


def _xfail_accepts(mark: Mark, exc: BaseException) -> bool:
    """Whether the xfail mark's `raises` predicate covers this exception."""
    if isinstance(exc, _ZT_CONTROL_TYPES):
        return False
    raises_expected = mark.kwargs.get("raises")
    if isinstance(exc, _TeardownErrors) and exc.raw_excs:
        if any(isinstance(sub, _ZT_CONTROL_TYPES) for sub in exc.raw_excs):
            return False
        if len(exc.raw_excs) == 1:
            return raises_expected is None or isinstance(exc.raw_excs[0], raises_expected)
        try:
            eg: BaseException = BaseExceptionGroup("errors while tearing down fixture", exc.raw_excs)
        except Exception:
            return False
        return raises_expected is None or isinstance(eg, raises_expected)
    return raises_expected is None or isinstance(exc, raises_expected)


def _formatted_reports(formatted: str, err: BaseException) -> bool:
    """Whether a formatted traceback block was produced from `err`.

    The block must end with the exception's own last line and name the
    finalizer frame — a different failure with the same message does not
    match.
    """
    tail = "".join(traceback.format_exception_only(type(err), err)).strip()
    if not tail or not formatted.rstrip().endswith(tail):
        return False
    frames = traceback.extract_tb(err.__traceback__)
    if not frames:
        return False
    last = frames[-1]
    return f"line {last.lineno}, in {last.name}" in formatted


def _take_reported_teardown(outcome: "_ItemOutcome") -> list[BaseException]:
    """Remove and return finalizer errors that became the item's reported failure.

    The stdlib sink records a finalizer exception as the item's error while
    the phase wrapper also tags it as a teardown origin. Returning them lets
    the caller tell "the body failed" apart from "only the finalizer failed"
    and keeps one record per failure.
    """
    if not outcome.teardown_errs:
        return []
    primary = outcome.exc
    source = outcome.exc_source
    formatted = getattr(primary, "formatted", None)
    if primary is None and source is None:
        return []
    matched: list[BaseException] = []
    kept: list[BaseException] = []
    for err in outcome.teardown_errs:
        if err is primary or err is source or (formatted and _formatted_reports(formatted, err)):
            matched.append(err)
        else:
            kept.append(err)
    outcome.teardown_errs = kept
    return matched


def _tag_teardown_origin(phase_exc: BaseException, budgets_phase: bool, outcome: "_ItemOutcome | None") -> None:
    # The teardown phase is known only here — the wrapper's finally
    # pops the phase before stdlib testPartExecutor calls the result's add* hook;
    # hence the teardown origin is marked in the wrapper when the exception escapes.
    if budgets_phase and outcome is not None and not isinstance(phase_exc, _ZT_CONTROL_TYPES):
        if phase_exc is not outcome.exc and phase_exc not in outcome.teardown_errs:
            outcome.teardown_errs.append(phase_exc)


def _wrap_phase(
    case: unittest.TestCase,
    budgets: _PhaseBudgets,
    label: str,
    attr: str,
    *,
    outcome: "_ItemOutcome | None" = None,
    kind_guard: bool = True,
    body: bool = False,
    budgets_phase: bool = False) -> Any:
    """ADR-005: a wrapper over the case's public method — per-phase budgets, the sticky flag, the sentinel, result guards; unwrapped after run() (see INVARIANTS.md)."""
    target = getattr(case, attr, None)
    if not callable(target):
        return None
    orig_own = case.__dict__.get(attr, _MISSING) if hasattr(case, "__dict__") else _MISSING
    is_async_tc = hasattr(case, "_setupAsyncioRunner")

    def _finalizer() -> None:
        try:
            if orig_own is _MISSING:
                delattr(case, attr)
            else:
                setattr(case, attr, orig_own)
        except (AttributeError, TypeError):
            pass

    if attr in ("asyncSetUp", "asyncTearDown") and is_async_tc:
        # Async phases — a coroutine wrapper (budget + await + boundary-check + result-guard); CPython's dispatcher drives it unchanged. Invariants: ADR-005-2.
        async def _ztest_wrapped() -> Any:
            budgets.heartbeat(f"unittest:{label}")
            if body:
                case._ztest_body_started = True  # type: ignore[attr-defined]
            if attr in ("tearDown", "asyncTearDown"):
                case._ztest_teardown_ran = True  # type: ignore[attr-defined]
            if budgets_phase:
                budgets.phase(f"unittest:{label}")
            budgets.arm(budgets.per_test_timeout)
            try:
                if kind_guard:
                    _ztest_lifecycle_kind_guard(target, f"unittest {label}", allow_coroutine=True)
                # The flush barrier: this boundary's heartbeat/phase frames leave
                # before the user phase (no pending frames across user code).
                _flush_ipc_now()
                res = await target()
                budgets.raise_if_fired(f"unittest {label}")
                _ztest_lifecycle_result_guard(res, f"unittest {label}")
                if body and outcome is not None:
                    outcome.body_ok = True
                return res
            except _TestTimeoutError as _tte:
                if attr == "asyncTearDown" and outcome is not None:
                    # An asyncTearDown timeout is pinned at the boundary, run()
                    # continues with a live runner → async cleanups still run.
                    budgets.disarm()
                    budgets.consume_fired()
                    outcome.teardown_errs.append(_tte)
                    return None
                _tag_teardown_origin(_tte, budgets_phase, outcome)
                raise
            except BaseException as phase_exc:
                _tag_teardown_origin(phase_exc, budgets_phase, outcome)
                raise
            finally:
                if budgets_phase:
                    budgets.phase_off()

        wrapper = _ztest_wrapped
    else:
        # Sync phases — a plain callable; a coroutine method on an async TC stays a coroutine (the same dispatch branch), on a plain TC — an error. Invariants: ADR-005-2, RETURN-GUARDS-COROUTINE-AWARE.
        _target_is_coro = inspect.iscoroutinefunction(target) and is_async_tc

        if _target_is_coro:

            async def _ztest_wrapped_coro() -> Any:
                budgets.heartbeat(f"unittest:{label}")
                if body:
                    case._ztest_body_started = True  # type: ignore[attr-defined]
                if attr in ("tearDown", "asyncTearDown"):
                    case._ztest_teardown_ran = True  # type: ignore[attr-defined]
                budgets.arm(budgets.per_test_timeout)
                try:
                    if body and not is_async_tc:
                        raise _ZtestNeverExecutedError(
                            f"async unittest test method {getattr(case, '_testMethodName', '?')!r} on a plain "
                            f"unittest.TestCase never executes (the coroutine object was never created): "
                            f"only unittest.IsolatedAsyncioTestCase can await it "
                            f"(an error)"
                        )
                    _flush_ipc_now()  # the flush barrier before the user coroutine
                    res = await target()
                    if body:
                        _guard_body_return(res, case)
                        if outcome is not None:
                            outcome.body_ok = True
                    budgets.raise_if_fired(f"unittest {label}")
                    return res
                except _TestTimeoutError as _tte:
                    if body and outcome is not None:
                        # The async body pins the timeout at the boundary and does not
                        # propagate control — run() continues with a live runner:
                        # asyncTearDown and async cleanups run normally; the verdict — fail.
                        # The user's sibling tasks are canceled too.
                        budgets.disarm()
                        budgets.consume_fired()
                        _cancel_pending_loop_tasks(case, budgets)
                        outcome.verdict = "fail"
                        outcome.exc = _tte
                        return None
                    _tag_teardown_origin(_tte, budgets_phase, outcome)
                    raise
                except BaseException as phase_exc:
                    _tag_teardown_origin(phase_exc, budgets_phase, outcome)
                    raise
                finally:
                    if budgets_phase:
                        budgets.phase_off()

            wrapper = _ztest_wrapped_coro
        else:

            def _ztest_wrapped_sync() -> Any:
                budgets.heartbeat(f"unittest:{label}")
                if body:
                    case._ztest_body_started = True  # type: ignore[attr-defined]
                if attr in ("tearDown", "asyncTearDown"):
                    case._ztest_teardown_ran = True  # type: ignore[attr-defined]
                if budgets_phase:
                    budgets.phase(f"unittest:{label}")
                budgets.arm(budgets.per_test_timeout)
                try:
                    if kind_guard:
                        _ztest_lifecycle_kind_guard(target, f"unittest {label}")
                    if body and not is_async_tc and inspect.iscoroutinefunction(target):
                        raise _ZtestNeverExecutedError(
                            f"async unittest test method {getattr(case, '_testMethodName', '?')!r} on a plain "
                            f"unittest.TestCase never executes (the coroutine object was never created): "
                            f"only unittest.IsolatedAsyncioTestCase can await it (an error)"
                        )
                    _flush_ipc_now()  # the flush barrier before the user phase
                    res = target()
                    if body:
                        _guard_body_return(res, case)
                        if outcome is not None:
                            outcome.body_ok = True
                    if budgets_phase:
                        # doCleanups/tearDown own their budget; the body does not.
                        budgets.phase_off()
                    budgets.raise_if_fired(f"unittest {label}")
                    return res
                except _TestTimeoutError as _tte:
                    if body and outcome is not None:
                        # Uniform with the async body: the sync body pins the timeout
                        # at the boundary — run() reaches tearDown/doCleanups normally; the verdict — fail.
                        budgets.disarm()
                        budgets.consume_fired()
                        outcome.verdict = "fail"
                        outcome.exc = _tte
                        return None
                    _tag_teardown_origin(_tte, budgets_phase, outcome)
                    raise
                except BaseException as phase_exc:
                    _tag_teardown_origin(phase_exc, budgets_phase, outcome)
                    raise
                finally:
                    if budgets_phase:
                        budgets.phase_off()

            wrapper = _ztest_wrapped_sync

    try:
        setattr(case, attr, wrapper)
    except (AttributeError, TypeError):
        return None
    # The __unittest_skip__/__unittest_expecting_failure__ markers are copied onto the wrapper — run() reads them from it. Invariants: ADR-005-2.
    for _mk in ("__unittest_expecting_failure__", "__unittest_skip__", "__unittest_skip_why__"):
        _mk_val = getattr(target, _mk, None)
        if _mk_val is not None:
            try:
                setattr(wrapper, _mk, _mk_val)
            except (AttributeError, TypeError):
                pass
    try:
        wrapper.__name__ = getattr(target, "__name__", attr)
        wrapper.__qualname__ = getattr(target, "__qualname__", attr)
    except (AttributeError, TypeError):
        pass
    return _finalizer


_MISSING = object()


class _FormattedFailure(Exception):
    """Formatted-record carrier: public TestResult lists hold strings, the real exception rides in the err tuple."""

    def __init__(self, formatted: str) -> None:
        super().__init__(formatted)
        self.formatted = formatted


def _guard_body_return(res: Any, case: unittest.TestCase) -> None:
    """A decorated test method returning a generator/coroutine: the body never executed — closed, then rejected."""
    if isinstance(res, (types.GeneratorType, types.AsyncGeneratorType)) or inspect.isawaitable(res):
        _ztest_close_unexecuted(res)
        raise _ZtestNeverExecutedError(
            f"unittest test method {getattr(case, '_testMethodName', '<test>')} returned a "
            f"{type(res).__name__}: generator/asyncgen/coroutine test bodies never "
            f"execute (rejected)"
        )


class _FunctionTestCase(unittest.TestCase):
    """ADR-005: an adapter of pytest functions to TestCase — setUp drives the pytest lifecycle, tearDown — hooks + finalizers via addCleanup LIFO; state in resolver/ctx (see INVARIANTS.md)."""

    def __init__(self, ctx: _FileRunCtx, item: TestItemSpec, resolver: FixtureResolver, call_kwargs: dict[str, Any], instance: Any) -> None:
        super().__init__("runTest")
        self.ctx = ctx
        self.item = item
        self.resolver = resolver
        self._call_kwargs = call_kwargs
        self._fn_instance = instance

    def setUp(self) -> None:
        # Function-scope teardown ownership is registered before any
        # fixture/hook execution — stdlib doCleanups runs even after a failed
        # setUp, so the finalizers of already-captured fixtures are not lost
        # (partial finalization). The registration order gives LIFO execution:
        # xunit teardown → conftest hooks → fixture finalizers (pytest: fixture
        # finalizers last). Invariants: ADR-005-4.
        self.addCleanup(_resolver_teardown_unit, self.ctx, self.resolver)
        # Conftest teardown hooks are registered immediately (they do not depend on
        # fixture resolution) — they run even when setup hooks fail.
        for cf_mod in self.ctx.conftest_mods:
            self.addCleanup(functools.partial(_conf_runtest_teardown_unit, self.ctx, cf_mod))
        # pytest_runtest_setup hooks over the conftest chain (plain callable
        # contract enforced — generator/coroutine hooks are rejected).
        for cf_mod in self.ctx.conftest_mods:
            try:
                _hook_rts = _invocable_conf_hook(cf_mod, "pytest_runtest_setup")
            except ZtestUnsupportedApiError as hook_e:
                raise _ZtestNeverExecutedError(str(hook_e)) from hook_e
            if _hook_rts is not None:
                _check_conf_hook_result("pytest_runtest_setup", _call_runtest_hook(_hook_rts, self.item.node, hook_name="pytest_runtest_setup"))
                self.ctx.budgets.raise_if_fired("conftest pytest_runtest_setup")
        # usefixtures targets are real fixture dependencies.
        for m in self.item.node.own_markers:
            if m.name == "usefixtures":
                for fn_name in m.args:
                    self.resolver.get_fixture_value(fn_name)
        self.ctx.budgets.raise_if_fired("usefixtures resolution")
        # Autouse fixtures execute before test fixture resolution (caplog order).
        self.resolver.run_autouse_fixtures()
        self.ctx.budgets.raise_if_fired("autouse fixtures")
        # xunit setup_method / setup_function.
        if self._fn_instance is not None:
            sm_fn = getattr(self._fn_instance, "setup_method", None)
            if callable(sm_fn):
                _ztest_lifecycle_kind_guard(sm_fn, "setup_method")
                _sm_fn_res = _call_arity_aware(sm_fn, self.item.func)
                _ztest_lifecycle_result_guard(_sm_fn_res, "setup_method")
                self.ctx.budgets.raise_if_fired("setup_method")
            # Teardown-method ownership — immediately after a
            # successful setup_method, before fixture resolution: a successful setup must
            # get its paired teardown even when the fixtures fail afterwards.
            # A failed setup does not trigger its own teardown (the exception never arrived).
            # A teardown-only hook (without setup_method) is legal in pytest —
            # registered immediately, unconditionally.
            tm_fn = getattr(self._fn_instance, "teardown_method", None)
            if callable(tm_fn):
                self.addCleanup(
                    lambda fn_tm=tm_fn, fn_t=self.item.func: _ztest_xunit_teardown_finalizer(fn_tm, fn_t, "teardown_method")
                )
        else:
            sf_fn = getattr(self.item.module, "setup_function", None)
            if callable(sf_fn):
                _ztest_lifecycle_kind_guard(sf_fn, "setup_function")
                _sf_fn_res = _call_arity_aware(sf_fn, self.item.func)
                _ztest_lifecycle_result_guard(_sf_fn_res, "setup_function")
                self.ctx.budgets.raise_if_fired("setup_function")
            # See the setup_method branch above (including the teardown-only case).
            tf_fn = getattr(self.item.module, "teardown_function", None)
            if callable(tf_fn):
                self.addCleanup(
                    lambda fn_tf=tf_fn, fn_t=self.item.func: _ztest_xunit_teardown_finalizer(fn_tf, fn_t, "teardown_function")
                )
        # Test fixture resolution (fills call kwargs lazily — stored on self).
        sig = inspect.signature(self.item.func)
        mock_patch_skip = 0
        try:
            _patchings = getattr(self.item.func, "patchings", ()) or ()
        except Exception:
            _patchings = ()  # a proxy with an exploding __getattr__
        for p_obj in _patchings:
            if getattr(p_obj, "attribute_name", None) is None and getattr(p_obj, "new", unittest.mock.DEFAULT) is unittest.mock.DEFAULT:
                mock_patch_skip += 1
        call_kwargs: dict[str, Any] = dict(self._call_kwargs)
        # Direct parameters absent from the test's signature (the axis
        # parameterized a fixture-name dependency) are not passed into the test call — pytest
        # passes only function argnames; the full dict stays
        # with the resolver via direct_params.
        _sig_names = {
            pname
            for pname, p in sig.parameters.items()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        call_kwargs = {k: v for k, v in call_kwargs.items() if k in _sig_names}
        for pname, p in sig.parameters.items():
            if pname == "self" and self._fn_instance is not None:
                continue
            if mock_patch_skip > 0:
                mock_patch_skip -= 1
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if pname in call_kwargs:
                continue
            try:
                call_kwargs[pname] = self.resolver.get_fixture_value(pname)
            except LookupError:
                if p.default is not inspect.Parameter.empty:
                    call_kwargs[pname] = p.default
                else:
                    raise
        self.ctx.budgets.raise_if_fired("test fixture resolution")
        self._resolved_kwargs = call_kwargs
        # Teardown is registered LIFO via stdlib cleanups — conftest hooks (right after the
        # resolver owner) + xunit (right after their own setup), each budgeted; the
        # function-scope owner was registered first in setUp. Invariants: ADR-005-1.

    def runTest(self) -> None:
        bound_fn = getattr(self._fn_instance, self.item.func.__name__) if self._fn_instance is not None else self.item.func
        call_kwargs = self._resolved_kwargs
        if inspect.iscoroutinefunction(bound_fn):
            # A coroutine body — on the resolver's runner; a swallowed SIGALRM is caught by the sticky-flag boundary. Invariants: ASYNC-FINALIZERS-ON-RESOLVER-RUNNER.
            runner = self.resolver._ensure_runner()
            try:
                _fn_res = runner.run(bound_fn(**call_kwargs))
            finally:
                if self.ctx.budgets._fired:
                    self.ctx.budgets.drain_runner(self.resolver, "timeout_drain:async_body")
            self.ctx.budgets.raise_if_fired("async test body")
            _guard_body_return(_fn_res, self)
        else:
            _fn_res = bound_fn(**call_kwargs)
            self.ctx.budgets.raise_if_fired("test body")
            _guard_body_return(_fn_res, self)


def _resolver_teardown_unit(ctx: _FileRunCtx, resolver: FixtureResolver) -> None:
    # Function-scope teardown is budgeted per finalizer in teardown_scope; the resolver is reset, scopes live in the registry. Invariants: FINALIZER-PER-UNIT-BUDGET.
    errs = resolver.teardown_scope("function")
    if errs:
        raise _TeardownErrors(errs, getattr(resolver, "last_teardown_excs", None))


class _TeardownErrors(Exception):
    def __init__(self, errs: list[str], raw_excs: list[BaseException] | None = None) -> None:
        super().__init__("\n".join(errs))
        self.errs = errs
        self.raw_excs: list[BaseException] = list(raw_excs or [])


def _conf_runtest_teardown_unit(ctx: _FileRunCtx, cf_mod: types.ModuleType) -> None:
    # One conftest's pytest_runtest_teardown hook as its own budgeted unit.
    budgets = ctx.budgets
    budgets.phase("teardown:conftest_runtest_teardown")
    try:
        try:
            _hook_rtt = _invocable_conf_hook(cf_mod, "pytest_runtest_teardown")
        except ZtestUnsupportedApiError as hook_e:
            raise _TeardownErrors([f"ZtestNeverExecutedError (teardown hook): {hook_e}"]) from None
        if _hook_rtt is not None:
            try:
                _check_conf_hook_result("pytest_runtest_teardown", _call_runtest_hook(_hook_rtt, ctx_item_node(cf_mod), nextitem=_ZT_NEXT_NODE, hook_name="pytest_runtest_teardown"))
            except ZtestUnsupportedApiError:
                raise _TeardownErrors([traceback.format_exc()]) from None
            budgets.raise_if_fired("teardown:conftest_runtest_teardown")
    finally:
        budgets.phase_off()


def ctx_item_node(cf_mod: types.ModuleType) -> Any:
    # Lazy node provider: the hook needs the current item's node; stored on
    # the ctx by the item runner before the cleanups run.
    return _ZT_CURRENT_NODE


_ZT_CURRENT_NODE: NodeStub | None = None
# The next item for the nextitem injection of teardown hooks.
_ZT_NEXT_NODE: Any = None


def _run_one_item(
    ctx: _FileRunCtx,
    item: TestItemSpec,
    passed: int,
    failed: int,
    skipped: int,
    xfail_mark: Mark | None,
    capture_output: bool) -> tuple[int, int, int, bool, bool]:
    """Run one item through the stdlib lifecycle and account: (passed, failed, skipped, exited, interrupted), exactly one primary."""
    global _ZT_CURRENT_NODE
    ipc = ctx.ipc
    rel_file = ctx.rel_file
    budgets = ctx.budgets
    t0_ns = _REAL_MONOTONIC_NS()
    outcome = _ItemOutcome(item)
    budgets.begin_item()  # the sticky flag is reset only here
    cap_out = _CapturedTextIO(fd=1)
    cap_err = _CapturedTextIO(fd=2)
    orig_out, orig_err = sys.stdout, sys.stderr
    orig_stdin = sys.stdin
    resolver: FixtureResolver | None = None
    exited = False
    interrupted = False
    # Counter snapshot — the accounting below derives the primary outcome that
    # this item already recorded (no double counts).
    _oc_p0, _oc_f0, _oc_s0 = passed, failed, skipped
    # A "clean PASS" = the only frame the item emitted is TEST_PASS. Only then
    # TEST_END is dropped; on any diagnostic path (FAIL/SKIP/teardown-error/
    # xfail/strict-XPASS/subtest-skip/interruption) it stays — diagnostic
    # completeness beats the frame saving.
    _clean_pass = False

    try:
        _fd_capture = None
        if capture_output:
            sys.stdout, sys.stderr = cap_out, cap_err
            sys.stdin = _DontReadFromInput()  # DontReadFromInput parity
            # fd-level capture — subprocess streams (inherit fd 1/2, bypassing
            # sys-capture) do not splice into the supervisor's verbose stream
            # (pytest capfd parity). IPC goes over a dedicated fd and is not
            # touched. Mid-item reads of the fd buffer via capsys are not
            # supported (a documented approximation).
            try:
                _flush_user_stdio()
                _fo = tempfile.TemporaryFile(mode="w+b")
                _fe = tempfile.TemporaryFile(mode="w+b")
                _s1 = os.dup(1)
                _s2 = os.dup(2)
                try:
                    os.dup2(_fo.fileno(), 1)
                    os.dup2(_fe.fileno(), 2)
                except OSError:
                    # A partial failure — restore the already redirected fds first,
                    # otherwise closing _fo/_fe leaves fd1/fd2 pointing at a
                    # closed file.
                    try:
                        os.dup2(_s1, 1)
                    except OSError:
                        pass
                    try:
                        os.dup2(_s2, 2)
                    except OSError:
                        pass
                    os.close(_s1)
                    os.close(_s2)
                    _fo.close()
                    _fe.close()
                    raise
                _fd_capture = (_fo, _fe, _s1, _s2)
            except OSError:
                _fd_capture = None
        # ini filterwarnings for the duration of the item (pytest per-item
        # behavior; the last ini line wins — apply in direct order,
        # warnings.filterwarnings inserts the filter at the list head).
        #
        # catch_warnings is entered always — pytest (catch_warnings_for_item)
        # with an empty -W does per-item warnings.simplefilter("always")
        # (PEP 565): a user catch_warnings(record=True) inside the test must
        # record DeprecationWarning even from non-__main__ modules. Without
        # "always" CPython's default filter silences those (a divergence).
        _warn_filters = getattr(ctx, "warn_filters", None)
        # Mark-level filterwarnings: pytest warnings.py does
        # pytest warnings.py: filterwarnings(*parse_warning_filter(arg)),
        # pytest order — from the nearest mark outward (prepend ⇒ outer levels
        # are stronger: the module mark applies last).
        _mark_fw = [m for m in reversed(list(getattr(item, "markers", None) or getattr(item, "marks", []) or [])) if getattr(m, "name", None) == "filterwarnings"]
        _wcm = warnings.catch_warnings(record=True)
        _wcm.__enter__()
        if not sys.warnoptions:
            # pytest parity: without an explicit user -W — "always" for the item
            warnings.simplefilter("always")
        for _act, _wmsg, _wcat, _wmod, _wlno in _warn_filters or []:
            warnings.filterwarnings(_act, message=_wmsg, category=_wcat, module=_wmod, lineno=_wlno)
        for _m in _mark_fw:
            # pytest warnings.py: warnings.filterwarnings(*parse_warning_filter(arg))
            # — only positional string specs; pytest ignores the mark's kwargs
            # (extra keyword arguments such as category= are ignored by pytest).
            for _a in _m.args:
                if isinstance(_a, str):
                    for _act, _wmsg, _wcat, _wmod, _wlno in _parse_warn_filters([_a]):
                        warnings.filterwarnings(_act, message=_wmsg, category=_wcat, module=_wmod, lineno=_wlno)
                else:
                    warnings.filterwarnings(_a)
        try:
            # Class-level setup before test instance/fixture resolution.
            if item.cls is not None:
                if item.cls in ctx.failed_classes:
                    raise ctx.failed_classes[item.cls]
                if item.cls not in ctx.setup_classes:
                    ctx.attempted_classes.add(item.cls)
                    su_cls = getattr(item.cls, "setUpClass", None) if item.is_unittest else getattr(item.cls, "setup_class", None)
                    if callable(su_cls):
                        try:
                            _ztest_lifecycle_kind_guard(su_cls, f"{item.cls.__name__}.setUpClass/setup_class")
                            # Class setup — under an armed phase budget before the
                            # user code runs; a hung setUpClass = a timeout-fail,
                            # not a silent leak past --timeout.
                            budgets.phase("setup_class")
                            try:
                                # pytest-style setup_class(cls) — a plain function without
                                # @classmethod (not bound via the class); the arity-aware call
                                # passes cls by signature. A bound classmethod (unittest) gets
                                # a 0-ary call — a signature without cls.
                                _su_cls_res = _call_arity_aware(su_cls, item.cls)
                            finally:
                                budgets.phase_off()
                            _ztest_lifecycle_result_guard(_su_cls_res, f"{item.cls.__name__}.setUpClass/setup_class")
                            budgets.raise_if_fired("unittest setUpClass" if item.is_unittest else "setup_class")
                        except BaseException as cls_exc:
                            _cls_flag = budgets.consume_fired()
                            if _cls_flag and not isinstance(cls_exc, _ZT_CONTROL_TYPES):
                                cls_exc = budgets.timeout_dominance_exc("setup_class")
                            ctx.failed_classes[item.cls] = cls_exc
                            budgets.disarm()
                            raise cls_exc
                    ctx.setup_classes.add(item.cls)

            layers = list(ctx.fixture_layers)
            if item.cls is not None:
                _cls_fx = ctx.cls_fixture_cache.get(item.cls)
                if _cls_fx is None:
                    _cls_fx = _extract_fixtures_from_obj(item.cls)
                    ctx.cls_fixture_cache[item.cls] = _cls_fx
                layers.append(_cls_fx)

            # For unittest items the TestCase instance is created before the
            # resolver — autouse/method fixtures of the class get self (pytest
            # binds method fixtures to the same instance as the test).
            instance = None
            ut_case = None
            if item.cls is not None:
                if item.is_unittest:
                    # unittest resolves the method by the class attribute name
                    # (item.name), not by func.__name__ — metaclass factories and
                    # decorators without functools.wraps give
                    # __name__ "test"/"wrapper" → ValueError: no such test method.
                    ut_case = item.cls(item.name.split("[")[0])
                    instance = ut_case
                else:
                    instance = item.cls()

            resolver = FixtureResolver(
                layers=layers,
                node=item.node,
                tmp_factory=ctx.tmp_factory,
                async_runner=None,
                indirect_params=item.indirect_params,
                instance=instance,
                direct_params=item.call_params)
            if ctx.prev_resolver is not None:
                resolver.share_higher_scopes(ctx.prev_resolver)
            ctx.prev_resolver = resolver
            ctx.active_resolver = resolver
            _ZT_CURRENT_NODE = item.node

            zr = ZtestResult(ctx, item, outcome)
            if item.is_unittest and item.cls is not None:
                case = ut_case
            else:
                case = _FunctionTestCase(ctx, item, resolver, item.call_params, instance)

            # Per-test budget: armed inside the try (bills the alarm to this item).
            # All fixture preparation of unittest items runs under the budget
            # (a hung autouse/usefixtures = a timeout-fail, not an hour of silence).
            if budgets.per_test_timeout > 0:
                budgets.arm(budgets.per_test_timeout)

            if item.is_unittest:
                # Conftest setup hooks and autouse fixtures run for unittest items
                # too (pytest parity: the conftest preamble is shared, so a
                # unittest item sees the same setup hooks as a function item).
                # Function-scope teardown ownership is registered before any fixture
                # runs — stdlib doCleanups works even after a failed setUp; exactly-once
                # = the pop semantics of _cleanups + teardown_scope. Invariants: ADR-005-4.
                case.addCleanup(_resolver_teardown_unit, ctx, resolver)
                for cf_mod in ctx.conftest_mods:
                    case.addCleanup(functools.partial(_conf_runtest_teardown_unit, ctx, cf_mod))
                try:
                    for cf_mod in ctx.conftest_mods:
                        try:
                            _hook_rts = _invocable_conf_hook(cf_mod, "pytest_runtest_setup")
                        except ZtestUnsupportedApiError as hook_e:
                            raise _ZtestNeverExecutedError(str(hook_e)) from hook_e
                        if _hook_rts is not None:
                            _check_conf_hook_result("pytest_runtest_setup", _call_runtest_hook(_hook_rts, item.node, hook_name="pytest_runtest_setup"))
                            budgets.raise_if_fired("conftest pytest_runtest_setup")
                    # Autouse fixtures execute before usefixtures resolution (pytest caplog order).
                    resolver.run_autouse_fixtures()
                    budgets.raise_if_fired("unittest autouse fixtures")
                    for m in item.node.own_markers:
                        if m.name == "usefixtures":
                            for fn_name in m.args:
                                resolver.get_fixture_value(fn_name)
                    budgets.raise_if_fired("unittest usefixtures resolution")
                except BaseException:
                    # An async fixture timeout during preparation leaves a pending task
                    # on the resolver's runner — cancel and drain it before the manual
                    # teardown drain, otherwise the fixture code resumes in teardown.
                    _cancel_runner_tasks_nowait(getattr(resolver, "async_runner", None))
                    budgets.drain_runner(resolver, "timeout_drain:prep_handler")
                    # Preparation failed before run() — the stdlib cleanup layer is
                    # unreachable (run() created no _outcome); the finalizers are
                    # drained manually in LIFO registration order (conftest teardown →
                    # function-scope). Exactly-once: after the drain teardown_scope is
                    # empty; the addCleanup records die together with the case.
                    # Drain errors — into teardown diagnostics; the primary exception dominates.
                    for cf_mod in reversed(ctx.conftest_mods):
                        try:
                            _conf_runtest_teardown_unit(ctx, cf_mod)
                        except BaseException as _td_e:
                            if not isinstance(_td_e, _ZT_CONTROL_TYPES):
                                outcome.teardown_errs.append(_td_e)
                    try:
                        _resolver_teardown_unit(ctx, resolver)
                    except BaseException as _td_e:
                        if not isinstance(_td_e, _ZT_CONTROL_TYPES):
                            outcome.teardown_errs.append(_td_e)
                    raise

            _install_phase_wraps(case, budgets, zr)

            _ctrl_exc: BaseException | None = None
            try:
                # setUp/body/tearDown/doCleanups/xfail/skip — the stock TestCase.run; capture outside run(). Invariants: ADR-005-1.
                case.run(zr)
            except _ZT_CONTROL_TYPES as _ctl:
                _ctrl_exc = _ctl
                raise
            finally:
                # A control exception of the body cut run() short before tearDown —
                # the phase must run (through the wrapper: heartbeat+budget) before
                # the doCleanups safety net. Only if the body started (a setUp failure
                # calls no tearDown — stdlib) and tearDown did not run. For
                # IsolatedAsyncioTestCase the runner is already torn by run() → a sync tearDown fallback (best effort, asyncTearDown is lost).
                if (
                    _ctrl_exc is not None
                    and getattr(case, "_ztest_body_started", False)
                    and not getattr(case, "_ztest_teardown_ran", False)
                ):
                    try:
                        case._callTearDown()
                    except (AttributeError, AssertionError):
                        try:
                            case.tearDown()
                        except BaseException as _td2:
                            if not isinstance(_td2, _ZT_CONTROL_TYPES):
                                outcome.teardown_errs.append(_td2)
                    except BaseException as _td_exc:
                        if not isinstance(_td_exc, _ZT_CONTROL_TYPES):
                            outcome.teardown_errs.append(_td_exc)
                # Safety net: cleanups skipped due to a control exception (timeout/KI/EscExit — then
                # run() never reaches its own doCleanups) or an early return on the skip path run here
                # — function-scope finalizers are lost on no path. Exactly-once: the pop semantics of
                # stdlib _cleanups + the idempotent teardown_scope; on the normal path the list is
                # already empty (a no-op). Errors — into teardown diagnostics. Invariants: ADR-005-4.
                try:
                    # After a control exception run() already reset case._outcome —
                    # an outcome-less doCleanups drops the whole rest of the list at the
                    # first cleanup failure (stdlib reporting requires a result). Restore
                    # a valid outcome with result=zr: the stock stdlib cycle (LIFO,
                    # per-callback testPartExecutor) continues after a single callback
                    # fails; the errors go to zr.addError → the teardown bridge.
                    if getattr(case, "_outcome", None) is None:
                        from unittest.case import _Outcome as _UOutcome
                        case._outcome = _UOutcome(result=zr)
                    try:
                        case.doCleanups()
                    finally:
                        case._outcome = None
                except BaseException as _cleanup_exc:
                    if not isinstance(_cleanup_exc, _ZT_CONTROL_TYPES):
                        outcome.teardown_errs.append(_cleanup_exc)
                _remove_phase_wraps(case)

            # Body boundary: a swallowed SIGALRM must not leak past run().
            budgets.raise_if_fired("unittest lifecycle (post-run)")

            # Non-skip records dominate; teardown-phase records — diagnostics (the bridge lives for the whole instance). Invariants: SUBTEST-AGGREGATION-PHASE-AWARE.
            if zr.sub_failures:
                if outcome.verdict is None and not (zr.failures or zr.errors):
                    exc = zr.sub_failures[0]
                    # Native @expectedFailure + subTest — the bridge suppresses the subtest
                    # exception (stdlib sees a body "success" and writes unexpectedSuccess); a
                    # subtest failure in the body of an expectedFailure method is an EXPECTED
                    # failure, not a fail. Invariants: ADR-005-11.
                    _tm_fn = getattr(case, getattr(case, "_testMethodName", ""), None)
                    if getattr(_tm_fn, "__unittest_expecting_failure__", False) or getattr(case, "__unittest_expecting_failure__", False):
                        outcome.verdict = "skip"
                        outcome.reason = f"expected failure: {exc}"
                        outcome.exc = exc
                        # All sub-failures of an expectedFailure method's body are
                        # expected; do not let them become teardown diagnostics (that would convert a skip into a fail).
                        zr.sub_failures.clear()
                    else:
                        outcome.verdict = "fail"
                        outcome.exc = exc
            for exc in zr.sub_failures:
                if exc not in (outcome.exc,):
                    outcome.teardown_errs.append(exc)

            # The verdict from the result's public lists; the real exceptions — from the add* hooks' err-tuples; _Outcome never consulted for the verdict (its only use: restoration for the safety-net doCleanups). Invariants: ADR-005-3.
            if outcome.verdict is None:
                if zr.failures or zr.errors:
                    outcome.verdict = "fail"
                    if outcome.exc is None:
                        _reported_exc = (zr.failures or zr.errors)[0][1]
                        outcome.exc = _FormattedFailure(_reported_exc)
                        # The source exception identifies the phase: the same
                        # object is what the phase wrapper tagged as a
                        # teardown origin.
                        outcome.exc_source = _reported_exc
                elif zr.expectedFailures:
                    # expectedFailure + failing body == xfail (skipped report).
                    outcome.verdict = "skip"
                    outcome.reason = "expected failure"
                    _exp_msg = zr.expectedFailures[0][1].rstrip().rsplit("\n", 1)[-1]
                    outcome.reason = f"xfail: {_exp_msg}"
                elif zr.unexpectedSuccesses:
                    outcome.verdict = "fail"
                    outcome.reason = "expected failure, but the test passed"
                elif zr.skipped:
                    outcome.verdict = "skip"
                    outcome.reason = str(zr.skipped[0][1])
                else:
                    outcome.verdict = "pass"
            # A finalizer exception that is already the reported failure must
            # not produce a second record for the same item; when the body
            # completed without raising, that finalizer failure is the only
            # failure of the item.
            _reported_td = _take_reported_teardown(outcome)
            if (
                _reported_td
                and outcome.body_ok
                and outcome.verdict == "fail"
                and not isinstance(outcome.exc, _ZT_CONTROL_TYPES)
            ):
                outcome.verdict = "pass"
                outcome.exc = None
                outcome.exc_source = None
                outcome.reason = ""
                outcome.teardown_errs = _reported_td + outcome.teardown_errs
            # An xfail mark covers every phase of the item: under a matching
            # mark a failing finalizer is an expected failure, not a failure
            # (pytest converts the teardown report the same way it converts
            # the call report). The expected ones leave the diagnostics list
            # so they neither dominate the verdict nor repeat as failures.
            if xfail_mark is not None and outcome.teardown_errs:
                _expected_td: list[BaseException] = []
                _unexpected_td: list[BaseException] = []
                for _td_e in outcome.teardown_errs:
                    if _xfail_accepts(xfail_mark, _td_e):
                        _expected_td.append(_td_e)
                    else:
                        _unexpected_td.append(_td_e)
                outcome.teardown_errs = _unexpected_td
                outcome.expected_teardown = _expected_td
            # subTest skips of a passed body — into the skipped counter
            # (pytest-subtests reports each as a separate skipped report).
            if outcome.verdict == "pass" and zr.sub_skips:
                outcome.sub_skip_count = len(zr.sub_skips)
                outcome.sub_skip_reasons = list(zr.sub_skips)

            # xfail classification for a FAILED body (expectedFailure is
            # unittest-native and already accounted above).
            # xfail is not applied when there are independent teardown errors —
            # an expected body failure must not mask an unexpected finalization
            # failure (an exit-code lie).
            if outcome.verdict == "fail" and xfail_mark is not None and outcome.exc is not None and not outcome.teardown_errs:
                if not isinstance(outcome.exc, _ZT_CONTROL_TYPES):
                    raises_expected = xfail_mark.kwargs.get("raises")
                    if raises_expected is None or isinstance(outcome.exc, raises_expected):
                        outcome.verdict = "skip"
                        outcome.reason = f"xfail: {outcome.exc}"

            # Timeout dominance recorded by stopTest (teardown phase).
            if outcome.timeout_dominant:
                outcome.verdict = "fail"
                outcome.exc = budgets.timeout_dominance_exc("teardown")
        finally:
            if _wcm is not None:
                _wcm.__exit__(None, None, None)
            # fd-restore before sys-restore; splicing the fd buffer into capture.
            # Flush sys.__stdout__/sys.__stderr__ while fd 1/2 still point at
            # _fo/_fe so bytes written through a cached sys.stdout.buffer (e.g.
            # cached stdout buffer references) land in this item's capture instead of
            # bleeding into the next test's capfd or the supervisor stream.
            if _fd_capture is not None:
                _flush_user_stdio()
                _fo, _fe, _s1, _s2 = _fd_capture
                try:
                    os.dup2(_s1, 1)
                    os.dup2(_s2, 2)
                finally:
                    os.close(_s1)
                    os.close(_s2)
                for _tf, _cap in ((_fo, cap_out), (_fe, cap_err)):
                    try:
                        _tf.seek(0)
                        _data = _tf.read()
                        if _data:
                            _cap.write(_data.decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                    finally:
                        try:
                            _tf.close()
                        except Exception:
                            pass
            if capture_output:
                sys.stdout, sys.stderr = orig_out, orig_err
                sys.stdin = orig_stdin
            budgets.disarm()
    except _TestTimeoutError as exc:
        if resolver is not None:
            budgets.drain_runner(resolver, "timeout_drain:timeout_handler")
        _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns, "TestTimeoutError")
        failed += 1
    except Exit as exc:
        _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns, "ExitError")
        failed += 1
        exited = True
    except _ZtestNeverExecutedError as exc:
        _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns, "NeverExecutedError")
        failed += 1
    except (Skipped, unittest.SkipTest) as exc:
        # Skipped (and the native unittest.SkipTest, e.g. from setUpClass — cached
        # in failed_classes, deterministically skipping the whole class) escaping
        # from preparation is a skip, not a fail; unexpected teardown errors of the
        # drain dominate over the skip. Invariants: ADR-005-6.
        if budgets.consume_fired():
            _account_failure(ctx, item, outcome, budgets.timeout_dominance_exc("skip"), cap_out, cap_err, t0_ns, "TestTimeoutError")
            failed += 1
        elif outcome.teardown_errs:
            _account_failure(ctx, item, outcome, outcome.teardown_errs[0], cap_out, cap_err, t0_ns, "TeardownError")
            failed += 1
        else:
            dur_ms = (_REAL_MONOTONIC_NS() - t0_ns) // 1_000_000
            skipped += 1
            ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=str(exc) or "skipped", duration_ms=dur_ms)
    except KeyboardInterrupt as exc:
        _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns, "KeyboardInterrupt")
        failed += 1
        interrupted = True
    except SystemExit as exc:
        _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns, "SystemExit")
        failed += 1
    except BaseException as exc:
        if budgets.consume_fired():
            exc = budgets.timeout_dominance_exc("xfail-matched failure")
            _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns, "TestTimeoutError")
            failed += 1
        elif xfail_mark is not None:
            raises_expected = xfail_mark.kwargs.get("raises")
            if raises_expected is None or isinstance(exc, raises_expected):
                if outcome.teardown_errs:
                    # xfail does not mask a teardown failure on the exception path
                    # either (an expected preparation failure + a failing finalizer
                    # = a plain FAIL, not a skip + diagnostics).
                    _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns)
                    failed += 1
                else:
                    dur_ms = (_REAL_MONOTONIC_NS() - t0_ns) // 1_000_000
                    skipped += 1
                    ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=f"xfail: {exc}", duration_ms=dur_ms)
            else:
                _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns)
                failed += 1
        else:
            _account_failure(ctx, item, outcome, exc, cap_out, cap_err, t0_ns)
            failed += 1
    else:
        # Normal accounting path: the item's primary outcome + teardown
        # diagnostics + counter moves. No exception escaped the run.
        dur_ms = (_REAL_MONOTONIC_NS() - t0_ns) // 1_000_000
        outcome.duration_ms = dur_ms
        # Teardown dominance over pass/skip — unexpected finalization errors fail
        # the item (the first teardown error becomes primary, the rest stay
        # diagnostics). The conversion happens before the counter dispatch — no
        # double counting; timeout-dominance sits above.
        if not outcome.timeout_dominant and outcome.teardown_errs and outcome.verdict in (None, "pass", "skip"):
            outcome.verdict = "fail"
            outcome.exc = outcome.teardown_errs.pop(0)
        if outcome.timeout_dominant:
            # stopTest flipped the verdict: a timeout in teardown/doCleanups
            # dominates the primary outcome — a FAILED timeout record.
            failed += 1
            _report_failure(
                ipc, item, budgets.timeout_dominance_exc("teardown"),
                cap_out.getvalue(), cap_err.getvalue(), dur_ms,
                error_name="TestTimeoutError")
        elif outcome.verdict == "fail":
            if outcome.exc is not None and isinstance(outcome.exc, XFailed) and not outcome.teardown_errs:
                skipped += 1
                ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=str(outcome.exc), duration_ms=dur_ms)
            else:
                failed += 1
                _account_verdict(ctx, item, outcome, cap_out.getvalue(), cap_err.getvalue(), dur_ms)
        elif outcome.verdict == "skip":
            skipped += 1
            reason = outcome.reason or "skipped"
            if outcome.exc is not None and isinstance(outcome.exc, (Skipped, unittest.SkipTest, XFailed)):
                reason = str(outcome.exc) or reason
            ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=reason, duration_ms=dur_ms)
        else:
            # verdict == "pass": strict-XPASS check first (pytest semantics).
            if xfail_mark is not None and xfail_mark.kwargs.get("strict", ctx.xfail_strict):
                failed += 1
                exc = _StrictXPassError(f"[XPASS(strict)] {xfail_mark.kwargs.get('reason', '')}")
                _report_failure(ipc, item, exc, cap_out.getvalue(), cap_err.getvalue(), dur_ms)
            elif xfail_mark is not None:
                # A non-strict XPASS in pytest is PASSED
                # (report.passed=True, wasxfail), not a skip. Count it as passed.
                passed += 1
                # PASS accumulates in the out-buffer (flush at the nearest barrier:
                # the next TEST_START/test-body/PHASE_START boundary/FILE_DONE).
                ipc.send_event("TEST_PASS", nodeid=item.nodeid, duration_ms=dur_ms, defer=True)
                _clean_pass = True
            else:
                passed += 1
                # Always emit: the inactivity re-arm depends on it.
                ipc.send_event("TEST_PASS", nodeid=item.nodeid, duration_ms=dur_ms, defer=True)
                _clean_pass = True
            # skipped subTests of a passed test — separate skipped reports
            # (pytest-subtests parity). The extra skipped frames = a diagnostic
            # path → TEST_END is kept.
            for _sr in outcome.sub_skip_reasons:
                skipped += 1
                _clean_pass = False
                ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=f"subtest skip: {_sr}", duration_ms=dur_ms)
        # Expected finalizer failures (an xfail mark covers the teardown
        # phase) are their own report — pytest counts them as xfailed next to
        # the call phase verdict.
        for _td_exp in outcome.expected_teardown:
            skipped += 1
            _clean_pass = False
            ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=f"xfail: {_td_exp}", duration_ms=dur_ms)
        if outcome.exited:
            exited = True
        # (accounted counters only move once — see the snapshot guard below)
    finally:
        # Teardown diagnostics are published on all paths — on control-exception
        # paths the else branch does not run, but finalizer errors must not be
        # lost. Exactly-once: the list is drained here. Separate frames, nodeid::teardown.
        if outcome.teardown_errs:
            dur_ms = (_REAL_MONOTONIC_NS() - t0_ns) // 1_000_000
            for td_exc in outcome.teardown_errs:
                if isinstance(td_exc, KeyboardInterrupt):
                    interrupted = True
                _report_failure(ipc, item, td_exc, cap_out.getvalue(), cap_err.getvalue(), dur_ms)
            outcome.teardown_errs.clear()
            # a teardown-error is a diagnostic: TEST_END is mandatory.
            _clean_pass = False
        # Function-scope finalization + teardown errors from cleanups accounted; module/session scopes — in ctx.prev_resolver. Invariants: EXPECTED-FAILURE-NO-SETUP-COVERAGE, ADR-005-1.
        _ZT_CURRENT_NODE = None
        # On a clean PASS TEST_END is not sent (TEST_PASS already carries
        # nodeid+duration_ms; the summary comes from FILE_DONE). TEST_PASS is
        # queued deferred, so the inactivity deadline is re-armed when the frame
        # is actually written — with the next unconditional frame (the next
        # item's TEST_START, a PHASE_START boundary or FILE_DONE), never later
        # than a flush barrier.
        if not _clean_pass:
            ipc.send_event("TEST_END", nodeid=item.nodeid)

    return passed, failed, skipped, exited, interrupted


def _install_phase_wraps(case: unittest.TestCase, budgets: _PhaseBudgets, zr: ZtestResult) -> None:
    """Install _wrap_phase wrappers on the case's public lifecycle methods and
    the subtest bridge; the returned finalizers restore the originals."""
    wraps: list[Callable[[], None]] = []
    zr.install_subtest_bridge(case)
    specs = [
        ("setUp", dict(kind_guard=True, budgets_phase=False)),
        ("asyncSetUp", dict(kind_guard=True, budgets_phase=False)),
        ("runTest", dict(kind_guard=False, body=True, budgets_phase=False)),
        ("tearDown", dict(kind_guard=True, budgets_phase=True)),
        ("asyncTearDown", dict(kind_guard=True, budgets_phase=True)),
        ("doCleanups", dict(kind_guard=False, budgets_phase=True)),
    ]
    for attr, kw in specs:
        fin = _wrap_phase(case, budgets, attr, attr, outcome=zr.outcome, **kw)
        if fin is not None:
            wraps.append(fin)
    # The kind-guard of the wrapped method — on a real TestCase the method attribute itself is wrapped, not runTest. Invariants: KIND-CLASSIFICATION-OUTER-ONLY.
    if not isinstance(case, _FunctionTestCase):
        meth_name = getattr(case, "_testMethodName", None)
        if meth_name and meth_name != "runTest":
            fin = _wrap_phase(case, budgets, meth_name, meth_name, outcome=zr.outcome, kind_guard=False, body=True)
            if fin is not None:
                wraps.append(fin)
    case._ztest_wrap_finalizers = wraps  # type: ignore[attr-defined]

    # A per-unit budget for every cleanup callback — the addCleanup
    # registration is wrapped, not the whole doCleanups at once: an aggregate of several
    # individually-legal cleanups need not fit one shared budget (fixture
    # finalizers are already per-unit in teardown_scope).
    _orig_add_cleanup = case.addCleanup

    def _budgeted_add_cleanup(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(fn):
            # A coroutine callback must keep its async identity —
            # IsolatedAsyncioTestCase._callMaybeAsync dispatches on iscoroutinefunction;
            # a sync trampoline would return a coroutine without an await. stdlib addAsyncCleanup itself
            # delegates to addCleanup, so a separate addAsyncCleanup override is not needed.
            async def _acleanup_unit() -> Any:
                budgets.phase("teardown:addAsyncCleanup")
                try:
                    return await fn(*args, **kwargs)
                except _TestTimeoutError as _tte:
                    # A timeout of an individual cleanup — an ordinary teardown
                    # error (recorded by the bridge), not a control exception crashing
                    # the whole remaining cleanup stack. KI/EscExit still abort.
                    budgets.absorb_timeout()
                    raise _CleanupTimeoutError(f"async cleanup timed out: {_tte}") from _tte
                finally:
                    budgets.phase_off()

            _acleanup_unit.__name__ = getattr(fn, "__name__", "async_cleanup")
            return _orig_add_cleanup(_acleanup_unit)

        def _cleanup_unit() -> Any:
            budgets.phase("teardown:addCleanup")
            try:
                return fn(*args, **kwargs)
            except _TestTimeoutError as _tte:
                # See the async branch above.
                budgets.absorb_timeout()
                raise _CleanupTimeoutError(f"cleanup timed out: {_tte}") from _tte
            finally:
                budgets.phase_off()

        _cleanup_unit.__name__ = getattr(fn, "__name__", "cleanup")
        return _orig_add_cleanup(_cleanup_unit)

    try:
        case.addCleanup = _budgeted_add_cleanup  # type: ignore[method-assign]
        wraps.append(lambda: setattr(case, "addCleanup", _orig_add_cleanup))
    except (AttributeError, TypeError):
        pass


def _remove_phase_wraps(case: unittest.TestCase) -> None:
    wraps = getattr(case, "_ztest_wrap_finalizers", None)
    if not wraps:
        return
    while wraps:
        try:
            wraps.pop()()
        except BaseException:
            pass


def _account_failure(
    ctx: _FileRunCtx,
    item: TestItemSpec,
    outcome: _ItemOutcome,
    exc: BaseException,
    cap_out: io.StringIO,
    cap_err: io.StringIO,
    t0_ns: int,
    error_name: str | None = None) -> None:
    dur_ms = (_REAL_MONOTONIC_NS() - t0_ns) // 1_000_000
    _report_failure(ctx.ipc, item, exc, cap_out.getvalue(), cap_err.getvalue(), dur_ms, error_name=error_name)


def _account_verdict(
    ctx: _FileRunCtx,
    item: TestItemSpec,
    outcome: _ItemOutcome,
    out_txt: str,
    err_txt: str,
    dur_ms: int) -> None:
    exc = outcome.exc
    if exc is None:
        exc = Failed(outcome.reason or "test failed")
    if isinstance(exc, XFailed):
        ctx.ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=str(exc), duration_ms=dur_ms)
        return
    if isinstance(exc, _FormattedFailure):
        # The full formatted traceback came from the stdlib sink; emit it as-is.
        err_name = "UnexpectedSuccess" if outcome.reason.startswith("expected failure") else None
        first_user_line = ""
        for ln in exc.formatted.splitlines():
            if ln.startswith("  File ") and "ztest_shim" not in ln and "unittest" not in ln:
                first_user_line = ln.strip()
                break
        msg = exc.formatted.rstrip().rsplit("\n", 1)[-1]
        formatted = exc.formatted
        captured = _capture_sections(out_txt, err_txt)
        if captured:
            # pytest shows what the test printed right below the traceback;
            # the same block rides along with the record.
            msg = f"{msg}\n\n{captured}"
            formatted = f"{formatted}\n{captured}"
        ctx.ipc.send_event(
            "TEST_FAIL",
            nodeid=item.nodeid,
            loc=first_user_line or str(item.filepath),
            error=err_name or "AssertionError",
            msg=msg,
            tb=formatted,
            stdout_tail=out_txt[-500:] if out_txt else "",
            stderr_tail=err_txt[-500:] if err_txt else "",
            duration_ms=dur_ms)
        return
    _report_failure(ctx.ipc, item, exc, out_txt, err_txt, dur_ms, error_name="UnexpectedSuccess" if outcome.reason.startswith("expected failure") else None)


def _pytest_prepend_dir(filepath: Path) -> Path:
    """pytest's prepend import-mode inserts into sys.path the first ancestor
    WITHOUT __init__.py (the directory above the top package), not the file's
    parent. Otherwise sibling packages of the test tree become top-level and
    shadow stdlib/site-packages (e.g. test/html/ shadowing stdlib `html` if test/ were inserted directly)."""
    d = filepath.parent
    while (d / "__init__.py").is_file() and d.parent != d:
        d = d.parent
    return d


def _extract_pytest_skip_reason(report: Any) -> str:
    wasxfail = getattr(report, "wasxfail", None)
    if wasxfail is not None:
        return f"xfail: {wasxfail}" if wasxfail else "xfail"
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        r = str(longrepr[2])
        return r[len("Skipped: "):] if r.startswith("Skipped: ") else (r or "skipped")
    return str(longrepr or "skipped")


def _extract_pytest_fail_info(report: Any, default_file: str, default_err: str = "AssertionError") -> tuple[str, str, str, str]:
    tb_s = str(getattr(report, "longreprtext", "") or getattr(report, "longrepr", "") or default_err)
    longrepr = getattr(report, "longrepr", None)
    rcrash = getattr(longrepr, "reprcrash", None) if longrepr is not None else None
    if rcrash is not None:
        loc_path = getattr(rcrash, "path", None) or default_file
        loc_line = getattr(rcrash, "lineno", None)
        loc = f"{loc_path}:{loc_line}" if loc_line is not None else str(loc_path)
        msg = str(getattr(rcrash, "message", "") or tb_s)
    else:
        loc = default_file
        lines = [ln for ln in tb_s.splitlines() if ln.strip()]
        msg = lines[-1] if lines else tb_s
    err_name = default_err
    if msg.startswith("Failed: [XPASS(strict)]"):
        err_name = "StrictXPassError"
    elif ":" in msg:
        head = msg.split(":", 1)[0].strip()
        if head and "/" not in head and "\\" not in head and " " not in head:
            short = head.split(".")[-1]
            if short.isidentifier():
                err_name = short
    for sec_title, sec_body in getattr(report, "sections", None) or ():
        if sec_body:
            tb_s = f"{tb_s}\n--- {sec_title} ---\n{sec_body}"
    return loc, err_name, msg[:200_000], tb_s[:12_000_000]


class _ZtestPytestBridgePlugin:
    """In-worker pytest plugin that translates upstream pytest lifecycle hooks
    into ZTST binary IPC frames for the Zig supervisor (--engine=pytest)."""

    def __init__(
        self,
        ipc: IpcChannel,
        rel_file: str,
        *,
        collect_only: bool,
        llm_mode: bool,
        no_capture: bool,
        timeout_sec: float,
    ) -> None:
        self.ipc = ipc
        self.rel_file = rel_file
        self.filepath: Path | None = None
        self.collect_only = collect_only
        self.llm_mode = llm_mode
        self.no_capture = no_capture
        self.timeout_sec = timeout_sec
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.deselected = 0
        self.collected = 0
        self.collection_errors = 0
        self.collection_done_sent = False
        self.module_skipped = False
        self._call_passed: dict[str, int] = {}
        self._test_closed: set[str] = set()
        self._devnull_file: Any = None

    def _silence_terminal_reporter(self, config: Any) -> None:
        tr = config.pluginmanager.get_plugin("terminalreporter")
        if tr is not None and hasattr(tr, "_tw"):
            try:
                if self._devnull_file is None:
                    self._devnull_file = open(os.devnull, "w", encoding="utf-8")
                tr._tw._file = self._devnull_file
            except Exception:
                pass

    def pytest_configure(self, config: Any) -> None:
        self._silence_terminal_reporter(config)

    def pytest_sessionstart(self, session: Any) -> None:
        self._silence_terminal_reporter(session.config)

    def pytest_unconfigure(self, config: Any) -> None:
        if self._devnull_file is not None:
            try:
                self._devnull_file.close()
            except Exception:
                pass
            self._devnull_file = None

    def pytest_deselected(self, items: Sequence[Any]) -> None:
        self.deselected += len(items)

    def pytest_collection_modifyitems(self, session: Any, config: Any, items: list[Any]) -> None:
        # When ztest passes an individual file path on the pytest CLI, pytest treats
        # that file as an initial path (isinitpath) and skips parent conftest
        # collect_ignore / collect_ignore_glob / pytest_ignore_collect checks that
        # would have pruned the file during a directory walk. Re-evaluate them here.
        if self.filepath is None or not items:
            return
        fp = self.filepath
        try:
            root = Path(str(config.rootpath)).resolve()
        except Exception:
            return
        import fnmatch as _fnmatch

        for plug in config.pluginmanager.get_plugins():
            cf_file = getattr(plug, "__file__", None)
            if not cf_file or not str(cf_file).endswith("conftest.py"):
                continue
            try:
                cd = Path(cf_file).resolve().parent
            except Exception:
                continue
            for frag in getattr(plug, "collect_ignore", None) or []:
                try:
                    t = (cd / str(frag)).resolve()
                    if fp == t or fp.is_relative_to(t):
                        items.clear()
                        return
                except Exception:
                    continue
            for glob_pat in getattr(plug, "collect_ignore_glob", None) or []:
                try:
                    if _fnmatch.fnmatch(str(fp), str(cd / str(glob_pat))):
                        items.clear()
                        return
                except Exception:
                    continue
            hook_ic = getattr(plug, "pytest_ignore_collect", None)
            if callable(hook_ic):
                d = fp
                while d != root and (d == fp or root in d.parents):
                    if cd == d or cd in d.parents:
                        try:
                            kw = _bind_hook_kwargs(
                                hook_ic,
                                {"collection_path": d, "path": LegacyPath(d), "config": config},
                                "pytest_ignore_collect",
                            )
                            if hook_ic(**kw):
                                items.clear()
                                return
                        except Exception:
                            pass
                    d = d.parent

    def pytest_collectreport(self, report: Any) -> None:
        if getattr(report, "failed", False):
            self.collection_errors += 1
            nid = getattr(report, "nodeid", "") or f"{self.rel_file}::<collection>"
            loc, err_name, msg, tb_s = _extract_pytest_fail_info(report, self.rel_file, "CollectionError")
            self.ipc.send_event(
                "TEST_FAIL",
                nodeid=nid,
                loc=loc,
                error="CollectionError" if err_name == "AssertionError" else err_name,
                msg=msg,
                tb=tb_s,
            )
        elif getattr(report, "skipped", False) and getattr(report, "nodeid", "") == self.rel_file:
            self.skipped += 1
            self.module_skipped = True

    def pytest_collection_finish(self, session: Any) -> None:
        items = getattr(session, "items", None) or []
        self.collected = len(items)
        self.collection_done_sent = True
        self.ipc.send_event("COLLECTION_DONE", file=self.rel_file, collected=self.collected)
        if self.collect_only and self.collection_errors == 0:
            ids = [getattr(it, "nodeid", "") for it in items]
            if not self.llm_mode:
                for i_b in range(0, len(ids), 100):
                    self.ipc.send_event("COLLECT_BATCH", nodeids=ids[i_b : i_b + 100])

    def _phase_timer_guard(self) -> Any:
        if self.timeout_sec <= 0.0:
            yield
            return
        t_sec = self.timeout_sec

        def _alrm_handler(_signum: int, _frame: Any) -> None:
            raise _TestTimeoutError(f"Test exceeded timeout of {t_sec:.3f}s")

        prev_handler = signal.signal(signal.SIGALRM, _alrm_handler)
        signal.setitimer(signal.ITIMER_REAL, t_sec)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, prev_handler)

    def pytest_runtest_setup(self, item: Any) -> Any:
        yield from self._phase_timer_guard()

    def pytest_runtest_call(self, item: Any) -> Any:
        yield from self._phase_timer_guard()

    def pytest_runtest_teardown(self, item: Any) -> Any:
        yield from self._phase_timer_guard()

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        self.ipc.send_event("TEST_START", nodeid=nodeid)
        _flush_ipc_now()

    def pytest_runtest_logreport(self, report: Any) -> None:
        nid = getattr(report, "nodeid", "") or self.rel_file
        dur_ms = int(round(float(getattr(report, "duration", 0.0) or 0.0) * 1000.0))
        when = getattr(report, "when", "call")
        if when == "setup":
            if getattr(report, "skipped", False):
                self.skipped += 1
                self._test_closed.add(nid)
                reason = _extract_pytest_skip_reason(report)
                self.ipc.send_event("TEST_SKIP", nodeid=nid, reason=reason, duration_ms=dur_ms)
                self.ipc.send_event("TEST_END", nodeid=nid)
            elif getattr(report, "failed", False):
                self.failed += 1
                self._test_closed.add(nid)
                loc, err_name, msg, tb_s = _extract_pytest_fail_info(report, self.rel_file, "SetupError")
                self.ipc.send_event("TEST_FAIL", nodeid=nid, loc=loc, error=err_name, msg=msg, tb=tb_s, duration_ms=dur_ms)
                self.ipc.send_event("TEST_END", nodeid=nid)
        elif when == "call":
            if getattr(report, "passed", False):
                self._call_passed[nid] = dur_ms
            elif getattr(report, "skipped", False):
                self.skipped += 1
                self._test_closed.add(nid)
                reason = _extract_pytest_skip_reason(report)
                self.ipc.send_event("TEST_SKIP", nodeid=nid, reason=reason, duration_ms=dur_ms)
                self.ipc.send_event("TEST_END", nodeid=nid)
            elif getattr(report, "failed", False):
                self.failed += 1
                self._test_closed.add(nid)
                loc, err_name, msg, tb_s = _extract_pytest_fail_info(report, self.rel_file, "AssertionError")
                self.ipc.send_event("TEST_FAIL", nodeid=nid, loc=loc, error=err_name, msg=msg, tb=tb_s, duration_ms=dur_ms)
                self.ipc.send_event("TEST_END", nodeid=nid)
        elif when == "teardown":
            if getattr(report, "failed", False):
                self.failed += 1
                loc, err_name, msg, tb_s = _extract_pytest_fail_info(report, self.rel_file, "TeardownError")
                self.ipc.send_event("TEST_FAIL", nodeid=f"{nid}::<teardown>", loc=loc, error=err_name, msg=msg, tb=tb_s, duration_ms=dur_ms)
                if nid not in self._test_closed:
                    self.ipc.send_event("TEST_END", nodeid=nid)
                    self._test_closed.add(nid)
            else:
                if nid in self._call_passed and nid not in self._test_closed:
                    self.passed += 1
                    self.ipc.send_event("TEST_PASS", nodeid=nid, duration_ms=self._call_passed[nid], defer=True)
                    self._test_closed.add(nid)


_PYTEST_BRIDGE_HOOKWRAPPERS_INSTALLED = False


def _run_test_file_pytest_engine(
    filepath_str: str,
    options: dict[str, Any],
    ipc: IpcChannel,
) -> int:
    """Execute `filepath_str` inside the supervised worker via upstream `pytest.main`
    with `_ZtestPytestBridgePlugin` attached (`--engine=pytest`)."""
    global _PYTEST_BRIDGE_HOOKWRAPPERS_INSTALLED
    start_ns = _REAL_MONOTONIC_NS()
    raw_target = filepath_str.split("::", 1)[0] if "::" in filepath_str else filepath_str
    filepath = Path(raw_target).resolve()
    rootpath = Path(options.get("rootdir") or os.getcwd()).resolve()
    if str(rootpath) not in sys.path:
        sys.path.insert(0, str(rootpath))
    _prepend_dir = _pytest_prepend_dir(filepath)
    if str(_prepend_dir) not in sys.path:
        sys.path.insert(0, str(_prepend_dir))
    try:
        rel_file = str(filepath.relative_to(rootpath))
    except ValueError:
        rel_file = filepath.name

    ipc.send_event("FILE_START", file=rel_file)
    try:
        import pytest as real_pytest
    except Exception as imp_exc:
        msg = f"ztest --engine=pytest requires 'pytest' in the active Python environment: {imp_exc}"
        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<engine>", loc=rel_file, error="EngineError", msg=msg, tb=msg)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=1, skipped=0, deselected=0, collected=0, status="COLLECTION_ERROR", duration_ms=0)
        return 2

    if not _PYTEST_BRIDGE_HOOKWRAPPERS_INSTALLED:
        for _hw_name in ("pytest_runtest_setup", "pytest_runtest_call", "pytest_runtest_teardown"):
            setattr(
                _ZtestPytestBridgePlugin,
                _hw_name,
                real_pytest.hookimpl(hookwrapper=True)(getattr(_ZtestPytestBridgePlugin, _hw_name)),
            )
        _PYTEST_BRIDGE_HOOKWRAPPERS_INSTALLED = True

    collect_only = bool(options.get("collect_only", False))
    llm_mode = bool(options.get("llm_mode", False))
    no_capture = bool(options.get("no_capture", False))
    t_opt = options.get("timeout_us")
    timeout_sec = (t_opt / 1_000_000.0) if (isinstance(t_opt, int) and not isinstance(t_opt, bool) and t_opt > 0) else 0.0

    # Prevent nested worker pools (ztest's Zig supervisor already parallelizes across files).
    argv: list[str] = ["-p", "no:xdist"]
    if no_capture:
        argv.append("-s")
    if collect_only:
        argv.append("--collect-only")
    k_expr = str(options.get("k_expr") or "").strip()
    if k_expr:
        argv.extend(["-k", k_expr])
    m_expr = str(options.get("m_expr") or "").strip()
    if m_expr:
        argv.extend(["-m", m_expr])
    maxfail = int(options.get("maxfail") or 0)
    if maxfail > 0:
        argv.append(f"--maxfail={maxfail}")
    sandbox = str(options.get("sandbox") or "").strip()
    if sandbox:
        argv.append(f"--basetemp={sandbox}/pytest_tmp_{os.getpid()}")
    argv.append(filepath_str)

    def _invoke(args_list: list[str], br: _ZtestPytestBridgePlugin) -> int:
        try:
            res = real_pytest.main(list(args_list), plugins=[br])
            return int(res)
        except SystemExit as se:
            return int(se.code) if isinstance(se.code, int) else 1

    bridge = _ZtestPytestBridgePlugin(
        ipc,
        rel_file,
        collect_only=collect_only,
        llm_mode=llm_mode,
        no_capture=no_capture,
        timeout_sec=timeout_sec,
    )
    bridge.filepath = filepath
    pytest_rc = _invoke(argv, bridge)
    # If pyproject.toml/pytest.ini carries addopts flags for uninstalled third-party
    # plugins (e.g. --cov without pytest-cov), pytest exits with USAGE_ERROR (4)
    # during config parsing before collection starts. Retry once with `-o addopts=`
    # so only addopts is cleared while all other ini settings (markers, xfail_strict,
    # filterwarnings, pythonpath) remain intact.
    if pytest_rc == 4 and not bridge.collection_done_sent and bridge.collection_errors == 0:
        try:
            sys.__stderr__.write(f"ztest: pytest addopts failed (exit 4) for {rel_file}; retrying with -o addopts=\n")
            sys.__stderr__.flush()
        except Exception:
            pass
        bridge = _ZtestPytestBridgePlugin(
            ipc,
            rel_file,
            collect_only=collect_only,
            llm_mode=llm_mode,
            no_capture=no_capture,
            timeout_sec=timeout_sec,
        )
        bridge.filepath = filepath
        pytest_rc = _invoke(["-o", "addopts=", *argv], bridge)

    total_ms = (_REAL_MONOTONIC_NS() - start_ns) // 1_000_000
    if bridge.collection_errors > 0 or (pytest_rc in (2, 3, 4) and not bridge.collection_done_sent):
        if bridge.collection_errors == 0 and bridge.failed == 0:
            msg = f"pytest exited with code {pytest_rc} during collection/startup"
            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<collection>", loc=rel_file, error="CollectionError", msg=msg, tb=msg)
            bridge.collection_errors = 1
        final_failed = max(bridge.failed, bridge.collection_errors)
        ipc.send_event(
            "FILE_DONE",
            file=rel_file,
            passed=bridge.passed,
            failed=final_failed,
            skipped=bridge.skipped,
            deselected=bridge.deselected,
            collected=bridge.collected,
            status="COLLECTION_ERROR",
            duration_ms=total_ms,
        )
        return 2

    if pytest_rc not in (0, 5) and bridge.failed == 0:
        msg = f"pytest session exited with status {pytest_rc}"
        ipc.send_event(
            "TEST_FAIL",
            nodeid=f"{rel_file}::<pytest-session>",
            loc=rel_file,
            error="SessionError",
            msg=msg,
            tb=msg,
        )
        bridge.failed = 1

    if collect_only:
        ipc.send_event(
            "FILE_DONE",
            file=rel_file,
            passed=0,
            failed=bridge.failed,
            skipped=bridge.skipped,
            deselected=bridge.deselected,
            collected=bridge.collected,
            status="COLLECTED",
            duration_ms=total_ms,
        )
        return 0 if bridge.failed == 0 else 1

    if bridge.collected == 0 and bridge.passed == 0 and bridge.failed == 0:
        st = "SKIPPED" if bridge.module_skipped else "NO_TESTS_SELECTED"
        ipc.send_event(
            "FILE_DONE",
            file=rel_file,
            passed=0,
            failed=0,
            skipped=bridge.skipped,
            deselected=bridge.deselected,
            collected=0,
            status=st,
            duration_ms=total_ms,
        )
        return 0

    status = "PASSED" if bridge.failed == 0 else "FAILED"
    ipc.send_event(
        "FILE_DONE",
        file=rel_file,
        passed=bridge.passed,
        failed=bridge.failed,
        skipped=bridge.skipped,
        deselected=bridge.deselected,
        collected=bridge.collected,
        status=status,
        duration_ms=total_ms,
    )
    return 0 if bridge.failed == 0 else 1


def run_test_file(
    filepath_str: str,
    options: dict[str, Any],
    ipc: IpcChannel) -> int:
    """The worker entry: guarantees a flush of the remaining out-buffer on ANY exit.

    Pending frames must not outlive the file. Normally the queue is already
    empty at this point (FILE_DONE/test frames are unconditional sends), so
    the finally-flush is a no-op; it guards early returns (collection errors,
    skip-at-import, collect-only). A transport error here is silenced: the
    channel is terminal, FILE_DONE did not leave → the supervisor closes the
    file fail-closed (CRASHED), and an exception from finally must not overtake the worker's os._exit(rc).
    """
    _file_fd_guard: tuple[int, int] | None = None
    if not bool(options.get("no_capture", False)):
        try:
            _flush_user_stdio()
            _dn = os.open(os.devnull, os.O_RDWR)
            try:
                _fs1 = os.dup(1)
                _fs2 = os.dup(2)
                os.dup2(_dn, 1)
                os.dup2(_dn, 2)
                _file_fd_guard = (_fs1, _fs2)
            finally:
                os.close(_dn)
        except OSError:
            _file_fd_guard = None
    try:
        if options.get("engine") == "pytest" or os.environ.get("ZTEST_ENGINE") == "pytest":
            return _run_test_file_pytest_engine(filepath_str, options, ipc)
        return _run_test_file_impl(filepath_str, options, ipc)
    finally:
        try:
            ipc.flush()
        except BaseException:
            pass
        # The worker leaves through os._exit(), which skips interpreter
        # teardown: without this flush a piped stdout loses every no-capture
        # byte the tests printed (stderr is line-buffered and survives).
        _flush_user_stdio()
        if _file_fd_guard is not None:
            for _orig_fd, _target_fd in ((_file_fd_guard[0], 1), (_file_fd_guard[1], 2)):
                try:
                    os.dup2(_orig_fd, _target_fd)
                except OSError:
                    pass
                try:
                    os.close(_orig_fd)
                except OSError:
                    pass


def _run_test_file_impl(
    filepath_str: str,
    options: dict[str, Any],
    ipc: IpcChannel) -> int:
    """ADR-005: orchestration around the stdlib lifecycle — collection/conftest/filters/finalization are shim-owned, items run through TestCase.run (see INVARIANTS.md)."""
    if bool(options.get("no_capture", False)):
        # -s means "let the tests write to the real stdio": make that stream
        # line-buffered so the output is live and survives an os._exit.
        _line_buffer_user_stdio()
    _ensure_root_log_capture()
    start_ns = _REAL_MONOTONIC_NS()
    raw_target = filepath_str
    target_filter: list[str] = []
    if "::" in raw_target:
        parts = raw_target.split("::")
        raw_target = parts[0]
        target_filter = parts[1:]

    filepath = Path(raw_target).resolve()
    rootpath = Path(options.get("rootdir") or os.getcwd()).resolve()
    cache_dir = rootpath / ".ztest_cache"
    # pytest's global capture is active already at module import time
    # (stdin=DontReadFromInput, not io.IOBase) — import-time conditions like
    # skipUnless(isinstance(stdin, io.IOBase)) must see the same type. Set stdin
    # at session level, before module imports.
    # Capture mode only; under -s pytest leaves stdin real.
    if not bool(options.get("no_capture", False)):
        sys.stdin = _DontReadFromInput()
    if str(rootpath) not in sys.path:
        sys.path.insert(0, str(rootpath))
    _prepend_dir = _pytest_prepend_dir(filepath)
    if str(_prepend_dir) not in sys.path:
        sys.path.insert(0, str(_prepend_dir))

    try:
        rel_file = str(filepath.relative_to(rootpath))
    except ValueError:
        rel_file = filepath.name

    ipc.send_event("FILE_START", file=rel_file)
    # pytest reads its ini from the target's ancestor chain, so a suite run
    # from an unrelated CWD still honors its own pytest.ini/pyproject.toml.
    cfg = ConfigStub(rootpath, options=options, ini_root=_discover_ini_dir(filepath.parent) or rootpath)
    parser_stub = ParserStub(cfg)
    session_stub = types.SimpleNamespace(config=cfg, items=[], exitstatus=0)

    # 1. Discover & load hierarchical conftest.py chain
    conftest_paths: list[Path] = []
    cur = filepath.parent
    while True:
        cf = cur / "conftest.py"
        if cf.exists():
            conftest_paths.append(cf)
        if cur == rootpath or cur.parent == cur:
            break
        cur = cur.parent
    conftest_paths.reverse()

    conftest_mods: list[types.ModuleType] = []
    fixture_layers: list[dict[str, FixtureDef]] = []

    # timeout_us is in whole microseconds; 0/absent = off. Invariants: TIMEOUT-INTEGER-MICROSECONDS.
    _timeout_us_opt = options.get("timeout_us")
    if isinstance(_timeout_us_opt, int) and _timeout_us_opt > 0:
        per_test_timeout = _timeout_us_opt / 1_000_000.0
    else:
        per_test_timeout = 0.0

    _budgets = _PhaseBudgets(ipc, rel_file, per_test_timeout)
    _budgets.install()  # SIGALRM handler + module-level phase hooks for teardown_scope

    def _run_session_finalize(cur_failed: int) -> int:
        _depth0 = len(_budgets._phase_stack)
        try:
            for cf_mod in reversed(conftest_mods):
                _cf_name = getattr(cf_mod, "__name__", "conftest")
                _budgets.phase(f"session_finalize:sessionfinish:{_cf_name}")
                try:
                    try:
                        _hook_sfin = _invocable_conf_hook(cf_mod, "pytest_sessionfinish")
                    except BaseException:
                        # Absorb in the lookup branch too — the storm does not live in the handler
                        _budgets.absorb_timeout()
                        cur_failed += 1
                        tb_s = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<pytest_sessionfinish>", loc=rel_file, error="SessionFinishHookLookupError", msg=tb_s, tb=tb_s)
                        _hook_sfin = None
                    if _hook_sfin is not None:
                        try:
                            _flush_ipc_now()  # the flush barrier before the user hook
                            _check_conf_hook_result("pytest_sessionfinish", _hook_sfin(session_stub, 0 if cur_failed == 0 else 1))
                            _budgets.raise_if_fired(f"session_finalize:sessionfinish:{_cf_name}")
                        except KeyboardInterrupt:
                            _budgets.absorb_timeout()
                            cur_failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<pytest_sessionfinish>", loc=rel_file, error="SessionFinishInterrupt", msg=tb_s, tb=tb_s)
                            _mark_teardown_interrupt()
                        except BaseException as _sf_exc:
                            _budgets.absorb_timeout()
                            cur_failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<pytest_sessionfinish>", loc=rel_file, error="SessionFinishError", msg=tb_s, tb=tb_s)
                finally:
                    # The only phase-close point is the finally: a phase_off() in an
                    # except branch would double-pop the phase stack.
                    _budgets.phase_off()
                _budgets.phase(f"session_finalize:unconfigure:{_cf_name}")
                try:
                    try:
                        _hook_uncf = _invocable_conf_hook(cf_mod, "pytest_unconfigure")
                    except BaseException:
                        _budgets.absorb_timeout()
                        cur_failed += 1
                        tb_s = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<pytest_unconfigure>", loc=rel_file, error="UnconfigureHookLookupError", msg=tb_s, tb=tb_s)
                        _hook_uncf = None
                    if _hook_uncf is not None:
                        try:
                            _check_conf_hook_result("pytest_unconfigure", _call_arity_aware(_hook_uncf, cfg))
                            _budgets.raise_if_fired(f"session_finalize:unconfigure:{_cf_name}")
                        except KeyboardInterrupt:
                            _budgets.absorb_timeout()
                            cur_failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<pytest_unconfigure>", loc=rel_file, error="UnconfigureInterrupt", msg=tb_s, tb=tb_s)
                            _mark_teardown_interrupt()
                        except BaseException as _uc_exc:
                            _budgets.absorb_timeout()
                            cur_failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<pytest_unconfigure>", loc=rel_file, error="UnconfigureError", msg=tb_s, tb=tb_s)
                finally:
                    _budgets.phase_off()
            if getattr(session_stub, "exitstatus", 0) != 0 and cur_failed == 0:
                cur_failed += 1
                ipc.send_event(
                    "TEST_FAIL",
                    nodeid=f"{rel_file}::<pytest_sessionfinish>",
                    loc=rel_file,
                    error="SessionExitstatusError",
                    msg=f"session.exitstatus was mutated to {session_stub.exitstatus!r} by a sessionfinish hook (rejected)",
                    tb="")
        finally:
            # Drain only the phases left open by this function (protection against
            # an unclosed phase on an exception); other frames of the stack are not touched.
            while len(_budgets._phase_stack) > _depth0:
                _budgets.phase_off()
        return cur_failed

    def _safe_session_finalize(cur_failed: int) -> int:
        # Belt for early-exit paths (skip-at-import, collection/load/modifyitems
        # errors, collect-only, no-tests-selected) — they too must not lose FILE_DONE.
        try:
            return _run_session_finalize(cur_failed)
        except BaseException as _sf2_exc:
            _budgets.absorb_timeout()
            cur_failed += 1
            try:
                tb_s = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<session_finalize>", loc=rel_file, error="SessionFinalizeError", msg=tb_s, tb=tb_s)
            except BaseException:
                pass
            return cur_failed

    try:
        # Load all conftests first, then extend the chain with plugin modules
        # (ini plugins= + the conftest's pytest_plugins variable): a plugin goes
        # before the declaring conftest (its fixtures are weaker); ini plugins are
        # the outermost layer. A plugin ImportError becomes a CollectionError for
        # the file (pytest fails at startup).
        _loaded_confs: list[tuple[Path, types.ModuleType]] = []
        for idx, cf_path in enumerate(conftest_paths):
            abs_cf = str(cf_path.resolve())
            if abs_cf in _PRELOADED_CONFTESTS:
                cf_mod = _PRELOADED_CONFTESTS[abs_cf]
            else:
                # conftest import warnings are collected (record=True), not printed
                # to stderr — pytest accumulates them for the warnings summary; the
                # per-file verbose lines stay clean.
                with warnings.catch_warnings(record=True):
                    warnings.simplefilter("always")
                    cf_mod = load_module_with_ast_cache(cf_path, _package_qualified_name(cf_path, f"_ztest_conftest_{idx}"), cache_dir=cache_dir)
            _loaded_confs.append((cf_path, cf_mod))
        _expanded_confs: list[tuple[Path, types.ModuleType]] = []
        _seen_plugins: set[str] = set()

        def _load_plugin(name: str) -> types.ModuleType | None:
            if name in _seen_plugins:
                return None
            _seen_plugins.add(name)
            return importlib.import_module(name)

        for _pname in _iter_plugin_names(cfg.getini("plugins")):
            _pmod = _load_plugin(_pname)
            if _pmod is not None:
                _expanded_confs.append((Path(getattr(_pmod, "__file__", None) or _pname), _pmod))
        for cf_path, cf_mod in _loaded_confs:
            try:
                _pp = getattr(cf_mod, "pytest_plugins", [])
            except Exception:
                _pp = []
            for _pname in _iter_plugin_names(_pp):
                _pmod = _load_plugin(_pname)
                if _pmod is not None:
                    _expanded_confs.append((Path(getattr(_pmod, "__file__", None) or _pname), _pmod))
            _expanded_confs.append((cf_path, cf_mod))

        for cf_path, cf_mod in _expanded_confs:
            conftest_mods.append(cf_mod)
            # The audit and fixture extraction run per-file from the live
            # module: cross-module fixture mutations through a foreign
            # pytest_configure are seen at once. The audit is the single
            # _audit_conftest_hooks helper (the explode-attr/noop/wrapper
            # handling lives there).
            _audit_conftest_hooks(cf_mod, cf_path)
            _hook_addoption = _invocable_conf_hook(cf_mod, "pytest_addoption")
            if _hook_addoption is not None:
                _check_conf_hook_result("pytest_addoption", _call_arity_aware(_hook_addoption, parser_stub))
            _hook_configure = _invocable_conf_hook(cf_mod, "pytest_configure")
            if _hook_configure is not None:
                _check_conf_hook_result("pytest_configure", _call_arity_aware(_hook_configure, cfg))
            _hook_sessstart = _invocable_conf_hook(cf_mod, "pytest_sessionstart")
            if _hook_sessstart is not None:
                _check_conf_hook_result("pytest_sessionstart", _call_arity_aware(_hook_sessstart, session_stub))
            fixture_layers.append(_extract_fixtures_from_obj(cf_mod))

        # pytest_ignore_collect — pluggy firstresult: hook impls go LIFO
        # (leaf-first), the first non-None decides. The hook is consulted for
        # ancestor directories of the file too (pytest consults it during the
        # directory walk): for a directory D the conftests strictly above D apply
        # (already loaded by decision time), for a file — the whole chain. The
        # rootpath (the initial target) is not consulted — an approximation of
        # pytest's isinitpath semantics.
        cf_dirs = [Path(getattr(m, "__file__", "") or "").resolve().parent for m in conftest_mods]

        def _ignore_decision(target: Path, applicable: list[types.ModuleType]) -> bool:
            for cf_mod in reversed(applicable):  # LIFO: leaf-first
                _hook_ic = _invocable_conf_hook(cf_mod, "pytest_ignore_collect")
                if _hook_ic is None:
                    continue
                # the legacy path argument — the py.path.local form (LegacyPath with
                # .basename); collection_path — the modern Path.
                _ic_kw = _bind_hook_kwargs(
                    _hook_ic,
                    {"collection_path": target, "path": LegacyPath(target), "config": cfg},
                    "pytest_ignore_collect")
                _flush_ipc_now()  # the flush barrier before the user hook
                _ic_res = _hook_ic(**_ic_kw)
                _check_conf_hook_result("pytest_ignore_collect", _ic_res)
                if _ic_res is not None:
                    return bool(_ic_res)
            return False

        _ignored = False
        _chain_dirs: list[Path] = []
        _d = filepath.parent
        while _d != rootpath and rootpath in _d.parents:
            _chain_dirs.append(_d)
            _d = _d.parent
        for _d in reversed(_chain_dirs):  # root-to-leaf, like pytest's descent
            _applicable = [m for m, cd in zip(conftest_mods, cf_dirs) if cd in _d.parents]
            if _ignore_decision(_d, _applicable):
                _ignored = True
                break
        if not _ignored:
            _ignored = _ignore_decision(filepath, conftest_mods)
        # conftest attributes collect_ignore / collect_ignore_glob. Paths are
        # relative to the conftest's directory; a dir entry kills the whole
        # subtree (pytest consults them at every walk level — for the per-file
        # model that is is_relative_to).
        if not _ignored:
            import fnmatch as _fnmatch

            for _cf_mod, _cd in zip(conftest_mods, cf_dirs):
                try:
                    _ci = getattr(_cf_mod, "collect_ignore", None)
                    _cig = getattr(_cf_mod, "collect_ignore_glob", None)
                except Exception:
                    continue
                for _frag in _ci or []:
                    try:
                        _target = (_cd / str(_frag)).resolve()
                        if filepath == _target or filepath.is_relative_to(_target):
                            _ignored = True
                            break
                    except Exception:
                        continue
                if not _ignored:
                    for _glob in _cig or []:
                        try:
                            if _fnmatch.fnmatch(str(filepath), str(_cd / str(_glob))):
                                _ignored = True
                                break
                        except Exception:
                            continue
                if _ignored:
                    break
        if _ignored:
            fin_failed = _safe_session_finalize(0)
            ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=0, deselected=0, status="NO_TESTS_SELECTED", duration_ms=0)
            return 0 if fin_failed == 0 else 1

        # A non-.py file (reached only when the supervisor found an ancestor
        # conftest.py with pytest_collect_file) — a custom collector.
        if filepath.suffix != ".py":
            _cc_rc = _run_custom_collect_items(ipc, filepath, rel_file, cfg, conftest_mods, options, target_filter, finalize_fn=_safe_session_finalize)
            return int(_cc_rc or 0)

        # pytest_collect_file takes priority over .py files too — pytest calls
        # the hook first; None along the whole
        # chain → the normal .py collection below.
        if any(_invocable_conf_hook(m, "pytest_collect_file") is not None for m in conftest_mods):
            _cc_rc = _run_custom_collect_items(ipc, filepath, rel_file, cfg, conftest_mods, options, target_filter, py_fallback=True, finalize_fn=_safe_session_finalize)
            if _cc_rc is not None:
                return _cc_rc

        # The pytest_generate_tests chain — real support.
        gen_tests_hooks: list[tuple[types.ModuleType, Callable[..., Any]]] = []
        for cf_mod in conftest_mods:
            _hook_gt = _invocable_conf_hook(cf_mod, "pytest_generate_tests")
            if _hook_gt is not None:
                gen_tests_hooks.append((cf_mod, _hook_gt))

        # pytest_pycollect_makeitem — custom collection of module attributes
        # (data-driven non-.py test files). The raw attribute
        # (not _invocable_conf_hook — the wrapper is driven ourselves with a real
        # Result; the generic wrapper driver does not fit here: post-yield reads
        # the outcome). Leaf-first (reversed) like pytest_collect_file.
        pcmi_hooks: list[Callable[..., Any]] = []
        for cf_mod in reversed(conftest_mods):
            _h = cf_mod.__dict__.get("pytest_pycollect_makeitem")
            if callable(_h):
                pcmi_hooks.append(_h)

        # 2. Load the test module itself under its natural stem name for multiprocessing pickle compatibility
        # sys.path instead of os.environ['PYTHONPATH'] — test subprocesses
        # inherit a clean environment (pytest parity).
        # The ini option pythonpath (pytest 7+ builtin) — rootdir-relative
        # paths, prepended in declaration order (recognized ini key).
        try:
            _pp_raw = cfg.getini("pythonpath")
        except Exception:
            _pp_raw = None
        _pp_paths: list[str] = []
        if isinstance(_pp_raw, str):
            _pp_paths = [p.strip() for p in re.split(r"[\s,]+", _pp_raw) if p.strip()]
        elif isinstance(_pp_raw, (list, tuple)):
            _pp_paths = [str(p).strip() for p in _pp_raw if str(p).strip()]
        if _pp_paths:
            # Entries are relative to the config file's directory.
            _root = Path(getattr(cfg, "ini_root", None) or getattr(cfg, "rootpath", None) or filepath.parent)
            for _p in reversed(_pp_paths):
                _rp = _p if os.path.isabs(_p) else str((_root / _p).resolve())
                if _rp not in sys.path:
                    sys.path.insert(0, _rp)
        if str(_prepend_dir) not in sys.path:
            sys.path.insert(0, str(_prepend_dir))
        mod_name = _package_qualified_name(filepath, filepath.stem)
        # ini filterwarnings apply at collection too (module import) — pytest
        # escalates error::pytest.PytestWarning already at the decoration of a bare
        # usefixtures. Parsed once per file into a local (ctx is not created yet —
        # born below); the later block picks it up.
        try:
            _warn_filters_early = _parse_warn_filters(cfg.getini("filterwarnings"))
        except AttributeError as exc:
            tb_str = traceback.format_exc()
            ipc.send_event(
                "TEST_FAIL",
                nodeid=f"{rel_file}::<collection>",
                loc=f"{rel_file}:1",
                error="CollectionError",
                msg=f"filterwarnings: failed to resolve category: {exc}\n{tb_str}",
                tb=tb_str)
            ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=1, skipped=0, deselected=0, collected=0, status="COLLECTED", duration_ms=0)
            return 1
        _wcm_collect = warnings.catch_warnings(record=True)
        _wcm_collect.__enter__()
        if not sys.warnoptions:
            warnings.simplefilter("always")
        try:
            for _act, _wmsg, _wcat, _wmod, _wlno in _warn_filters_early:
                warnings.filterwarnings(_act, message=_wmsg, category=_wcat, module=_wmod, lineno=_wlno)
            test_mod = load_module_with_ast_cache(filepath, mod_name, cache_dir=cache_dir)
        finally:
            _wcm_collect.__exit__(None, None, None)
        # pytest_generate_tests defined in the test module itself is consulted too
        # (pytest calls it as well).
        _hook_gt_mod = getattr(test_mod, "pytest_generate_tests", None)
        if callable(_hook_gt_mod) and not inspect.isgeneratorfunction(_hook_gt_mod):
            gen_tests_hooks.append((test_mod, _hook_gt_mod))
        fixture_layers.append(_extract_fixtures_from_obj(test_mod))
    except (Skipped, unittest.SkipTest):
        # A native unittest.SkipTest at import = a module skip (stdlib discovery),
        # not COLLECTION_ERROR. Finalization keeps the right to fail.
        fin_failed = _safe_session_finalize(0)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=1, deselected=0, status="SKIPPED", duration_ms=0)
        return 0 if fin_failed == 0 else 1
    except BaseException:
        tb_str = traceback.format_exc()
        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<collection>", loc=f"{rel_file}:1", error="CollectionError", msg=tb_str, tb=tb_str)
        fin_failed = _safe_session_finalize(1)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=0, deselected=0, status="COLLECTION_ERROR", duration_ms=0)
        return 2

    mod_marks: list[Mark] = _norm_marks(getattr(test_mod, "pytestmark", []))

    # 3. Collect functions & classes. The pytest 9 defaults (python_functions=["test"],
    # python_classes=["Test"]) are prefix matches: test, test_*, testfoo, the
    # methods tested/testFoo (camelCase unittest subclasses included) — verified
    # empirically against pytest 9.0.3.
    # Suffix rules (*_test, *Tests) are not pytest defaults; re-collecting them
    # would falsely collect helper factories and mixin classes.
    items: list[TestItemSpec] = []
    # A collector stub of the module for pytest_pycollect_makeitem(collector=...)
    _mod_collector: Any = None
    if pcmi_hooks:
        _mod_collector = _PytestNodeBase(name=rel_file, parent=None, path=filepath)
        _mod_collector.config = cfg
    try:
        for obj_name, obj in list(test_mod.__dict__.items()):
            if inspect.isfunction(obj) and obj_name.startswith("test"):
                if getattr(obj, "_ztest_fixture", None) is not None:
                    continue
                # The pytest protocol __test__=False — the object is excluded from
                # collection (template Base* classes, helper functions). getattr with
                # the default True; None does not steal it.
                if getattr(obj, "__test__", True) is False:
                    continue
                if inspect.isgeneratorfunction(obj) or inspect.isasyncgenfunction(obj):
                    raise ZtestUnsupportedApiError(
                        f"test function {obj_name!r} in {rel_file} is a "
                        f"{'async-generator' if inspect.isasyncgenfunction(obj) else 'generator'} function: "
                        f"its body never executes (collection rejection)"
                    )
                # The conftest hook pytest_pycollect_makeitem may take an attribute
                # into custom collection (data-driven). pluggy semantics: a wrapper
                # hook (hookwrapper=True) is final: its force_result(None) suppresses
                # the default collection (pytest 9: items are skipped when the hook forces None). A plain hook returning None is "no opinion", the
                # chain/default continues.
                if pcmi_hooks:
                    _custom: Any = None
                    _decided = False
                    for _h in pcmi_hooks:
                        _custom = _drive_pcmi_hook(_h, _mod_collector, obj_name, obj)
                        if getattr(_h, "_ztest_hookimpl_wrapper", False) or inspect.isgeneratorfunction(_h):
                            _decided = True  # wrapper-final (None = suppression)
                            break
                        if _custom is not None:
                            _decided = True
                            break
                    if _decided:
                        if _custom is not None:
                            items.extend(
                                _wrap_custom_collected(_custom, rel_file, filepath, test_mod, mod_marks, obj_name)
                            )
                        continue  # None from the wrapper — the attribute is suppressed
                fn_marks = mod_marks + _norm_marks(getattr(obj, "pytestmark", []))
                _gt_marks = _generate_tests_marks(gen_tests_hooks, obj, None, test_mod, cfg, filepath, rel_file, obj_name, layers=fixture_layers, extra_marks=fn_marks)
                items.extend(_expand_parametrize(obj_name, rel_file, None, filepath, obj, None, test_mod, fn_marks, fixture_layers, extra_param_marks=_gt_marks, mark_split=(len(mod_marks), 0)))
            elif inspect.isclass(obj):
                is_ut = issubclass(obj, unittest.TestCase) and obj is not unittest.TestCase and obj is not unittest.IsolatedAsyncioTestCase
                is_pytest_cls = obj_name.startswith("Test") and not is_ut
                if not (is_ut or is_pytest_cls):
                    continue
                # __test__=False on a class — the whole class is outside collection
                # (inherited; an explicit __test__=True on a subclass re-enables it —
                # getattr walks the MRO).
                if getattr(obj, "__test__", True) is False:
                    continue
                # Abstract classes (inspect.isabstract) — pytest does not collect them
                # at all, neither TestCase nor pytest-style (a pytest 9.0.3 probe: a bare
                # ABC TestTranslatorBase is skipped).
                if inspect.isabstract(obj):
                    continue
                cls_own_marks = _norm_marks(getattr(obj, "pytestmark", []))
                cls_marks = mod_marks + cls_own_marks
                cls_layers = fixture_layers + [_extract_fixtures_from_obj(obj)]
                ut_had_test_methods = False
                for m_name in dir(obj):
                    if not m_name.startswith("test"):
                        continue
                    # A descriptor with an exploding __get__ must not fail the class
                    # collection.
                    try:
                        m_obj = getattr(obj, m_name, None)
                    except Exception:
                        continue
                    # A callable-attribute proxy with an exploding __getattr__ (the
                    # LocalProxy class) — the _ztest_fixture mark is tried under
                    # protection; a proxy cannot
                    # carry a ztest fixture.
                    try:
                        _fx_mark = getattr(m_obj, "_ztest_fixture", None)
                    except Exception:
                        _fx_mark = None
                    if not callable(m_obj) or _fx_mark is not None:
                        continue
                    # isgeneratorfunction explodes on proxies from inside inspect
                    # (getattr __name__ in _signature_is_functionlike) — a proxy is not
                    # a generator function; its failure at call time is a test-level
                    # concern, not a collection one.
                    try:
                        _is_gen = inspect.isgeneratorfunction(m_obj) or inspect.isasyncgenfunction(m_obj)
                    except Exception:
                        _is_gen = False
                    if _is_gen:
                        raise ZtestUnsupportedApiError(
                            f"test method {obj_name}.{m_name!r} in {rel_file} is a "
                            f"{'async-generator' if inspect.isasyncgenfunction(m_obj) else 'generator'} function: "
                            f"its body never executes (collection rejection)"
                        )
                    try:
                        _m_pytestmark = getattr(m_obj, "pytestmark", [])
                    except Exception:
                        _m_pytestmark = []
                    all_m = cls_marks + _norm_marks(_m_pytestmark)
                    if is_ut:
                        nodeid = f"{rel_file}::{obj_name}::{m_name}"
                        items.append(TestItemSpec(nodeid, m_name, filepath, m_obj, obj, test_mod, all_m, {}, {}, is_unittest=True))
                        ut_had_test_methods = True
                    else:
                        _gt_marks = _generate_tests_marks(gen_tests_hooks, m_obj, obj, test_mod, cfg, filepath, rel_file, m_name, layers=cls_layers, extra_marks=all_m)
                        items.extend(_expand_parametrize(m_name, rel_file, obj_name, filepath, m_obj, obj, test_mod, all_m, cls_layers, extra_param_marks=_gt_marks, mark_split=(len(mod_marks), len(cls_own_marks))))
                # A unittest.TestCase subclass without test_* methods but with runTest —
                # the pytest/unittest loader collects runTest (getTestCaseNames: the
                # hasattr(runTest) fallback; in 3.11+ TestCase has no runTest — the
                # attribute's presence = a user definition).
                if is_ut and not ut_had_test_methods:
                    _rt = getattr(obj, "runTest", None)
                    if _rt is not None and callable(_rt):
                        nodeid = f"{rel_file}::{obj_name}::runTest"
                        items.append(TestItemSpec(nodeid, "runTest", filepath, _rt, obj, test_mod, cls_marks, {}, {}, is_unittest=True))
    except BaseException:
        tb_str = traceback.format_exc()
        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<collection>", loc=f"{rel_file}:1", error="CollectionError", msg=tb_str, tb=tb_str)
        fin_failed = _safe_session_finalize(1)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=0, deselected=0, status="COLLECTION_ERROR", duration_ms=0)
        return 2

    for it in items:
        it.node.config = cfg

    # Run pytest_collection_modifyitems hook on conftest chain
    node_list = [it.node for it in items]
    session_stub.items = node_list
    try:
        for cf_mod in conftest_mods:
            hook_cmi = _invocable_conf_hook(cf_mod, "pytest_collection_modifyitems")
            if hook_cmi is not None:
                sig = inspect.signature(hook_cmi)
                kw: dict[str, Any] = {}
                for pn in sig.parameters:
                    if pn == "session":
                        kw[pn] = session_stub
                    elif pn == "config":
                        kw[pn] = cfg
                    elif pn == "items":
                        kw[pn] = node_list
                _flush_ipc_now()  # the flush barrier before the user hook
                _check_conf_hook_result("pytest_collection_modifyitems", hook_cmi(**kw))
    except BaseException:
        tb_str = traceback.format_exc()
        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<collection_modifyitems>", loc=f"{rel_file}:1", error="CollectionError", msg=tb_str, tb=tb_str)
        fin_failed = _safe_session_finalize(1)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=0, deselected=0, status="COLLECTION_ERROR", duration_ms=0)
        return 2

    node_map = {id(it.node): it for it in items}
    items = [node_map[id(n)] for n in node_list if id(n) in node_map]
    for it in items:
        it.markers = list(it.node.own_markers)

    # 4. Filter by ::target, -k and -m
    k_expr = str(options.get("k_expr") or "").strip()
    m_expr = str(options.get("m_expr") or "").strip()
    selected: list[TestItemSpec] = []
    deselected = 0

    for it in items:
        if target_filter:
            suffix = "::".join(target_filter)
            rel_part = it.nodeid[len(rel_file) + 2 :] if it.nodeid.startswith(f"{rel_file}::") else it.nodeid
            if not (rel_part == suffix or rel_part.startswith(f"{suffix}[") or rel_part.startswith(f"{suffix}::")):
                deselected += 1
                continue
        if k_expr:
            # pytest -k matches over the whole nodeid (parent names — Dir/File/Class
            # — are part of the keyword chain), plus the item's marker names:
            # `-k slow` selects `@pytest.mark.slow` tests whose nodeid has no
            # "slow" in it.
            _hay = _k_haystack(it.nodeid, it.node.own_markers, it.cls)
            if not _eval_bool_expr(k_expr, lambda tok, h=_hay: tok.lower() in h):
                deselected += 1
                continue
        if m_expr:
            mark_names = {m.name for m in it.node.own_markers}
            if not _eval_bool_expr(m_expr, lambda tok: tok in mark_names):
                deselected += 1
                continue
        selected.append(it)

    # COLLECTION_DONE closes the collection window — the normal per-test budget afterwards; sent unconditionally. Invariants: COLLECTION-DONE-GATE.
    # It also carries the post-selection size (`collected`): the supervisor keeps
    # it as the file's collected count, so a worker that dies later without
    # FILE_DONE still reports what was collected instead of 0.
    ipc.send_event("COLLECTION_DONE", file=rel_file, collected=len(selected))

    if options.get("collect_only", False):
        # --collect-only always lists the collected nodeids: -q trims the
        # progress output, never the listing (pytest prints the nodeids and
        # the collected count under -q as well).
        if not options.get("llm_mode", False):
            for i_b in range(0, len(selected), 100):
                batch_ids = [it.nodeid for it in selected[i_b : i_b + 100]]
                ipc.send_event("COLLECT_BATCH", nodeids=batch_ids)
        fin_failed = _safe_session_finalize(0)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=0, deselected=deselected, collected=len(selected), status="COLLECTED", duration_ms=0)
        return 0 if fin_failed == 0 else 1

    if not selected:
        dur_ms = (_REAL_MONOTONIC_NS() - start_ns) // 1_000_000
        fin_failed = _safe_session_finalize(0)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=fin_failed, skipped=0, deselected=deselected, status="NO_TESTS_SELECTED", duration_ms=dur_ms)
        return 0 if fin_failed == 0 else 1

    # 5. Execute selected tests — delegated to the stdlib lifecycle (ADR-005).
    tmp_factory = TmpPathFactory(base_dir=options.get("sandbox"))
    passed = 0
    failed = 0
    skipped = 0
    maxfail = int(options.get("maxfail") or 0)
    capture_output = not bool(options.get("no_capture", False))

    current_cls: type | None = None
    # pytest.exit() fired inside a test — aborts the remaining tests of this file.
    exited_during_test = False
    # KeyboardInterrupt inside a test/teardown — the file stops (fail-closed).
    interrupted_during_test = False
    module_setup_done = False
    ctx = _FileRunCtx(
        rel_file=rel_file,
        ipc=ipc,
        options=options,
        budgets=_budgets,
        conftest_mods=conftest_mods,
        fixture_layers=fixture_layers,
        test_mod=test_mod,
        tmp_factory=tmp_factory,
        per_test_timeout=per_test_timeout)
    # ini filterwarnings apply per item (pytest per-item catch_warnings); parsed
    # once per file. An ImportError of a dotted category — warning+skip (pytest);
    # AttributeError (module present, class missing) — pytest makes it a fatal
    # UsageError; here — a file-level CollectionError naming the filter,
    # not a worker crash.
    try:
        if "_warn_filters_early" in locals():
            ctx.warn_filters = _warn_filters_early  # parsed before the module import
        else:
            ctx.warn_filters = _parse_warn_filters(cfg.getini("filterwarnings"))  # type: ignore[attr-defined]
    except AttributeError as exc:
        tb_str = traceback.format_exc()
        ipc.send_event(
            "TEST_FAIL",
            nodeid=f"{rel_file}::<collection>",
            loc=f"{rel_file}:1",
            error="CollectionError",
            msg=f"filterwarnings: failed to resolve category: {exc}\n{tb_str}",
            tb=tb_str)
        ipc.send_event("FILE_DONE", file=rel_file, passed=0, failed=1, skipped=0, deselected=0, collected=0, status="COLLECTED", duration_ms=0)
        return 1
    try:
        _xs = cfg.getini("xfail_strict")
        ctx.xfail_strict = str(_xs).strip().lower() in ("1", "true", "yes", "on") if not isinstance(_xs, bool) else _xs
    except Exception:
        ctx.xfail_strict = False

    try:
        # xunit setup_module(test_mod) once before first item
        sm_mod = getattr(test_mod, "setup_module", None)
        if callable(sm_mod):
            _ztest_lifecycle_kind_guard(sm_mod, "setup_module")
            _budgets.phase("setup_module")
            try:
                _sm_mod_res = _call_arity_aware(sm_mod, test_mod)
                _ztest_lifecycle_result_guard(_sm_mod_res, "setup_module")
                _budgets.raise_if_fired("setup_module")
            finally:
                _budgets.phase_off()
        # The unittest canon setUpModule() (no arguments).
        sum_mod = getattr(test_mod, "setUpModule", None)
        if callable(sum_mod):
            _ztest_lifecycle_kind_guard(sum_mod, "setUpModule")
            _budgets.phase("setUpModule")
            try:
                _sum_res = _call_arity_aware(sum_mod, test_mod)
                _ztest_lifecycle_result_guard(_sum_res, "setUpModule")
                _budgets.raise_if_fired("setUpModule")
            finally:
                _budgets.phase_off()
        module_setup_done = True

        for _sel_idx, item in enumerate(selected):
            # Teardown hooks get the real next item of the file (pytest's nextitem
            # semantics), None on the last one.
            global _ZT_NEXT_NODE
            _ZT_NEXT_NODE = selected[_sel_idx + 1].node if _sel_idx + 1 < len(selected) else None
            if _TEARDOWN_INTERRUPTED:
                _ = _consume_teardown_interrupt()
                interrupted_during_test = True
                break
            if item.cls is not current_cls:
                if current_cls is not None:
                    failed = _close_class_lifecycle(ctx, current_cls, failed)
                    if _consume_teardown_interrupt():
                        interrupted_during_test = True
                        break
                current_cls = item.cls

            # Marker-level skip / skipif / xfail decision (body never runs).
            skip_reason: str | None = None
            xfail_mark: Mark | None = None
            for m in item.node.own_markers:
                if m.name == "skip":
                    skip_reason = str(m.args[0]) if m.args else str(m.kwargs.get("reason", "skipped"))
                    break
                if m.name == "skipif":
                    # pytest 9: a bare @pytest.mark.skipif — skipped (condition=None).
                    if not m.args and "condition" not in m.kwargs:
                        skip_reason = str(m.kwargs.get("reason") or "unconditional skip")
                        break
                    _conds = list(m.args) if m.args else [m.kwargs.get("condition", False)]
                    _matched = False
                    for cond in _conds:
                        if isinstance(cond, str):
                            cond = bool(eval(cond, _skipif_eval_ns(item.module)))
                        if bool(cond):
                            _matched = True
                            break
                    if _matched:
                        skip_reason = str(m.kwargs.get("reason") or "skipif")
                        break
                if m.name == "xfail":
                    cond = m.args[0] if m.args else m.kwargs.get("condition", True)
                    if isinstance(cond, str):
                        cond = bool(eval(cond, _skipif_eval_ns(item.module)))
                    if cond:
                        xfail_mark = m

            if item.cls is not None and getattr(item.cls, "__unittest_skip__", False):
                skip_reason = getattr(item.cls, "__unittest_skip_why__", "skipped")
            # Proxy objects with an exploding __getattr__ (the LocalProxy class) are
            # probed with safe semantics — an attribute probe must not take down the
            # whole module/class (pytest's safe_getattr catches Exception).
            try:
                _skip_flag = getattr(item.func, "__unittest_skip__", False)
                _skip_why = getattr(item.func, "__unittest_skip_why__", "skipped")
            except Exception:
                _skip_flag, _skip_why = False, "skipped"
            if _skip_flag:
                skip_reason = _skip_why

            if skip_reason is not None:
                skipped += 1
                # Always emit: the supervisor's per-test inactivity deadline re-arms on
                # TEST_SKIP regardless of verbosity (the verbose gate).
                ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason=skip_reason, duration_ms=0)
                continue

            # xfail(run=False): the body is never executed; reported skipped.
            if xfail_mark is not None and not bool(xfail_mark.kwargs.get("run", True)):
                skipped += 1
                ipc.send_event("TEST_SKIP", nodeid=item.nodeid, reason="xfail (run=False): not executed", duration_ms=0)
                continue

            passed, failed, skipped, exited, interrupted = _run_one_item(
                ctx, item, passed, failed, skipped, xfail_mark, capture_output
            )
            if exited:
                exited_during_test = True
            if interrupted:
                interrupted_during_test = True

            if maxfail > 0 and failed >= maxfail:
                break
            if exited_during_test:
                break
            if interrupted_during_test:
                break
    except BaseException as mod_exc:
        _budgets.disarm()
        _fired_at_setup = _budgets.consume_fired()
        if isinstance(mod_exc, _TestTimeoutError) or _fired_at_setup:
            # the timeout dominates (including a swallowed timeout surfacing as a skip)
            failed += 1
            tb_s = traceback.format_exc()
            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<module_setup>", loc=rel_file, error="TestTimeoutError", msg=str(mod_exc), tb=tb_s)
        elif isinstance(mod_exc, (Skipped, unittest.SkipTest)):
            # A skip at module setup = a skip of all the module's tests (stdlib
            # _handleModuleFixture), not a fail.
            _skip_reason = str(mod_exc) or "skipped in module setup"
            for _it in selected:
                skipped += 1
                ipc.send_event("TEST_SKIP", nodeid=_it.nodeid, reason=_skip_reason)
        else:
            failed += 1
            tb_s = traceback.format_exc()
            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<module_setup>", loc=rel_file, error=type(mod_exc).__name__, msg=str(mod_exc), tb=tb_s)
    finally:
        # The whole finalization under a last-resort containment — no failure may
        # take down FILE_DONE (FAILED ≠ CRASHED).
        try:
            if current_cls is not None:
                failed = _close_class_lifecycle(ctx, current_cls, failed)
            if ctx.prev_resolver is not None:
                for sc in ("module", "session"):
                    for err_tb in ctx.prev_resolver.teardown_scope(sc):
                        failed += 1
                        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<{sc}_teardown>", loc=rel_file, error="TeardownError", msg=err_tb, tb=err_tb)
            try:
                _budgets.phase("teardown:module")
            except _TestTimeoutError:
                # A pending-storm inside the phase arm itself — absorb and retry
                _budgets.absorb_timeout()
                _budgets.phase("teardown:module")
            try:
                if module_setup_done:
                    tm_mod = getattr(test_mod, "teardown_module", None)
                    if callable(tm_mod):
                        try:
                            _ztest_lifecycle_kind_guard(tm_mod, "teardown_module")
                            _tm_mod_res = _call_arity_aware(tm_mod, test_mod)
                            _ztest_lifecycle_result_guard(_tm_mod_res, "teardown_module")
                            _budgets.raise_if_fired("teardown:module")
                        except KeyboardInterrupt:
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<teardown_module>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                            _mark_teardown_interrupt()
                        except BaseException as _tm_exc:
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<teardown_module>", loc=rel_file, error="TeardownError", msg=tb_s, tb=tb_s)
                    # The unittest canon tearDownModule() must run: silently
                    # skipping it is an exit-code lie with a failing module teardown.
                    tdm_mod = getattr(test_mod, "tearDownModule", None)
                    if callable(tdm_mod):
                        try:
                            _ztest_lifecycle_kind_guard(tdm_mod, "tearDownModule")
                            _tdm_res = _call_arity_aware(tdm_mod, test_mod)
                            _ztest_lifecycle_result_guard(_tdm_res, "tearDownModule")
                            _budgets.raise_if_fired("teardown:module")
                        except KeyboardInterrupt:
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<tearDownModule>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                            _mark_teardown_interrupt()
                        except BaseException as _tdm_exc:
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<tearDownModule>", loc=rel_file, error="TeardownError", msg=tb_s, tb=tb_s)
                # Module cleanups run outside the module_setup_done gate: addModuleCleanup
                # exists to release resources captured before setUpModule failed (stdlib
                # _handleModuleFixture does the same); a cleanup failure is a file-level
                # fail. The stack is drained manually with a per-cleanup budget (heartbeat
                # + re-arm, LIFO to the end) — stdlib doModuleCleanups() continues only
                # after an Exception, and _TestTimeoutError (a BaseException) would drop
                # the whole rest.
                try:
                    import unittest.case as _uc

                    _mc = getattr(_uc, "_module_cleanups", None)
                except Exception:
                    _mc = None
                if _mc is None:
                    # fall back to stdlib if the private stack is unavailable
                    try:
                        unittest.doModuleCleanups()
                    except KeyboardInterrupt:
                        _budgets.absorb_timeout()
                        failed += 1
                        tb_s = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<doModuleCleanups>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                        _mark_teardown_interrupt()
                    except BaseException as _dmc_exc:
                        _budgets.absorb_timeout()
                        failed += 1
                        tb_s = traceback.format_exc()
                        ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<doModuleCleanups>", loc=rel_file, error="ModuleCleanupError", msg=tb_s, tb=tb_s)
                else:
                    while _mc:
                        _mc_fn, _mc_args, _mc_kwargs = _mc.pop()
                        try:
                            _budgets.phase("teardown:module_cleanup")
                        except _TestTimeoutError:
                            # A storm raise at the phase boundary itself — absorb and retry
                            _budgets.absorb_timeout()
                            _budgets.phase("teardown:module_cleanup")
                        try:
                            _flush_ipc_now()  # the flush barrier before the user module-cleanup
                            _mc_fn(*_mc_args, **_mc_kwargs)
                        except KeyboardInterrupt:
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<doModuleCleanups>", loc=rel_file, error="TeardownInterrupt", msg=tb_s, tb=tb_s)
                            _mark_teardown_interrupt()
                        except _TestTimeoutError as _tte:
                            # Absorb before formatting/IPC — otherwise a recurring SIGALRM
                            # storm lives through format_exc/send_event and can kill the
                            # channel before FILE_DONE.
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<doModuleCleanups>", loc=rel_file, error="ModuleCleanupTimeout", msg=str(_tte), tb=tb_s)
                        except BaseException:
                            _budgets.absorb_timeout()
                            failed += 1
                            tb_s = traceback.format_exc()
                            ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<doModuleCleanups>", loc=rel_file, error="ModuleCleanupError", msg=tb_s, tb=tb_s)
                        finally:
                            _budgets.phase_off()
                try:
                    _budgets.raise_if_fired("teardown:module")
                except _TestTimeoutError as _mtd:
                    # A swallowed module-teardown timeout surfaces here — record FAIL, do
                    # not let it escape (otherwise FILE_DONE never leaves → CRASHED).
                    _budgets.absorb_timeout()
                    failed += 1
                    tb_s = traceback.format_exc()
                    ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<module_teardown>", loc=rel_file, error="TeardownTimeout", msg=str(_mtd), tb=tb_s)
            finally:
                _budgets.phase_off()
            try:
                failed = _run_session_finalize(failed)
            except BaseException as _sf_exc:
                # A session-finalize failure must not take down FILE_DONE
                if isinstance(_sf_exc, _TestTimeoutError):
                    _budgets.absorb_timeout()
                failed += 1
                tb_s = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<session_finalize>", loc=rel_file, error="SessionFinalizeError", msg=tb_s, tb=tb_s)
            finally:
                try:
                    _budgets.phase("teardown:tmp_factory_cleanup")
                except _TestTimeoutError:
                    _budgets.absorb_timeout()
                    _budgets.phase("teardown:tmp_factory_cleanup")
                try:
                    tmp_factory.cleanup()
                    _budgets.raise_if_fired("teardown:tmp_factory_cleanup")
                except BaseException as _tf_exc:
                    _budgets.absorb_timeout()
                    failed += 1
                    tb_s = traceback.format_exc()
                    ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<tmp_factory_cleanup>", loc=rel_file, error="TmpFactoryCleanupError", msg=tb_s, tb=tb_s)
                finally:
                    _budgets.phase_off()
            _ = _consume_teardown_interrupt()
        except BaseException as _fin_exc:
            try:
                _budgets.absorb_timeout()
                failed += 1
                tb_s = traceback.format_exc()
                ipc.send_event("TEST_FAIL", nodeid=f"{rel_file}::<finalization>", loc=rel_file, error="FinalizationError", msg=tb_s, tb=tb_s)
            except BaseException:
                pass

    total_ms = (_REAL_MONOTONIC_NS() - start_ns) // 1_000_000
    status = "PASSED" if failed == 0 else "FAILED"
    # The root capture handler's buffer is cleared between the zygote process's
    # files — otherwise a monotonic memory growth on logging suites.
    if _ROOT_CAP_HANDLER is not None:
        _ROOT_CAP_HANDLER.clear()
    ipc.send_event(
        "FILE_DONE",
        file=rel_file,
        passed=passed,
        failed=failed,
        skipped=skipped,
        deselected=deselected,
        # The session summary reports the selection size in every mode, not
        # only under --collect-only: a run of N tests must not advertise
        # "collected": 0.
        collected=len(selected),
        status=status,
        duration_ms=total_ms)
    return 0 if failed == 0 else 1


_SHIM_BASENAME = "ztest_shim.py"
_TB_FRAME_RE = re.compile(r'^\s+File "(?P<path>[^"]+)", line \d+, in (?P<func>.*)$')
# The tail of a captured stream carried into a failure record. A test can log
# megabytes; the report keeps the end of the stream, which is what explains
# the failure.
_CAPTURE_REPORT_CHARS = 65536


def _is_shim_frame(path: str) -> bool:
    return os.path.basename(path) == _SHIM_BASENAME


def _strip_shim_frames(text: str) -> str:
    """Remove the runner's own frames from a formatted traceback.

    A report shows the test's frames; the kernel's runtime path (its code is
    unpacked into a per-process temporary directory) must not surface in user
    output. Frame blocks are the ``File "..."`` header plus the indented
    source/caret lines that belong to it.
    """
    if _SHIM_BASENAME not in text:
        return text
    kept: list[str] = []
    dropping = False
    for line in text.splitlines():
        match = _TB_FRAME_RE.match(line)
        if match is not None:
            dropping = _is_shim_frame(match.group("path"))
            if dropping:
                continue
        elif dropping:
            if line[:1] in (" ", "\t") and line.strip():
                continue
            dropping = False
        kept.append(line)
    return "\n".join(kept)


def _capture_sections(out_txt: str, err_txt: str) -> str:
    """The captured-output block pytest appends to a failure report."""
    chunks: list[str] = []
    for label, text in (("stdout", out_txt), ("stderr", err_txt)):
        if not text:
            continue
        if len(text) > _CAPTURE_REPORT_CHARS:
            text = "... output truncated ...\n" + text[-_CAPTURE_REPORT_CHARS:]
        if not text.endswith("\n"):
            text += "\n"
        chunks.append(f"{f' Captured {label} call ':-^80}\n{text}")
    return "\n".join(chunks)


def _sanitize_frame_data(data: dict[str, Any]) -> dict[str, Any]:
    """Scrub the runner's internal paths from the textual frame fields."""
    for key in ("tb", "msg"):
        value = data.get(key)
        if isinstance(value, str) and _SHIM_BASENAME in value:
            data[key] = _strip_shim_frames(value)
    return data


def _report_failure(ipc: IpcChannel, item: TestItemSpec, exc: BaseException, out_txt: str, err_txt: str, dur_ms: int, error_name: str | None = None) -> None:
    tb_entries = traceback.extract_tb(exc.__traceback__)
    filtered = [f for f in tb_entries if not _is_shim_frame(f.filename)]
    last_frame = filtered[-1] if filtered else (tb_entries[-1] if tb_entries else None)
    loc = f"{last_frame.filename}:{last_frame.lineno}" if last_frame else str(item.filepath)
    short_tb = "\n".join(
        f"  File \"{f.filename}\", line {f.lineno}, in {f.name}\n    {f.line or ''}"
        for f in (filtered or tb_entries)[-6:]
    )
    msg = str(exc)
    if len(msg) > 256_000:
        msg = msg[:256_000] + "... [truncated]"
    full_tb = f"{short_tb}\n{type(exc).__name__}: {msg}"
    captured = _capture_sections(out_txt, err_txt)
    if captured:
        # Captured output belongs to the failure context: without it a
        # failure caused by what the test printed is undebuggable.
        msg = f"{msg}\n\n{captured}"
        if len(msg) > 256_000:
            msg = msg[:256_000] + "... [truncated]"
        full_tb = f"{full_tb}\n{captured}"
    ipc.send_event(
        "TEST_FAIL",
        nodeid=item.nodeid,
        loc=loc,
        error=error_name or type(exc).__name__,
        msg=msg,
        # The traceback is cut under the 16 MiB payload ceiling — a giant
        # traceback must not convert FAILED into CRASHED (PayloadTooLarge).
        tb=full_tb[:12_000_000],
        stdout_tail=out_txt[-500:] if out_txt else "",
        stderr_tail=err_txt[-500:] if err_txt else "",
        duration_ms=dur_ms)


# ============================================================================
# 9. Zygote Server & Clean-Spawn Entrypoints
# ============================================================================

def _connect_ipc_fd199(ipc_sock_path: str) -> int:
    global _ipc_active_fd
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    s.connect(ipc_sock_path)
    raw_fd = s.fileno()
    if raw_fd != IPC_TARGET_FD:
        os.dup2(raw_fd, IPC_TARGET_FD)
        s.close()
    else:
        s.detach()
    os.set_inheritable(IPC_TARGET_FD, False)
    # fd 199 is non-blocking — send_event works via poll(POLLOUT) + deadline.
    os.set_blocking(IPC_TARGET_FD, False)
    try:
        import fcntl
        _fd_flags = fcntl.fcntl(IPC_TARGET_FD, fcntl.F_GETFD)
        fcntl.fcntl(IPC_TARGET_FD, fcntl.F_SETFD, _fd_flags | fcntl.FD_CLOEXEC)
    except (ImportError, OSError):
        pass
    _ipc_active_fd = IPC_TARGET_FD
    return IPC_TARGET_FD


def _zygote_thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except Exception:
        return 2  # fail-closed: treat unreadable /proc/self/task as multi-threaded


def run_zygote_server(ctrl_sock_path: str, ipc_sock_path: str, supervisor_pid: int) -> None:
    # setpgid runs before connect — the accept-time invariant getpgid(zygote)==zygote is checked via SO_PEERCRED; a mismatch rejects the zygote. Invariants: ZYGOTE-GROUP-IDENTITY.
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    # Re-arming the zygote's pdeathsig — covers the exec→python-startup window; prctl is idempotent. Invariants: ADR-004-1.
    _arm_pdeathsig(supervisor_pid)
    # Prevent native BLAS/OpenMP libraries imported by root conftest.py from spawning
    # background thread pools before fork(), keeping the zygote single-threaded.
    for _thr_env in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ.setdefault(_thr_env, "1")
    # 1. Pre-import clean whitelist + optional packages + root conftest.py if single-threaded
    _is_pytest_engine = os.environ.get("ZTEST_ENGINE") == "pytest"
    if _is_pytest_engine:
        try:
            import pluggy  # noqa: F401
            import _pytest  # noqa: F401
            import pytest  # noqa: F401
        except Exception:
            pass
    else:
        install_pytest_modules()
    cwd_path = Path(os.getcwd()).resolve()
    if str(cwd_path) not in sys.path:
        sys.path.insert(0, str(cwd_path))
    # On small suites (files <= 8) the supervisor sets ZTEST_PRELOAD_HEAVY=0
    # so the zygote skips optional pre-imports that would not amortize.
    # Third-party preloading is opt-in via --preload / ZTEST_PRELOAD_PKGS
    # (comma-separated dotted module names); by default only the standard
    # library modules below are pre-imported before gc.freeze().
    if os.environ.get("ZTEST_PRELOAD_HEAVY") != "0":
        for _opt_pkg in os.environ.get("ZTEST_PRELOAD_PKGS", "").split(","):
            _opt_pkg = _opt_pkg.strip()
            if not _opt_pkg or not all(_part.isidentifier() for _part in _opt_pkg.split(".")):
                continue
            try:
                __import__(_opt_pkg)
            except Exception:
                pass

    # Pre-import the shim's lazy stdlib modules, otherwise every worker pays their
    # import anew (configparser ~5-8 ms/file, tomllib 20-30 ms, sysconfig,
    # fractions; importlib.metadata — up to 50-70 ms per file for entry_points
    # lexer/codec scans). Idempotent stdlib, no network/file side effects: done
    # in the zygote before gc.freeze, the workers get it via COW. An import error
    # does not kill the zygote (the per-file path imports it itself). Gated by the
    # same files<=8 threshold as the optional package preload above.
    if os.environ.get("ZTEST_PRELOAD_HEAVY") != "0":
        for _std_mod in ("configparser", "tomllib", "sysconfig", "fractions", "importlib.metadata"):
            try:
                __import__(_std_mod)
            except Exception:
                pass

    # Preload root conftests into the zygote; only KeyboardInterrupt propagates, each preload uses a unique module name, and failed loads are evicted from sys.modules. Invariants: ZYGOTE-PRELOAD.
    if not _is_pytest_engine:
        for _cf_idx, rel_cf in enumerate(("conftest.py", "tests/conftest.py")):
            cf_candidate = cwd_path / rel_cf
            if not cf_candidate.exists():
                continue
            try:
                abs_cf = str(cf_candidate.resolve())
                cf_mod = load_module_with_ast_cache(cf_candidate, _package_qualified_name(cf_candidate, f"_ztest_conftest_preload_{_cf_idx}"), cache_dir=cwd_path / ".ztest_cache")
            except KeyboardInterrupt:
                raise
            except BaseException:
                continue  # the zygote survives; the worker reports the real conftest error per-file
            if _zygote_thread_count() == 1:
                _PRELOADED_CONFTESTS[abs_cf] = cf_mod
    faulthandler.register(signal.SIGQUIT, file=sys.stderr, all_threads=True)
    # ZYGOTE-SWEEP-EXCLUDED: the zygote's pdeathsig is armed at the TOP of run_zygote_server
    # (before install_pytest_modules and the conftest preload) — see the
    # comment there.

    # Prewarm the ini cache before gc.freeze() — the workers get it via COW and
    # pay nothing per file for ini options (~16 ms: the configparser import +
    # re.compile). A prewarm error is silenced: the per-file path reads the ini
    # itself. (Conftest fixture introspection is per-file against the live
    # module — there is no introspection prewarm.)
    try:
        _warm_ini_options_cache(cwd_path)
    except BaseException:
        pass

    # Return the glibc free chunks to the OS before freeze — the workers get a
    # trimmed address space via COW (see _trim_free_arenas).
    _trim_free_arenas()
    # The zygote's pdeathsig is armed at the TOP of run_zygote_server
    # (before install_pytest_modules and the conftest preload) — see the
    # comment there.

    gc.collect()
    gc.freeze()

    zygote_pid = os.getpid()
    ctrl_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ctrl_sock.connect(ctrl_sock_path)
    ctrl_file = ctrl_sock.makefile("rw", encoding="utf-8", buffering=1)
    if _zygote_thread_count() > 1:
        # Never os.fork() from a multi-threaded zygote: an inherited mutex locked
        # by a background thread at fork time can deadlock the child worker.
        # Signal the supervisor to fall back cleanly to per-file exec mode.
        try:
            ctrl_file.write(f"MULTITHREADED\t{zygote_pid}\n")
            ctrl_file.flush()
            ctrl_file.close()
            ctrl_sock.close()
        except Exception:
            pass
        return
    ctrl_file.write(f"READY\t{zygote_pid}\n")
    ctrl_file.flush()

    active_children: set[int] = set()
    exited_children: set[int] = set()  # pinned zombies: WNOWAIT-detected, not yet reaped
    draining = False

    def _poll_children() -> None:
        # A WNOWAIT mark + a group sweep without waitpid — the zombie pins pid/pgid from reuse, the status is taken by the REAP handshake. Invariants: ZOMBIE-PINNING.
        for cpid in list(active_children):
            if cpid in exited_children:
                continue
            try:
                info = os.waitid(os.P_PID, cpid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if info is not None:
                    try:
                        os.kill(-cpid, signal.SIGKILL)
                    except OSError:
                        pass
                    exited_children.add(cpid)
            except ChildProcessError:
                active_children.discard(cpid)
                exited_children.discard(cpid)

    def _poll_children_drain() -> None:
        # During DRAIN the supervisor is gone: fully reap exited children so the loop can terminate.
        for cpid in list(active_children):
            try:
                info = os.waitid(os.P_PID, cpid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if info is not None:
                    try:
                        os.kill(-cpid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        os.waitpid(cpid, 0)
                    except ChildProcessError:
                        pass
                    active_children.discard(cpid)
                    exited_children.discard(cpid)
            except ChildProcessError:
                active_children.discard(cpid)
                exited_children.discard(cpid)

    while True:
        if draining:
            _poll_children_drain()
        else:
            _poll_children()
        if draining and not active_children:
            break

        # Non-blocking wait when draining so WNOHANG can reap exiting workers
        if draining:
            rlist, _, _ = select.select([ctrl_sock], [], [], 0.02)
            if not rlist:
                continue

        line = ctrl_file.readline()
        if not line:
            # EOF on the ctrl socket = the supervisor's death → killpg of all live workers (including non-accepted) → _exit; SIGCHLD=SIG_IGN is forbidden. Invariants: ADR-004-2.
            for cpid in list(active_children):
                try:
                    os.killpg(cpid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                _real_stderr.write("ztest-shim: supervisor EOF; killed all worker groups\n")
                _real_stderr.flush()
            except Exception:
                pass
            os._exit(0)

        line = line.rstrip("\r\n")
        if line == "DRAIN":
            draining = True
            try:
                ctrl_file.write("DRAINING\n")
                ctrl_file.flush()
            except BrokenPipeError:
                break
            continue

        if line.startswith("REAP\t"):
            target_pid = int(line.split("\t")[1])
            _poll_children()
            if target_pid not in active_children:
                active_children.discard(target_pid)
                exited_children.discard(target_pid)
                # Never return 0 on an unknown/lost child — the REAP answer is -1
                ctrl_file.write(f"REAPED\t{target_pid}\t-1\n")
                ctrl_file.flush()
                continue
            # Bounded REAP 2.5s → SIGKILL → a 1s final reap; the answer is -1. Invariants: ZYGOTE-REAP-BOUNDED.
            status: int = -1
            reap_until = _REAL_MONOTONIC() + 2.5
            _reap_spins = 0
            while True:
                try:
                    info = os.waitid(os.P_PID, target_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    info = None
                    status = -1
                    break
                if info is not None:
                    try:
                        os.kill(-target_pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        _wpid, status = os.waitpid(target_pid, 0)
                    except ChildProcessError:
                        status = -1
                    break
                if _REAL_MONOTONIC() >= reap_until:
                    try:
                        os.kill(-target_pid, signal.SIGKILL)
                        os.kill(target_pid, signal.SIGKILL)
                    except OSError:
                        pass
                    final_until = _REAL_MONOTONIC() + 1.0
                    while _REAL_MONOTONIC() < final_until:
                        try:
                            info = os.waitid(os.P_PID, target_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        except ChildProcessError:
                            info = None
                            break
                        if info is not None:
                            try:
                                _wpid, status = os.waitpid(target_pid, 0)
                            except ChildProcessError:
                                status = -1
                            break
                        time.sleep(0.005)
                    break
                _reap_spins += 1
                time.sleep(0.0002 if _reap_spins <= 10 else 0.002)
            active_children.discard(target_pid)
            exited_children.discard(target_pid)
            ctrl_file.write(f"REAPED\t{target_pid}\t{status}\n")
            ctrl_file.flush()
            continue

        if line.startswith("FORK\t"):
            # The JSON FORK protocol; a parse error → an ERR line, the zygote keeps serving (fallback: kill zygote → exec retry). Invariants: FORK-JSON-FRAMING.
            try:
                fork_req = json.loads(line[len("FORK\t"):])
                if not isinstance(fork_req, dict):
                    raise ValueError("FORK request is not a JSON object")
                slot_ipc_sock = fork_req["slot"]
                filepath_str = fork_req["file"]
                options = fork_req["opts"]
                if not isinstance(slot_ipc_sock, str) or not isinstance(filepath_str, str) or not isinstance(options, dict):
                    raise ValueError("FORK request fields have wrong types")
            except Exception as fork_err:
                try:
                    ctrl_file.write("ERR\t" + json.dumps(f"{type(fork_err).__name__}: {fork_err}") + "\n")
                    ctrl_file.flush()
                except Exception:
                    pass
                continue
            if _zygote_thread_count() > 1:
                try:
                    ctrl_file.write("ERR\t\"zygote became multi-threaded; refusing os.fork()\"\n")
                    ctrl_file.flush()
                except Exception:
                    pass
                break
            # A pipe handshake confirming setpgid — FORKED is sent only after the
            # child has certainly entered its own group (otherwise the accept-time
            # checkPeerPgid could catch the parent's pgid → a false CRASHED). If the
            # child died before the handshake — read returns b'' on pipe close.
            spg_r, spg_w = os.pipe()
            child_pid = os.fork()
            if child_pid > 0:
                os.close(spg_w)
                try:
                    os.setpgid(child_pid, child_pid)
                except OSError:
                    pass
                try:
                    os.read(spg_r, 1)
                except OSError:
                    pass
                os.close(spg_r)
                active_children.add(child_pid)
                ctrl_file.write(f"FORKED\t{child_pid}\n")
                ctrl_file.flush()
            else:
                os.close(spg_r)
                try:
                    os.setpgid(0, 0)
                except OSError:
                    pass
                try:
                    os.write(spg_w, b"1")
                except OSError:
                    pass
                os.close(spg_w)
                # The worker's signal semantics = plain python (pytest parity):
                # SIGPIPE stays SIG_IGN (inherited from the supervisor through
                # execve/fork). A write to a closed pipe/socket — BrokenPipeError,
                # not death by signal 13.
                # Shell pipelines of tests are protected by subprocess
                # restore_signals=True (the CPython default: SIGPIPE/SIGXFZ/SIGXFSZ →
                # SIG_DFL in the child) — no separate reset at the worker level.
                try:
                    ctrl_file.close()
                    ctrl_sock.close()
                except Exception:
                    pass
                # register_at_fork before opening fd 199 — the "fd open, handler not
                # registered" window is fully closed.
                os.register_at_fork(after_in_child=_close_ipc_in_grandchild)
                ipc_fd = _connect_ipc_fd199(slot_ipc_sock)
                _close_extra_fds({0, 1, 2, ipc_fd})
                random.seed(os.urandom(16))
                ipc = IpcChannel(ipc_fd, os.getpid())
                # The worker re-arms pdeathsig onto the zygote post-fork; the group is killed by the kernel (cascade + EOF watch) and the Zig batch watchdog. Invariants: ADR-004-1.
                _arm_pdeathsig(zygote_pid)
                rc = run_test_file(filepath_str, options, ipc)
                try:
                    os.close(ipc_fd)
                except OSError:
                    pass
                os._exit(rc)


def run_clean_spawn_worker(ipc_sock_path: str, filepath_str: str, opts_json: str, supervisor_pid: int) -> None:
    # Clean-spawn worker: pdeathsig is armed pre-execve in the Zig child branch
    # (spawn_arm.zig) and survives exec, so there is no re-arm here. This entry
    # point creates the process group (ARM-ORDER); pdeathsig = SIGKILL —
    # the kernel kills the worker GIL-independently (see INVARIANTS.md).
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    # Clean-spawn is armed pre-exec (no re-arm needed). Invariants: ADR-004-1.
    # SIGPIPE: python semantics (SIG_IGN), as in the zygote path
    # (both modes must match); the shell pipelines of tests are protected by
    # subprocess restore_signals=True.
    # register_at_fork before any code able to fork (install_pytest_modules may
    # pull multiprocessing imports) and before connect.
    os.register_at_fork(after_in_child=_close_ipc_in_grandchild)
    if os.environ.get("ZTEST_ENGINE") != "pytest":
        install_pytest_modules()
    # clean-spawn forks no workers, but this process's PSS = the file's PSS: the
    # shim's compilation garbage + the AST pre-imports are returned to the OS right
    # after install_pytest_modules() (before the user module import and its AST).
    _trim_free_arenas()
    ipc_fd = _connect_ipc_fd199(ipc_sock_path)
    _close_extra_fds({0, 1, 2, ipc_fd})
    options = json.loads(opts_json)
    ipc = IpcChannel(ipc_fd, os.getpid())
    rc = run_test_file(filepath_str, options, ipc)
    try:
        os.close(ipc_fd)
    except OSError:
        pass
    os._exit(rc)


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--zygote":
        run_zygote_server(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif len(sys.argv) >= 6 and sys.argv[1] == "--worker":
        run_clean_spawn_worker(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        sys.stderr.write("Usage: ztest_shim.py [--zygote <ctrl_sock> <ipc_sock> <sup_pid> | --worker <ipc_sock> <file> <opts_json> <sup_pid>]\n")
        sys.exit(2)
