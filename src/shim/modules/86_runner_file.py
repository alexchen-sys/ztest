# ztest shim source module: 86_runner_file
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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
        # ini addopts -k/-m/--maxfail join the per-file options before either
        # engine dispatches (poetry-core: addopts = "-m 'not integration'").
        options = _with_ini_addopts_filters(filepath_str, options)
        _engine = str(options.get("engine") or os.environ.get("ZTEST_ENGINE") or "native").strip()
        if _engine == "pytest":
            return _run_test_file_pytest_engine(filepath_str, options, ipc)
        # The native engine's only failure mode for such a file is a missing
        # pytester fixture, so a file that asks for pytest's pytester machinery
        # is delegated to the real-pytest bridge. There is no `--engine=auto`
        # CLI value: the delegation is a property of the native engine, and the
        # explicit engine choices stay `native` and `pytest`.
        if _engine == "native" and _ztest_file_needs_pytester(filepath_str, options):
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
    # The active config for fixtures that delegate to installed plugins
    # (mocker → pytest_mock.MockerFixture); one file per process.
    global _ZT_ACTIVE_CONFIG
    _ZT_ACTIVE_CONFIG = cfg
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

        # pytest11 entry-point plugins — pytest registers them before any
        # conftest, so they go first: their fixtures stay the weakest layer,
        # exactly like upstream. Only the project's own plugin is auto-loaded
        # (the Bench-100 platformdirs case): importing every installed plugin
        # would drag arbitrary third-party code into each worker under the
        # shim's pytest stub, so the rest is opt-in via ZTEST_PYTEST11_ALL=1.
        _pytest11_all = os.environ.get("ZTEST_PYTEST11_ALL") == "1"
        _entrypoint_plugin_ids: set[int] = set()
        for _ep_name, _ep_mod_name in _pytest11_entry_points():
            if not (_pytest11_all or _entrypoint_plugin_in_project(_ep_mod_name, rootpath)):
                continue
            try:
                _ep_mod = _load_plugin(_ep_mod_name)
            except KeyboardInterrupt:
                raise
            except BaseException:
                continue  # a broken entry-point plugin must not take the file down
            if _ep_mod is None:
                continue
            _entrypoint_plugin_ids.add(id(_ep_mod))
            _expanded_confs.append((Path(getattr(_ep_mod, "__file__", None) or _ep_mod_name), _ep_mod))
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
            # A pytest11 entry-point plugin is third-party code the shim does not
            # own: its hooks may use APIs the shim deliberately does not model
            # (fail-closed for conftests). Such a plugin is dropped whole — with
            # a note — instead of failing the file: no plugin is exactly the
            # pre-existing behaviour.
            _is_ep_plugin = id(cf_mod) in _entrypoint_plugin_ids
            _layers_before = len(fixture_layers)
            conftest_mods.append(cf_mod)
            try:
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
            except KeyboardInterrupt:
                raise
            except BaseException:
                if not _is_ep_plugin:
                    raise
                if conftest_mods and conftest_mods[-1] is cf_mod:
                    conftest_mods.pop()
                del fixture_layers[_layers_before:]
                if os.environ.get("ZTEST_DEBUG_ENTRYPOINTS") == "1":
                    try:
                        _ep_skip_msg = traceback.format_exc().strip().splitlines()[-1]
                        sys.__stderr__.write(
                            f"ztest: pytest11 plugin {getattr(cf_mod, '__name__', str(cf_path))} "
                            f"skipped for this file: {_ep_skip_msg}\n"
                        )
                        sys.__stderr__.flush()
                    except Exception:
                        pass

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
