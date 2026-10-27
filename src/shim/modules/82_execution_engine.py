# ztest shim source module: 82_execution_engine
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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


