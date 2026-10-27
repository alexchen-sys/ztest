"""OSS-regression behavioral gate for ztest (see BENCHMARKS.md).

Each fixture tree under fixtures/ reproduces a confirmed OSS-bench bug.
The tests run the REAL ztest binary with --llm JSONL and assert
summary/verdict parity with pytest 9 semantics.

Every case spawns real worker processes, so the gate is slow; run it
explicitly:

    ZTEST_BIN=/path/to/ztest python3 -m pytest tests/test_oss_regressions.py -q

The binary under test is $ZTEST_BIN when set, else this repo's own
zig-out build, else the first `ztest` on PATH. With none of those the
module skips.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPO_BIN = HERE.parent / "zig-out" / "bin" / "ztest"

# Resolution order: explicit $ZTEST_BIN, then the binary built from this
# tree, then PATH. The PATH fallback serves installed checkouts, but it can
# pick up a stale ztest whose behavior no longer matches this tree, so the
# repo build wins whenever it exists.
ZTEST_BIN = (
    os.environ.get("ZTEST_BIN")
    or (str(REPO_BIN) if REPO_BIN.is_file() else None)
    or shutil.which("ztest")
)
if ZTEST_BIN:
    # Normalize once, at import: every spawn below runs with cwd= inside a
    # scratch copy of a fixture tree, so a relative ZTEST_BIN (README and CI
    # pass `zig-out/bin/ztest`) would resolve inside the scratch tree and
    # not be found. `shutil.which` already returns an absolute path.
    ZTEST_BIN = os.path.abspath(ZTEST_BIN)


def _have_binary() -> bool:
    return bool(ZTEST_BIN) and Path(ZTEST_BIN).is_file() and os.access(ZTEST_BIN, os.X_OK)


_PYTEST_COUNT_RE = re.compile(
    r"(\d+) (passed|failed|skipped|errors|error|xfailed|xpassed|deselected)"
)


def _pytest_counts(output: str) -> dict[str, int]:
    """Per-outcome counts from pytest's last summary line."""
    counts = dict.fromkeys(
        ("passed", "failed", "skipped", "error", "xfailed", "xpassed", "deselected"), 0
    )
    for line in reversed(output.splitlines()):
        found = _PYTEST_COUNT_RE.findall(line)
        if not found:
            continue
        for number, kind in found:
            counts["error" if kind in ("error", "errors") else kind] = int(number)
        break
    return counts


def _run_pytest_tree(work: Path, args: list[str]) -> tuple[str, int]:
    """Run real pytest on a tree: the parity reference for every case here."""
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    return proc.stdout + proc.stderr, proc.returncode


def _llm_frames(stdout: str) -> tuple[list[dict], dict]:
    """Split --llm output into (events, summary)."""
    events: list[dict] = []
    summary: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("t") == "summary":
            summary = frame
        else:
            events.append(frame)
    return events, summary


@unittest.skipUnless(_have_binary(), "ZTEST_BIN not available (build ztest first)")
class TestOssRegressions(unittest.TestCase):
    maxDiff = None

    def _run_suite(self, fixture: str) -> tuple[list[dict], dict, set[str]]:
        """Copy fixture tree to a scratch dir (repo stays clean), run ztest.

        Returns (fail_events, summary, collected_nodeids). --llm emits only
        failures + summary; nodeids come from a separate --collect-only pass.
        """
        import tempfile

        src = FIXTURES / fixture
        self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
        events: list[dict] = []
        summary: dict = {}
        collected: set[str] = set()
        with tempfile.TemporaryDirectory(prefix=f"ztest-{fixture}-") as td:
            work = Path(td) / fixture
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1", "--llm"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("t") == "summary":
                    summary = ev
                else:
                    events.append(ev)
            co = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1", "--collect-only"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            collected = {ln.strip() for ln in co.stdout.splitlines() if "::" in ln}
        return events, summary, collected

    def _run_suite_args(self, fixture: str, extra: list[str]) -> tuple[list[dict], dict, str, int]:
        """Like _run_suite, but with extra CLI arguments (e.g. --timeout 2).

        Returns (events, summary, combined_output, returncode). A single run
        (no --collect-only pass: these scenarios are about execution
        behavior).
        """
        src = FIXTURES / fixture
        self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
        events: list[dict] = []
        summary: dict = {}
        with tempfile.TemporaryDirectory(prefix=f"ztest-{fixture}-") as td:
            work = Path(td) / fixture
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1", "--llm", *extra],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=180,
            )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("t") == "summary":
                summary = ev
            else:
                events.append(ev)
        return events, summary, proc.stdout + proc.stderr, proc.returncode

    def _fail_events(self, events: list[dict]) -> list[dict]:
        return [e for e in events if e.get("t") == "fail"]

    def _collect_ids(self, fixture: str, extra: list[str]) -> set[str]:
        """Nodeids from a --collect-only run with extra CLI arguments."""
        src = FIXTURES / fixture
        self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
        with tempfile.TemporaryDirectory(prefix=f"ztest-co-{fixture}-") as td:
            work = Path(td) / fixture
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "--collect-only", *extra],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return {ln.strip() for ln in proc.stdout.splitlines() if "::" in ln}

    def _run_suite_jobs(
        self, fixture: str, extra: list[str], env_extra: dict[str, str]
    ) -> tuple[list[dict], dict, str, int]:
        """controlplane_delay helper: like _run_suite_args, but with EXPLICIT
        parallelism (-jN in `extra`).

        The only difference from _run_suite_args is the env overlay (`env_extra`):
        the controlplane_delay suite parameterizes the simulated worker delay.
        Returns (events, summary, combined_output, returncode).
        """
        src = FIXTURES / fixture
        self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
        events: list[dict] = []
        summary: dict = {}
        env = dict(os.environ)
        env.update(env_extra)
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        with tempfile.TemporaryDirectory(prefix=f"ztest-{fixture}-") as td:
            work = Path(td) / fixture
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "--llm", *extra],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("t") == "summary":
                summary = ev
            else:
                events.append(ev)
        return events, summary, proc.stdout + proc.stderr, proc.returncode

    # conftest/test-module attribute scan must survive objects whose
    # __getattr__ raises non-AttributeError.
    def test_localproxy_conftest_scan(self) -> None:
        events, summary, _ = self._run_suite("localproxy")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(self._fail_events(events), [])

    # pytest_generate_tests must parametrize functions through the
    # conftest hook, not fail the module.
    def test_generate_tests_hook(self) -> None:
        events, summary, collected = self._run_suite("generate_tests")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")
        self.assertIn("tests/test_gen.py::test_param[1]", collected)
        self.assertIn("tests/test_gen.py::test_param[2]", collected)

    # a function named exactly ``test`` is
    # collected by pytest's default python_functions and must be collected here.
    def test_exact_test_name_collected(self) -> None:
        events, summary, collected = self._run_suite("exact_test_name")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertIn("tests/test_mod.py::test[okay]", collected)

    # pytest never collects ``*_test``
    # functions or ``*Tests`` classes by default; ztest must not either.
    def test_no_suffix_collection(self) -> None:
        events, summary, collected = self._run_suite("suffix_rules")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertNotIn("tests/test_suffix.py::atest_test", collected)
        self.assertNotIn("tests/test_suffix.py::_make_full_earley_test", collected)
        self.assertFalse(any("SomethingTests" in i for i in collected), collected)

    # pytest_report_header is a metadata-only hook; ztest prints no
    # header, so the hook is recognized and not invoked - never fail-loud.
    def test_report_header_noop(self) -> None:
        events, summary, _ = self._run_suite("report_header")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    # pytest_ignore_collect(collection_path, config) returning True
    # excludes the file from collection.
    def test_ignore_collect(self) -> None:
        events, summary, collected = self._run_suite("ignore_collect")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_keep.py::test_keep"}, collected)

    # tmpdir exposes the py.path.local API surface (as_cwd,
    # write_binary/read_binary, write_text/read_text, dirname, basename,
    # purebasename, ext, check, ensure) - target/target/target/
    # PosixPath delegation on py.path.local-compatible objects.
    def test_tmpdir_py_path_api(self) -> None:
        events, summary, _ = self._run_suite("tmpdir_pyapi")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    # (path project mechanism): @pytest.fixture over @classmethod must
    # register and drive (autouse class-scope importorskip -> module tests skip).
    def test_classmethod_fixture(self) -> None:
        events, summary, _ = self._run_suite("classmethod_fixture")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 2, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    # recwarn is a WarningsRecorder: list-like with
    # .list and .pop(cls) by category, AssertionError when no match.
    def test_recwarn_recorder(self) -> None:
        events, summary, _ = self._run_suite("recwarn")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # ExceptionInfo.traceback iterates entries whose str() carries
    # File 'path':line + source.
    def test_excinfo_traceback(self) -> None:
        events, summary, _ = self._run_suite("excinfo_traceback")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    # ini/pyproject is read; filterwarnings=error applies to
    # items; plugins= and conftest pytest_plugins load plugin modules (fixtures
    # + collection_modifyitems with a live add_marker).
    def test_ini_filterwarnings_and_plugins(self) -> None:
        events, summary, _ = self._run_suite("ini_plugins")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 1, f"events={events!r}")

    # request.keywords is a live shared per-node mapping (target writes to it
    # from one fixture and reads it in another), not a copy per FixtureRequest;
    # pytestconfig.pluginmanager is a minimal stub.
    def test_keywords_shared_and_pluginmanager(self) -> None:
        events, summary, collected = self._run_suite("keywords_pluginmanager")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")

    # --- collection and id semantics ---

    # A mismatch between the number of names and values is a loud collection
    # error (pytest), not a silent zip truncation.
    def test_param_count_mismatch_fails_loud(self) -> None:
        events, summary, _ = self._run_suite("param_count_mismatch")
        self.assertEqual(summary.get("passed", 0), 0, f"events={events!r}")
        mism = [e for e in events if "does not match" in str(e.get("msg", ""))]
        self.assertEqual(len(mism), 1, f"events={events!r}")

    # str-ids go through pytest ascii_escaped wholesale (non-ASCII, backslash);
    # dedup with "_" after a digit plus a while-in-resolved collision loop;
    # complex is str()-ed; a re.Pattern's pattern is escaped as well; bytes are
    # decode(backslashreplace)+translate (single backslash); explicit ids
    # (param id= / ids=[...]) are escaped too (pytest 9.0.3 empirics).
    def test_id_escaping_and_dedup(self) -> None:
        events, summary, collected = self._run_suite("id_escaping")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 19, f"events={events!r}")
        self.assertEqual(
            collected,
            {
                "tests/test_ids.py::test_str[\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442]",
                "tests/test_ids.py::test_str[a\\\\b]",
                "tests/test_ids.py::test_str[caf\\xe9]",
                "tests/test_ids.py::test_dup_digit[dup2_0]",
                "tests/test_ids.py::test_dup_digit[dup2_1]",
                "tests/test_ids.py::test_dup_int[1_0]",
                "tests/test_ids.py::test_dup_int[1_1]",
                "tests/test_ids.py::test_complex[(1+2j)]",
                "tests/test_ids.py::test_re[a.b]",
                "tests/test_ids.py::test_re[a\\\\.b]",
                "tests/test_ids.py::test_re[caf\\xe9]",
                "tests/test_ids.py::test_bytes[a\\b]",
                "tests/test_ids.py::test_bytes[\\x01\\xff]",
                "tests/test_ids.py::test_bytes[\\xff]",
                "tests/test_ids.py::test_explicit_escaped[caf\\xe9]",
                "tests/test_ids.py::test_param_id_escaped[\\u043f\\u0440\\u0438\\u0432\\u0435\\u0442]",
                "tests/test_ids.py::test_dedup_collision[5_1]",
                "tests/test_ids.py::test_dedup_collision[5_2]",
                "tests/test_ids.py::test_dedup_collision[5_0]",
            },
            collected,
        )

    # General scope rule: a class-scoped fixture must not depend on a
    # function-scoped fixture (not only on parameterized axes).
    def test_scope_ordering_general(self) -> None:
        events, summary, _ = self._run_suite("scope_ordering")
        self.assertEqual(summary.get("passed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("failed"), 1, f"events={events!r}")
        self.assertTrue(any("ScopeMismatch" in str(e.get("msg", "")) for e in events), events)

    # An unknown dotted filterwarnings category yields a warning + skipped
    # filter (pytest), not a worker crash.
    def test_filterwarnings_unknown_category_no_crash(self) -> None:
        events, summary, _ = self._run_suite("filterwarnings_unknown_cat")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    # Custom collectors honor -k and ::target filters.
    def test_custom_collector_selection(self) -> None:
        src = FIXTURES / "custom_collector"
        with tempfile.TemporaryDirectory(prefix="ztest-custom-b-") as td:
            work = Path(td) / "fx"
            shutil.copytree(src, work)
            cmd = [ZTEST_BIN, "tests", "-k", "alpha", "--python", sys.executable, "--llm"]
            proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=120)
            lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
            summary = [e for e in lines if e.get("t") == "summary"][-1]
            self.assertEqual(summary.get("passed"), 1, proc.stdout)
            self.assertEqual(summary.get("failed", 0), 0, proc.stdout)
        # A ::target on a file also yields exactly one item
        with tempfile.TemporaryDirectory(prefix="ztest-custom-c-") as td:
            work = Path(td) / "fx"
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests/data/alpha.txt", "--python", sys.executable, "--llm"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
            summary = [e for e in lines if e.get("t") == "summary"][-1]
            self.assertEqual(summary.get("passed"), 1, proc.stdout)
            self.assertEqual(summary.get("failed", 0), 0, proc.stdout)

    # Custom collectors with a target below the hook conftest: the scope is
    # inherited from ancestors (pytest loads the conftest chain from rootdir;
    # our discovery must walk the full chain — walking only downwards from the
    # root loses the scope on subdir targets).
    def test_ancestor_scope(self):
        src = FIXTURES / "custom_collector"
        with tempfile.TemporaryDirectory(prefix="ztest-cwd-hook-") as td:
            work = Path(td) / "fx"
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests/data/sub", "--python", sys.executable, "--llm"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
            summary = [e for e in lines if e.get("t") == "summary"][-1]
            self.assertEqual(summary.get("passed"), 1, proc.stdout + proc.stderr)
            self.assertEqual(summary.get("failed", 0), 0, proc.stdout + proc.stderr)

    # a bare @pytest.mark.xfail (no parens) is a legal
    # pytest API (xfail without reason, strict=False); a fail-closed rejection
    # drops the whole file (69 items). Bare skipif/usefixtures are legal too
    # (skipif skips, usefixtures is a no-op) — see test_bare_marks.
    def test_bare_xfail(self):
        events, summary, _ = self._run_suite("bare_xfail")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        # non-strict XPASS (test_bare_xfail_passes) counts as passed in pytest
        # (report.passed=True, wasxfail); skipped means a real xfail only.
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 1, f"events={events!r}")

    # A hook conftest in the cwd itself (repo root) is the most common layout;
    # the ancestor walk must check it too.
    def test_cwd_hook(self):
        src = FIXTURES / "cwd_hook"
        with tempfile.TemporaryDirectory(prefix="ztest-cwd-hook-") as td:
            work = Path(td) / "fx"
            shutil.copytree(src, work)
            cmd = [ZTEST_BIN, "tests", "--python", sys.executable, "--llm"]
            proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=120)
            lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
            summary = next((l for l in reversed(lines) if l.get("t") == "summary"), {})
            self.assertEqual(summary.get("failed", 1), 0, proc.stdout[-2000:])
            self.assertEqual(summary.get("passed"), 2, proc.stdout[-2000:])

    # ids=callable is invoked PER component (pytest); None
    # for a component means the component's value-id; an exception is a loud
    # collection error.
    def test_ids_callable_components(self):
        events, summary, collected = self._run_suite("ids_callable_components")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2)
        self.assertEqual(
            collected,
            {"tests/test_c.py::test_pairs[C1-Cx]", "tests/test_c.py::test_pairs[C2-S2]"},
            collected,
        )

    # fixture axes (params=) go through IdMaker: functions
    # become __name__ (_simulate_*), scalars become str().
    def test_fixture_axis_ids(self):
        events, summary, collected = self._run_suite("fixture_axis_ids")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3)
        self.assertEqual(
            collected,
            {
                "tests/test_f.py::test_scenario[_simulate_alpha]",
                "tests/test_f.py::test_scenario[_simulate_beta]",
                "tests/test_f.py::test_scenario[42]",
            },
            collected,
        )

    # the worker lives with SIGPIPE=SIG_IGN (plain python
    # semantics, pytest parity): writing to a closed pipe/socket raises
    # BrokenPipeError instead of killing the process (SIG_DFL would crash
    # the file).
    def test_sigpipe_worker_survives(self):
        events, summary, _ = self._run_suite("sigpipe_worker")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertEqual(summary.get("crashed", 0), 0, f"events={events!r}")

    # multiprocessing (ProcessPoolExecutor/Process)
    # inside a test: the pool stays alive, children do not die (the
    # _close_ipc_in_grandchild guard must not close a foreign fd reused as
    # 199).
    def test_multiprocessing_pool(self):
        events, summary, _ = self._run_suite("multiprocessing")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # ini xfail_strict=true: a bare xfail with XPASS becomes FAILED (pytest
    # semantics), xfailed stays skipped.
    def test_xfail_strict_ini(self):
        events, summary, _ = self._run_suite("xfail_strict")
        self.assertEqual(summary.get("passed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("failed"), 1, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 1, f"events={events!r}")

    # the conftest hook pytest_runtest_teardown(item,
    # nextitem): pytest (pluggy) matches arguments by name; nextitem is None
    # here.
    def test_hook_nextitem_signature(self):
        events, summary, _ = self._run_suite("hook_nextitem")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # An AttributeError in a dotted filterwarnings category is a file-level
    # CollectionError carrying the filter name (pytest: a fatal UsageError),
    # not a worker crash.
    def test_filterwarnings_attrerr_collection_error(self):
        events, summary, _ = self._run_suite("filterwarnings_attrerr")
        self.assertEqual(summary.get("crashed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("failed"), 1, f"events={events!r}")
        self.assertTrue(
            any("NoSuchWarning" in str(e.get("msg", "")) for e in events),
            events,
        )

    # pytest_collect_file takes priority over .py files too
    # (examplefiles/*.py are claimed by the hook); an empty item name
    # produces a nodeid with a trailing "::" (pytest: file::).
    def test_hook_claims_py_files(self):
        events, summary, ids = self._run_suite("hook_claims_py")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertIn("tests/claimed/sample.py::", ids, ids)
        self.assertIn("tests/test_regular.py::test_regular_still_works", ids, ids)

    # Explicit ids (a callable result and a list entry) go through
    # _idval_from_value: str/bytes are escaped, an unsupported callable result
    # falls back to the value's value-id; generators are covered too.
    def test_ids_idval_semantics(self):
        events, summary, ids = self._run_suite("ids_idval")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 8, f"events={events!r}")
        for expected in [
            r"tests/test_ids.py::test_callable_unicode[caf\xe9]",
            r"tests/test_ids.py::test_callable_bytes[\xffz]",
            "tests/test_ids.py::test_callable_unsupported[7]",
            r"tests/test_ids.py::test_fixture_unicode[caf\xe9]",
            r"tests/test_ids.py::test_fixture_bytes[\xffz]",
            "tests/test_ids.py::test_gen_ids[g0]",
            "tests/test_ids.py::test_gen_ids[g2]",
        ]:
            self.assertIn(expected, ids, ids)

    # Bare skipif/usefixtures are legal: skipif without arguments means
    # skipped, usefixtures is a no-op + passed.
    def test_bare_marks(self):
        events, summary, _ = self._run_suite("bare_marks")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 1, f"events={events!r}")

    # positional-only item + a real nextitem in teardown; a sessionfinish
    # assert records the order one->two->None.
    def test_hook_signature_and_nextitem(self):
        events, summary, _ = self._run_suite("hook_sig")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # ids="xy" means per-char; ids=itertools.count() is isliced to the number
    # of parametersets (an infinite iterator must not hang).
    def test_str_and_count_ids(self):
        events, summary, ids = self._run_suite("str_and_count_ids")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 5, f"events={events!r}")
        for expected in [
            "tests/test_ids.py::test_str_ids[x]",
            "tests/test_ids.py::test_str_ids[y]",
            "tests/test_ids.py::test_count_ids[0]",
            "tests/test_ids.py::test_count_ids[2]",
        ]:
            self.assertIn(expected, ids, ids)

    # Generator fixture-ids persist: a second consumer of the same fixture
    # sees the same ids.
    def test_generator_ids_reused(self):
        events, summary, ids = self._run_suite("gen_ids_reuse")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 6, f"events={events!r}")
        self.assertIn("tests/test_two.py::test_b[g2]", ids, ids)

    # param-id parity with the pytest IdMaker: value-ids (bytes ascii-escaped
    # without b'', Enum Class.MEMBER), control chars escaped, duplicates
    # suffixed with a number for all occurrences (dup0/dup1), stacked axis
    # order is lower-decorator-outer (slower), like pytest.
    def test_param_id_parity(self) -> None:
        events, summary, collected = self._run_suite("param_ids")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 11, f"events={events!r}")
        self.assertEqual(
            collected,
            {
                "tests/test_ids.py::test_pairs[1-x]",
                "tests/test_ids.py::test_pairs[2-y]",
                "tests/test_ids.py::test_str[x\\r\\ny]",
                "tests/test_ids.py::test_str[dup0]",
                "tests/test_ids.py::test_str[dup1]",
                "tests/test_ids.py::test_bytes[\\x01\\xff]",
                "tests/test_ids.py::test_enum[Color.RED]",
                "tests/test_ids.py::test_stacked[p-1]",
                "tests/test_ids.py::test_stacked[p-2]",
                "tests/test_ids.py::test_stacked[q-1]",
                "tests/test_ids.py::test_stacked[q-2]",
            },
            collected,
        )

    # stdout/stderr of subprocesses spawned by a
    # test must not be mixed into the supervisor's verbose stream (pytest
    # capfd parity): the -v output of the binary carries no subprocess
    # markers.
    def test_fd_level_capture(self) -> None:
        src = FIXTURES / "fd_capture"
        with tempfile.TemporaryDirectory(prefix="ztest-fd-") as td:
            work = Path(td) / "fx"
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "-v", "--python", sys.executable],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("SUBPROC_MARKER", proc.stdout, proc.stdout)
        self.assertNotIn("SUBPROC_MARKER", proc.stderr, proc.stderr)
        # OSS-COLOR: piped (not-a-tty) output carries no ANSI escapes (pytest parity)
        self.assertNotIn("\x1b[", proc.stdout, proc.stdout)

    # CLI subprocesses must see a
    # clean environment: pytest does not set os.environ['PYTHONPATH'].
    def test_no_pythonpath_pollution(self) -> None:
        events, summary, _ = self._run_suite("pythonpath_env")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # a tuple value of a single parametrize gets the positional id
    # [v0] as in pytest, without a str+tuple crash (IdMaker parity).
    def test_tuple_param_id(self) -> None:
        events, summary, collected = self._run_suite("tuple_param_id")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_t.py::test_t[v0]"}, collected)

    # pytest_collect_file custom collectors: non-.py files
    # under a conftest with the hook are collected via pytest.File/Item
    # subclasses (from_parent/fspath/runtest/repr_failure), teardown hooks run.
    def test_custom_collect_file(self) -> None:
        events, summary, collected = self._run_suite("custom_collector")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 4, f"events={events!r}")
        self.assertEqual(
            collected,
            {
                "tests/data/alpha.txt::content",
                "tests/data/beta.txt::content",
                "tests/data/sub/deep.txt::content",
                "tests/test_regular.py::test_regular",
            },
            collected,
        )

    # a custom pytest_collect_file collector generates many items
    # from one file (.cases data and a .py taken by the hook before the native
    # .py collection). `collected` is a stable --llm contract key and must equal
    # the real item count — pytest 9 counts every generated item, 6 here.
    def test_custom_collect_file_counts_generated_items(self) -> None:
        events, summary, collected = self._run_suite("custom_collect_multi_item")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 6, f"events={events!r}")
        self.assertEqual(
            len(collected),
            6,
            f"collect-only pass disagrees with the summary: collected={sorted(collected)!r}",
        )
        self.assertEqual(
            summary.get("collected"),
            6,
            f"generated custom items are missing from collected: summary={summary!r}",
        )

    # --- fixture/parametrization semantics ---

    # Node.from_parent forwards every user kwarg to __init__ (pytest 9
    # NodeMeta._create: one full call, and only on TypeError a retry filtered
    # down to the declared parameters). ztest's old from_parent modelled that
    # as a two-way heuristic — "cooperative iff **kwargs or a `path`
    # parameter" — and otherwise called cls(name, parent): a custom File with
    # no `fspath` died on the injected fspath, a custom Item with mandatory
    # `payload`/`index` lost them (collection error instead of 2 passed).
    def test_custom_collector_ctor_user_kwargs(self) -> None:
        events, summary, collected = self._run_suite("custom_collector_kwargs")
        self.assertEqual(summary.get("failed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 4, f"events={events!r}")
        self.assertEqual(summary.get("collection_errors", 0), 0, f"events={events!r}")
        self.assertEqual(
            collected,
            {
                "tests/data/alpha.kv::content",
                "tests/data/beta.kv::content",
                "tests/data/delta.kv2::content",
                "tests/data/gamma.kv2::content",
            },
            collected,
        )

    # Direct parametrize of a name whose fixture has params= must mask the
    # fixture axis (not cross-multiply); the indirect=True case is covered by
    # a separate fixture below.
    def test_direct_masks_fixture_axis(self) -> None:
        events, summary, collected = self._run_suite("direct_mask")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_dm.py::test_direct_masks_fixture[10]", "tests/test_dm.py::test_direct_masks_fixture[20]"}, collected)

    # The duplicate-parametrization gate must cover decorator x decorator
    # and hook x hook too (pytest: loud collection error).
    def test_duplicate_gate_all_pairs(self) -> None:
        events, summary, _ = self._run_suite("dup_gate")
        coll = [e for e in events if "duplicate parametrization" in str(e.get("msg", ""))]
        self.assertEqual(len(coll), 2, f"events={events!r}")
        self.assertEqual(summary.get("passed", 0), 0, f"events={events!r}")

    # autouse fixtures' own dependencies must
    # join the fixturenames closure BFS (hook gating on a name reachable only
    # through an autouse fixture).
    def test_autouse_deps_in_closure(self) -> None:
        events, summary, collected = self._run_suite("autouse_closure")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_a.py::test_x[t1]", "tests/test_a.py::test_x[t2]"}, collected)

    # MRO override: the most-derived class fixture wins, for
    # classmethod-bound and plain fixtures alike.
    def test_mro_override(self) -> None:
        events, summary, _ = self._run_suite("mro_override")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # module-level pytest_generate_tests participates.
    def test_module_generate_tests(self) -> None:
        events, summary, collected = self._run_suite("module_generate_tests")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")
        self.assertIn("tests/test_mod.py::test_newaxis[1]", collected, collected)

    # a class-scoped fixture depending on a function-scoped axis:
    # pytest fails loudly with ScopeMismatch, no silent first-value reuse.
    def test_scope_mismatch_loud(self) -> None:
        events, summary, _ = self._run_suite("scope_mismatch")
        self.assertEqual(summary.get("passed", 0), 0, f"events={events!r}")
        self.assertEqual(summary.get("failed"), 2, f"events={events!r}")
        self.assertTrue(any("ScopeMismatch" in str(e.get("msg", "")) for e in events), events)

    # WarningsRecorder.pop sibling rule, TracebackEntry.lineno
    # 0-based, LegacyPath.__eq__ against str/Path.
    def test_warnings_pop_lineno_and_legacy_path_eq(self) -> None:
        events, summary, _ = self._run_suite("pop_lineno_legacyeq")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")

    # VAR_POSITIONAL/VAR_KEYWORD of fixture
    # signatures do not leak into fixturenames; pytestconfig.getvalue parity.
    def test_var_filter_and_getvalue(self) -> None:
        events, summary, _ = self._run_suite("var_filter_getvalue")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # The legacy `path` kwarg of pytest_ignore_collect carries a
    # py.path.local-shaped object (with .basename) for old-style hooks.
    def test_legacy_path_kwarg(self) -> None:
        events, summary, collected = self._run_suite("legacy_path_kwarg")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_kept.py::test_kept"}, collected)

    # pkgutil.get_data(__name__, rel) on the module name needs a
    # real SourceFileLoader-backed __spec__ (loader=None makes pkgutil return None).
    def test_pkgutil_get_data(self) -> None:
        events, summary, _ = self._run_suite("pkgutil_getdata")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # conftest attributes collect_ignore / collect_ignore_glob
    # (paths relative to the conftest's own dir; dir entries ignore the subtree).
    def test_collect_ignore_attrs(self) -> None:
        events, summary, collected = self._run_suite("collect_ignore")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_kept.py::test_kept"}, collected)

    # (validators): parametrize(("value",), [(uuid,), ...]): tuple/list argnames
    # enable force_tuple: a sequence value is unpacked; a string name leaves
    # the value as is.
    def test_force_tuple_unpacking(self) -> None:
        events, summary, collected = self._run_suite("force_tuple")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 4, f"events={events!r}")

    # supervisor discovery honors python_files from
    # pyproject/ini (replaces defaults) - a file named tests.py is collected,
    # test_*.py stops matching.
    def test_python_files_discovery(self) -> None:
        events, summary, collected = self._run_suite("python_files")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertEqual(
            collected,
            {"tests/tests.py::test_in_custom_file", "tests/tests.py::test_second"},
            collected,
        )

    # capture streams expose .buffer (BytesIO-backed,
    # like pytest CaptureIO over fd capture), and sys.stdin during tests is a
    # DontReadFromInput object (non-io.IOBase, read* raise OSError).
    def test_stdio_buffer_and_stdin_guard(self) -> None:
        events, summary, _ = self._run_suite("stdio_buffer")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")

    # @pytest.mark.parametrize("spec", ..., indirect=True)
    # on a name whose fixture has its own params= must replace the fixture axis,
    # not cross-multiply it.
    def test_indirect_overrides_fixture_params(self) -> None:
        events, summary, collected = self._run_suite("indirect_override")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")
        self.assertEqual(
            collected,
            {
                "tests/test_ovr.py::test_override[3.0.0]",
                "tests/test_ovr.py::test_base[2.0]",
                "tests/test_ovr.py::test_base[3.0.0]",
            },
            collected,
        )

    # public pytest names and _pytest internals used by real suites:
    # pytest.TempPathFactory / pytest.CaptureFixture[str] (target, target),
    # _pytest.fixtures.SubRequest, _pytest.recwarn.WarningsRecorder.
    def test_pytest_internals_surface(self) -> None:
        events, summary, _ = self._run_suite("pytest_internals")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")

    # pytest default python_functions=["test"]
    # is prefix matching - testfoo, TestX::tested and unittest camelCase
    # testFoo are collected; only non-prefixed names are skipped.
    def test_prefix_collection_rules(self) -> None:
        events, summary, collected = self._run_suite("prefix_rules")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 5, f"events={events!r}")
        self.assertIn("tests/test_prefix.py::testfoo", collected)
        self.assertIn("tests/test_prefix.py::TestProbe::tested", collected)
        self.assertIn("tests/test_prefix.py::CamelCase::testFoo", collected)
        self.assertNotIn("tests/test_prefix.py::atest_x", collected)

    # a callable class attribute named test_*
    # whose __getattr__ raises non-AttributeError must not blow up the class
    # scan; pytest collects and fails it at call time, siblings still run.
    def test_callable_proxy_attribute(self) -> None:
        events, summary, collected = self._run_suite("proxy_method")
        coll_errors = [
            e for e in self._fail_events(events)
            if "<collection>" in str(e.get("id", ""))
        ]
        self.assertEqual(coll_errors, [], f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertIn("tests/test_proxy.py::TestProxyHost::test_real", collected)

    # metafunc.fixturenames carries the transitive
    # fixture closure + autouse names, and direct (non-indirect) call params
    # mask fixture lookups of the same name inside fixture bodies.
    def test_fixturenames_closure_and_param_masking(self) -> None:
        events, summary, collected = self._run_suite("closure_masking")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 5, f"events={events!r}")
        self.assertIn("tests/test_closure.py::test_via_fixture[3.6]", collected)
        self.assertIn("tests/test_closure.py::test_via_fixture[3.7]", collected)
        self.assertIn("tests/test_closure.py::test_direct_mixed[3.6]", collected)
        self.assertIn("tests/test_closure.py::test_autouse_visible[tagged]", collected)

    # pluggy firstresult: leaf-first, first
    # non-None wins (leaf False overrides root True); the hook is consulted
    # for ancestor directories of the file too.
    def test_ignore_collect_firstresult_and_dirs(self) -> None:
        events, summary, collected = self._run_suite("firstresult")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(collected, {"tests/test_keep.py::test_ok"}, collected)

    # decorator + hook parametrizing the same
    # argname must fail loud at collection (pytest: "duplicate parametrization").
    def test_duplicate_argname_fails_loud(self) -> None:
        events, summary, collected = self._run_suite("duplicate_argname")
        self.assertEqual(summary.get("passed", 0), 0, f"events={events!r}")
        self.assertGreaterEqual(summary.get("failed", 0), 1, f"events={events!r}")
        self.assertFalse(any("test_dup[" in i for i in collected), collected)
        dup = [e for e in self._fail_events(events) if "duplicate parametrization" in str(e.get("msg", ""))]
        self.assertTrue(dup, f"events={events!r}")

    # ExceptionInfo.errisinstance and the
    # pytestconfig builtin fixture.
    def test_errisinstance_and_pytestconfig(self) -> None:
        events, summary, _ = self._run_suite("errisinstance_pytestconfig")
        self.assertEqual(summary.get("failed"), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # A bare usefixtures warns with the PytestWarning category (pytest 9),
    # not UserWarning: a selective error::pytest.PytestWarning filter must
    # escalate it into a collection error.
    def test_usefixtures_warning_category(self) -> None:
        events, summary, _ = self._run_suite("usefixtures_warn_cat")
        self.assertEqual(summary.get("crashed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("failed"), 1, f"events={events!r}")
        self.assertTrue(
            any("usefixtures" in str(e.get("msg", "")) for e in events),
            events,
        )

    # Pinning the cardinality of explicit ids: a loud collection error when
    # it does not match the number of parametersets (pytest 9 parity).
    def test_ids_cardinality_loud(self) -> None:
        events, summary, _ = self._run_suite("ids_cardinality")
        self.assertEqual(summary.get("crashed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed", 1), 0, f"events={events!r}")
        self.assertGreaterEqual(summary.get("failed", 0), 1, f"events={events!r}")

    # Empty ids ([], "") mean: generate the id from the values (pytest #1849),
    # not a cardinality error. Both axes: the decorator and fixture params.
    def test_empty_ids_generate_value_ids(self) -> None:
        events, summary, ids = self._run_suite("empty_ids")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 7, f"events={events!r}")
        self.assertIn("tests/test_ids.py::test_empty_list_ids[1]", ids, ids)
        self.assertIn("tests/test_ids.py::test_empty_list_ids[3]", ids, ids)
        self.assertIn("tests/test_ids.py::test_empty_str_ids[10]", ids, ids)
        self.assertIn("tests/test_ids.py::test_fixture_empty_ids[b]", ids, ids)

    # A hook parameter with a default is not injected (pluggy varnames vs
    # kwargnames): a defaulted nextitem is always None in pytest.
    def test_nextitem_defaulted_is_none(self) -> None:
        events, summary, _ = self._run_suite("nextitem_defaulted")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    # pytest_terminal_summary in a conftest is a reporting hook of
    # the pytest_report_header kind: a deliberate no-op that does not break
    # collection.
    def test_terminal_summary_noop(self) -> None:
        events, summary, _ = self._run_suite("terminal_summary_noop")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    # Hook-reporting surfaces that must not break collection: pytest's
    # pytest_report_header-style hooks and future-annotations fixtures.
    def test_noop_report_hooks(self) -> None:
        events, summary, _ = self._run_suite("noop_report_hooks")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_future_annotations_not_inherited(self) -> None:
        events, summary, _ = self._run_suite("future_annotations")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_monotonic_monkeypatch_safe(self) -> None:
        events, summary, _ = self._run_suite("monotonic_real")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_abstract_testcase_skipped(self) -> None:
        events, summary, ids = self._run_suite("abstract_testcase")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        # unittest form (1) + pytest form (2 params)
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")
        self.assertFalse(any("TestTranslatorBase::" in i or "TestAbstractPytestBase::" in i for i in ids), f"ids={ids!r}")

    def test_runtest_collected_when_no_test_methods(self) -> None:
        events, summary, ids = self._run_suite("runtest_method")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertTrue(any(i.endswith("PyparsingLike::runTest") for i in ids), f"ids={ids!r}")
        self.assertFalse(any("WithReal::runTest" in i for i in ids), f"ids={ids!r}")

    def test_legacy_path_surface(self) -> None:
        events, summary, _ = self._run_suite("legacy_path")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_unittest_method_fixture_gets_self(self) -> None:
        events, summary, _ = self._run_suite("fixture_self")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_import_py_error(self) -> None:
        events, summary, _ = self._run_suite("import_py")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_axis_order_hook_fixture_method(self) -> None:
        events, summary, ids = self._run_suite("axis_order")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        # pytest: the fixture axis comes before the decorator axis: test_axis[3.10-a + a]
        self.assertTrue(any("test_axis[3.10-a + a]" in i for i in ids), f"ids={ids!r}")

    def test_skipif_sys_namespace(self) -> None:
        events, summary, _ = self._run_suite("skipif_sys_ns")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        # linux: skipif(False) -> passed, xfail(False) -> plain passed
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_pytest_stub_version(self) -> None:
        events, summary, _ = self._run_suite("pytest_version_stub")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_outcomes_functions(self) -> None:
        events, summary, _ = self._run_suite("outcomes_funcs")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        # o_skip inside raises counts as passed; test_after_skip is passed
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_fixture_function_definition_type_stub(self) -> None:
        events, summary, _ = self._run_suite("ffd_stub")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_pytest_stub_file(self) -> None:
        events, summary, _ = self._run_suite("pytest_file")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_norecursedirs(self) -> None:
        events, summary, ids = self._run_suite("norecursedirs")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertTrue(any("test_ok" in i for i in ids), f"ids={ids!r}")
        self.assertFalse(any("mypy_inputs" in i for i in ids), f"ids={ids!r}")

    def test_root_log_capture(self) -> None:
        events, summary, _ = self._run_suite("root_log_capture")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_mark_filterwarnings(self) -> None:
        events, summary, _ = self._run_suite("mark_filterwarnings")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_xpass_soft(self) -> None:
        # non-strict XPASS = passed (pytest report.passed); a real
        # xfail (the test failed) = skipped. failed=0 is mandatory.
        events, summary, _ = self._run_suite("xpass_soft")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 1, f"events={events!r}")

    def test_subtest_skip_counts(self) -> None:
        events, summary, _ = self._run_suite("subtest_skip")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")
        self.assertEqual(summary.get("skipped"), 1, f"events={events!r}")

    # ---- second batch of core regressions ----

    def test_catch_warnings_records(self) -> None:
        # catch_warnings(record=True) must record a
        # DeprecationWarning from a non-test module: pytest per-item
        # simplefilter("always") (PEP 565 parity).
        events, summary, _ = self._run_suite("catch_warnings_record")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_pythonpath_ini(self) -> None:
        # The pythonpath ini option (pytest 7+ builtin) adds rootdir-relative
        # paths to sys.path.
        events, summary, _ = self._run_suite("pythonpath_ini")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_collector_conftest(self) -> None:
        # a conftest may define a pytest.Collector subclass: the
        # conftest must load.
        events, summary, _ = self._run_suite("collector_conftest")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_filterwarnings_pytest_category(self) -> None:
        # ini filterwarnings with pytest.* categories resolve to
        # stub classes (PytestCacheWarning etc.).
        events, summary, _ = self._run_suite("filterwarnings_pytest_cat")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_monkeypatch_context_classmethod(self) -> None:
        # MonkeyPatch.context() is a classmethod.
        events, summary, _ = self._run_suite("monkeypatch_context")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_fixture_pytest_markers(self) -> None:
        # fixture functions carry _pytestfixturefunction and
        # __pytest_wrapped__.obj; the fixture is invoked through .obj (class
        # decorators replace the fixture body).
        events, summary, _ = self._run_suite("fixture_pytest_markers")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_mocker_mock_open(self) -> None:
        # MockerFixture.mock_open proxies unittest.mock.mock_open.
        events, summary, _ = self._run_suite("mocker_mock_open")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_hookimpl_wrapper(self) -> None:
        # hookimpl(hookwrapper=True): the wrapper/ordering options
        # are ignored (hooks run as plain functions), the conftest survives.
        events, summary, _ = self._run_suite("hookimpl_wrapper")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_stdio_fileno(self) -> None:
        # sys.stdout/sys.stderr under capture expose a real fileno
        # (fd-capture parity); target console_scripts calls it.
        events, summary, _ = self._run_suite("stdio_fileno")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_ext_dictconfig(self) -> None:
        # ext://tests.mod.attr in dictConfig resolves:
        # the test module is importable under its package name.
        events, summary, _ = self._run_suite("ext_dictconfig")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 1, f"events={events!r}")

    def test_unittest_attr_name(self) -> None:
        # unittest items are resolved by the class
        # attribute name, not by func.__name__ (metaclass factories and
        # decorators without functools.wraps).
        events, summary, _ = self._run_suite("unittest_attr_name")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 5, f"events={events!r}")

    def test_excinfo_legacypath_surface(self) -> None:
        # ExceptionInfo.__repr__/__str__ carry the exception text.
        # ExceptionInfo.exconly().
        # LegacyPath.__truediv__ (py.path.local `/`).
        events, summary, _ = self._run_suite("excinfo_surface")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 3, f"events={events!r}")

    def test_preload_opt_in(self) -> None:
        # Third-party preload is opt-in: by default the zygote must not import
        # an importable project module; --preload / ZTEST_PRELOAD_PKGS import
        # it. The fixture exceeds the 8-file gate so the preload path runs.
        events, summary, _, rc = self._run_suite_args("preload_opt_in", [])
        self.assertEqual(rc, 0, f"events={events!r}")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 9, f"events={events!r}")

        os.environ["ZTEST_FIXTURE_PRELOAD_EXPECT"] = "probe"
        self.addCleanup(os.environ.pop, "ZTEST_FIXTURE_PRELOAD_EXPECT", None)
        events, summary, _, rc = self._run_suite_args(
            "preload_opt_in", ["--preload", "ztest_probe_mod"]
        )
        self.assertEqual(rc, 0, f"events={events!r}")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 9, f"events={events!r}")

        os.environ["ZTEST_PRELOAD_PKGS"] = "ztest_probe_mod"
        self.addCleanup(os.environ.pop, "ZTEST_PRELOAD_PKGS", None)
        events, summary, _, rc = self._run_suite_args("preload_opt_in", [])
        self.assertEqual(rc, 0, f"events={events!r}")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 9, f"events={events!r}")

    def test_prepend_semantics(self) -> None:
        # the sys.path insertion point is the first NON-package
        # ancestor (pytest prepend), not the file's parent; the sibling
        # package tests/html/ must not shadow stdlib html.
        events, summary, _ = self._run_suite("prepend_semantics")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")

    def test_pycollect_makeitem(self) -> None:
        # the pytest_pycollect_makeitem wrapper hook (pluggy
        # hookwrapper + force_result) drives data-driven custom collection of
        # module attributes; a wrapper None suppresses default collection
        # (test_plain is not collected; verified against pytest 9.0.3).
        events, summary, collected = self._run_suite("pycollect_makeitem")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")
        self.assertTrue(
            any("test_render::PAYLOAD-A" in i for i in collected),
            f"custom item nodeid missing: {sorted(collected)!r}",
        )
        self.assertFalse(
            any("test_plain" in i for i in collected),
            f"wrapper-None must suppress default collection: {sorted(collected)!r}",
        )

    def test_dunder_test(self) -> None:
        # the __test__=False protocol excludes classes/functions
        # from collection; an explicit __test__=True on a subclass re-enables
        # them. Collected: TestConcrete.test_template + test_real.
        events, summary, collected = self._run_suite("dunder_test")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r} collected={sorted(collected)!r}")
        self.assertFalse(
            any("BaseTemplate" in i for i in collected),
            f"__test__=False class collected: {sorted(collected)!r}",
        )

    def test_ipc_pristine_stdlib(self) -> None:
        # the test patches json.dumps via mock.patch.start(); the
        # shim's IPC core must hold pristine stdlib references and must not
        # route through user-monkeypatched module attributes.
        events, summary, _ = self._run_suite("ipc_pristine")
        self.assertEqual(summary.get("crashed", 1), 0, f"summary={summary!r}")
        self.assertEqual(summary.get("failed", 1), 0, f"events={events!r}")
        self.assertEqual(summary.get("passed"), 2, f"events={events!r}")


    # --- rate-limit heartbeat: the inactivity invariant is checked by these
    # two executable suites on the real binary.
    # Suite 1: 50 fast tests (heartbeats suppressed) + a test sleeping T-0.2 s
    # under --timeout T, + a multiphase test running 4.5 s (> the 3.5 s window,
    # saved only by phase-boundary heartbeats) -> everything PASSes, the file
    # is not CRASHED (neither SIGALRM nor the supervisor kill fired).
    # Verified against a broken shim (H = 2*window): the suite dies with
    # crash/signal 9 -> the test goes red, so it is not vacuous.
    def test_sleep_below_timeout_stays_green_with_heartbeats(self) -> None:
        events, summary, out, rc = self._run_suite_args(
            "hb_sleep_under_timeout", ["--timeout", "2"]
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("failed"), 0, f"summary={summary!r} out={out}")
        # 50 parameterized fast + the sleeping one + the multiphase one + the second file's test
        self.assertEqual(summary.get("passed"), 53, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("crashed"), 0, f"summary={summary!r} out={out}")
        self.assertEqual(self._fail_events(events), [], f"events={events!r}")
        self.assertNotIn("CRASHED", out, out)

    # Suite 2: a hung setup after a series of fast tests under --timeout T -> a
    # real per-test worker timeout (TestTimeoutError + FILE_DONE), not a
    # supervisor inactivity-kill (that would give crashed=1 with no counted
    # passed).
    def test_hung_setup_is_worker_timeout(self) -> None:
        events, summary, out, rc = self._run_suite_args(
            "hung_setup_timeout", ["--timeout", "2"]
        )
        self.assertEqual(rc, 1, out)
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r} out={out}")
        # 50 fast ones counted + the second file's test: FILE_DONE arrived => the worker is alive
        self.assertEqual(summary.get("passed"), 51, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("crashed"), 0, f"summary={summary!r} out={out}")
        fails = self._fail_events(events)
        self.assertEqual(len(fails), 1, f"events={events!r} out={out}")
        self.assertIn("TestTimeoutError", fails[0].get("error", ""), fails[0])
        self.assertIn("test_hung_setup", fails[0].get("id", ""), fails[0])
        self.assertIn("timed out after 2", fails[0].get("msg", ""), fails[0])
        self.assertNotIn("CRASHED", out, out)

    # Suite 3: pytest_configure of the root conftest replaces a fixture defined
    # in tests/conftest.py. Any form of introspection/fixture caching would
    # serve the stale 'initial' -> 2 failed; the live module gives 2 passed.
    def test_cross_conftest_fixture_mutation(self) -> None:
        events, summary, out, rc = self._run_suite_args(
            "cross_conftest_fixture_mutation", []
        )
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("passed"), 2, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("failed"), 0, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("crashed"), 0, f"summary={summary!r} out={out}")

    # Suite 4 (supervision gap with a delayed control plane):
    # 3 files: S (test_a_slow.py) keeps an attached worker silent for ~4 s during
    # module import (before the first frame about that file), F1/F2 are fast.
    # While slot S is silent, the supervisor has time to fully finalize its
    # neighbors: per-slot REAP, orphan-sweep and slot refill. Requirement: with
    # jobs>1 not A single file gets a spurious inactivity-kill: all files
    # complete, passed matches, crashed=0, "CRASHED" is absent from the output.
    # Checked across -j2, -j4, and --maxfail=1.
    # The FX_DELAY_S=6.0 subcase pins the collection-budget contract: a delay
    # larger than the inactivity window (--timeout 3 => max(T,1.5)+1.5 = 4.5 s)
    # after FILE_START is still not inactivity (the boundary is the 600 s
    # collection budget), so the slot must survive; a regression in
    # re-arm/budget turns this red.
    # assertGreaterEqual(duration_ms, 3900) guarantees the delay is really
    # induced, i.e. the gate is not vacuous.
    def test_controlplane_delay(self) -> None:
        cases: list[tuple[list[str], dict[str, str], int]] = [
            (["-j2", "--timeout", "3"], {}, 3900),
            (["-j4", "--timeout", "3"], {}, 3900),
            (["-j2", "--maxfail=1", "--timeout", "3"], {}, 3900),
            (["-j2", "--timeout", "3"], {"FX_DELAY_S": "6.0"}, 5900),
        ]
        for extra, env_extra, min_dur in cases:
            events, summary, out, rc = self._run_suite_jobs(
                "controlplane_delay", extra, env_extra
            )
            label = f"args={extra} env={env_extra}"
            self.assertEqual(rc, 0, f"{label}: rc={rc} out={out}")
            # 2 tests in S + 2 each in F1/F2
            self.assertEqual(summary.get("passed"), 6, f"{label}: summary={summary!r} out={out}")
            self.assertEqual(summary.get("failed"), 0, f"{label}: summary={summary!r} out={out}")
            self.assertEqual(summary.get("crashed"), 0, f"{label}: summary={summary!r} out={out}")
            self.assertEqual(self._fail_events(events), [], f"{label}: events={events!r}")
            self.assertNotIn("CRASHED", out, f"{label}: {out}")
            self.assertGreaterEqual(
                int(summary.get("duration_ms", 0)), min_dur,
                f"{label}: delay not induced, gate is vacuous: summary={summary!r}",
            )

    # --maxfail runs on the same rolling dispatcher: once the failure budget
    # is spent the refill stops and only the in-flight files drain.
    def test_maxfail_stops_refill(self) -> None:
        # -j2 fills both slots in the first iteration; no further file starts.
        events, summary, out, rc = self._run_suite_jobs(
            "maxfail_truncation", ["-j2", "--maxfail=1"], {}
        )
        fails = self._fail_events(events)
        files = {str(f.get("id", "")).split("::")[0] for f in fails}
        self.assertEqual(rc, 1, out)
        self.assertEqual(summary.get("failed"), 2, f"summary={summary!r} out={out}")
        self.assertEqual(len(files), 2, fails)

    # -j1 --maxfail=1 stops after the first failing file, like pytest -x.
    def test_maxfail_j1_matches_pytest_x(self) -> None:
        events, summary, out, rc = self._run_suite_jobs(
            "maxfail_truncation", ["-j1", "--maxfail=1"], {}
        )
        fails = self._fail_events(events)
        files = {str(f.get("id", "")).split("::")[0] for f in fails}
        self.assertEqual(rc, 1, out)
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r} out={out}")
        self.assertEqual(len(files), 1, fails)
        py_out, py_rc = self._pytest_tree("maxfail_truncation", ["-x"])
        counts = _pytest_counts(py_out)
        self.assertEqual(py_rc, 1, py_out)
        self.assertEqual(counts["failed"], 1, py_out)

    # Without --maxfail the same tree runs all six files.
    def test_maxfail_fixture_runs_fully_without_cap(self) -> None:
        events, summary, out, rc = self._run_suite_jobs(
            "maxfail_truncation", ["-j4"], {}
        )
        self.assertEqual(rc, 1, out)
        self.assertEqual(summary.get("failed"), 6, f"summary={summary!r} out={out}")
        self.assertEqual(len(self._fail_events(events)), 6, events)


    # ------------------------------------------------------------------
    # pytest parity for shim-owned behavior. Each case below runs the same
    # tree under the real pytest and asserts ztest agrees with it.
    # ------------------------------------------------------------------

    def _pytest_tree(self, fixture: str, args: list[str] | None = None) -> tuple[str, int]:
        """Run the fixture tree under real pytest and return (output, rc)."""
        src = FIXTURES / fixture
        self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
        with tempfile.TemporaryDirectory(prefix=f"pytest-{fixture}-") as td:
            work = Path(td) / fixture
            shutil.copytree(src, work)
            return _run_pytest_tree(work, args or ["tests"])

    # capfd is a file-descriptor capture: os.write(1/2) and child-process
    # output land in readouterr(), which sys-level capture cannot see.
    def test_capfd_fd_level_capture(self) -> None:
        events, summary, out, rc = self._run_suite_args("capfd_real", [])
        py_out, py_rc = self._pytest_tree("capfd_real")
        counts = _pytest_counts(py_out)
        self.assertEqual(py_rc, 0, py_out)
        self.assertEqual(counts["passed"], 4, py_out)
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("passed"), 4, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("failed"), 0, f"summary={summary!r} out={out}")
        self.assertEqual(self._fail_events(events), [], events)
        # Everything the fixture captured must stay out of the supervisor's
        # own streams.
        for marker in ("PRINTED_MARK", "FD_OUT_MARK", "FD_ERR_MARK", "CHILD_OUT_MARK", "SYS_LEVEL_MARK"):
            self.assertNotIn(marker, out)

    # --collect-only always lists nodeids; -q trims progress, not the listing.
    def test_collect_only_quiet_lists_nodeids(self) -> None:
        src = FIXTURES / "collect_quiet"
        with tempfile.TemporaryDirectory(prefix="ztest-collectq-") as td:
            work = Path(td) / "collect_quiet"
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "--collect-only", "-q"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        nodeids = sorted(line.strip() for line in proc.stdout.splitlines() if "::" in line)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(nodeids), 4, proc.stdout)
        py_out, py_rc = self._pytest_tree("collect_quiet", ["tests", "--collect-only"])
        self.assertEqual(py_rc, 0, py_out)
        py_nodeids = sorted(line.strip() for line in py_out.splitlines() if "::" in line)
        self.assertEqual(nodeids, py_nodeids, (nodeids, py_out))

    # Summary parity: the selection size is reported in run mode too.
    def test_llm_summary_collected_field(self) -> None:
        events, summary, out, rc = self._run_suite_args("collect_quiet", [])
        py_out, py_rc = self._pytest_tree("collect_quiet")
        self.assertEqual(py_rc, 0, py_out)
        collected = _pytest_counts(py_out)["passed"]
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("passed"), collected, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("collected"), collected, f"summary={summary!r} out={out}")
        # --collect-only reports the same number (one meaning of the field).
        src = FIXTURES / "collect_quiet"
        with tempfile.TemporaryDirectory(prefix="ztest-collected-") as td:
            work = Path(td) / "collect_quiet"
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "--collect-only", "--llm"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        _, collect_summary = _llm_frames(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(collect_summary.get("collected"), collected, proc.stdout)

    # An xfail mark covers the teardown phase: a failing finalizer is an
    # expected failure, and the unexpected body pass is an xpassed test.
    def test_xfail_teardown_counter_parity(self) -> None:
        events, summary, out, rc = self._run_suite_args("xfail_teardown", [])
        py_out, py_rc = self._pytest_tree("xfail_teardown")
        counts = _pytest_counts(py_out)
        self.assertEqual(py_rc, 1, py_out)
        self.assertEqual(counts["xfailed"], 6, py_out)
        self.assertEqual(counts["xpassed"], 3, py_out)
        self.assertEqual(counts["failed"], 1, py_out)
        self.assertEqual(rc, 1, out)
        # pytest's xfailed -> skipped, xpassed -> passed in the ztest counter
        # vocabulary; a strict XPASS is a real failure on both sides.
        self.assertEqual(summary.get("skipped"), 6, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("passed"), 3, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r} out={out}")
        fails = self._fail_events(events)
        self.assertEqual(
            [f.get("id") for f in fails],
            ["tests/test_strict_bodypass.py::test_body_passes"],
            fails,
        )

    # A failing finalizer fails the test once: one record, one nodeid.
    def test_teardown_failure_one_record_per_test(self) -> None:
        events, summary, out, rc = self._run_suite_args("teardown_error_dedup", [])
        py_out, py_rc = self._pytest_tree("teardown_error_dedup")
        counts = _pytest_counts(py_out)
        self.assertEqual(py_rc, 1, py_out)
        self.assertEqual(counts["error"], 2, py_out)
        self.assertEqual(rc, 1, out)
        # pytest reports the passed body next to the teardown error; an item
        # is counted once, and the finalizer failure dominates.
        self.assertEqual(summary.get("failed"), 2, f"summary={summary!r} out={out}")
        fails = self._fail_events(events)
        ids = [f.get("id") for f in fails]
        self.assertEqual(len(ids), 2, fails)
        self.assertEqual(len(set(ids)), 2, fails)
        for record in fails:
            self.assertIn("module teardown boom", str(record.get("msg", "")), record)

    # pytest resolves its config from the target's ancestor chain, not from
    # the process CWD.
    def test_ini_discovery_from_target_ancestors(self) -> None:
        for fixture in ("ini_from_ancestor", "ini_pyproject_ancestor"):
            src = FIXTURES / fixture
            self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
            with tempfile.TemporaryDirectory(prefix=f"ztest-{fixture}-") as td:
                root = Path(td) / fixture
                shutil.copytree(src, root)
                elsewhere = Path(td) / "elsewhere"
                elsewhere.mkdir()
                target = root / "proj" / "tests"
                proc = subprocess.run(
                    [ZTEST_BIN, str(target), "--python", sys.executable, "-j1", "--llm"],
                    cwd=elsewhere,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                py_out, py_rc = _run_pytest_tree(elsewhere, [str(target)])
            events, summary = _llm_frames(proc.stdout)
            label = f"fixture={fixture} out={proc.stdout}{proc.stderr}"
            self.assertEqual(py_rc, 1, py_out)
            self.assertEqual(_pytest_counts(py_out)["failed"], 1, py_out)
            self.assertEqual(proc.returncode, 1, label)
            self.assertEqual(summary.get("failed"), 1, label)
            self.assertEqual(summary.get("passed"), 0, label)
            self.assertEqual(len(self._fail_events(events)), 1, label)

    # What the test printed belongs to the failure context.
    def test_failure_report_includes_captured_output(self) -> None:
        events, summary, out, rc = self._run_suite_args("failure_capture", [])
        py_out, py_rc = self._pytest_tree("failure_capture")
        self.assertEqual(py_rc, 1, py_out)
        self.assertEqual(rc, 1, out)
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r} out={out}")
        fails = self._fail_events(events)
        self.assertEqual(len(fails), 1, fails)
        msg = str(fails[0].get("msg", ""))
        for marker in (
            "Captured stdout call",
            "Captured stderr call",
            "STDOUT_BEFORE_FAILURE",
            "helper saw 4",
            "STDERR_DIAGNOSTIC",
            "values differ",
        ):
            self.assertIn(marker, msg, msg)
            self.assertIn(marker, py_out, py_out)
        # The human report carries the same block.
        src = FIXTURES / "failure_capture"
        with tempfile.TemporaryDirectory(prefix="ztest-capture-") as td:
            work = Path(td) / "failure_capture"
            shutil.copytree(src, work)
            human = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(human.returncode, 1, human.stdout + human.stderr)
        self.assertIn("Captured stdout call", human.stdout, human.stdout)
        self.assertIn("Captured stderr call", human.stdout, human.stdout)

    # Reports carry the test's frames only: the runner's own runtime path is
    # internal and never surfaces.
    def test_traceback_excludes_runner_internals(self) -> None:
        events, summary, out, rc = self._run_suite_args("collection_error_capture", [])
        # Only the report content is pinned here; the exit-code mapping of a
        # collection error is the supervisor's contract.
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r} out={out}")
        fails = self._fail_events(events)
        self.assertEqual(len(fails), 1, fails)
        msg = str(fails[0].get("msg", ""))
        self.assertIn("modifyitems boom", msg, msg)
        self.assertIn("conftest.py", msg, msg)
        for blob in (out, json.dumps(fails)):
            self.assertNotIn("ztest_shim", blob)
            self.assertNotIn("/tmp/ztest_", blob)
        src = FIXTURES / "collection_error_capture"
        with tempfile.TemporaryDirectory(prefix="ztest-tbpath-") as td:
            work = Path(td) / "collection_error_capture"
            shutil.copytree(src, work)
            human = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertNotEqual(human.returncode, 0, human.stdout + human.stderr)
        self.assertNotIn("ztest_shim", human.stdout + human.stderr)
        self.assertIn("modifyitems boom", human.stdout, human.stdout)
        # pytest shows the same conftest frame without its own internals.
        py_out, py_rc = self._pytest_tree("collection_error_capture")
        self.assertEqual(py_rc, 3, py_out)
        self.assertIn("modifyitems boom", py_out, py_out)


    # -k matches marker names, not only the nodeid: pytest evaluates the
    # expression over item.keywords, which carries every mark attached to the
    # item (own marks and the enclosing class's marks).
    def test_k_expression_matches_marker_names(self) -> None:
        py_out, py_rc = self._pytest_tree("k_marker_match", ["tests", "-k", "slow"])
        py_counts = _pytest_counts(py_out)
        self.assertEqual(py_rc, 0, py_out)
        self.assertEqual(py_counts["passed"], 3, py_out)
        self.assertEqual(py_counts["deselected"], 1, py_out)

        events, summary, out, rc = self._run_suite_args("k_marker_match", ["-k", "slow"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("passed"), 3, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("failed"), 0, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("deselected"), 1, f"summary={summary!r} out={out}")
        self.assertEqual(self._fail_events(events), [], events)
        self.assertEqual(
            self._collect_ids("k_marker_match", ["-k", "slow"]),
            {
                "tests/test_marked.py::test_alpha",
                "tests/test_marked.py::TestBunch::test_gamma",
                "tests/test_marked.py::TestBunch::test_delta",
            },
            "marker-selected nodeids",
        )

        # Boolean expressions keep working with the widened haystack.
        py_out, py_rc = self._pytest_tree("k_marker_match", ["tests", "-k", "slow and gamma"])
        py_counts = _pytest_counts(py_out)
        self.assertEqual(py_rc, 0, py_out)
        self.assertEqual(py_counts["passed"], 1, py_out)
        self.assertEqual(py_counts["deselected"], 3, py_out)
        events, summary, out, rc = self._run_suite_args("k_marker_match", ["-k", "slow and gamma"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("passed"), 1, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("deselected"), 3, f"summary={summary!r} out={out}")

        # Name matching itself is unchanged: a plain nodeid substring still
        # selects exactly the same test it always did.
        events, summary, out, rc = self._run_suite_args("k_marker_match", ["-k", "beta"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(summary.get("passed"), 1, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("deselected"), 3, f"summary={summary!r} out={out}")

    # -s sends the tests' own stdout to the supervisor's stdout even when that
    # stream is a pipe or a file: the worker leaves through os._exit(), which
    # skipped the buffered-flush, so only the line-buffered stderr survived.
    def test_no_capture_stdout_survives_a_pipe(self) -> None:
        src = FIXTURES / "no_capture_pipe"
        self.assertTrue(src.is_dir(), "fixture no_capture_pipe missing")
        py_out, py_rc = self._pytest_tree("no_capture_pipe", ["tests", "-s"])
        self.assertEqual(py_rc, 0, py_out)
        self.assertIn("STDOUT_MARKER_PIPE", py_out, py_out)
        self.assertIn("STDERR_MARKER_PIPE", py_out, py_out)
        with tempfile.TemporaryDirectory(prefix="ztest-nocap-") as td:
            work = Path(td) / "no_capture_pipe"
            shutil.copytree(src, work)
            piped = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1", "-s"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            out_path = Path(td) / "captured_stdout.txt"
            err_path = Path(td) / "captured_stderr.txt"
            with open(out_path, "w", encoding="utf-8") as fo, open(err_path, "w", encoding="utf-8") as fe:
                redirected = subprocess.run(
                    [ZTEST_BIN, "tests", "--python", sys.executable, "-j1", "-s"],
                    cwd=work,
                    stdout=fo,
                    stderr=fe,
                    text=True,
                    timeout=120,
                )
            captured = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            redirected_out = out_path.read_text(encoding="utf-8")
            redirected_err = err_path.read_text(encoding="utf-8")
        self.assertEqual(piped.returncode, 0, piped.stdout + piped.stderr)
        self.assertIn("STDOUT_MARKER_PIPE", piped.stdout, piped.stdout)
        self.assertIn("STDERR_MARKER_PIPE", piped.stderr, piped.stderr)
        self.assertEqual(redirected.returncode, 0, redirected_out + redirected_err)
        self.assertIn("STDOUT_MARKER_PIPE", redirected_out)
        self.assertIn("STDERR_MARKER_PIPE", redirected_err)
        # Without -s the capture contract is unchanged: nothing leaks.
        self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
        self.assertNotIn("STDOUT_MARKER_PIPE", captured.stdout + captured.stderr)
        self.assertNotIn("STDERR_MARKER_PIPE", captured.stdout + captured.stderr)

    # A worker that dies mid-file must keep what verifiably finished: the
    # per-test frames stand in for the FILE_DONE that never arrived, and the
    # collected count comes from COLLECTION_DONE instead of collapsing to 0.
    def test_crash_keeps_earlier_results_and_collected_count(self) -> None:
        events, summary, out, rc = self._run_suite_args("crash_partial_counts", [])
        self.assertEqual(rc, 1, out)
        self.assertEqual(summary.get("passed"), 3, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("crashed"), 1, f"summary={summary!r} out={out}")
        self.assertEqual(summary.get("collected"), 5, f"summary={summary!r} out={out}")
        crashes = [e for e in events if e.get("t") == "crash"]
        self.assertEqual(len(crashes), 1, events)
        self.assertIn("test_crash.py", str(crashes[0].get("file")), crashes)
        # The reported collected count is the real one, not a guess: it equals
        # the --collect-only listing of the same tree.
        self.assertEqual(len(self._collect_ids("crash_partial_counts", [])), 5)

    # SIGINT must not be a silent exit: the operator gets the tallies of the
    # files that finished (stderr), and the exit status stays pytest's 2.
    def test_interrupt_reports_partial_tally(self) -> None:
        src = FIXTURES / "interrupt_tally"
        self.assertTrue(src.is_dir(), "fixture interrupt_tally missing")
        with tempfile.TemporaryDirectory(prefix="ztest-int-") as td:
            work = Path(td) / "interrupt_tally"
            shutil.copytree(src, work)
            sentinel = Path(td) / "slow_started.flag"
            env = dict(os.environ)
            env["ZT_INTERRUPT_SENTINEL"] = str(sentinel)
            env.pop("PYTEST_CURRENT_TEST", None)
            env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
            proc = subprocess.Popen(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1"],
                cwd=work,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            try:
                # -j1 keeps file order: the slow file starts only after the fast
                # file's FILE_DONE, so the interrupt lands with one file behind it.
                deadline = time.time() + 120
                while time.time() < deadline and not sentinel.exists() and proc.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(sentinel.exists(), "the slow file never started")
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=120)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        self.assertEqual(proc.returncode, 2, out + err)
        self.assertIn("ztest: interrupted (", err, err)
        self.assertIn("3 passed", err, err)
        self.assertIn("files finished: 1", err, err)
        # The diagnostic goes to stderr only: the progress stream stays clean.
        self.assertNotIn("ztest: interrupted", out, out)

    def _run_suite_human(self, fixture: str) -> tuple[str, int]:
        """Run WITHOUT --llm; return (combined output, returncode).

        The summary line's terminal form only exists in the human mode, so
        the collection-error note is asserted on this output.
        """
        src = FIXTURES / fixture
        self.assertTrue(src.is_dir(), f"fixture {fixture} missing")
        with tempfile.TemporaryDirectory(prefix=f"ztest-{fixture}-") as td:
            work = Path(td) / fixture
            shutil.copytree(src, work)
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "--python", sys.executable, "-j1"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        return proc.stdout + proc.stderr, proc.returncode

    # A file that dies at import time must be visibly different from a
    # failing test: the summary names the collection error, the --llm
    # contract carries collection_errors, and the exit code stays the
    # pytest-parity 2 (a collection error interrupts the run in pytest).
    def test_collection_error_visible_in_llm_summary(self) -> None:
        events, summary, out, rc = self._run_suite_args("collection_error", [])
        self.assertEqual(rc, 2, f"rc={rc} out={out}")
        # The dead file counts as one failure...
        self.assertEqual(summary.get("failed"), 1, f"summary={summary!r}")
        # ...and is separately identified as a collection error.
        self.assertEqual(summary.get("collection_errors"), 1, f"summary={summary!r}")
        # Exactly one fail frame, of the collection kind: the file's test
        # never ran, so no test-level failure exists.
        fails = self._fail_events(events)
        self.assertEqual(len(fails), 1, f"events={events!r}")
        self.assertEqual(fails[0].get("error"), "CollectionError", f"events={events!r}")
        self.assertTrue(fails[0].get("id", "").endswith("::<collection>"), f"events={events!r}")

    def test_collection_error_human_summary_segment(self) -> None:
        out, rc = self._run_suite_human("collection_error")
        self.assertEqual(rc, 2, f"rc={rc} out={out}")
        self.assertIn("1 failed (1 collection error — file not run)", out)

    # One real failing test next to one file dead at import: both counters
    # stay distinguishable (2 failed = 1 failing test + 1 dead file).
    def test_collection_error_mixed_with_test_failure(self) -> None:
        events, summary, out, rc = self._run_suite_args("collection_error_mixed", [])
        self.assertEqual(rc, 2, f"rc={rc} out={out}")
        self.assertEqual(summary.get("passed"), 1, f"summary={summary!r}")
        self.assertEqual(summary.get("failed"), 2, f"summary={summary!r}")
        self.assertEqual(summary.get("collection_errors"), 1, f"summary={summary!r}")
        # Two fail frames of distinguishable kinds: one dead file
        # (CollectionError) and one real failing test.
        fails = self._fail_events(events)
        self.assertEqual(len(fails), 2, f"events={events!r}")
        kinds = sorted(e.get("error") == "CollectionError" for e in fails)
        self.assertEqual(kinds, [False, True], f"events={events!r}")
        out_h, rc_h = self._run_suite_human("collection_error_mixed")
        self.assertEqual(rc_h, 2, f"rc={rc_h} out={out_h}")
        self.assertIn("1 passed, 2 failed (1 collection error — file not run)", out_h)

    # The --llm field is a stable contract: present (and 0) on a healthy
    # suite, and the human summary omits the collection-errors segment.
    def test_collection_errors_field_zero_when_healthy(self) -> None:
        _, summary, _ = self._run_suite("report_header")
        self.assertEqual(summary.get("collection_errors"), 0, f"summary={summary!r}")
        out, rc = self._run_suite_human("report_header")
        self.assertEqual(rc, 0, f"rc={rc} out={out}")
        self.assertNotIn("collection error", out)

    # pyenv/conda/mise trap: with no --python/$VIRTUAL_ENV/./.venv, a python3
    # found on $PATH outside /usr/bin is the user's deliberate toolchain and
    # must be used without the system-interpreter warning; landing on
    # /usr/bin/python3 must warn.
    def test_path_python3_precedence_over_usr_bin(self) -> None:
        src = FIXTURES / "report_header"
        self.assertTrue(src.is_dir(), "fixture report_header missing")
        with tempfile.TemporaryDirectory(prefix="ztest-path-resolv-") as td:
            work = Path(td) / "fx"
            shutil.copytree(src, work)
            fakebin = Path(td) / "fakepyenv"
            fakebin.mkdir()
            marker = Path(td) / "marker"
            fake_py = fakebin / "python3"
            fake_py.write_text(
                "#!/bin/sh\n"
                f'echo used >> "{marker}"\n'
                f'exec "{sys.executable}" "$@"\n'
            )
            fake_py.chmod(0o755)
            env = dict(os.environ)
            env.pop("VIRTUAL_ENV", None)
            env["PATH"] = f"{fakebin}:/usr/bin:/bin"
            proc = subprocess.run(
                [ZTEST_BIN, "tests", "-j1", "--llm"],
                cwd=work, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(marker.exists(), f"$PATH python3 was not used: {proc.stderr}")
            self.assertNotIn("no virtualenv found", proc.stderr)

            # Control: the same run without the fake dir lands on
            # /usr/bin/python3 and emits the fallback warning.
            marker.unlink()
            env["PATH"] = "/usr/bin:/bin"
            proc2 = subprocess.run(
                [ZTEST_BIN, "tests", "-j1", "--llm"],
                cwd=work, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
            self.assertIn("no virtualenv found", proc2.stderr)
            self.assertFalse(marker.exists(), "system fallback unexpectedly used the fake python3")

    def test_cached_stdout_buffer_and_import_print_do_not_bleed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ztest-stdout-buf-") as td:
            work = Path(td)
            tdir = work / "tests"
            tdir.mkdir()
            (tdir / "test_sources.py").write_text(
                "import pickle, sys\n"
                "print('import-time banner')\n"
                "stdout_binary = sys.stdout.buffer\n\n"
                "def test_one():\n"
                "    pickle.dump(('foo', 'bar'), stdout_binary, protocol=5)\n"
                "    pickle.dump(('a', 1), stdout_binary, protocol=5)\n\n"
                "def test_two(capfd):\n"
                "    stdout_binary.write(b'foo,bar\\r\\na,1\\r\\nb,2\\r\\n')\n"
                "    out = capfd.readouterr().out\n"
                "    assert out in ('foo,bar\\r\\na,1\\r\\nb,2\\r\\n', 'foo,bar\\na,1\\nb,2\\n'), repr(out)\n\n"
                "def test_three():\n"
                "    pickle.dump(('Johann Strauß', 2), stdout_binary, protocol=5)\n",
                encoding="utf-8",
            )
            (tdir / "test_second.py").write_text(
                "print('second import banner')\n"
                "def test_ok():\n"
                "    assert 1 + 1 == 2\n",
                encoding="utf-8",
            )
            for jobs in ("-j1", "-j2"):
                proc = subprocess.run(
                    [ZTEST_BIN, "tests", jobs, "--llm"],
                    cwd=work, capture_output=True, timeout=120,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace"))
                lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
                self.assertGreaterEqual(len(lines), 1)
                for ln in lines:
                    obj = json.loads(ln.decode("utf-8"))
                    self.assertIn("t", obj)
                summary = json.loads(lines[-1].decode("utf-8"))
                self.assertEqual(summary.get("passed"), 4, summary)
                self.assertEqual(summary.get("failed"), 0, summary)

    def test_raises_check_array_truth_custom_setup_and_cli_validation(self) -> None:
        """Pin raises(check=...), array ambiguous truth ValueError, custom collector
        pytest_runtest_setup / collection_modifyitems / markers, multiline python_files
        with ? and [...], exit code 4 on unimplemented semantic CLI flags and invalid
        -k expressions, single pytestmark on a class, and xfail(raises=...) on
        fixture finalizers."""
        with tempfile.TemporaryDirectory(prefix="ztest-parity-") as tmp:
            work = Path(tmp)
            # 1) raises(check=...), array truth ValueError, collection_modifyitems order, single class mark, finalizer xfail(raises=...)
            s1 = work / "s1"
            s1.mkdir()
            (s1 / "conftest.py").write_text(
                "def pytest_collection_modifyitems(session, config, items):\n"
                "    items.reverse()\n",
                encoding="utf-8",
            )
            (s1 / "test_all.py").write_text(
                "import pytest\n\n"
                "ORDER = []\n\n"
                "class Vec:\n"
                "    def __init__(self, d):\n"
                "        self.d = d\n"
                "        self.shape = (len(d),)\n"
                "        self.size = len(d)\n"
                "    def __eq__(self, o):\n"
                "        return Vec([a == b for a, b in zip(self.d, o.d)])\n"
                "    def __bool__(self):\n"
                "        raise ValueError('The truth value of an array with more than one element is ambiguous')\n"
                "    def all(self):\n"
                "        return all(self.d)\n\n"
                "class TestSingleClassMark:\n"
                "    pytestmark = pytest.mark.skip(reason='class-level single mark')\n"
                "    def test_skipped_method(self):\n"
                "        assert False\n\n"
                "@pytest.fixture\n"
                "def fin_raises():\n"
                "    yield\n"
                "    raise ValueError('expected finalizer')\n\n"
                "@pytest.mark.xfail(raises=ValueError)\n"
                "def test_finalizer_xfail(fin_raises):\n"
                "    pass\n\n"
                "def test_raises_check_only_and_callable():\n"
                "    def boom(*, check):\n"
                "        assert check == 'payload'\n"
                "        raise ValueError('good')\n"
                "    pytest.raises(ValueError, boom, check='payload')\n"
                "    with pytest.raises(check=lambda e: isinstance(e, ValueError)):\n"
                "        raise ValueError('good')\n\n"
                "def test_first_in_file_runs_second():\n"
                "    assert ORDER == ['second']\n\n"
                "def test_second_in_file_runs_first():\n"
                "    ORDER.append('second')\n\n"
                "def test_raises_check_false_fails():\n"
                "    with pytest.raises(ValueError, check=lambda e: 'good' in str(e)):\n"
                "        raise ValueError('bad')\n\n"
                "def test_array_ambiguous_truth_raises_value_error():\n"
                "    assert Vec([1, 2]) == Vec([1, 2])\n",
                encoding="utf-8",
            )
            p1 = subprocess.run([ZTEST_BIN, str(s1), "-j1", "--llm"], capture_output=True, timeout=60)
            self.assertEqual(p1.returncode, 1)
            sum1 = json.loads([ln for ln in p1.stdout.splitlines() if ln.strip()][-1].decode("utf-8"))
            self.assertEqual(sum1["passed"], 4, sum1)
            self.assertEqual(sum1["failed"], 2, sum1)
            self.assertEqual(sum1["skipped"], 2, sum1)

            # 2) Custom collector item honours pytest_runtest_setup failure
            s2 = work / "s2"
            s2.mkdir()
            (s2 / "conftest.py").write_text(
                "import pytest\n\n"
                "class MyItem(pytest.Item):\n"
                "    def runtest(self):\n"
                "        pass\n\n"
                "class MyFile(pytest.File):\n"
                "    def collect(self):\n"
                "        yield MyItem.from_parent(self, name='custom_one')\n\n"
                "def pytest_collect_file(file_path, parent):\n"
                "    if file_path.suffix == '.custom':\n"
                "        return MyFile.from_parent(parent, path=file_path)\n\n"
                "def pytest_runtest_setup(item):\n"
                "    raise RuntimeError('setup boom')\n",
                encoding="utf-8",
            )
            (s2 / "case.custom").write_text("x\n", encoding="utf-8")
            p2 = subprocess.run([ZTEST_BIN, str(s2 / "case.custom"), "-j1", "--llm"], capture_output=True, timeout=60)
            self.assertEqual(p2.returncode, 1)
            sum2 = json.loads([ln for ln in p2.stdout.splitlines() if ln.strip()][-1].decode("utf-8"))
            self.assertEqual(sum2["passed"], 0, sum2)
            self.assertEqual(sum2["failed"], 1, sum2)

            # 3) Multiline python_files in pyproject.toml with [...] class not last and leading [...]
            s3 = work / "s3"
            s3.mkdir()
            (s3 / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\n"
                "python_files = [\n"
                "    'spec_[ab].py',\n"
                "    '[ab]check_?.py',\n"
                "]\n",
                encoding="utf-8",
            )
            (s3 / "acheck_1.py").write_text("def test_a(): assert True\n", encoding="utf-8")
            (s3 / "spec_b.py").write_text("def test_b(): assert True\n", encoding="utf-8")
            p3 = subprocess.run([ZTEST_BIN, ".", "-j1", "--llm"], cwd=s3, capture_output=True, timeout=60)
            self.assertEqual(p3.returncode, 0, p3.stderr.decode("utf-8", "replace"))
            sum3 = json.loads([ln for ln in p3.stdout.splitlines() if ln.strip()][-1].decode("utf-8"))
            self.assertEqual(sum3["passed"], 2, sum3)

            # 4) Exit code 4 on unimplemented semantic CLI flags and bad -k expressions
            for bad_args in (["-W", "error"], ["-o", "xfail_strict=true"], ["--strict-markers"], ["-k", "foo bar"], ["-k", "(foo"]):
                p4 = subprocess.run([ZTEST_BIN, str(s3), *bad_args], capture_output=True, timeout=30)
                self.assertEqual(p4.returncode, 4, f"Expected rc=4 for {bad_args}, got {p4.returncode}")

            # 5) Custom collector parent File mark inheritance, dynamic strict xfail in setup, and modifyitems append
            s5 = work / "s5"
            s5.mkdir()
            (s5 / "conftest.py").write_text(
                "import pytest\n\n"
                "class MyItem(pytest.Item):\n"
                "    def setup(self):\n"
                "        if self.name == 'strict_xpass':\n"
                "            self.add_marker(pytest.mark.xfail(strict=True, reason='dynamic'))\n"
                "    def runtest(self):\n"
                "        pass\n\n"
                "class MyFile(pytest.File):\n"
                "    def collect(self):\n"
                "        self.add_marker(pytest.mark.skipif(False, True, reason='multi-cond'))\n"
                "        yield MyItem.from_parent(self, name='skipped_by_parent')\n\n"
                "def pytest_collect_file(file_path, parent):\n"
                "    if file_path.suffix == '.custom':\n"
                "        return MyFile.from_parent(parent, path=file_path)\n\n"
                "def pytest_collection_modifyitems(items):\n"
                "    if items:\n"
                "        root_parent = items[0].parent.parent\n"
                "        items.append(MyItem.from_parent(root_parent, name='strict_xpass'))\n",
                encoding="utf-8",
            )
            (s5 / "case.custom").write_text("x\n", encoding="utf-8")
            p5 = subprocess.run([ZTEST_BIN, str(s5 / "case.custom"), "-j1", "--llm"], capture_output=True, timeout=60)
            self.assertEqual(p5.returncode, 1)
            sum5 = json.loads([ln for ln in p5.stdout.splitlines() if ln.strip()][-1].decode("utf-8"))
            self.assertEqual(sum5["skipped"], 1, sum5)
            self.assertEqual(sum5["failed"], 1, sum5)

            # 6) Multiple failing finalizers wrapped in BaseExceptionGroup do not match single-type xfail(raises=...)
            s6 = work / "s6"
            s6.mkdir()
            (s6 / "test_multi_fin.py").write_text(
                "import pytest\n\n"
                "@pytest.fixture\n"
                "def two_bad(request):\n"
                "    def f1(): raise ValueError('one')\n"
                "    def f2(): raise ValueError('two')\n"
                "    request.addfinalizer(f1)\n"
                "    request.addfinalizer(f2)\n\n"
                "@pytest.mark.xfail(raises=ValueError, strict=True)\n"
                "def test_it(two_bad):\n"
                "    pass\n",
                encoding="utf-8",
            )
            p6 = subprocess.run([ZTEST_BIN, str(s6), "-j1", "--llm"], capture_output=True, timeout=60)
            self.assertEqual(p6.returncode, 1)

            # 7) TOML unicode escape in python_files
            s7 = work / "s7"
            s7.mkdir()
            (s7 / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\npython_files = ["\\u0063heck_*.py"]\n',
                encoding="utf-8",
            )
            (s7 / "check_u.py").write_text("def test_u(): assert True\n", encoding="utf-8")
            p7 = subprocess.run([ZTEST_BIN, ".", "-j1", "--llm"], cwd=s7, capture_output=True, timeout=60)
            self.assertEqual(p7.returncode, 0)
            sum7 = json.loads([ln for ln in p7.stdout.splitlines() if ln.strip()][-1].decode("utf-8"))
            self.assertEqual(sum7["passed"], 1, sum7)




class TestShimInternals(unittest.TestCase):
    """Direct unit pins of the shim (module import, no binary run)."""

    @staticmethod
    def _import_shim():
        import importlib.util
        shim_path = Path(__file__).resolve().parent.parent / "src" / "shim" / "ztest_shim.py"
        spec = importlib.util.spec_from_file_location("zshim_probe", shim_path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_k_haystack_includes_marker_names(self) -> None:
        # A unit pin of the -k haystack builder: marker names join the nodeid,
        # class marks are inherited, and a class whose attribute lookup raises
        # must not break the selection.
        m = self._import_shim()

        class _Mark:
            def __init__(self, name: str) -> None:
                self.name = name

        hay = m._k_haystack("tests/test_a.py::test_x", [_Mark("slow"), _Mark("api")])
        self.assertIn("tests/test_a.py::test_x", hay)
        self.assertIn("slow", hay)
        self.assertIn("api", hay)

        class _ClassMarked:
            pytestmark = [_Mark("db")]

        self.assertIn("db", m._k_haystack("t.py::C::test_y", None, _ClassMarked))
        # A single mark (not a list) is legal on a class as well.
        class _SingleMarked:
            pytestmark = _Mark("heavy")

        self.assertIn("heavy", m._k_haystack("t.py::D::test_z", None, _SingleMarked))

        class _Exploding:
            def __getattr__(self, name: str) -> object:
                raise RuntimeError(name)

        self.assertIn("t.py::test_plain", m._k_haystack("t.py::test_plain", None, _Exploding))
        # With no markers attached, only the lowercased nodeid is returned.
        self.assertEqual(m._k_haystack("T.py::Test_X::test_Y"), "t.py::test_x::test_y")

    def test_ast_cache_key_includes_shim_version(self) -> None:
        # A SHIM_VERSION bump must invalidate the AST cache (otherwise a
        # compile-flag change silently does not apply to cached modules).
        m = self._import_shim()
        with tempfile.TemporaryDirectory(prefix="zshim-cache-") as td:
            tdp = Path(td)
            f = tdp / "t_mod.py"
            f.write_text("X = 1\n")
            m.load_module_with_ast_cache(f, "t_mod", tdp)
            n1 = len(list((tdp / "ast").glob("*.pyc")))
            self.assertEqual(n1, 1)
            m.SHIM_VERSION = m.SHIM_VERSION + "-probe"
            m.load_module_with_ast_cache(f, "t_mod", tdp)
            n2 = len(list((tdp / "ast").glob("*.pyc")))
            self.assertEqual(n2, 2, "SHIM_VERSION bump did not invalidate the cache")

            # Compound AST assert rewrite: chained Compare (single-eval + short-circuit),
            # BoolOp (and/or short-circuit), UnaryOp(not), and Call with argument values.
            f_compound = tdp / "t_compound.py"
            f_compound.write_text(
                "calls = []\n"
                "def bump(v):\n"
                "    calls.append(v)\n"
                "    return v\n"
                "def check_chain_fail():\n"
                "    assert 0 < bump(15) < 10\n"
                "def check_chain_short_circuit():\n"
                "    assert 10 < bump(5) < bump(99)\n"
                "def check_and_fail():\n"
                "    a, b = 1, 5\n"
                "    assert a == 1 and b == 2 and bump(999) == 999\n"
                "def check_or_fail():\n"
                "    x, y = 3, 4\n"
                "    assert x == 1 or y == 2\n"
                "def check_not_fail():\n"
                "    errors = ['bad_token']\n"
                "    assert not errors\n"
                "def check_call_fail():\n"
                "    def is_valid(role, strict=True):\n"
                "        return False\n"
                "    r = 'guest'\n"
                "    assert is_valid(r, strict=True)\n"
            )
            mod_c = m.load_module_with_ast_cache(f_compound, "t_compound", tdp)
            with self.assertRaises(AssertionError) as cm_chain:
                mod_c.check_chain_fail()
            self.assertEqual(mod_c.calls, [15], "middle operand of chained comparison must be evaluated once")
            self.assertIn("Left:  15", str(cm_chain.exception))
            self.assertIn("Right: 10 (op: <)", str(cm_chain.exception))

            mod_c.calls.clear()
            with self.assertRaises(AssertionError):
                mod_c.check_chain_short_circuit()
            self.assertEqual(mod_c.calls, [5], "chained comparison must short-circuit on first false link")

            mod_c.calls.clear()
            with self.assertRaises(AssertionError) as cm_and:
                mod_c.check_and_fail()
            self.assertEqual(mod_c.calls, [], "BoolOp(And) must short-circuit after failing branch")
            self.assertIn("assert b == 2", str(cm_and.exception))
            self.assertIn("Left:  5", str(cm_and.exception))

            with self.assertRaises(AssertionError) as cm_or:
                mod_c.check_or_fail()
            self.assertIn("assert x == 1", str(cm_or.exception))
            self.assertIn("assert y == 2", str(cm_or.exception))

            with self.assertRaises(AssertionError) as cm_not:
                mod_c.check_not_fail()
            self.assertIn("where errors = ['bad_token']", str(cm_not.exception))

            with self.assertRaises(AssertionError) as cm_call:
                mod_c.check_call_fail()
            self.assertIn("where False = is_valid(r='guest', strict=True)", str(cm_call.exception))

            # Builtin fixtures (doctest_namespace, capsysbinary, capfdbinary, capteesys,
            # record_property, benchmark), ExceptionInfo._excinfo, and empty parametrize [NOTSET].
            rc_ctx = m.raises(ValueError)
            with rc_ctx as ei:
                raise ValueError("boom")
            self.assertIsNotNone(ei._excinfo)
            self.assertIs(ei._excinfo[0], ValueError)
            self.assertEqual(str(ei._excinfo[1]), "boom")

            f_empty = tdp / "test_empty_param.py"
            f_empty.write_text(
                "import pytest\n"
                "@pytest.mark.parametrize(('a', 'b'), [])\n"
                "def test_empty(a, b):\n"
                "    pass\n"
            )
            mod_e = m.load_module_with_ast_cache(f_empty, "test_empty_param", tdp)
            specs_e = m._expand_parametrize(
                "test_empty", "test_empty_param.py", None, f_empty,
                mod_e.test_empty, None, mod_e,
                list(getattr(mod_e.test_empty, "pytestmark", [])),
            )
            self.assertEqual(len(specs_e), 1)
            self.assertEqual(specs_e[0].nodeid, "test_empty_param.py::test_empty[NOTSET]")
            self.assertTrue(any(mk.name == "skip" for mk in specs_e[0].markers))

    def test_pytest_engine_bridge_end_to_end(self) -> None:
        """--engine=pytest runs upstream pytest inside the supervised worker and
        reports ZTST binary frames (pass/fail/skip/deselect, --collect-only, addopts retry)."""
        with tempfile.TemporaryDirectory(prefix="ztest-engine-pytest-") as td:
            tdp = Path(td)
            (tdp / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\n'
                'addopts = "--uninstalled-plugin-flag-xyz"\n'
            )
            (tdp / "conftest.py").write_text(
                "import pytest\n"
                "def pytest_collection_modifyitems(session, config, items):\n"
                "    for it in items:\n"
                "        if 'custom_hook_skip' in it.name:\n"
                "            it.add_marker(pytest.mark.skip(reason='skipped by pluggy hook'))\n"
            )
            (tdp / "test_sample.py").write_text(
                "import pytest\n"
                "def test_ok():\n"
                "    assert 1 + 1 == 2\n"
                "def test_custom_hook_skip():\n"
                "    assert False\n"
                "@pytest.mark.xfail(reason='expected fail')\n"
                "def test_xf():\n"
                "    assert 1 == 2\n"
                "def test_deselected_by_k():\n"
                "    assert True\n"
            )
            r_co = subprocess.run(
                [str(ZTEST_BIN), "--engine=pytest", "--collect-only", "-k", "not deselected", "test_sample.py"],
                cwd=tdp,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(r_co.returncode, 0, r_co.stderr)
            co_lines = [ln.strip() for ln in r_co.stdout.splitlines() if "::" in ln]
            self.assertEqual(
                co_lines,
                [
                    "test_sample.py::test_ok",
                    "test_sample.py::test_custom_hook_skip",
                    "test_sample.py::test_xf",
                ],
            )

            r_run = subprocess.run(
                [str(ZTEST_BIN), "--engine=pytest", "--llm", "-k", "not deselected", "test_sample.py"],
                cwd=tdp,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(r_run.returncode, 0, r_run.stderr)
            summary = json.loads(r_run.stdout.strip().splitlines()[-1])
            self.assertEqual(summary["passed"], 1)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["skipped"], 2)
            self.assertEqual(summary["deselected"], 1)
            self.assertEqual(summary["collected"], 3)

            r_bad = subprocess.run(
                [str(ZTEST_BIN), "--engine=unknown", "test_sample.py"],
                cwd=tdp,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(r_bad.returncode, 4)

            # capfdbinary unread tail in teardown + capteesys tee output + BadRepr in assert
            (tdp / "test_edge_parity.py").write_text(
                "import os\n"
                "def test_capfdbinary_unread_tail(capfdbinary):\n"
                "    os.write(1, b'unread-binary-tail')\n"
                "def test_capteesys_tee(capteesys):\n"
                "    print('TEE-MUST-BE-VISIBLE')\n"
                "    assert capteesys.readouterr().out == 'TEE-MUST-BE-VISIBLE\\n'\n"
            )
            r_edge = subprocess.run(
                [str(ZTEST_BIN), "-s", "test_edge_parity.py"],
                cwd=tdp,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r_edge.returncode, 0, r_edge.stderr)
            self.assertIn("TEE-MUST-BE-VISIBLE", r_edge.stdout)

            (tdp / "test_bad_repr.py").write_text(
                "class BadRepr:\n"
                "    def __repr__(self): raise RuntimeError('repr boom')\n"
                "    def __eq__(self, o): return False\n"
                "    def __bool__(self): return False\n"
                "class ExplodingFalsy:\n"
                "    called = 0\n"
                "    def __bool__(self): return False\n"
                "    def __repr__(self):\n"
                "        ExplodingFalsy.called += 1\n"
                "        raise RuntimeError('must not be called when Or succeeds')\n"
                "def test_or_lazy_repr():\n"
                "    assert ExplodingFalsy() or True\n"
                "    assert (ExplodingFalsy() == 1) or True\n"
                "    assert ExplodingFalsy.called == 0\n"
                "def test_bad():\n"
                "    assert BadRepr() == 1\n"
            )
            r_br = subprocess.run(
                [str(ZTEST_BIN), "test_bad_repr.py"],
                cwd=tdp,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r_br.returncode, 1)
            self.assertIn("1 passed, 1 failed", r_br.stdout)
            self.assertIn("AssertionError: assert BadRepr() == 1", r_br.stdout)
            self.assertIn("raised in repr()", r_br.stdout)

            # B3 + B4: --engine=pytest preserves ini options on addopts retry (-o addopts=)
            # and fails the file when pytest_sessionfinish sets non-zero exitstatus after collection.
            b34_dir = tdp / "b34"
            b34_dir.mkdir()
            (b34_dir / "pyproject.toml").write_text(
                '[tool.pytest.ini_options]\naddopts = "--nonexistent-plugin-flag"\nxfail_strict = true\n'
            )
            (b34_dir / "test_strict.py").write_text(
                "import pytest\n@pytest.mark.xfail\ndef test_unexpected_pass():\n    assert True\n"
            )
            r_b4 = subprocess.run(
                [str(ZTEST_BIN), "--engine=pytest", "--llm", "test_strict.py"],
                cwd=b34_dir,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r_b4.returncode, 1, r_b4.stderr)
            self.assertIn("XPASS(strict)", r_b4.stdout)
            b4_sum = json.loads(r_b4.stdout.strip().splitlines()[-1])
            self.assertEqual(b4_sum["failed"], 1)

            b3_dir = tdp / "b3_sess"
            b3_dir.mkdir()
            (b3_dir / "conftest.py").write_text(
                "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 1\n"
            )
            (b3_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n")
            r_b3 = subprocess.run(
                [str(ZTEST_BIN), "--engine=pytest", "--llm", "test_ok.py"],
                cwd=b3_dir,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r_b3.returncode, 1, r_b3.stderr)
            self.assertIn("SessionError", r_b3.stdout)
            b3_sum = json.loads(r_b3.stdout.strip().splitlines()[-1])
            self.assertEqual(b3_sum["failed"], 1)

            # B5: multi-threaded root conftest degrades zygote to clean per-file exec mode
            b5_dir = tdp / "b5_mt"
            b5_dir.mkdir()
            (b5_dir / "conftest.py").write_text(
                "import os, threading, time\n"
                "CONFTEST_LOAD_PID = os.getpid()\n"
                "t = threading.Thread(target=lambda: time.sleep(10), daemon=True)\n"
                "t.start()\n"
            )
            for idx in range(3):
                (b5_dir / f"test_mt_{idx}.py").write_text(
                    "import os, conftest\n"
                    f"def test_mt_{idx}():\n"
                    "    assert conftest.CONFTEST_LOAD_PID == os.getpid()\n"
                )
            r_b5 = subprocess.run(
                [str(ZTEST_BIN), "--llm", "."],
                cwd=b5_dir,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(r_b5.returncode, 0, r_b5.stderr)
            b5_sum = json.loads(r_b5.stdout.strip().splitlines()[-1])
            self.assertEqual(b5_sum["passed"], 3)
            self.assertEqual(b5_sum["failed"], 0)

            # B1a: dropped stdout bytes on an unwritable FIFO sink must fail-closed (exit 2, never 0)
            # Darwin has no /proc and anonymous pipes reject fchmod(EINVAL);
            # use a named FIFO whose mode can be changed via chmod(2).
            fifo_path = tdp / "b1a_unwritable.fifo"
            os.mkfifo(fifo_path, 0o600)
            r_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
            w_fd = os.open(fifo_path, os.O_WRONLY)
            try:
                os.chmod(fifo_path, 0o400)
                r_b1 = subprocess.run(
                    [str(ZTEST_BIN), "--llm", "test_mt_0.py"],
                    cwd=b5_dir,
                    stdout=w_fd,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=20,
                )
            finally:
                os.close(r_fd)
                os.close(w_fd)
                fifo_path.unlink(missing_ok=True)
            self.assertEqual(r_b1.returncode, 2, r_b1.stderr)
            self.assertIn("dropped", r_b1.stderr)

            # B1b: backpressured stdout that overflows MAX_OUT_BACKLOG mid-stream drops only whole
            # JSONL records (every emitted line parses cleanly as JSON) and exits with code 2.
            b1_dir = tdp / "b1_bp"
            b1_dir.mkdir()
            (b1_dir / "test_flood.py").write_text(
                "import pytest\n"
                "PAYLOAD = 'X' * 40000\n"
                "@pytest.mark.parametrize('i', range(45))\n"
                "def test_big_fail(i):\n"
                "    raise RuntimeError(f'{i}:{PAYLOAD}')\n"
            )
            import socket
            s_a, s_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                proc_b1 = subprocess.Popen(
                    [str(ZTEST_BIN), "--llm", "test_flood.py"],
                    cwd=b1_dir,
                    stdout=s_b.fileno(),
                    stderr=subprocess.PIPE,
                )
                s_b.close()
                s_b = None
                chunks: list[bytes] = []
                # Let the socket buffer + 1 MiB backlog overflow before draining
                time.sleep(0.6)
                while True:
                    data = s_a.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                _, err_b1 = proc_b1.communicate(timeout=20)
            finally:
                s_a.close()
                if s_b is not None:
                    s_b.close()
            self.assertIn(proc_b1.returncode, (1, 2), err_b1.decode("utf-8", "replace"))
            self.assertIn("dropped", err_b1.decode("utf-8", "replace"))
            out_b1 = b"".join(chunks).decode("utf-8")
            b1_lines = [ln for ln in out_b1.splitlines() if ln.strip()]
            self.assertGreater(len(b1_lines), 1)
            parsed_b1 = [json.loads(ln) for ln in b1_lines]
            self.assertIn("passed", parsed_b1[-1])
            self.assertEqual(parsed_b1[-1]["failed"], 45)


    # --- CLI UX (lane B2): named unknown-flag diagnostic + short summary block ---

    def test_unrecognized_argument_names_flag(self) -> None:
        """An unknown flag must be named in the diagnostic, not swallowed by a
        blind "invalid command-line arguments" line."""
        proc = subprocess.run(
            [ZTEST_BIN, "--bogus-flag"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("--bogus-flag", out)
        self.assertIn("unrecognized argument", out)

    def test_short_summary_info_block(self) -> None:
        """One failing test in human mode: pytest-style
        `=== short test summary info ===` block with a FAILED line before the
        final summary."""
        with tempfile.TemporaryDirectory(prefix="ztest-shortsum-") as td:
            work = Path(td)
            (work / "test_one_fail.py").write_text(
                "def test_ok():\n"
                "    assert True\n"
                "\n"
                "\n"
                "def test_boom():\n"
                "    assert 1 == 2\n"
            )
            proc = subprocess.run(
                [ZTEST_BIN, "--python", sys.executable, "-j1", "."],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("=== short test summary info ===", out)
        self.assertIn("FAILED", out)
        self.assertIn("test_one_fail.py::test_boom", out)
        # The block precedes the totals line.
        block_at = out.index("=== short test summary info ===")
        summary_at = out.index("ztest summary:")
        self.assertLess(block_at, summary_at, out)


class TestLaneB3ShimCallBoundary(unittest.TestCase):
    """Bench-100 shim fixes, pinned in-process against the real shim module.

    Each case runs one file through `run_test_file` (the native worker entry)
    with a socketpair as the ZTST channel: no binary build is needed, and the
    assertions live inside the test bodies that the shim itself executes.
    """

    SHIM_MODULE_NAME = "zshim_b3_probe"

    @classmethod
    def _import_shim(cls):
        import importlib.util

        shim_path = Path(__file__).resolve().parent.parent / "src" / "shim" / "ztest_shim.py"
        spec = importlib.util.spec_from_file_location(cls.SHIM_MODULE_NAME, shim_path)
        m = importlib.util.module_from_spec(spec)
        # Registered under its own name so a plugin/conftest loaded by the shim
        # can reach the very module under test (see the pytest11 case below).
        sys.modules[cls.SHIM_MODULE_NAME] = m
        spec.loader.exec_module(m)
        return m

    @staticmethod
    def _run_native(m, root: Path, target: Path, **overrides) -> tuple[int, str]:
        """Run one file through the native engine in-process; (rc, decoded frames)."""
        import socket
        import threading

        opts = {
            "rootdir": str(root),
            "no_capture": True,
            "engine": "native",
            "k_expr": "",
            "m_expr": "",
            "maxfail": 0,
        }
        opts.update(overrides)
        sock_a, sock_b = socket.socketpair()
        ipc = m.IpcChannel(sock_a.fileno(), os.getpid())
        chunks: list[bytes] = []

        def _drain() -> None:
            while True:
                try:
                    data = sock_b.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        try:
            rc = m.run_test_file(str(target), opts, ipc)
        finally:
            sock_a.close()
            reader.join(timeout=10)
            sock_b.close()
        return rc, b"".join(chunks).decode("utf-8", "replace")

    def test_test_call_has_pytest_runner_frame_and_astroid_compensation(self) -> None:
        """freezegun/astroid: the test body sees a `_pytest.runner` frame and +25 recursion limit."""
        m = self._import_shim()
        with tempfile.TemporaryDirectory(prefix="ztest-b3-call-") as td:
            root = Path(td)
            (root / "test_boundary.py").write_text(
                "import inspect\n"
                "import sys\n"
                "BASE_LIMIT = sys.getrecursionlimit()\n"
                "\n"
                "def test_call_boundary():\n"
                "    # The immediate caller is the trampoline: its frame's module is\n"
                "    # '_pytest.runner', which is what freezegun's stack scan looks for\n"
                "    # (the enclosing harness may hold other _pytest frames, so the\n"
                "    # direct caller is the honest pin).\n"
                "    caller = inspect.stack()[1].frame.f_globals.get('__name__')\n"
                "    assert caller == '_pytest.runner', caller\n"
                "    assert sys.getrecursionlimit() == BASE_LIMIT + 25, (\n"
                "        sys.getrecursionlimit(), BASE_LIMIT)\n",
                encoding="utf-8",
            )
            limit_before = sys.getrecursionlimit()
            rc, frames = self._run_native(m, root, root / "test_boundary.py")
            self.assertEqual(rc, 0, frames)
            self.assertIn('"t": "TEST_PASS"', frames)
            # finally-restore: the process-wide limit never leaks out of the item.
            self.assertEqual(sys.getrecursionlimit(), limit_before)

            # Negative control: without the trampoline the same body fails, so the
            # pin above really observes the call boundary (and not a stray frame).
            m._ZT_CALL_TRAMPOLINE = None
            m._ztest_call_trampoline = lambda: (lambda fn, kwargs: fn(**kwargs))
            rc2, frames2 = self._run_native(m, root, root / "test_boundary.py")
            self.assertEqual(rc2, 1, frames2)
            self.assertIn('"t": "TEST_FAIL"', frames2)
            self.assertIn("_pytest.runner", frames2)
            self.assertIn("test_call_boundary", frames2)

    def test_addopts_marker_filter_applies_and_cli_wins(self) -> None:
        """poetry-core: `addopts = "-m 'not integration'"` deselects integration tests; CLI -m wins."""
        m = self._import_shim()
        with tempfile.TemporaryDirectory(prefix="ztest-b3-addopts-") as td:
            root = Path(td)
            (root / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\n"
                'addopts = "-m \'not integration\'"\n'
                'markers = ["integration: slow"]\n',
                encoding="utf-8",
            )
            (root / "test_marks.py").write_text(
                "import pathlib\n"
                "import pytest\n"
                "HERE = pathlib.Path(__file__).parent\n"
                "\n"
                "def test_unit():\n"
                "    assert True\n"
                "\n"
                "@pytest.mark.integration\n"
                "def test_integration():\n"
                "    (HERE / 'INTEGRATION_RAN').write_text('ran')\n"
                "    assert True\n",
                encoding="utf-8",
            )
            rc, frames = self._run_native(m, root, root / "test_marks.py")
            self.assertEqual(rc, 0, frames)
            self.assertFalse(
                (root / "INTEGRATION_RAN").exists(),
                "addopts -m did not deselect the integration test",
            )
            self.assertIn('"deselected": 1', frames)
            self.assertIn('"passed": 1', frames)

            # An explicit CLI -m overrides the addopts value (pytest precedence).
            rc2, frames2 = self._run_native(
                m, root, root / "test_marks.py", m_expr="integration"
            )
            self.assertEqual(rc2, 0, frames2)
            self.assertTrue(
                (root / "INTEGRATION_RAN").exists(),
                "the CLI -m selection was overridden by addopts",
            )
            self.assertIn('"passed": 1', frames2)

            # Unit pin of the addopts tokenizer itself.
            self.assertEqual(
                m._addopts_selection_filters(root), ("", "not integration", 0)
            )
            with tempfile.TemporaryDirectory(prefix="ztest-b3-addopts2-") as td2:
                root2 = Path(td2)
                (root2 / "pyproject.toml").write_text(
                    '[tool.pytest.ini_options]\naddopts = "-k fast --maxfail=3 -x"\n',
                    encoding="utf-8",
                )
                # -x (maxfail=1) comes last, so it wins over --maxfail=3.
                self.assertEqual(m._addopts_selection_filters(root2), ("fast", "", 1))
            # A separate root: the ini-options cache is keyed by directory.
            with tempfile.TemporaryDirectory(prefix="ztest-b3-addopts3-") as td3:
                root3 = Path(td3)
                (root3 / "pyproject.toml").write_text(
                    '[tool.pytest.ini_options]\naddopts = "-kfast --maxfail 2"\n',
                    encoding="utf-8",
                )
                self.assertEqual(m._addopts_selection_filters(root3), ("fast", "", 2))

    def test_mocker_fixture_delegates_to_installed_pytest_mock(self) -> None:
        """platformdirs: `mocker` resolves to pytest-mock's MockerFixture when installed."""
        m = self._import_shim()
        import types as _types

        class _FakeMocker:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self.args = args
                self.patched: list[str] = []

            def patch(self, target: str, *a: object, **kw: object) -> None:
                self.patched.append(target)

            def stopall(self) -> None:
                self.patched.clear()

            def resetall(self) -> None:
                pass

        fake_plugin = _types.ModuleType("pytest_mock.plugin")
        fake_plugin.MockerFixture = _FakeMocker  # type: ignore[attr-defined]
        fake_pkg = _types.ModuleType("pytest_mock")
        fake_pkg.plugin = fake_plugin  # type: ignore[attr-defined]
        saved = {n: sys.modules.get(n) for n in ("pytest_mock", "pytest_mock.plugin")}
        sys.modules["pytest_mock"] = fake_pkg
        sys.modules["pytest_mock.plugin"] = fake_plugin
        try:
            with tempfile.TemporaryDirectory(prefix="ztest-b3-mocker-") as td:
                root = Path(td)
                (root / "test_mock.py").write_text(
                    "def test_mocker(mocker):\n"
                    "    assert type(mocker).__name__ == '_FakeMocker', type(mocker).__name__\n"
                    "    mocker.patch('os.getcwd')\n"
                    "    assert mocker.patched == ['os.getcwd']\n",
                    encoding="utf-8",
                )
                rc, frames = self._run_native(m, root, root / "test_mock.py")
                self.assertEqual(rc, 0, frames)
                self.assertIn('"t": "TEST_PASS"', frames)
        finally:
            for name, mod in saved.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod

        # Without pytest-mock the built-in stub still serves the fixture.
        with tempfile.TemporaryDirectory(prefix="ztest-b3-mocker2-") as td:
            root2 = Path(td)
            (root2 / "test_mock_stub.py").write_text(
                "import os\n"
                "\n"
                "def test_stub(mocker):\n"
                "    assert type(mocker).__name__ == 'MockerFixture'\n"
                "    mocker.patch('os.getcwd', return_value='/stub')\n"
                "    assert os.getcwd() == '/stub'\n",
                encoding="utf-8",
            )
            rc, frames = self._run_native(m, root2, root2 / "test_mock_stub.py")
            self.assertEqual(rc, 0, frames)

    def test_pytest11_entrypoint_plugin_fixtures_are_registered(self) -> None:
        """platformdirs: a pytest11 entry-point plugin's fixtures resolve under the native engine."""
        m = self._import_shim()
        with tempfile.TemporaryDirectory(prefix="ztest-b3-ep-") as td:
            root = Path(td)
            # The plugin registers with the shim's own fixture decorator: the
            # worker's `pytest` module is the shim's stub, which is what an
            # in-project plugin sees in a real run.
            (root / "local_pytest_plugin.py").write_text(
                f"import {self.SHIM_MODULE_NAME} as _shim\n"
                "\n"
                "@_shim.fixture\n"
                "def from_entrypoint():\n"
                "    return 'entrypoint-value'\n",
                encoding="utf-8",
            )
            (root / "test_ep.py").write_text(
                "def test_uses_entrypoint_fixture(from_entrypoint):\n"
                "    assert from_entrypoint == 'entrypoint-value'\n",
                encoding="utf-8",
            )
            original = m._pytest11_entry_points
            m._pytest11_entry_points = lambda: [("localproj", "local_pytest_plugin")]
            try:
                rc, frames = self._run_native(m, root, root / "test_ep.py")
            finally:
                m._pytest11_entry_points = original
            self.assertEqual(rc, 0, frames)
            self.assertIn('"t": "TEST_PASS"', frames)

            # Negative control: without the entry-point list the fixture is missing.
            rc2, frames2 = self._run_native(m, root, root / "test_ep.py")
            self.assertEqual(rc2, 1, frames2)
            self.assertIn("fixture 'from_entrypoint' not found", frames2)
            self.assertIn('"t": "TEST_FAIL"', frames2)

            # The environment's own entry points are enumerated, minus the
            # plugins that would fight the supervisor (xdist/cov/sugar/html).
            eps = m._pytest11_entry_points()
            self.assertIsInstance(eps, list)
            for name, module_name in eps:
                for hint in ("xdist", "cov", "sugar", "html"):
                    self.assertNotIn(hint, name.lower())
                self.assertNotIn(":", module_name)

    def test_pytester_files_are_detected_for_bridge_delegation(self) -> None:
        """execnet: a file (or conftest) asking for `pytester` is delegated to the pytest bridge."""
        m = self._import_shim()
        with tempfile.TemporaryDirectory(prefix="ztest-b3-pyt-") as td:
            root = Path(td)
            (root / "testing").mkdir()
            (root / "testing" / "conftest.py").write_text(
                'pytest_plugins = ["pytester"]\n', encoding="utf-8"
            )
            (root / "testing" / "test_sub.py").write_text(
                "def test_uses_pytester(pytester):\n"
                "    pytester.makepyfile('def test_x(): assert True')\n",
                encoding="utf-8",
            )
            (root / "test_plain.py").write_text(
                "def test_plain():\n    assert True\n", encoding="utf-8"
            )
            self.assertTrue(
                m._ztest_file_needs_pytester(
                    str(root / "testing" / "test_sub.py"), {"rootdir": str(root)}
                )
            )
            self.assertFalse(
                m._ztest_file_needs_pytester(
                    str(root / "test_plain.py"), {"rootdir": str(root)}
                )
            )
            saved = os.environ.get("ZTEST_NO_PYTESTER_DELEGATION")
            os.environ["ZTEST_NO_PYTESTER_DELEGATION"] = "1"
            try:
                self.assertFalse(
                    m._ztest_file_needs_pytester(
                        str(root / "testing" / "test_sub.py"), {"rootdir": str(root)}
                    )
                )
            finally:
                if saved is None:
                    os.environ.pop("ZTEST_NO_PYTESTER_DELEGATION", None)
                else:
                    os.environ["ZTEST_NO_PYTESTER_DELEGATION"] = saved


    def test_bridge_restores_upstream_pytest_after_stub_install(self) -> None:
        """execnet/pytester: the bridge gets upstream pytest, not the shim's stub.

        The native engine installs the shim's pytest stubs process-wide; the
        pytester delegation runs the file through real pytest, so the stub
        entries have to be handed back first. Checked in a subprocess: it
        rewrites sys.modules['pytest'] for the whole interpreter.
        """
        shim_path = Path(__file__).resolve().parent.parent / "src" / "shim" / "ztest_shim.py"
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('zs_probe', {str(shim_path)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(m)\n"
            "m.install_pytest_modules()\n"
            "stub = sys.modules['pytest']\n"
            "print('STUB_IS_SHIM', stub is not None)\n"
            "m._ensure_real_pytest_modules()\n"
            "import pytest\n"
            "print('UPSTREAM', pytest is not stub)\n"
            "print('HAS_MAIN', callable(getattr(pytest, 'main', None)))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=180
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("STUB_IS_SHIM True", proc.stdout)
        self.assertIn("UPSTREAM True", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("HAS_MAIN True", proc.stdout, proc.stdout + proc.stderr)

    def test_pytest11_fast_scan_reads_dist_metadata(self) -> None:
        """platformdirs: the hand metadata scan finds pytest11 entry points without importlib.metadata."""
        m = self._import_shim()
        with tempfile.TemporaryDirectory(prefix="ztest-b3-dist-") as td:
            site = Path(td)
            dist_info = site / "pplocal-1.0.dist-info"
            dist_info.mkdir()
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: pplocal\nVersion: 1.0\n", encoding="utf-8"
            )
            (dist_info / "entry_points.txt").write_text(
                "[pytest11]\npplocal = local_pytest_plugin\n"
                "[console_scripts]\npplocal-cli = pplocal:main\n",
                encoding="utf-8",
            )
            # A distribution without entry_points.txt and a skipped plugin name.
            (site / "other-2.0.dist-info").mkdir()
            (site / "pytest_cov-9.9.dist-info").mkdir()
            (site / "pytest_cov-9.9.dist-info" / "entry_points.txt").write_text(
                "[pytest11]\ncov = pytest_cov.plugin\n", encoding="utf-8"
            )
            sys.path.insert(0, str(site))
            try:
                found = m._scan_pytest11_entry_points_fast()
            finally:
                sys.path.remove(str(site))
            self.assertIn(("pplocal", "local_pytest_plugin"), found, found)
            self.assertNotIn("cov", [name for name, _ in found])
            for _name, _mod in found:
                self.assertNotIn(":", _mod)



if __name__ == "__main__":
    unittest.main()
