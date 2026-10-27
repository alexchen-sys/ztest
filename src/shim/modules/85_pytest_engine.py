# ztest shim source module: 85_pytest_engine
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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
    # A native worker delegating a pytester file still carries the shim's pytest
    # stubs in sys.modules — give them back to upstream pytest before importing.
    _ensure_real_pytest_modules()
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


# Files/conftests seen so far that need pytest's own `pytester`/`testdir`
# machinery (execnet's suite imports the self-hosting `pytester` plugin from
# its conftest). Cached per path: a suite shares its conftests.
_PYTESTER_SCAN_CACHE: dict[str, bool] = {}
_PYTESTER_TOKEN_RE = re.compile(r"\b(?:pytester|testdir|runpytest|runpytest_inprocess)\b")


def _mentions_pytester(path: Path) -> bool:
    """Whether a source file references the pytester plugin/fixtures (cached)."""
    key = str(path)
    cached = _PYTESTER_SCAN_CACHE.get(key)
    if cached is not None:
        return cached
    found = False
    try:
        found = bool(_PYTESTER_TOKEN_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except Exception:
        found = False
    _PYTESTER_SCAN_CACHE[key] = found
    return found


def _ztest_file_needs_pytester(filepath_str: str, options: dict[str, Any]) -> bool:
    """Whether this file has to run under real pytest (automatic pytester delegation).

    The native shim has no in-process pytest for `pytester` to drive, so such a
    file (or a conftest above it declaring `pytest_plugins = ["pytester"]`) is
    delegated to the existing `_ZtestPytestBridgePlugin` path — transparently,
    and only when real pytest is importable (otherwise the native engine runs
    the file exactly as before). Opt out with ZTEST_NO_PYTESTER_DELEGATION=1.
    """
    if os.environ.get("ZTEST_NO_PYTESTER_DELEGATION") == "1":
        return False
    try:
        raw_target = filepath_str.split("::", 1)[0] if "::" in filepath_str else filepath_str
        filepath = Path(raw_target).resolve()
        rootpath = Path(options.get("rootdir") or os.getcwd()).resolve()
    except Exception:
        return False
    chain: list[Path] = [filepath]
    probe = filepath.parent
    while True:
        chain.append(probe / "conftest.py")
        if probe == rootpath or probe.parent == probe:
            break
        probe = probe.parent
    if not any(_mentions_pytester(p) for p in chain):
        return False
    # Upstream pytest availability is resolved without importing it: in the
    # native engine sys.modules['pytest'] is the shim's stub, so `import pytest`
    # would answer the wrong question. The probe also stays cheap.
    try:
        if importlib.machinery.PathFinder.find_spec("pytest") is None:
            return False
    except Exception:
        return False
    return True


