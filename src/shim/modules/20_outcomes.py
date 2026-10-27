# ztest shim source module: 20_outcomes
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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


class TestTimeoutError(BaseException):
    """Per-test timeout: BaseException — never swallowed by a bare `except Exception` and never matched by xfail/skip.

    Public name (UX audit A3): the error reaches the user as ``TestTimeoutError``, not
    the private ``_TestTimeoutError`` spelling of an internal class.
    """
    pass


# Backward-compatibility alias: the whole shim (and any third party importing the
# internal name) keeps resolving; the class object and its ``__name__`` are the
# single public one above.
_TestTimeoutError = TestTimeoutError


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


