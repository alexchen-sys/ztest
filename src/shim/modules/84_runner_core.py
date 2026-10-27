# ztest shim source module: 84_runner_core
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
# ============================================================================
# Call-boundary parity (Bench-100: freezegun runner identity, astroid depth)
#
# freezegun/api.py decides whether a frozen ticker survives into the next
# test by scanning inspect.stack() for a frame whose module name starts with
# '_pytest.' (its "are we inside the pytest runner" probe). The native shim
# calls the test function from ztest_shim, so that probe finds nothing and
# the frozen time is reset (freezegun::test_time_freeze_stays_across_tests).
# The final call therefore goes through a one-line trampoline whose
# f_globals['__name__'] is '_pytest.runner' — exactly the frame pytest's own
# runner supplies. The trampoline is created lazily (never at import time:
# --engine=pytest must keep the real _pytest.runner untouched) and its code
# object carries the shim's own filename, so failure reports keep filtering
# it as an internal runner frame.
_ZT_CALL_TRAMPOLINE: Any = None
_ZT_CALL_TRAMPOLINE_SRC = "def _ztest_call_trampoline(fn, kwargs):\n    return fn(**kwargs)\n"


def _ztest_call_trampoline() -> Any:
    """The single call-boundary trampoline (its frame's module is '_pytest.runner')."""
    global _ZT_CALL_TRAMPOLINE
    tramp = _ZT_CALL_TRAMPOLINE
    if tramp is not None:
        return tramp
    mod = sys.modules.get("_pytest.runner")
    tramp = getattr(mod, "_ztest_call_trampoline", None) if mod is not None else None
    if tramp is None:
        if mod is None:
            mod = types.ModuleType("_pytest.runner")
            mod.__doc__ = "ztest shim: call-boundary trampoline (freezegun parity)."
            try:
                sys.modules["_pytest.runner"] = mod
                _parent = sys.modules.get("_pytest")
                if _parent is not None and getattr(_parent, "runner", None) is None:
                    _parent.runner = mod
            except Exception:
                pass
        # A fixed source string compiled under the shim's own filename: the
        # frame lives in the module's namespace (freezegun sees
        # '_pytest.runner'), while failure reports strip it as runner machinery.
        _tramp_file = globals().get("__file__") or _SHIM_BASENAME
        exec(compile(_ZT_CALL_TRAMPOLINE_SRC, _tramp_file, "exec"), mod.__dict__)
        tramp = mod.__dict__["_ztest_call_trampoline"]
    _ZT_CALL_TRAMPOLINE = tramp
    return tramp


@contextlib.contextmanager
def _shim_recursion_compensation(depth: int = 25) -> Any:
    """+`depth` on sys.getrecursionlimit() for one test body, restored in finally.

    astroid's recursion-guard test expects the interpreter's guard to trip at
    pytest's stack depth; the shim's stack is shallower, so the call would
    finish. The limit is raised right before the call and restored afterwards
    even on exceptions — never at module level: the process-wide limit must
    not leak between items or files.
    """
    prev = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(prev + depth)
    except Exception:
        # An unusable limit is not a test failure — run uncompensated.
        yield
        return
    try:
        yield
    finally:
        try:
            sys.setrecursionlimit(prev)
        except Exception:
            pass


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
        # The final call runs inside the recursion compensation (astroid stack
        # depth parity) — restored in finally, i.e. also on a test failure.
        with _shim_recursion_compensation():
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
                # Through the '_pytest.runner' trampoline (freezegun inspects the
                # stack for a '_pytest.*' frame before it keeps the frozen clock
                # across tests).
                _fn_res = _ztest_call_trampoline()(bound_fn, call_kwargs)
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


