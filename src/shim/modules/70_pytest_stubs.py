# ztest shim source module: 70_pytest_stubs
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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


# sys.modules entries written by install_pytest_modules() (name → object); see
# _ensure_real_pytest_modules.
_INSTALLED_STUB_MODULES: dict[str, types.ModuleType] = {}


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
    # Bookkeeping for the reverse direction: a native worker that delegates a
    # pytester file to the real-pytest bridge has to hand sys.modules back to
    # upstream pytest, and it must remove exactly these entries (see
    # _ensure_real_pytest_modules).
    global _INSTALLED_STUB_MODULES
    _INSTALLED_STUB_MODULES = {
        _n: _m
        for _n, _m in sys.modules.items()
        if _n == "pytest" or _n == "_pytest" or _n.startswith("_pytest.")
    }


def _ensure_real_pytest_modules() -> None:
    """Undo install_pytest_modules() so the bridge can drive upstream pytest.

    The native engine installs the shim's pytest stubs process-wide; the
    automatic pytester delegation (native engine -> real-pytest bridge) runs the
    file through real pytest, so the stub entries are removed first. Nothing is imported here: when no
    upstream pytest exists, the bridge reports the usual EngineError. A pytest
    engine run already has the real package in sys.modules (the stubs were never
    installed) and this is a no-op.
    """
    if os.environ.get("ZTEST_ENGINE") == "pytest" or not _INSTALLED_STUB_MODULES:
        return
    try:
        if importlib.machinery.PathFinder.find_spec("pytest") is None:
            return
    except Exception:
        return
    for _n, _stub in list(_INSTALLED_STUB_MODULES.items()):
        if sys.modules.get(_n) is _stub:
            try:
                del sys.modules[_n]
            except KeyError:
                pass
    _INSTALLED_STUB_MODULES.clear()


