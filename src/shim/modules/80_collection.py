# ztest shim source module: 80_collection
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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

