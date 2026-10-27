# ztest shim source module: 40_builtin_fixtures
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
# ============================================================================
# 4. Builtin Fixtures: MonkeyPatch, CapLog, CapSys, Mocker, TmpPath, Request
# ============================================================================

class MonkeyPatch:
    _notset = object()

    def __init__(self) -> None:
        self._setattr: list[tuple[Any, str, Any]] = []
        self._setitem: list[tuple[Any, Any, Any]] = []
        self._cwd: str | None = None
        self._syspath: list[str] | None = None

    @classmethod
    @contextlib.contextmanager
    def context(cls) -> Any:
        # pytest implements MonkeyPatch.context as a classmethod —
        # the shim must accept a call on the class.
        m = cls()
        try:
            yield m
        finally:
            m.undo()

    @staticmethod
    def _resolve_dotted(target: str) -> tuple[Any, str]:
        """Resolves multi-level dotted paths like 'pkg.mod.Class.attr'."""
        if "." not in target:
            raise TypeError(f"Must provide dotted path in setattr({target!r})")
        parts = target.split(".")
        attr_name = parts[-1]
        prefix_parts = parts[:-1]
        # Find longest importable module prefix
        mod_obj = None
        split_idx = 0
        for i in range(len(prefix_parts), 0, -1):
            cand = ".".join(prefix_parts[:i])
            try:
                mod_obj = importlib.import_module(cand)
                split_idx = i
                break
            except Exception:
                continue
        if mod_obj is None:
            mod_obj = importlib.import_module(prefix_parts[0])
            split_idx = 1
        cur = mod_obj
        for sub in prefix_parts[split_idx:]:
            cur = getattr(cur, sub)
        return cur, attr_name

    def setattr(self, target: Any, name: Any = _notset, value: Any = _notset, raising: bool = True) -> None:
        if value is self._notset:
            if not isinstance(target, str):
                raise TypeError("use setattr(target, name, value) or setattr('module.attr', value)")
            value = name
            target_obj, attr_name = self._resolve_dotted(target)
            name = attr_name
        else:
            target_obj = importlib.import_module(target) if isinstance(target, str) else target
        oldval = getattr(target_obj, name, self._notset)
        if raising and oldval is self._notset:
            raise AttributeError(f"{target_obj!r} has no attribute {name!r}")
        self._setattr.append((target_obj, name, oldval))
        setattr(target_obj, name, value)

    def delattr(self, target: Any, name: Any = _notset, raising: bool = True) -> None:
        if name is self._notset:
            target_obj, attr_name = self._resolve_dotted(target)
            name = attr_name
        else:
            target_obj = importlib.import_module(target) if isinstance(target, str) else target
        oldval = getattr(target_obj, name, self._notset)
        if raising and oldval is self._notset:
            raise AttributeError(f"{target_obj!r} has no attribute {name!r}")
        self._setattr.append((target_obj, name, oldval))
        if oldval is not self._notset:
            delattr(target_obj, name)

    def setitem(self, dic: Any, name: Any, value: Any) -> None:
        self._setitem.append((dic, name, dic.get(name, self._notset)))
        dic[name] = value

    def delitem(self, dic: Any, name: Any, raising: bool = True) -> None:
        if name not in dic:
            if raising:
                raise KeyError(name)
            return
        self._setitem.append((dic, name, dic.get(name, self._notset)))
        del dic[name]

    def setenv(self, name: str, value: str, prepend: str | None = None) -> None:
        if prepend and name in os.environ:
            value = f"{value}{prepend}{os.environ[name]}"
        self.setitem(os.environ, name, str(value))

    def delenv(self, name: str, raising: bool = True) -> None:
        self.delitem(os.environ, name, raising=raising)

    def syspath_prepend(self, path: Any) -> None:
        if self._syspath is None:
            self._syspath = list(sys.path)
        sys.path.insert(0, str(path))

    def chdir(self, path: Any) -> None:
        if self._cwd is None:
            self._cwd = os.getcwd()
        os.chdir(path)

    def undo(self) -> None:
        for obj, name, val in reversed(self._setattr):
            if val is self._notset:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, val)
        self._setattr.clear()
        for dic, name, val in reversed(self._setitem):
            if val is self._notset:
                dic.pop(name, None)
            else:
                dic[name] = val
        self._setitem.clear()
        if self._syspath is not None:
            sys.path[:] = self._syspath
            self._syspath = None
        if self._cwd is not None:
            os.chdir(self._cwd)
            self._cwd = None


class _CapLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []
        self.stream = io.StringIO()
        self.setFormatter(logging.Formatter("%(levelname)-8s %(name)s:%(filename)s:%(lineno)d %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            self.stream.write(self.format(record) + "\n")
        except Exception:
            pass

    def clear(self) -> None:
        self.records.clear()
        self.stream = io.StringIO()


# pytest always keeps the root capture handler (a LogCaptureHandler on
# root, level=0) — without it logging.lastResort writes the warning to
# sys.stderr, which leaks into CliRunner.capture in CLI tests. Installed
# in run_test_file (the common entry of both modes: zygote-fork/clean-spawn).
_ROOT_CAP_HANDLER: "_CapLogHandler | None" = None


def _ensure_root_log_capture() -> None:
    global _ROOT_CAP_HANDLER
    if _ROOT_CAP_HANDLER is None:
        _ROOT_CAP_HANDLER = _CapLogHandler()
        _ROOT_CAP_HANDLER.setLevel(0)
        logging.getLogger().addHandler(_ROOT_CAP_HANDLER)


class LogCaptureFixture:
    def __init__(self) -> None:
        self._handler = _CapLogHandler()
        self._saved_levels: list[tuple[logging.Logger, int]] = []
        self._attached = False
        self._finalized = False

    def _attach(self) -> None:
        if not self._attached and not self._finalized:
            root = logging.getLogger()
            root.addHandler(self._handler)
            self._attached = True

    def _finalize(self) -> None:
        self._finalized = True
        for logger, lvl in reversed(self._saved_levels):
            logger.setLevel(lvl)
        self._saved_levels.clear()
        if self._attached:
            logging.getLogger().removeHandler(self._handler)
            self._attached = False

    @property
    def handler(self) -> _CapLogHandler:
        self._attach()
        return self._handler

    @property
    def records(self) -> list[logging.LogRecord]:
        self._attach()
        return self._handler.records

    @property
    def text(self) -> str:
        self._attach()
        return self._handler.stream.getvalue()

    @property
    def messages(self) -> list[str]:
        self._attach()
        return [r.getMessage() for r in self._handler.records]

    @property
    def record_tuples(self) -> list[tuple[str, int, str]]:
        self._attach()
        return [(r.name, r.levelno, r.getMessage()) for r in self._handler.records]

    def clear(self) -> None:
        self._handler.clear()

    def set_level(self, level: int | str, logger: str | None = None) -> None:
        self._attach()
        lvl = getattr(logging, level.upper()) if isinstance(level, str) else level
        target = logging.getLogger(logger)
        self._saved_levels.append((target, target.level))
        target.setLevel(lvl)

    @contextlib.contextmanager
    def at_level(self, level: int | str, logger: str | None = None) -> Any:
        self._attach()
        lvl = getattr(logging, level.upper()) if isinstance(level, str) else level
        target = logging.getLogger(logger)
        old = target.level
        target.setLevel(lvl)
        try:
            yield
        finally:
            target.setLevel(old)


CaptureResult = collections.namedtuple("CaptureResult", ["out", "err"])


class _CapturedTextIO(io.TextIOBase):
    """A text capture stream with a real .buffer (BytesIO-backed).

    io.StringIO has no .buffer — black reads sys.stdout.buffer and dies
    with AttributeError. The pytest CaptureIO analog (a TextIOWrapper over
    BytesIO): byte writes via .buffer and text writes via write() land in
    the same stream; getvalue() decodes utf-8/replace.

    fileno() returns the real stream's fd (1/stdout, 2/stderr) — fd-capture
    has already dup2'ed them into temp files; os.isatty(fileno()) and similar
    queries must work as under pytest fd-capture.
    """

    def __init__(self, fd: int = 1, tee: Any = None) -> None:
        self._raw = io.BytesIO()
        self._text = io.TextIOWrapper(self._raw, encoding="utf-8", errors="replace", write_through=True)
        self._fd = fd
        self._tee = tee

    def fileno(self) -> int:
        return self._fd

    @property
    def buffer(self) -> io.BytesIO:
        return self._raw

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def write(self, s: str) -> int:
        n = self._text.write(s)
        if self._tee is not None:
            try:
                self._tee.write(s)
                self._tee.flush()
            except Exception:
                pass
        return n

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        try:
            self._text.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return False

    def seek(self, pos: int, whence: int = 0) -> int:
        # CapSysFixture.readouterr() resets the stream via seek(0)/truncate(0) —
        # the operations hit the raw buffer (a text wrapper with write_through).
        self._text.flush()
        return self._raw.seek(pos, whence)

    def truncate(self, size: int | None = None) -> int:
        self._text.flush()
        return self._raw.truncate(size)

    def getvalue(self) -> str:
        self.flush()
        return self._raw.getvalue().decode("utf-8", "replace")


class _DontReadFromInput:
    """pytest DontReadFromInput — deliberately NOT io.IOBase (callers checking isinstance(sys.stdin, io.IOBase) skip under capture, as under pytest); read* raise
    OSError, .buffer returns self (pytest 9 behavior, probe-verified)."""

    @property
    def encoding(self) -> str:
        return getattr(sys.__stdin__, "encoding", None) or "utf-8"

    @property
    def buffer(self) -> "_DontReadFromInput":
        return self

    def read(self, size: int = -1) -> str:
        raise OSError("pytest: reading from stdin while output is captured!  Consider using `-s`.")

    readline = read

    def readlines(self, hint: int = -1) -> list[str]:
        raise OSError("pytest: reading from stdin while output is captured!  Consider using `-s`.")

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> str:
        return self.readline()

    # Parity with pytest DontReadFromInput (the TextIO surface): the
    # multiprocessing child bootstrap calls sys.stdin.close() (util._close_stdin)
    # — without it forked children die in _bootstrap.
    def close(self) -> None:
        pass

    @property
    def closed(self) -> Any:
        # pytest parity: DontReadFromInput.closed == None.
        return None

    # The exception type is io.UnsupportedOperation (both OSError and
    # ValueError), like pytest DontReadFromInput/TextIO.
    def flush(self) -> None:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, has no flush()")

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def seek(self, offset: int, whence: int = 0) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not seekable")

    def tell(self) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not seekable")

    def truncate(self, size: Any = None) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not writable")

    def write(self, s: str) -> int:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not writable")

    def writelines(self, lines: Any) -> None:
        raise io.UnsupportedOperation("ztest: redirected stdin is pseudofile, not writable")

    def __enter__(self) -> "_DontReadFromInput":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def fileno(self) -> int:
        raise io.UnsupportedOperation("redirected stdin is pseudofile, has no fileno()")


class CapSysFixture:
    # CaptureFixture[str] annotations run at
    # runtime — the class must be subscriptable.
    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(self, binary: bool = False, tee: bool = False) -> None:
        self._binary = binary
        self._prev_out = sys.stdout
        self._prev_err = sys.stderr
        self._out = _CapturedTextIO(fd=1, tee=self._prev_out if tee else None)
        self._err = _CapturedTextIO(fd=2, tee=self._prev_err if tee else None)
        sys.stdout = self._out
        sys.stderr = self._err

    def readouterr(self) -> CaptureResult:
        if self._binary:
            self._out.flush()
            self._err.flush()
            out_b = self._out._raw.getvalue()
            err_b = self._err._raw.getvalue()
            self._out.seek(0)
            self._out.truncate(0)
            self._err.seek(0)
            self._err.truncate(0)
            return CaptureResult(out_b, err_b)
        out = self._out.getvalue()
        err = self._err.getvalue()
        self._out.seek(0)
        self._out.truncate(0)
        self._err.seek(0)
        self._err.truncate(0)
        return CaptureResult(out, err)

    @contextlib.contextmanager
    def disabled(self) -> Any:
        """Restores the real underlying process stdout/stderr."""
        sys.stdout = _real_stdout
        sys.stderr = _real_stderr
        try:
            yield
        finally:
            sys.stdout = self._out
            sys.stderr = self._err

    def _finalize(self) -> None:
        sys.stdout = self._prev_out
        sys.stderr = self._prev_err


class CapFdFixture:
    """The ``capfd`` fixture: captures at the file-descriptor level.

    Output that never passes through ``sys.stdout``/``sys.stderr`` — raw
    ``os.write(1, ...)``, C extensions, child processes inheriting fd 1/2 —
    is captured here, which is exactly the difference from ``capsys``.

    The mechanism mirrors pytest's ``FDCapture``: the target fd is dup2'ed
    onto a temporary file, and ``sys.stdout``/``sys.stderr`` become text
    wrappers over that same file, so ``print()`` and fd writes land in one
    buffer in write order. ``readouterr()`` drains and truncates it. On
    finalize the unread tail is forwarded to the original fd — captured
    output of a failing test is not lost, it still reaches the failure
    report.
    """

    # CaptureFixture[str] annotations in suites run at runtime — the class
    # must be subscriptable.
    def __class_getitem__(cls, item: Any) -> Any:
        return cls

    def __init__(self, binary: bool = False) -> None:
        _flush_user_stdio()
        self._binary = binary
        self._prev_out = sys.stdout
        self._prev_err = sys.stderr
        self._files: dict[int, Any] = {}
        self._saved: dict[int, int | None] = {}
        self._streams: dict[int, Any] = {}
        for fd in (1, 2):
            try:
                os.fstat(fd)
            except OSError:
                # pytest parity: a closed target fd is pointed at /dev/null
                # instead of failing the test on an environment quirk.
                self._saved[fd] = None
                try:
                    os.dup2(os.open(os.devnull, os.O_RDWR), fd)
                except OSError:
                    pass
            else:
                self._saved[fd] = os.dup(fd)
            tf = tempfile.TemporaryFile(buffering=0)
            self._files[fd] = tf
            try:
                os.dup2(tf.fileno(), fd)
            except OSError:
                pass
            self._streams[fd] = io.TextIOWrapper(
                tf, encoding="utf-8", errors="replace", write_through=True, newline="")
        sys.stdout = self._streams[1]
        sys.stderr = self._streams[2]

    def _drain(self, fd: int) -> str:
        stream = self._streams.get(fd)
        tf = self._files.get(fd)
        if tf is None:
            return ""
        _flush_user_stdio()
        try:
            stream.flush()
        except Exception:
            pass
        try:
            # The fd and the file object share one open file description, so
            # seeking the object also rewinds the writing offset: after
            # truncate() new writes start at 0 instead of leaving a hole.
            tf.seek(0)
            data = tf.read()
            tf.seek(0)
            tf.truncate(0)
        except Exception:
            return b"" if getattr(self, "_binary", False) else ""
        if getattr(self, "_binary", False):
            return data if isinstance(data, bytes) else str(data).encode("utf-8", "replace")
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return str(data)

    def readouterr(self) -> CaptureResult:
        """Drain what has been captured so far and reset both buffers."""
        return CaptureResult(self._drain(1), self._drain(2))

    @contextlib.contextmanager
    def disabled(self) -> Any:
        """Temporarily hand fd 1/2 and the streams back to the process."""
        _flush_user_stdio()
        sys.stdout = self._prev_out
        sys.stderr = self._prev_err
        for fd in (1, 2):
            saved = self._saved.get(fd)
            if saved is not None:
                try:
                    os.dup2(saved, fd)
                except OSError:
                    pass
        try:
            yield
        finally:
            _flush_user_stdio()
            for fd in (1, 2):
                tf = self._files.get(fd)
                if tf is None:
                    continue
                try:
                    os.dup2(tf.fileno(), fd)
                except OSError:
                    pass
            sys.stdout = self._streams[1]
            sys.stderr = self._streams[2]

    def _finalize(self) -> None:
        # Collect before restoring: the unread tail is written back to the
        # original fd afterwards, so the item-level capture still sees it.
        rest = {fd: self._drain(fd) for fd in (1, 2)}
        if sys.stdout is self._streams[1]:
            sys.stdout = self._prev_out
        if sys.stderr is self._streams[2]:
            sys.stderr = self._prev_err
        for fd in (1, 2):
            saved = self._saved.get(fd)
            if saved is not None:
                self._saved[fd] = None
                try:
                    os.dup2(saved, fd)
                except OSError:
                    pass
                try:
                    os.close(saved)
                except OSError:
                    pass
            try:
                self._streams[fd].close()
            except Exception:
                pass
            tf = self._files.get(fd)
            if tf is not None:
                try:
                    tf.close()
                except Exception:
                    pass
            if rest.get(fd):
                try:
                    tail_val = rest[fd]
                    tail_bytes = tail_val if isinstance(tail_val, bytes) else str(tail_val).encode("utf-8", "replace")
                    os.write(fd, tail_bytes)
                except OSError:
                    pass


class LegacyPath:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def strpath(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __fspath__(self) -> str:
        return str(self._path)

    def __eq__(self, other: Any) -> bool:
        # py.path.local parity: equality by the string form with
        # LegacyPath, str and pathlib.Path.
        if isinstance(other, LegacyPath):
            return self._path == other._path
        if isinstance(other, (str, Path)):
            return str(self._path) == str(other)
        return NotImplemented

    def __hash__(self) -> int:
        # hash is consistent with __eq__ by the string form (lp == str(lp)).
        return hash(str(self._path))

    def join(self, *parts: str) -> LegacyPath:
        return LegacyPath(self._path.joinpath(*parts))

    def __truediv__(self, other: str) -> LegacyPath:
        # py.path.local supports the / operator — tests build paths as
        # fspath / "file.pdf".
        return LegacyPath(self._path / other)

    def dirpath(self) -> LegacyPath:
        # py.path.local.dirpath() — the parent as py.path (the result must stay LegacyPath, not PosixPath).
        return LegacyPath(self._path.parent)

    @property
    def basename(self) -> str:
        # py.path.local.basename — the last path component (py.path API).
        return self._path.name

    def __add__(self, other: str) -> LegacyPath:
        # py.path.local: string path concatenation (fspath + '.output').
        return LegacyPath(Path(str(self._path) + str(other)))

    def mkdir(self, *parts: str) -> LegacyPath:
        p = self._path.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return LegacyPath(p)

    def write(self, content: str | bytes, mode: str = "w", ensure: bool = False) -> None:
        # py.path.local: ensure=True creates parent directories.
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        if "b" in mode and isinstance(content, str):
            self._path.write_bytes(content.encode("utf-8"))
        elif isinstance(content, bytes):
            self._path.write_bytes(content)
        else:
            self._path.write_text(str(content), encoding="utf-8")

    def read(self, mode: str = "r") -> str | bytes:
        return self._path.read_bytes() if "b" in mode else self._path.read_text(encoding="utf-8")

    # --- py.path.local surface, confirmed by the test corpus ---
    # Standard py.path.local filesystem helpers (as_cwd, write_binary, ensure, dirname).
    @contextlib.contextmanager
    def as_cwd(self) -> Any:
        old = os.getcwd()
        os.chdir(str(self._path))
        try:
            yield self
        finally:
            os.chdir(old)

    def write_binary(self, data: bytes, ensure: bool = False) -> None:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(bytes(data))

    def read_binary(self) -> bytes:
        return self._path.read_bytes()

    def write_text(self, text: str, encoding: str = "utf-8", ensure: bool = False) -> None:
        if ensure:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text, encoding=encoding)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._path.read_text(encoding=encoding)

    def ensure(self, *parts: str, dir: bool = False) -> LegacyPath:
        p = self._path.joinpath(*parts)
        if dir:
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
        return LegacyPath(p)

    def check(self, dir: bool = False, file: bool = False, exists: bool = False, **kwargs: Any) -> bool:
        if dir:
            return self._path.is_dir()
        if file:
            return self._path.is_file()
        return self._path.exists()

    @property
    def dirname(self) -> str:
        return str(self._path.parent)

    @property
    def basename(self) -> str:
        return self._path.name

    @property
    def purebasename(self) -> str:
        return self._path.stem

    @property
    def ext(self) -> str:
        return self._path.suffix

    def __getattr__(self, name: str) -> Any:
        return getattr(self._path, name)


class _LegacyTmpdirFactory:
    """tmpdir_factory (the py.path era): mktemp/getbasetemp return LegacyPath
    so user code can use .strpath/.write(ensure=...).
    tmp_path_factory stays pathlib-modern."""

    def __init__(self, inner: TmpPathFactory) -> None:
        self._inner = inner

    def mktemp(self, basename: str, numbered: bool = True) -> LegacyPath:
        return LegacyPath(self._inner.mktemp(basename, numbered=numbered))

    def getbasetemp(self) -> LegacyPath:
        return LegacyPath(self._inner.getbasetemp())


class TmpPathFactory:
    def __init__(self, base_dir: str | None = None) -> None:
        # Session temp trees under the supervisor's opts["sandbox"] — the exit sweep collects them even when a worker is SIGKILLed; standalone — /tmp. Invariants: TEMP-FILES-CONTAINED.
        if base_dir:
            parent = Path(base_dir) / "wtmp"
            parent.mkdir(parents=True, exist_ok=True)
            self._base = Path(tempfile.mkdtemp(prefix="ztest_session_", dir=parent))
        else:
            self._base = Path(tempfile.mkdtemp(prefix="ztest_session_"))

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        clean = re.sub(r"[^\w.-]", "_", basename) or "tmp"
        if not numbered:
            p = self._base / clean
            p.mkdir(parents=True, exist_ok=True)
            return p
        return Path(tempfile.mkdtemp(prefix=f"{clean}_", dir=self._base))

    def getbasetemp(self) -> Path:
        return self._base

    def cleanup(self) -> None:
        shutil.rmtree(self._base, ignore_errors=True)


# The live config of the file being run (ConfigStub). Some builtin fixtures
# delegate to the installed third-party implementation, whose constructor
# expects a real pytest config object; set per file in _run_test_file_impl.
_ZT_ACTIVE_CONFIG: Any = None


def _make_mocker_fixture() -> Any:
    """`mocker`: the installed pytest-mock MockerFixture when available, else the in-shim stub.

    platformdirs' suite (and anything using pytest-mock features the stub does
    not model) needs the real fixture; the name was previously hard-wired to
    the stub. Construction is signature-tolerant: pytest-mock's
    `MockerFixture(config, patcher)` changed across releases, so every plausible
    shape is tried and anything unexpected falls back to the stub instead of
    failing the test.
    """
    plugin: Any = None
    for _mod_name in ("pytest_mock.plugin", "pytest_mock"):
        try:
            _cand = importlib.import_module(_mod_name)
        except Exception:
            continue
        if getattr(_cand, "MockerFixture", None) is not None:
            plugin = _cand
            break
    cls = getattr(plugin, "MockerFixture", None)
    if cls is None:
        return MockerFixture()
    cfg = _ZT_ACTIVE_CONFIG
    patcher: Any = None
    _patcher_cls = getattr(plugin, "Patcher", None)
    if _patcher_cls is not None and cfg is not None:
        try:
            patcher = _patcher_cls(cfg)
        except Exception:
            patcher = None
    for _args in ((cfg, patcher), (cfg,), (patcher,), ()):
        if _args == (cfg, patcher) and (patcher is None or cfg is None):
            continue
        try:
            mk = cls(*_args)
        except Exception:
            continue
        if hasattr(mk, "stopall") and hasattr(mk, "patch"):
            return mk
    return MockerFixture()


class MockerFixture:
    def __init__(self) -> None:
        self._patches: list[Any] = []
        self.MagicMock = unittest.mock.MagicMock
        self.Mock = unittest.mock.Mock
        self.AsyncMock = unittest.mock.AsyncMock
        self.PropertyMock = unittest.mock.PropertyMock
        self.call = unittest.mock.call
        self.ANY = unittest.mock.ANY
        self.DEFAULT = unittest.mock.DEFAULT
        self.sentinel = unittest.mock.sentinel
        # pytest-mock proxies mock_open onto mocker.*
        self.mock_open = unittest.mock.mock_open
        self.patch = self._PatchProxy(self)

    class _PatchProxy:
        def __init__(self, owner: MockerFixture) -> None:
            self._owner = owner

        @staticmethod
        def _bind_ctx(mock_obj: Any) -> Any:
            """Ensures `with mocker.patch(...) as m:` yields `mock_obj` itself."""
            try:
                if isinstance(mock_obj, unittest.mock.Mock):
                    mock_obj.__enter__ = unittest.mock.MagicMock(return_value=mock_obj)
                    mock_obj.__exit__ = unittest.mock.MagicMock(return_value=False)
            except Exception:
                pass
            return mock_obj

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch(*args, **kwargs)
            self._owner._patches.append(p)
            return self._bind_ctx(p.start())

        def object(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch.object(*args, **kwargs)
            self._owner._patches.append(p)
            return self._bind_ctx(p.start())

        def dict(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch.dict(*args, **kwargs)
            self._owner._patches.append(p)
            return p.start()

        def multiple(self, *args: Any, **kwargs: Any) -> Any:
            p = unittest.mock.patch.multiple(*args, **kwargs)
            self._owner._patches.append(p)
            return p.start()

    def spy(self, obj: Any, name: str) -> Any:
        orig = getattr(obj, name)
        return self.patch.object(obj, name, wraps=orig)

    def stub(self, name: str | None = None) -> unittest.mock.MagicMock:
        return unittest.mock.MagicMock(name=name)

    def resetall(self) -> None:
        for p in self._patches:
            try:
                target = getattr(p, "temp_original", None)
                if hasattr(target, "reset_mock"):
                    target.reset_mock()
            except Exception:
                pass

    def stopall(self) -> None:
        for p in reversed(self._patches):
            try:
                p.stop()
            except Exception:
                pass
        self._patches.clear()


