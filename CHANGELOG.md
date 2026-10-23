# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
