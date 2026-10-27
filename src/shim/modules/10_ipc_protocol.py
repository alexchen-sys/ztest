# ztest shim source module: 10_ipc_protocol
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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


