# ztest shim source module: 30_marks_and_helpers
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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


