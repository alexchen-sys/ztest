# ztest

Process-supervised Python test runner in Zig: native process-group containment
(`pidfd` on Linux, kqueue on macOS), a Zygote worker pool, 2.6× lower memory
than `pytest-xdist` in the Linux benchmark, and `--llm` JSONL for AI agents.

## Install in 1 second

```sh
pip install ztest-runner                  # PyPI wheel, no toolchain
uvx --from ztest-runner ztest --version   # zero-install, one command
```

```sh
curl -LsSf https://raw.githubusercontent.com/alexchen-sys/ztest/main/scripts/install.sh | sh
curl -LsSf https://raw.githubusercontent.com/alexchen-sys/ztest/main/scripts/install.sh | sh -s -- --system
```

Prebuilt static `musl` binaries for Linux `x86_64`/`aarch64` and Mach-O
binaries for macOS `x86_64`/`arm64` sit on
[GitHub Releases](https://github.com/alexchen-sys/ztest/releases) if you would
rather not pipe a shell script. Darwin builds link the system libraries and
carry an ad-hoc signature; a universal2 tarball is also published. Download a
tarball and its `SHA256SUMS` for a manual installation, or let the installer
select the asset and verify its checksum. The installer honors `ZTEST_VERSION`
/ `ZTEST_INSTALL_DIR`. `ztest --version` prints
`ztest 0.6.0 (Zig 0.17.0)`. Building from source needs Zig 0.17.0 and is
covered at the end of this file.

## Why ztest

### 1. For AI agents: `ztest --llm`

`pytest` starts in 600–900 ms and hands an agent 5,000–25,000 tokens of
banners, progress bars, `PASSED` lines and multi-page tracebacks. `ztest --llm`
starts in ~1 ms, prints nothing for a passing test, and emits one JSON line per
failure plus a final summary — measured 85.7 % fewer bytes than `pytest -v` on a
42-test probe, and the gap widens as tracebacks deepen. A test that hangs inside
a C extension holding the GIL cannot take the agent's session with it: the
kernel kills the process group, not a Python-level timer. Numbers and method:
[BENCHMARKS.md §1](BENCHMARKS.md).

### 2. Containment and timeouts that hold (ADR-004)

`pytest-timeout`'s signal method cannot interrupt a C extension that holds the
GIL: the handler runs when the call returns, which is how a 1 s deadline became
a 17.6 s wait, and how a call that never returns turns a one-minute CI step into
a sixty-minute job. Its thread method uses `os._exit`, which leaves every child
the test created alive and holding the job's pipes. ztest pins worker identity
with `pidfd_open`, holds the zombie with `waitid(WNOWAIT)`, kills with
`kill(-pgid, SIGKILL)`, sweeps `setsid` escapees under a child subreaper, and
runs a native watchdog over a `MAP_SHARED` process-group registry — a hung
worker dies on schedule and leaves zero processes behind (measured: 1.28 s,
0 leaked processes). Rationale and accepted limits:
[ADR-004](docs/adr/ADR-004-process-containment.md).

### 3. A drop-in answer to `pytest-xdist`

`pytest -n4` re-collects the suite in every worker over `execnet`, errors out
whenever collection order differs between workers, and pays a full interpreter
plus import graph per worker. In a nine-suite cross-check it failed on 3 of 9
(33 %) and peaked at **1.70×–4.94× (median 2.60×) the memory of `ztest -j4`** —
1.38 GiB against 462 MiB on dateparser. ztest forks workers from one pre-warmed
zygote, schedules by file with LPT, recycles workers per file, and holds a flat
memory ceiling on suites where a monolithic `pytest` is OOM-killed.

## What is ztest

ztest runs pytest suites under kernel-level process-group containment. Linux
uses `pidfd` identity and process waits; macOS uses kqueue process events. It keeps the Python test surface — `conftest.py`, fixtures,
markers, `pytest.raises`, `unittest.TestCase` — and moves supervision,
parallelism, and timeout enforcement into a native Zig supervisor.

On Linux every worker runs in its own process group under `PR_SET_PDEATHSIG`,
`pidfd`-pinned identity, `waitid(WNOWAIT)` zombie pinning, and a subreaper
sweep. Darwin implements the same lifecycle layers with kqueue `NOTE_EXIT` /
`NOTE_TRACK`, process groups and libproc inspection; see ADR-004 for its exact
boundaries. The supervisor starts in ~1.2 ms (`--version`) without booting a master
Python interpreter or `pluggy` plugin chain, and workers fork from a pre-warmed
Python zygote to amortize interpreter startup across files.

For CI and automation, `--llm` emits JSON Lines: one object per failure event
and a stable summary record at the end.

The compatibility shim is compiled into the binary. No Python package or
`pytest` installation is required at runtime: the shim registers the `pytest`
and `_pytest` modules directly.

## Features

- `-j N` parallel workers (default 4, accepts 1–64, clamped to 16) with
  file-batched LPT scheduling.
- Hard per-test timeout (`--timeout=<sec>`, up to 86400). The worker arms an
  in-process timer while the supervisor enforces an outside inactivity window
  (`max(1500, sec*1000) + 1500` ms, or 1 h when `--timeout` is unset).
- Race-free timeout kill: Linux uses `pidfd_open` and `waitid(WNOWAIT)` to pin
  worker identity; Darwin uses kqueue process-exit events and the same
  process-group registry. Group kills complete before the group is reaped.
- Three-layer process-tree containment: parent-death notification, zygote EOF
  watch, and a native watchdog over a shared process-group registry, plus a
  per-file orphan sweep. Linux uses `PR_SET_PDEATHSIG`; Darwin uses kqueue.
- Nonblocking IPC over file descriptor 199 with CRC32 framing and authenticated
  Unix-socket peer credentials (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` or
  `LOCAL_PEERPID` on Darwin).
- Terminal progress view and `--llm` JSONL output (`{"t":"fail",...}`,
  `{"t":"crash",...}`, and `{"t":"summary",...}` with `passed`, `failed`,
  `skipped`, `deselected`, `crashed`, `collection_errors`, `collected`,
  `duration_ms`). Avoid combining `--llm` with `-s` if tests write to stdout.
  If a downstream stdout reader stalls past the 1 MiB nonblocking backlog (or
  the 2 s final drain deadline), whole unbuffered records are dropped, the
  drop count is reported to stderr, and `ztest` exits non-zero (exit `2` when
  all tests otherwise passed).
- pytest-compatible exit codes: `0` pass, `1` failure, `2` interrupted or
  collection error, `3` internal error, `4` usage error, `5` nothing collected.

## Requirements

- Linux (`x86_64` and `aarch64`) or macOS (`x86_64` and `arm64`). Linux uses
  `pidfd_open(2)`, `statx(2)`, `prctl(PR_SET_PDEATHSIG)`, process groups and
  `/proc`; Darwin uses kqueue process events, process groups, libproc and
  `/dev/fd`. Windows and other architectures are not supported.
- Python 3.11 or newer at runtime, discoverable on `PATH` or passed with
  `--python=<path>`.
- No build toolchain unless you build from source, which needs Zig 0.17.0.

## Build from source

Prebuilt binaries and PyPI wheels are the normal path (see *Install in 1 second*).
Contributors build from a checkout:

```sh
git clone https://github.com/alexchen-sys/ztest
cd ztest
zig build -Doptimize=ReleaseSafe
```

The binary lands at `zig-out/bin/ztest`. Installing it is optional:

```sh
sudo install -m 0755 zig-out/bin/ztest /usr/local/bin/ztest
```

`ReleaseSafe` keeps the overflow and bounds checks that matter for a program whose job
is killing process trees. Debug builds are fine for development; `ReleaseFast` gains
little here because run time is dominated by the tests, not by the supervisor.

## Quickstart

Run the example suite shipped in this repository:

```sh
ztest examples/
```

The summary line reads `8 passed`. Point ztest at a directory or a set of
files:

```sh
ztest examples/
ztest examples/test_calc.py examples/test_text.py
```

With no arguments, ztest runs the default target `tests/`:

```sh
ztest
```

Eight workers, and a 30 second ceiling on any single test:

```sh
ztest -j8 --timeout=30 tests/
```

Filter by name or by marker, using pytest expression syntax:

```sh
ztest -k "login or logout and not slow" tests/
ztest -m "integration" tests/
```

List what would run, without running it:

```sh
ztest --collect-only tests/
```

Machine-readable output for agents and CI:

```sh
ztest --llm --timeout=60 tests/
```

Stop early, first failure or after N failures:

```sh
ztest -x tests/
ztest --maxfail=5 tests/
```

## Options

| Option | Meaning |
| --- | --- |
| `-k <expr>` | Filter tests by substring or boolean expression (matches test names, parametrized ids and marker names) |
| `-m <expr>` | Filter tests by marker expression |
| `-x`, `--exitfirst` | Stop on the first failure |
| `--maxfail=<N>` | Stop after N failures |
| `--timeout=<sec>` | Per-test timer; also widens the supervisor's inactivity window (`0 < sec <= 86400`; user timer default: off, window then 1 h) |
| `-j <N>`, `-n <N>` | Parallel workers, 1 to 64 (clamped to 16, default 4) |
| `--llm` | JSONL output for agents and tooling |
| `--engine=native\|pytest` | Test execution engine: built-in zero-dependency shim (`native`, default) or in-worker upstream `pytest` bridge (`pytest`) for third-party `pluggy` plugins (also read from `ZTEST_ENGINE`) |
| `--mode=zygote\|exec` | Worker start strategy (default: `zygote`) |
| `--preload=<list>` | Comma-separated modules to pre-import once into the zygote (opt-in; default: stdlib only; engages above the 8-file small-suite threshold, never in single-file clean-spawn). Also read from `ZTEST_PRELOAD_PKGS` |
| `--no-auto-preload` | Disable best-effort automatic preloading of the detected project package and optional heavy imports (also `ZTEST_AUTO_PRELOAD=0`) |
| `--python=<path>` | Explicit Python 3.11+ interpreter |
| `--collect-only`, `--co` | Collect and print test node IDs only |
| `-s`, `--no-capture` | Do not capture stdout/stderr |
| `-v`, `--verbose` | Verbose output |
| `-q`, `--quiet` | Quiet output |
| `-h`, `--help` | Usage |
| `-V`, `--version` | Version |

Interpreter resolution order without `--python`: `$VIRTUAL_ENV/bin/python`
(when the variable is set and the file exists) → `./.venv/bin/python` →
`./venv/bin/python` → the first `python3` found on `PATH` → `/usr/bin/python3`.
A `python3` resolved from `PATH` outside `/usr/bin` is used without comment —
that is the deliberate toolchain of pyenv/conda/mise users. Landing on
`/usr/bin/python3` itself, whether reached through `PATH` or as the last
fallback, prints a one-line warning on stderr, so a run without a project
virtualenv is never silently executed against the system Python.

For command-line compatibility, the parser accepts pytest display-formatting
flags (`--tb=...`, `--tb <mode>`, `--color=...`, `-ra`, `-rf`, `-rA`) as no-ops.
Unimplemented semantic flags (`-W`, `-o`, `-p`, `-c`, `--import-mode`,
`--rootdir`, `--strict-markers`, `--disable-warnings`) are rejected with exit
code 4 rather than silently ignored.

## Compatibility

Supported:

- `conftest.py` discovery with the usual directory scoping.
- Fixtures as a dependency graph, including transitive parameterization identity —
  the same parameterized fixture is one instance per parameter set, not one per
  consumer.
- Builtin fixtures: `tmp_path`, `tmp_path_factory`, `tmpdir`, `tmpdir_factory`,
  `monkeypatch`, `caplog`, `capsys`, `capsysbinary`, `capteesys`, `capfd`,
  `capfdbinary`, `recwarn`, `doctest_namespace`, `record_property`, `request`,
  `pytestconfig`, `cache`, `benchmark` (lightweight pass-through stub), and a
  `mocker` fixture backed by a minimal `unittest.mock` integration.
  `capsys` captures Python-level writes to `sys.stdout`/`sys.stderr`; `capfd` captures
  at the file-descriptor level, so output from subprocesses and raw `os.write` calls
  lands in `readouterr()` as well.
- `pytest.raises`, `pytest.approx`, `pytest.mark.parametrize`, `pytest.mark.skip`,
  `pytest.mark.skipif`, `pytest.mark.xfail` including `strict`, and `pytest.fixture`
  with `scope`, `params`, `autouse`, `name`, `ids`.
- `xfail` and `skip` semantics, including a test that fails during teardown of an
  expected failure.
- `unittest.TestCase` classes with real lifecycle delegation: `setUp`, `tearDown`,
  `setUpClass`, `tearDownClass`, `addCleanup`, `subTest`, and async test methods.
- `async def` tests, run through `asyncio.Runner` with per-test event loop isolation
  and validation of the coroutine result.
- Assertion rewriting with source context, cached in `.ztest_cache/` by file content
  and interpreter build.
- Conftest hooks, invoked as plain functions: `pytest_addoption`, `pytest_configure`,
  `pytest_unconfigure`, `pytest_collection_modifyitems`, `pytest_generate_tests`,
  `pytest_sessionstart`, `pytest_sessionfinish`, `pytest_runtest_setup`,
  `pytest_runtest_teardown`, and custom collectors/items through
  `pytest_pycollect_makeitem` and `pytest_collect_file`.

Not supported in the default `native` engine (use `--engine=pytest` when required):

- Arbitrary third-party `pluggy` plugins (`pytest11` entry points). The default `native`
  engine implements the pytest API surface directly without booting `pluggy` or
  requiring `pytest` in the environment. When a suite depends on third-party plugins
  (`pytest-django`, `pytest-asyncio`, `hypothesis`, custom `pytest11` hooks), pass
  `--engine=pytest` (or `ZTEST_ENGINE=pytest`): workers fork from the zygote and run
  upstream `pytest` through an in-worker ZTST binary IPC bridge while preserving
  `pidfd` supervision, process-group containment, and `--llm` JSONL output.
- Unsupported API fails loud instead of degrading. Touching a `pytest` attribute the
  shim does not implement raises `ZtestUnsupportedApiError` (an `AttributeError`
  subclass, so `hasattr` probes still behave). `pytest.hookimpl` accepts the
  wrapper and ordering flags so a conftest that uses them still loads; the shim
  calls hooks as plain functions, so ordering flags such as `tryfirst` and
  `trylast` are accepted and ignored, and a wrapper-marked generator hook is
  driven through its pre-yield and post-yield halves back to back rather than
  composed around other hooks. A generator or coroutine hook without the
  wrapper marking still fails closed when the shim would invoke it; the
  reporting metadata hooks are recognized and never called.
- `pytest.ini` and project configuration generally: only the options the shim reads
  are honored, so keep per-project configuration thin (`python_files` and
  `norecursedirs` accept up to 256 patterns; exceeding 256 exits 4). Configuration
  is discovered by walking up from the target paths (a `pyproject.toml` counts
  only when it carries a `[tool.pytest.ini_options]` section), so running ztest
  from outside the project directory still picks up the project's ini options.
- Windows and architectures other than Linux `x86_64`/`aarch64` and macOS
  `x86_64`/`arm64`.

Known differences from pytest, by design:

- A test whose body passes but whose finalizer fails with an unexpected error is
  counted once, as failed (the finalizer failure dominates, unless a single
  finalizer exception matches a declared `@pytest.mark.xfail` on the item).
  pytest reports an unexpected finalizer error after a passing body as one
  passed plus one error; the exit code matches.
- A collection failure caused by a broken `conftest.py` exits 2 (collection
  error), where pytest 9 exits 4. A syntax-broken test module exits 2 under both.
- On `SIGINT` ztest prints a one-line partial tally to stderr
  (`ztest: interrupted (...)`) and exits 2, instead of pytest's banner.
- A crashed worker does not lose counts: tests that finished before the crash
  stay counted, the crashed file adds one failure, and the `--llm` summary's
  `collected` field still reflects what was collected.

## Design

- [docs/INVARIANTS.md](docs/INVARIANTS.md) — the invariants the runner must not
  break, keyed by the stable identifiers the code cites at each guard.
- [docs/adr/ADR-004-process-containment.md](docs/adr/ADR-004-process-containment.md) —
  why containment is three layers instead of one, what each layer covers, and where
  they overlap on purpose.
- [docs/adr/ADR-005-unittest-delegation.md](docs/adr/ADR-005-unittest-delegation.md) —
  the contract for delegating `unittest.TestCase` lifecycle to the real runner instead
  of imitating it.
- [docs/adr/ADR-006-layout.md](docs/adr/ADR-006-layout.md) — how modular shim
  sources become one embedded runtime bundle, and how `main.zig` is split.
- [docs/adr/README.md](docs/adr/README.md) — ADR index and numbering policy.

## Development

```sh
zig build test
```

This runs the native suite: 48 tests (43 in `src/main.zig`, 3 in `src/cli.zig`,
2 in `src/spawn_arm.zig`). The
suite covers framing and CRC rejection, IPC peer authentication, the containment
watchdog and its registry, orphan-sweep exclusions, CLI parsing and its bounds,
interpreter resolution, exit-code parity, and an anti-drift test that compiles
the embedded shim under
`python3` so the binary cannot ship a shim that does not parse.

The behavioral gate lives in `tests/`: 132 unittest cases that run the real binary
against 120 fixture trees, each reproducing a compatibility bug confirmed against an
OSS suite (see [BENCHMARKS.md](BENCHMARKS.md)). It needs the built binary and skips
cleanly without one:

```sh
ZTEST_BIN=zig-out/bin/ztest python3 -m pytest tests/test_oss_regressions.py -q
```

The binary under test is `$ZTEST_BIN` when set, else this repo's `zig-out`
build, else the first `ztest` on `PATH`.

`ztest --version` prints the version: `ztest 0.6.0 (Zig 0.17.0)`.

## Benchmarks

Full matrix, method, and the containment probes: [BENCHMARKS.md](BENCHMARKS.md).
Over 100 real PyPI suites on 4 vCPU / 8 GiB: wall median **1.465×** faster than
serial `pytest` across the 90 full-parity suites, **90/96 (93.8 %) FULL-PARITY**
on the native engine and **94/96 (97.9 %)** with `--engine=pytest`; against
`pytest -n4` (xdist), `ztest -j4` uses **1.70×–4.94× less peak memory (median
2.60×)** and passes the 3 of 9 suites where xdist fails. Every number is
reproducible from the manifest and the harness in `benchmarks/`.

## Limitations

- Windows and architectures outside the four targets in Requirements are not
  supported.
- A descendant that calls `setsid` and outlives the supervisor escapes group-based
  containment. While the supervisor runs, the orphan sweep finds and kills such
  processes; after it exits, nothing can. [ADR-004](docs/adr/ADR-004-process-containment.md)
  records this as an accepted limitation; closing it would require cgroup scoping
  per run.
- The per-file isolation model re-executes conftests and re-imports packages per
  file in each worker; suites dominated by heavy per-file import graphs with little
  per-test work (pygments) can run slower than serial pytest.
  [BENCHMARKS.md](BENCHMARKS.md) measures both directions.

## License

MIT. See `LICENSE`.
