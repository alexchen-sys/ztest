# ztest shim source module: 60_fixture_engine
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
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


def _addopts_selection_filters(ini_dir: Path) -> tuple[str, str, int]:
    """`-k` / `-m` / `--maxfail` parsed out of ini `addopts` (pytest appends addopts to argv).

    The supervisor's CLI carries no addopts channel (its bench protocol passes
    `-o addopts=` explicitly), so the shim recovers the selection filters the
    ini asks for: poetry-core's `addopts = "-m 'not integration'"` must deselect
    its integration tests under BOTH engines — including the bridge's
    `-o addopts=` retry, which by construction drops the ini addopts.

    Token grammar, in the order pytest itself resolves it:

    1. exact spellings — `-k`/`--keyword`, `-m`/`--markexpr`, `--maxfail`,
       `-x` and their `--opt=value` forms. These are consumed first, so the
       maxfail options can never be mistaken for a `-m` expression;
    2. attached short forms `-k<expr>` / `-m<expr>` (`-kfast`, `-mnot`), which
       argparse also accepts. A token that carries a second hyphenated word
       (`-keep-logs`, `-mypy-args`) is an option name, not a filter expression,
       and `-maxfail` is the long maxfail option spelled with one dash — both
       are refused, so a plugin flag is never reconstructed as a selection
       filter. The plugin registry is the only authority that could resolve the
       remaining ambiguity, and it is not loaded on this fast path; the cost of
       being wrong here (deselecting everything) is worse than ignoring a
       filter, so the ambiguous forms are left to pytest.

    Returns (k_expr, m_expr, maxfail); any of them may be empty/0.
    """
    try:
        raw = _ini_options_cached(ini_dir).get("addopts") or ""
    except Exception:
        return ("", "", 0)
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(part) for part in raw)
    if not isinstance(raw, str) or not raw.strip():
        return ("", "", 0)
    try:
        argv = shlex.split(raw)
    except ValueError:
        argv = raw.split()
    k_expr = ""
    m_expr = ""
    maxfail = 0

    def attached_value(tok: str, flag: str) -> str | None:
        """Value of an attached short selection option, or None if it is an option name."""
        if not tok.startswith(flag) or len(tok) <= len(flag):
            return None
        if tok.startswith("-maxfail"):
            # pytest's maxfail option with one dash instead of two: an option,
            # not the `-m` expression "axfail".
            return None
        value = tok[len(flag):]
        if "-" in value:
            return None
        return value

    idx = 0
    while idx < len(argv):
        tok = argv[idx]
        nxt = argv[idx + 1] if idx + 1 < len(argv) else ""
        if tok in ("-k", "--keyword") and nxt:
            k_expr = nxt
            idx += 2
            continue
        if tok in ("-m", "--markexpr") and nxt:
            m_expr = nxt
            idx += 2
            continue
        if tok == "--maxfail" and nxt:
            try:
                maxfail = int(nxt)
            except ValueError:
                maxfail = 0
            idx += 2
            continue
        if tok == "-x":
            maxfail = 1
        elif tok.startswith("--maxfail="):
            try:
                maxfail = int(tok.split("=", 1)[1])
            except ValueError:
                pass
        elif tok.startswith("--keyword="):
            k_expr = tok.split("=", 1)[1]
        elif tok.startswith("--markexpr="):
            m_expr = tok.split("=", 1)[1]
        elif (value := attached_value(tok, "-k")) is not None:
            k_expr = value
        elif (value := attached_value(tok, "-m")) is not None:
            m_expr = value
        idx += 1
    return (k_expr.strip(), m_expr.strip(), maxfail)


def _with_ini_addopts_filters(filepath_str: str, options: dict[str, Any]) -> dict[str, Any]:
    """Fold ini addopts `-k`/`-m`/`--maxfail` into the per-file options.

    A value passed on the CLI always wins (pytest's own precedence: addopts come
    first, an explicit CLI flag later overrides them), so the ini value is used
    only where the supervisor sent nothing.
    """
    cli_k = str(options.get("k_expr") or "").strip()
    cli_m = str(options.get("m_expr") or "").strip()
    cli_mf = options.get("maxfail") or 0
    try:
        cli_mf = int(cli_mf)
    except Exception:
        cli_mf = 0
    if cli_k and cli_m and cli_mf:
        return options
    try:
        raw_target = filepath_str.split("::", 1)[0] if "::" in filepath_str else filepath_str
        filepath = Path(raw_target).resolve()
        rootpath = Path(options.get("rootdir") or os.getcwd()).resolve()
        # The ini the shim's ConfigStub reads for this file (same discovery).
        ini_dir = _discover_ini_dir(filepath.parent) or rootpath
    except Exception:
        return options
    ini_k, ini_m, ini_mf = _addopts_selection_filters(ini_dir)
    if not (ini_k or ini_m or ini_mf):
        return options
    out = dict(options)
    if not cli_k and ini_k:
        out["k_expr"] = ini_k
    if not cli_m and ini_m:
        out["m_expr"] = ini_m
    if not cli_mf and ini_mf:
        out["maxfail"] = ini_mf
    return out


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


# pytest11 entry points: pytest loads every installed plugin declaring this
# group at startup, so a package that ships its fixtures as an entry-point
# plugin (platformdirs' own pytest11 plugin is the Bench-100 case) has them
# under pytest and none under the native shim, which only walked ini
# `plugins=` and conftest `pytest_plugins`. Enumerated once per process
# (importlib.metadata scans every sys.path distribution) and prewarmed in
# the zygote before gc.freeze().
_PYTEST11_ENTRYPOINT_CACHE: list[tuple[str, str]] | None = None
# Plugins whose presence in the shim's process would fight the supervisor
# (worker pools, coverage writing, output decoration) — never auto-loaded.
_PYTEST11_SKIP_HINTS = ("xdist", "cov", "sugar", "html")


def _pytest11_entry_points() -> list[tuple[str, str]]:
    """(name, module) of installed pytest11 entry points, minus the skipped ones."""
    global _PYTEST11_ENTRYPOINT_CACHE
    cached = _PYTEST11_ENTRYPOINT_CACHE
    if cached is not None:
        return cached
    found: list[tuple[str, str]] = []
    if os.environ.get("ZTEST_NO_ENTRYPOINT_PLUGINS") != "1" and os.environ.get("ZTEST_PYTEST11_FULL") == "1":
        try:
            from importlib import metadata as _importlib_metadata

            _eps = list(_importlib_metadata.entry_points(group="pytest11"))
        except Exception:
            _eps = []
        for _ep in _eps:
            try:
                _ep_name = str(getattr(_ep, "name", "") or "").strip()
                _ep_value = str(getattr(_ep, "value", "") or "").strip()
            except Exception:
                continue
            # value is "module" or "module:attr"
            _ep_mod = _ep_value.split(":", 1)[0].strip()
            if not _ep_name or not _ep_mod:
                continue
            _lower = _ep_name.lower()
            if any(_hint in _lower for _hint in _PYTEST11_SKIP_HINTS):
                continue
            found.append((_ep_name, _ep_mod))
    if not found and os.environ.get("ZTEST_NO_ENTRYPOINT_PLUGINS") != "1" and os.environ.get("ZTEST_PYTEST11_FULL") != "1":
        # Default path: a hand scan of sys.path metadata directories. Building
        # importlib.metadata Distribution objects for a typical venv costs ~24 ms
        # per worker process (each file is a fresh process) — too much for the
        # per-file budget. Only entry_points.txt files are opened, and only the
        # [pytest11] section is parsed. ZTEST_PYTEST11_FULL=1 keeps the
        # importlib.metadata scan for exotic layouts (zip/egg installs).
        found = _scan_pytest11_entry_points_fast()
    _PYTEST11_ENTRYPOINT_CACHE = found
    return found


def _scan_pytest11_entry_points_fast() -> list[tuple[str, str]]:
    """(name, module) of pytest11 entry points found by reading dist metadata directly."""
    found: list[tuple[str, str]] = []
    seen_dirs: set[str] = set()
    for _entry in list(sys.path):
        if not _entry:
            continue
        _dir = _entry
        if _dir in seen_dirs:
            continue
        seen_dirs.add(_dir)
        try:
            _names = os.listdir(_dir)
        except OSError:
            continue
        for _name in _names:
            _lower = _name.lower()
            if not (_lower.endswith(".dist-info") or _lower.endswith(".egg-info")):
                continue
            _ep_path = os.path.join(_dir, _name, "entry_points.txt")
            try:
                with open(_ep_path, "r", encoding="utf-8", errors="replace") as _fh:
                    _text = _fh.read(1 << 16)
            except OSError:
                continue
            if "pytest11" not in _text:
                continue
            _section = ""
            for _line in _text.splitlines():
                _line = _line.strip()
                if not _line or _line[0] in "#;":
                    continue
                if _line.startswith("[") and _line.endswith("]"):
                    _section = _line[1:-1].strip()
                    continue
                if _section != "pytest11" or "=" not in _line:
                    continue
                _ep_name, _, _ep_value = _line.partition("=")
                _ep_name = _ep_name.strip()
                _ep_mod = _ep_value.strip().split(":", 1)[0].strip()
                if not _ep_name or not _ep_mod:
                    continue
                _lower_name = _ep_name.lower()
                if any(_hint in _lower_name for _hint in _PYTEST11_SKIP_HINTS):
                    continue
                found.append((_ep_name, _ep_mod))
    return found


def _entrypoint_plugin_in_project(mod_name: str, rootpath: Path) -> bool:
    """Whether a pytest11 plugin's module lives inside the project being run.

    The entry-point list is global to the environment; only the project's own
    plugin is loaded by default so an unrelated installed plugin (pytest-timeout,
    anyio, ...) never enters a worker under the shim's pytest stub. Resolution
    goes through find_spec: it imports the parent package only. Memoized per
    (module, rootdir) — find_spec walks sys.path and imports the parent package,
    which must not be paid per file.
    """
    _cache_key = (mod_name, str(rootpath))
    _cached = _PYTEST11_PROJECT_CACHE.get(_cache_key)
    if _cached is not None:
        return _cached
    _verdict = _entrypoint_plugin_in_project_uncached(mod_name, rootpath)
    _PYTEST11_PROJECT_CACHE[_cache_key] = _verdict
    return _verdict


# Verdict cache for _entrypoint_plugin_in_project (module, rootdir) → bool.
_PYTEST11_PROJECT_CACHE: dict[tuple[str, str], bool] = {}


def _entrypoint_plugin_in_project_uncached(mod_name: str, rootpath: Path) -> bool:
    try:
        _spec = importlib.util.find_spec(mod_name)
    except Exception:
        return False
    if _spec is None:
        return False
    _origin = getattr(_spec, "origin", None) or ""
    if not _origin or _origin in ("built-in", "frozen"):
        _locs = list(getattr(_spec, "submodule_search_locations", None) or [])
        _origin = str(_locs[0]) if _locs else ""
    if not _origin:
        return False
    try:
        return Path(_origin).resolve().is_relative_to(rootpath)
    except Exception:
        return False


def _warm_pytest11_entrypoint_cache() -> None:
    """Prewarm the entry-point list in the zygote (COW-shared; errors silenced)."""
    try:
        _pytest11_entry_points()
    except BaseException:
        pass


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
            # Delegates to the installed pytest-mock (platformdirs' pytest11
            # suite), stub fallback when the package is absent.
            mk = _make_mocker_fixture()
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


