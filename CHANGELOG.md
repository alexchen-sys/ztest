# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-10-25

Distribution, reporting, and parity release. No change to the supervisor or the
worker model: the containment, scheduling and timeout behaviour benchmarked in
`BENCHMARKS.md` is unchanged.

### Added

- Static `musl` release binaries for `x86_64` and `aarch64` (built `ReleaseSafe`,
  stripped, no build-host paths in `strings`), published to GitHub Releases and mirrored into
  PyPI wheels under the `ztest-runner` distribution name, so `pip install
  ztest-runner` and `uvx --from ztest-runner ztest` both work without a
  toolchain.
- `scripts/install.sh`: one-line installer that picks the matching release
  binary, verifies its checksum, and installs into `${ZTEST_INSTALL_DIR:-$HOME/.local/bin}`.
  The install is fail-closed: it refuses a directory or a non-ztest file on the
  target path (`--force` overrides the latter), verifies the staged binary
  reports the requested version before replacing anything, and after the atomic
  rename checks that the file is executable and still reports it.
- Test timeouts now raise a public `TestTimeoutError` (no leading underscore) and
  are reported as an ordinary failure with the test's node id and timeout value,
  instead of a private class name leaking into user-visible output.
- `=== short test summary info ===` at the end of a human-mode run, listing the
  node ids of failures the worker reports as `TEST_FAIL`: failed, errored,
  teardown-failed and collection-error tests, plus in-worker timeouts (which
  raise the public `TestTimeoutError` inside the test body). A timeout enforced
  by the supervisor, which kills the worker, is *not* a per-test frame — it is
  reported on its own `CRASHED` line for the file and does not appear in this
  block. The block keeps at most 256 entries; when more fail, it ends with
  `... N more failed-test reports not shown` (a record count — uniqueness past
  the ring would need unbounded storage, so the line does not pretend to know
  it), and a node id too long for its slot is cut at a UTF-8 boundary with a
  `…` marker instead of disappearing.
- Unknown command-line arguments are named in the diagnostic
  (`ztest: unrecognized argument '--foo'`) instead of a generic usage message.
- Structural assertion diff for `assert a == b`: a `difflib` unified diff over
  the pretty-printed lines of sequences, sets and multi-line strings, and a
  per-key delta for dictionaries (`~ key: left -> right`, plus
  `(only in left)` / `(only in right)` entries). Both are capped at 80 lines
  with an explicit truncation marker. The operator is echoed as `(op: …)` only
  when it is not `==`; `==` keeps the two-value form without that suffix.

### Changed

- Tracebacks drop internal runner frames (`unittest/case.py`, `result.py`,
  `suite.py`, the shim's own runner) so the reported stack starts in user code.
  For a plain test function that is the same first frame pytest prints; for a
  `unittest.TestCase` it is a deliberate divergence — stock pytest ends its
  `--tb` output in `unittest/case.py`. The `--llm` `loc` field is cleaned the
  same way and therefore also points at user code.
- `ztest --llm` failure records carry the same cleaned traceback and the public
  timeout error type; the summary record is unchanged.

### Fixed

- Four of the six Bench-100 divergences are closed with receipts: `execnet`,
  `astroid`, `platformdirs` and `freezegun` reach FULL-PARITY under
  `--engine=pytest` (2.93×, 3.00×, 2.07× and 1.15× faster than serial pytest).
  The fifth, `poetry-core`, is the target of an `addopts` fix: the bridge now
  keeps the `-m` / `-k` / `-x` / `--maxfail` filters parsed out of `addopts`
  when it retries with `-o addopts=`, so its 9 integration tests stay
  deselected. Receipt-backed parity of the 96 suites with a green baseline rises
  from 90/96 (93.8 %) to **94/96 (97.9 %)**; the `poetry-core` row puts the
  number at an expected **95/96 (99.0 %)** and is treated as measured only once
  the release job confirms that row in `BENCHMARKS.md`. `num2words` remains a
  documented behavioural difference (file-isolated workers versus a global
  `decimal` mutation).
- PyPI wheels are architecture-verified: `scripts/build_wheels.py` reads the
  payload's ELF header (`EI_CLASS`, `EI_DATA`, `e_machine`) before packaging and
  again in `verify_wheel`, so an `x86_64` binary can no longer ship under
  `aarch64` tags. The architecture-blind `dist/ztest` fallback is gone; only
  arch-bound tarballs and `ztest-<arch>` files are accepted. `Requires-Python`
  is now `>=3.11`, matching the documented runtime requirement.
- `benchmarks/bench100_harness.py`: measured runs were quantized to 50 ms.
  CPython implements `Popen.wait(timeout=…)` as an `os.waitpid(WNOHANG)` poll
  loop with a backoff capped at 50 ms, so every run was rounded up to the next
  tick (a 0.121 s run was recorded as 0.164 s) and ztest looked 25–35 % slower
  than its own `duration_ms`. The harness now reaps with a blocking `waitpid` in
  the reaping thread and enforces the run cap with a separate watchdog that kills
  the process group at the deadline — overshoot is ~1 ms at 120 ms — and records
  wall time to four decimals.
- `benchmarks/bench100_harness.py`: `SAMPLE_INTERVAL` (0.05 s) is now the single
  PSS sampling cadence documented in `BENCHMARKS.md` — the sampler thread used to
  hard-code `0.05` and never read the constant — and `--help` prints usage
  instead of raising on a missing `--job`.
- Release CI builds the published musl binaries with `-Doptimize=ReleaseSafe`,
  the configuration `README.md` argues for and `BENCHMARKS.md` measured, and
  gates their sterility with `scripts/check_sterility.py`: an occurrence-level
  scan that rejects build-host paths (`/home/…`, `/mnt/…`, `/opt/…`,
  `.zig-cache`, `ztest-work`, `hunt-data`) while allowing the runtime
  `/tmp/ztest_*` templates and the production ignore-list literal `zig-cache`.
  The previous line-oriented regex failed CI on those legal literals and would
  have hidden a leak that shared one `strings` line with them. `install.sh` is
  copied into `dist/`, so the documented
  `releases/latest/download/install.sh` URL is a real release asset.

### Documentation

- `BENCHMARKS.md` rebuilt as v2.0: a containment and runaway section (hung C
  extension, `setsid` orphans, OOM ceiling, `--llm` output volume), a four-arm
  xdist memory matrix, the `--engine=pytest` parity table, sub-second wall times
  at their real 0.01 s resolution, an Amdahl explanation for single-file suites,
  and the fake `211.67×†` speedup on the crashed `execnet` native run replaced
  with an honest dash.
- `README.md`: install-first layout (PyPI, `uvx`, `install.sh`, releases) and
  three positioning sections — agent JSONL, ADR-004 containment, xdist
  replacement.

## [0.5.0] - 2026-10-23

Initial public release.

### Added

- Zig 0.17.0 supervisor (`src/main.zig`, `src/pyfiles.zig`, `src/cli.zig`,
  `src/spawn_arm.zig`) with two execution modes: zygote fork (`--mode=zygote`,
  default for multi-file suites) and clean spawn (`--mode=exec`, automatic for
  single-file targets).
- Interpreter discovery honors `--python`, `$VIRTUAL_ENV/bin/python`,
  `./.venv/bin/python`, `./venv/bin/python`, and `$PATH` before falling back to
  `/usr/bin/python3` with an explicit warning on stderr.
- Three-layer kernel process containment ([ADR-004](docs/adr/ADR-004-process-containment.md)):
  `PR_SET_PDEATHSIG(SIGKILL)` armed before `execve`, a zygote control-socket
  EOF watch that kills every worker process group, and a per-run native
  `pidfd` watchdog backed by a 32-slot `MAP_SHARED` process-group registry
  with fail-closed `pgid <= 1` guards.
- Rolling slot-refill dispatcher (`-j N`) with longest-processing-time (LPT)
  scheduling ordered by file byte size (`statFileSize`).
- `--preload a,b` (`ZTEST_PRELOAD_PKGS`): opt-in zygote pre-import list for
  third-party modules; the default preloads standard-library modules only.
- `--engine=native|pytest` (`ZTEST_ENGINE`): dual-engine execution supporting both
  the zero-dependency built-in shim (`native`, default) and an in-worker upstream
  `pytest` bridge (`pytest`) for full `pluggy` plugin compatibility under the
  same Zig supervisor, zygote, `pidfd` containment, and `--llm` stream.
- Framed binary IPC over an authenticated Unix stream socket (`SO_PEERCRED` +
  `getpgid(pid) == pid`, CRC32-checked frames, staged 256 KiB non-blocking
  writes with `SIGALRM`/`SIGINT` masked across write loops).
- Single-file embedded Python shim (`src/shim/ztest_shim.py`) that delegates the
  `unittest.TestCase` and `IsolatedAsyncioTestCase` lifecycle to the standard
  library ([ADR-005](docs/adr/ADR-005-unittest-delegation.md)) and provides a
  pytest compatibility layer:
  - AST assert rewriting across binary and chained comparisons, boolean `and`/`or`
    expressions (preserving short-circuit evaluation), unary `not`, and function/method
    calls, with scope preservation, walrus support, native `bool(res)` truth evaluation
    (preserving Python's ambiguous-truth `ValueError` on multi-element arrays), and
    optimization-level-aware `.ztest_cache/ast` keyed by `SHIM_VERSION` and compiler flags.
  - Fixtures across `function`, `class`, `module`, and `session` scopes (with
    `package` scope aliased to `module`), `autouse`, generator and async
    fixtures, `params=` and indirect parametrization closure, MRO overrides,
    and per-finalizer timeout budgets.
  - `@pytest.mark.parametrize` (stacked, tuple/list unpacking, `pytest.param`,
    callable and duplicate-suffixed IDs), `skip`, `skipif`, `xfail` (`strict`
    and `xfail_strict`), `usefixtures`, and `filterwarnings`.
  - Built-in fixtures: `tmp_path`, `tmp_path_factory`, `tmpdir`,
    `tmpdir_factory`, `monkeypatch`, `capsys`, `capsysbinary`, `capteesys`,
    `capfd`, `capfdbinary`, `caplog`, `recwarn`, `doctest_namespace`,
    `record_property`, `request`, `pytestconfig`, `cache`, `benchmark`, and
    `mocker` (minimal `unittest.mock` wrapper).
  - Conftest loading with ancestor chain walk from the target path up to
    `rootdir`, hook validation (`HOOK-EXPORT-FAIL-CLOSED`),
    `pytest_generate_tests`, `pytest_collection_modifyitems` (preserving
    reordering, deselection, and item additions), `pytest_ignore_collect`,
    `pytest_collect_file` (with signature-aware `Node.from_parent` and
    collector/item `setup()`/`teardown()`/`runtest()`/`repr_failure()`
    lifecycle), and `pytest_runtest_setup` / `pytest_runtest_teardown`.
  - `-k` substring/boolean expressions matched against the full node ID and
    attached marker names, and `-m` marker expressions.
  - Ini configuration reader (`pytest.ini`, `pyproject.toml`, `tox.ini`) for
    `python_files`, `norecursedirs`, `pythonpath`, `filterwarnings`, `plugins`,
    and `xfail_strict`.
- `--llm` machine output mode: one JSON object per failure plus a final summary
  line (`passed`, `failed`, `skipped`, `deselected`, `crashed`, `collected`,
  `collection_errors`, `duration_ms`).
- `docs/INVARIANTS.md` registry enforced in CI by `scripts/check_docs.py`.
- `tests/test_oss_regressions.py`: a behavioral gate of 132 cases that runs the
  real binary against 120 fixture trees and asserts outcome parity with pytest 9
  semantics.
