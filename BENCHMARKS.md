# Benchmarks

ztest is measured against stock pytest on real open-source suites in isolated cloud sandboxes, with every arm interleaved so machine load hits them equally: **ztest 0.5.0** (static musl, `ReleaseSafe`, Zig 0.17.0) and **pytest 9.0.3** on **4 vCPU / 8 GiB** (cgroup v2 quota `cpu.max=400000/100000`, `memory.max=8589934592`). Process-containment probes (Section 1) are single-worker experiments and were run on a plain 2 vCPU / 3 GiB Linux host.

All ratios are **pytest/ztest**: a wall ratio above 1.0 means ztest finished faster; a peak-PSS ratio above 1.0 means ztest used less memory. A PSS ratio of 0.555 means ztest's median peak footprint is 1/0.555 ≈ 1.8× pytest's — ztest used more memory. Memory ratios against `pytest -n4` are the other way round and always labelled as such.

v0.5.1 changed reporting and shim parity, not the supervisor or worker model. v0.6.0 adds the Darwin backend and modular shim sources but does not rerun this Linux stand. The numbers below remain Linux-only measurements from 0.5.0; the parity counts in Section 3 are the v0.5.1 ones and are not presented as Darwin results.

## Method

- One hundred real PyPI suites plus a nine-suite four-arm parallel cross-check. Every suite is cloned at bench time and installed with its declared test dependencies in a fresh venv; exact repository URLs, 40-char commit SHAs, and test commands for all 100 suites are recorded in [`benchmarks/manifest-100.json`](benchmarks/manifest-100.json), driven by [`benchmarks/bench100_harness.py`](benchmarks/bench100_harness.py).
- Per suite: one unmeasured **parity pass** per side, then three interleaved measured pairs (`p1, z1, p2, z2, p3, z3`). Time is the median of the three uninstrumented runs; memory is the peak process-tree PSS from the separate parity pass (sampled every 50 ms after a 30 ms initial delay via `/proc/<pid>/smaps_rollup` across all descendants; the sampler never runs during measured timing passes; fast suites are sampled up to three times).
- Measured commands, exactly as recorded in the receipts: `python -m pytest <dirs> -q -p no:cacheprovider -o addopts=` and `ztest <dirs> -j4 --llm --python=<venv python> --timeout=300`. The arms are not symmetric by construction, and the parity claim is scoped to what these commands actually run: the pytest arm clears project `addopts` (`-o addopts=`) so ini-supplied plugin and fixture flags cannot perturb a measurement, while ztest reads the ini itself — and since v0.5.1 it applies the ini's `-k` / `-m` / `--maxfail` selection before dispatching a file, so on a suite whose addopts carry a filter (poetry-core: `-m "not integration"`) the ztest arm runs the selected subset. Every `FULL-PARITY` verdict below is therefore a statement about these commands on the selected directories with each runner's default configuration, not a claim that the two invocations are identical. The 100-suite matrix and the four-arm cross-check were measured on 0.5.0, before the addopts handling changed, and were not rerun for this release; suites without selection filters are unaffected by the change.
- Parity compares the exit status, collected count, and collected nodeid sets (`collect-pytest.jsonl` vs `collect-ztest.txt`) plus the merged outcome counters (`xfail` folds into `skipped`, `xpass` into `passed`; a crashed worker counts as `failed`). `FULL-PARITY` means identical exit status, identical collected count and nodeid set, and identical merged outcome counters. Suites whose serial pytest baseline exits non-zero in the sandbox are reported as `BASELINE-RED`.

### Timing resolution

Measured walls used to be quantized to 50 ms. The harness reaped each child with `Popen.wait(timeout=…)`, which CPython implements as a `os.waitpid(WNOHANG)` poll loop with an exponential backoff capped at 50 ms, so a run that finished at 0.121 s was recorded as 0.164 s and every fast suite landed on the grid 0.164 / 0.214 / 0.264 / 0.314 / 0.364 s — inflating ztest by 25–35 % against its own `duration_ms`.

Since v0.5.1 the harness reaps with a blocking `waitpid` in the reaping thread and enforces the run cap with a separate watchdog that kills the process group at the deadline. The kernel wakes the blocking wait in the same scheduling slice as child exit:

| Wait strategy (120 ms child) | Median wall | Overshoot | Spread |
| --- | ---: | ---: | ---: |
| `Popen.wait(timeout=…)` — 50 ms poll quantum | 0.164 s | +44 ms | 0.001 s |
| Blocking `waitpid` + kill watchdog (v0.5.1) | 0.121 s | +1 ms | 0.001 s |

Sub-second wall columns in this document are reproduced from the pre-fix receipts, at the 0.01 s they actually carry — the grid is visible in rows like `0.31 / 0.26` and is a property of that stand, not of ztest. New runs resolve to 1 ms.

## 1. Process containment and runaway protection

The features that make ztest worth deploying are the ones `pytest` cannot express: a test process that ignores its deadline, a process tree that daemonises itself out of reach, and a suite whose memory grows until the kernel kills it. These are live probes against `ztest 0.5.0` (static musl) with `pytest 9.0.3` + `pytest-timeout` 2.3 as the comparison, run 2026-10-25. The probes use `hashlib.pbkdf2_hmac` (a C loop holding the GIL for ~18 s), a `while True: pass` loop, and a test that spawns `setsid sleep 612` before hanging.

| Scenario | Stock pytest | pytest + `pytest-timeout` | ztest |
| --- | --- | --- | --- |
| Hung C extension, GIL held, deadline 1 s | never returns | `--timeout-method=signal`: deadline reported at **17.59 s** — `SIGALRM` is queued but the CPython eval loop cannot run the handler until the C call returns; unbounded when it never returns | killed by the supervisor at **3.19 s** (`{"t":"crash","timed_out":true}`, `duration_ms: 3188`); `--timeout=2` → 3.66 s |
| Pure-Python infinite loop, deadline 1 s | never returns | signal method works: **1.01 s** | in-worker timer: **1.19 s** |
| `setsid` daemon orphan plus a hung test, deadline 1 s | never returns, orphan survives | `--timeout-method=thread`: exits in **1.23 s** but leaves **1 leaked process** — `os._exit` skips every child the test created | **1.28 s**, **0 leaked processes** (subreaper orphan sweep) |
| Runaway suite, 1,760 files, leaky C extensions (internal monorepo) | OOM-killed at **5.2–8.4 GiB**, no result | same | **~1.25 GiB flat** peak, complete pass |
| Output bytes for an agent loop (42 tests, 2 realistic failures) | `-q`: 1,294 B; default: 1,554 B; `-v`: 5,126 B | — | **732 B** under `--llm`: 43.4 % / 52.9 % / **85.7 %** fewer bytes; 0 bytes per passing test |
| Startup | `python -m pytest --version`: **155.3 ms** median | — | `ztest --version`: **1.0 ms** median on the same host; a one-test run 189 ms vs 218 ms |

Two readings of that table are worth keeping straight:

- The 3.19 s is not the 1 s user deadline — it is the supervisor's inactivity window, `max(1500, sec*1000) + 1500` ms, which takes over precisely because the in-worker timer cannot run while the GIL is pinned. What matters for an agent or a CI job is that the bound exists and is enforced by the kernel: the hung call is killed in a fixed ~2.5 s window regardless of whether it would have returned in 18 s or never. `pytest-timeout`'s deadline is only as enforceable as the C call's own return, which is how a one-minute CI step becomes a sixty-minute job.
- The 1.23 s row on `pytest-timeout` is not a bug in that plugin: it chose `os._exit`, which is the only thing that works from a timer thread. Its cost is the orphan tree — here a `setsid` descendant that no longer shares a process group with anyone, holding inherited file descriptors until the job times out.

How ztest gets there: every worker runs in its own process group under `PR_SET_PDEATHSIG(SIGKILL)`; worker identity is pinned with `pidfd_open` and the zombie is held with `waitid(WNOWAIT)` so a pid cannot be recycled before `kill(-pgid, SIGKILL)` completes; a per-run native watchdog polls the supervisor's `pidfd` against a 32-slot `MAP_SHARED` process-group registry with `pgid <= 1` fail-closed guards; and a child-subreaper sweep collects descendants that called `setsid`, which are outside group-based containment. The rationale, the overlap, and the accepted limits are in [ADR-004](docs/adr/ADR-004-process-containment.md).

## 2. Parallel pytest cross-check (pytest-xdist)

Nine suites, four arms (`pytest` serial, `pytest -n4`, `ztest -j1`, `ztest -j4`) interleaved round-robin on one 4 vCPU / 8 GiB stand, cold round discarded, warm medians reported. Outcome counters of serial pytest and `ztest -j4` match on all nine suites. `pytest -n4` fails on three of the nine.

### Peak memory, all four arms

| Suite | Tests | pytest serial | ztest -j1 | pytest -n4 | ztest -j4 | xdist / ztest -j4 | xdist status |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| pygments | 5346 | 186.8 | 85.9 | 596.9 | 120.9 | **4.94×** | GREEN |
| iniconfig | 54 | 30.6 | 36.6 | 128.1 | 36.6 | **3.51×** | GREEN |
| dateparser | 24976 | 521.7 | 176.9 | 1409.9 | 461.7 | **3.05×** (−948 MiB) | RED (3 failures) |
| pathspec | 1072 | 36.6 | 46.2 | 185.2 | 68.3 | **2.71×** | RED (crash) |
| arrow | 1904 | 57.0 | 56.6 | 212.7 | 82.0 | **2.60×** | RED (crash) |
| python-dotenv | 285 | 43.7 | 58.3 | 151.5 | 66.9 | **2.26×** | GREEN |
| simplejson | 278 | 72.4 | 75.0 | 202.1 | 94.4 | **2.14×** | GREEN |
| boltons | 525 | 149.3 | 173.7 | 280.9 | 153.0 | **1.84×** | GREEN |
| pyjwt | 476 | 52.0 | 63.7 | 184.7 | 108.5 | **1.70×** | GREEN |

Peak PSS in MiB, summed over the runner's process tree. `Tests` is the serial pytest outcome total (`passed + failed + skipped + xfailed`, including `subtests` items on pathspec and dateparser).

`ztest -j4` wins on all nine suites: **1.70× to 4.94× less peak memory than `pytest -n4`, median 2.60×**. On dateparser the difference is 948 MiB — `pytest -n4` peaks at 1.38 GiB (1409.9 MiB), `ztest -j4` at 462 MiB (461.7 MiB) — which is the difference between a 2 GiB CI runner and an OOM kill on exactly the suites people reach for parallelism on. The mechanism is the same one that decides the wall clock: xdist ships each worker a full Python interpreter plus its own import graph and re-collects the suite in every worker, while ztest forks workers from one pre-warmed zygote and recycles them per file, so resident pages stay shared and bounded.

The `ztest -j1` column is the control, and it reads both ways. At one worker ztest *does* win on the large suites — 85.9 MiB against pytest's 186.8 on pygments, 176.9 against 521.7 on dateparser, 56.6 against 57.0 on arrow — because pytest pays its interpreter and plugin import graph in the measuring process while a single ztest worker forks from a pre-warmed zygote. It loses on the six smallest suites (iniconfig 36.6 vs 30.6, pathspec 46.2 vs 36.6, python-dotenv 58.3 vs 43.7, simplejson 75.0 vs 72.4, boltons 173.7 vs 149.3, pyjwt 63.7 vs 52.0), where the zygote plus one forked interpreter is more than the whole pytest process. So the honest reading of the control is not "ztest is or is not a memory win at one worker" but this: the containment layer is a per-worker cost, and on small suites one worker never earns it back. The 1.70×–4.94× advantage over `pytest -n4` is what four workers sharing that zygote buy; it is a parallelism result, not a universal memory result.

### Wall clock, both pytest arms

| Suite | Tests | pytest serial s | pytest -n4 s | -n4 vs serial | xdist status |
| --- | ---: | ---: | ---: | ---: | --- |
| pygments | 5346 | 13.89 | 8.81 | 1.58× | green |
| iniconfig | 54 | 0.21 | 0.56 | 0.38× | green |
| dateparser | 24976 | 39.96 | 25.65 | — | red |
| pathspec | 1072 | 0.77 | 2.12 | — | red |
| arrow | 1904 | 2.32 | 0.92 | — | red |
| python-dotenv | 285 | 1.72 | 1.57 | 1.10× | green |
| simplejson | 278 | 1.01 | 0.92 | 1.11× | green |
| boltons | 525 | 2.07 | 1.22 | 1.70× | green |
| pyjwt | 476 | 5.97 | 4.93 | 1.21× | green |

The ztest wall columns from this stand are deliberately not republished: those runs predate the timing fix above, and one suite (dateparser, 183 s at `-j1` / 153 s at `-j4`) sits an order of magnitude away from the same binary on the 100-suite stand. Section 3 carries the ztest wall matrix measured by the corrected harness. `pytest -n4` exits non-zero on pathspec (29 regex-flag failures), arrow (`Different tests were collected between gw0 and gw1`) and dateparser (3 `test_weekday_with_modifier` failures); on those three rows its wall clock measures a crash, not a run, and the ratio is left out.

## 3. The 100-suite matrix

90 FULL-PARITY · 6 DIVERGED · 4 BASELINE-RED. Raw `passed` totals over all 100 suites: **117,233 under pytest, 116,745 under ztest**. The 488-test delta decomposes across five suites: execnet 482 (boundary tier, see below), platformdirs 3, freezegun 1, num2words 1, astroid 1.

Medians over the 90 FULL-PARITY suites: wall ratio **1.465** (ztest finishes ~1.5× faster), peak-PSS ratio **0.555** (four pre-warmed workers cost ~1.8× the peak memory of a single serial interpreter). By size stratum (pytest outcome total): wall 1.29 / PSS 0.57 under 300 tests (n=46), wall 1.57 / PSS 0.53 for 300–1500 (n=31), wall 1.71 / PSS 0.75 above 1500 (n=13) — parallelism gains grow with suite size while the relative memory overhead shrinks. Of the 90 full-parity suites, `ztest -j4` is faster than serial pytest in 88, slower in one (pygments, 0.89), and tied in one (schema, 1.00); aggregate wall time over the full-parity set is 573.0 s vs 364.4 s (1.57×).

### Closing five of the six divergences: `--engine=pytest`

The `native` engine implements the pytest API itself, without booting `pluggy`. Suites that depend on third-party plugins, `pytester`, or a stack that looks like `_pytest` can run upstream pytest *inside* each ztest worker with `--engine=pytest` — same zygote, same `pidfd` supervision, same process-group kill, same `--llm` stream. Four of the six divergences close completely:

| Suite | Tests | `--engine=native` | `pytest` serial s | `ztest --engine=pytest -j4` s | speedup | Reference |
| --- | ---: | --- | ---: | ---: | ---: | --- |
| execnet | 1107 | DIVERGED (10 collection errors, 0 run) | 46.91 | 16.01 | **2.93×** | 482 passed, 625 skipped, collect 1107/1107 |
| astroid | 2576 | DIVERGED (1 recursion-guard test) | 56.71 | 18.91 | **3.00×** | 2491 passed, 85 skipped, nodeids identical |
| platformdirs | 2391 | DIVERGED (3 `pytest11` fixture tests) | 4.59 | 2.21 | **2.07×** | 2266 passed, 125 skipped, nodeids identical |
| freezegun | 149 | DIVERGED (`_pytest` frame inspection) | 1.18 | 1.02 | **1.15×** | 143 passed, 6 skipped, nodeids identical |

FULL-PARITY means identical exit status, identical collected nodeids, identical merged counters. Receipt-backed parity therefore rises from **90/96 (93.8 %) to 94/96 (97.9 %)** of suites with a green baseline, and the residual failed-test delta falls from 488 to 1.

The fifth divergence, poetry-core, is an `addopts` problem rather than an engine one: its ini carries `-m "not integration"`, and the bridge's retry path used `-o addopts=` to drop an unknown plugin flag, erasing the marker filter with it. v0.5.1 preserves `-m`/`-k`/`-x`/`--maxfail` parsed out of `addopts` on retry, which puts poetry-core at **95/96 (99.0 %)** — the release job confirms the row before the number is treated as measured. The sixth, num2words, is a genuine behavioural difference and is documented below rather than closed.

### Why some suites do not speed up (Amdahl, not overhead)

Four workers cannot parallelize a single file: tests inside one file run in one worker, and the wall is bounded by that file's serial time plus the fixed cost of starting the run. Measured on the suites themselves (collected nodeids per file, `pytest --collect-only`):

| Suite | Tests | Share of tests in the largest file | wall p/z |
| --- | ---: | ---: | ---: |
| lark | 1329 | **100.0 %** (`tests/__main__.py`) | 1.04 |
| pyparsing | 2154 | **91.4 %** (`tests/test_unit.py`) | 1.40 |
| jmespath | 992 | **92.7 %** (`tests/test_compliance.py`) | 1.49 |

lark is the clean case: one 1329-test file, four workers, 1.04× — the entire gain comes from supervisor startup and per-test overhead, because there is nothing to schedule. Split the same tests across four files and the ratio moves toward 4. This is the honest reading of the 88-faster/1-slower split: what ztest sells is file-level parallelism with containment, and a suite shaped like one big file cannot use it. The mirror image is pygments (0.89×): 1,541 custom-collected files where every file's re-import cost, without `--preload`, exceeds what four-way overlap saves.

### The six classified divergences

| Suite | Class | Substance |
| --- | --- | --- |
| execnet | boundary tier | its conftest imports the self-hosting `pytester` plugin; the native engine reports 10 collection errors (`ModuleNotFoundError: pytester`) and runs zero tests (pytest collects 1,107). FULL-PARITY under `--engine=pytest` |
| freezegun | runner identity | `freezegun` inspects the call stack for `_pytest` frames (`test_time_freeze_stays_across_tests`). FULL-PARITY under `--engine=pytest` |
| num2words | file isolation | serial pytest runs all files in one process, so `tests/test_ar.py` (importing `lang_AR`) globally raises `decimal.getcontext().prec` before `tests/test_bn.py` runs; ztest runs each file in a fresh worker with the default `prec=28`, where `test_bn.py::test_parse_number` fails just as it does under `pytest tests/test_bn.py` in isolation |
| platformdirs | plugin fixture | three tests need the package's own `pytest11` entry-point fixture. FULL-PARITY under `--engine=pytest` |
| poetry-core | addopts erasure | pytest's ini deselects 9 integration tests via `-m "not integration"`; without `-m`, they collide on shared `fixtures/*/dist/` directories (6) and C-extension build subprocesses (3). Fixed in v0.5.1 by preserving `-m`/`-k` across the `addopts` retry |
| astroid | runner stack depth | one test in 2,576 expects a recursion guard at pytest's stack depth; ztest's shallower stack finishes the call. FULL-PARITY under `--engine=pytest` |

### The four red baselines (environment, not parity)

Identical counters under both runners; each fails for a sandbox reason:

| Suite | Failing | Cause |
| --- | ---: | --- |
| pypdf | 39 | tests download PDFs from arxiv.org (30 `URLError` 403, 9 `RemoteDisconnected`); sandbox egress is allow-listed. Serial pytest waits out the retry back-off one test at a time (median 1,418 s); ztest's four workers overlap the same back-off (544 s) |
| fpdf2 | 5 | four warning-capture asserts (`caplog.text` empty) plus one test fetching an image over the network |
| jedi | 1 | `test_complete_expanduser` expects a non-empty `$HOME` |
| pyopenssl | 1 | asserts the system CA-path layout |

### Per-suite table

`Tests` is the collected node count; `wall` and `PSS` are medians/peaks over the three measured pairs and the parity pass, in seconds and MiB. Sub-second walls carry the 0.01 s of the pre-fix receipts (see *Timing resolution*); values above one second are shown to 0.1 s.

| Suite | Tests | Verdict | pytest wall s | ztest -j4 wall s | wall p/z | pytest peak PSS MiB | ztest peak PSS MiB | PSS p/z |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| add-trailing-comma | 148 | FULL-PARITY | 0.31 | 0.21 | 1.47 | 35 | 63 | 0.56 |
| apispec | 642 | FULL-PARITY | 0.96 | 0.61 | 1.57 | 57 | 147 | 0.38 |
| arrow | 1904 | FULL-PARITY | 2.5 | 1.2 | 2.03 | 141 | 144 | 0.98 |
| astroid | 2576 | DIVERGED | 46.9 | 14.8 | 3.16 | 1173 | 1132 | 1.04 |
| autoflake | 178 | FULL-PARITY | 1.1 | 1.0 | 1.05 | 47 | 67 | 0.70 |
| backoff | 96 | FULL-PARITY | 0.36 | 0.26 | 1.38 | 38 | 59 | 0.64 |
| black | 616 | FULL-PARITY | 51.0 | 42.2 | 1.21 | 455 | 316 | 1.44 |
| bleach | 439 | FULL-PARITY | 0.51 | 0.36 | 1.41 | 39 | 80 | 0.48 |
| boltons | 525 | FULL-PARITY | 2.1 | 0.96 | 2.19 | 59 | 171 | 0.34 |
| cachetools | 366 | FULL-PARITY | 0.41 | 0.26 | 1.57 | 34 | 67 | 0.51 |
| chardet | 12066 | FULL-PARITY | 76.9 | 44.9 | 1.71 | 609 | 600 | 1.01 |
| charset-normalizer | 620 | FULL-PARITY | 3.0 | 1.7 | 1.81 | 464 | 507 | 0.92 |
| cleo | 250 | FULL-PARITY | 0.77 | 0.46 | 1.65 | 39 | 74 | 0.53 |
| configupdater | 167 | FULL-PARITY | 0.61 | 0.56 | 1.09 | 49 | 68 | 0.72 |
| convertdate | 146 | FULL-PARITY | 9.6 | 6.6 | 1.45 | 31 | 55 | 0.55 |
| croniter | 248 | FULL-PARITY | 3.6 | 2.3 | 1.56 | 31 | 87 | 0.35 |
| dateparser | 24967 | FULL-PARITY | 41.2 | 15.6 | 2.64 | 525 | 443 | 1.18 |
| diskcache | 249 | FULL-PARITY | 35.4 | 16.9 | 2.09 | 284 | 342 | 0.83 |
| docstring_parser | 254 | FULL-PARITY | 0.36 | 0.21 | 1.70 | 33 | 64 | 0.52 |
| emoji | 102 | FULL-PARITY | 18.0 | 16.8 | 1.07 | 78 | 127 | 0.61 |
| execnet | 1107 | DIVERGED | 45.3 | — | — | 142 | 55 | — |
| fastjsonschema | 631 | FULL-PARITY | 14.1 | 13.4 | 1.05 | 4039 | 3983 | 1.01 |
| feedparser | 4305 | FULL-PARITY | 5.2 | 3.6 | 1.46 | 68 | 91 | 0.75 |
| flake8 | 468 | FULL-PARITY | 0.96 | 0.46 | 2.08 | 53 | 79 | 0.67 |
| flask | 494 | FULL-PARITY | 1.4 | 0.67 | 2.13 | 53 | 101 | 0.53 |
| flask-cors | 99 | FULL-PARITY | 0.66 | 0.61 | 1.08 | 39 | 107 | 0.36 |
| flask-login | 107 | FULL-PARITY | 0.67 | 0.56 | 1.18 | 44 | 48 | 0.91 |
| fpdf2 | 2276 | BASELINE-RED | 81.2 | 50.0 | 1.62 | 281 | 410 | 0.69 |
| freezegun | 149 | DIVERGED | 0.92 | 0.56 | 1.62 | 43 | 99 | 0.43 |
| funcy | 260 | FULL-PARITY | 0.31 | 0.26 | 1.19 | 33 | 67 | 0.49 |
| glom | 202 | FULL-PARITY | 0.61 | 0.41 | 1.49 | 49 | 83 | 0.59 |
| h11 | 78 | FULL-PARITY | 0.31 | 0.26 | 1.19 | 40 | 77 | 0.52 |
| holidays | 8156 | FULL-PARITY | 25.8 | 12.4 | 2.09 | 354 | 196 | 1.80 |
| iniconfig | 54 | FULL-PARITY | 0.21 | 0.16 | 1.30 | 25 | 39 | 0.65 |
| installer | 150 | FULL-PARITY | 0.41 | 0.26 | 1.57 | 34 | 64 | 0.53 |
| itsdangerous | 297 | FULL-PARITY | 0.67 | 0.41 | 1.60 | 37 | 68 | 0.54 |
| jedi | 4032 | BASELINE-RED | 72.4 | 53.5 | 1.35 | 324 | 374 | 0.87 |
| jinja | 911 | FULL-PARITY | 1.6 | 0.66 | 2.43 | 59 | 94 | 0.62 |
| jmespath | 992 | FULL-PARITY | 0.92 | 0.61 | 1.49 | 29 | 61 | 0.48 |
| jsonpatch | 206 | FULL-PARITY | 1.3 | 1.2 | 1.09 | 46 | 57 | 0.82 |
| jsonref | 107 | FULL-PARITY | 0.31 | 0.26 | 1.19 | 34 | 72 | 0.47 |
| jupyter-core | 101 | FULL-PARITY | 1.2 | 0.81 | 1.43 | 60 | 79 | 0.76 |
| lark | 1329 | FULL-PARITY | 8.1 | 7.8 | 1.04 | 70 | 80 | 0.88 |
| loguru | 1678 | FULL-PARITY | 74.9 | 31.3 | 2.39 | 94 | 155 | 0.60 |
| markdown | 926 | FULL-PARITY | 1.2 | 0.81 | 1.49 | 37 | 78 | 0.48 |
| markupsafe | 80 | FULL-PARITY | 0.31 | 0.26 | 1.19 | 23 | 41 | 0.57 |
| marshmallow | 1192 | FULL-PARITY | 1.1 | 0.51 | 2.17 | 54 | 100 | 0.54 |
| mistune | 1324 | FULL-PARITY | 4.9 | 1.6 | 3.01 | 50 | 117 | 0.43 |
| more-itertools | 765 | FULL-PARITY | 13.6 | 7.7 | 1.77 | 112 | 246 | 0.46 |
| nbformat | 195 | FULL-PARITY | 1.2 | 0.77 | 1.52 | 83 | 163 | 0.51 |
| num2words | 1494 | DIVERGED | 1.7 | 1.1 | 1.56 | 51 | 97 | 0.53 |
| parse | 114 | FULL-PARITY | 0.26 | 0.21 | 1.23 | 31 | 57 | 0.55 |
| parsel | 394 | FULL-PARITY | 0.81 | 0.67 | 1.23 | 49 | 80 | 0.61 |
| parsimonious | 86 | FULL-PARITY | 0.41 | 0.36 | 1.14 | 26 | 56 | 0.46 |
| parso | 1985 | FULL-PARITY | 5.8 | 4.6 | 1.26 | 60 | 95 | 0.62 |
| path | 116 | FULL-PARITY | 4.4 | 4.4 | 1.01 | 36 | 39 | 0.91 |
| pathspec | 288 | FULL-PARITY | 0.77 | 0.31 | 2.44 | 36 | 65 | 0.55 |
| petl | 1115 | FULL-PARITY | 2.9 | 2.0 | 1.45 | 75 | 113 | 0.67 |
| platformdirs | 2391 | DIVERGED | 3.5 | 1.4 | 2.58 | 47 | 80 | 0.58 |
| poetry-core | 2957 | DIVERGED | 16.5 | 13.0 | 1.27 | 110 | 176 | 0.63 |
| prompt-toolkit | 156 | FULL-PARITY | 10.4 | 10.0 | 1.04 | 63 | 78 | 0.80 |
| puremagic | 95 | FULL-PARITY | 0.46 | 0.41 | 1.12 | 32 | 60 | 0.53 |
| pycodestyle | 775 | FULL-PARITY | 1.1 | 0.61 | 1.82 | 32 | 64 | 0.50 |
| pyflakes | 795 | FULL-PARITY | 1.2 | 0.51 | 2.27 | 41 | 78 | 0.53 |
| pygments | 5346 | FULL-PARITY | 14.6 | 16.4 | 0.89 | 177 | 109 | 1.63 |
| pyjwt | 476 | FULL-PARITY | 6.0 | 4.9 | 1.22 | 49 | 113 | 0.43 |
| pyopenssl | 435 | BASELINE-RED | 5.6 | 4.2 | 1.35 | 60 | 88 | 0.68 |
| pyparsing | 2154 | FULL-PARITY | 46.3 | 33.0 | 1.40 | 365 | 367 | 0.99 |
| pypdf | 2267 | BASELINE-RED | 1417.9 | 543.5 | 2.61 | 2198 | 2473 | 0.89 |
| pyproject-hooks | 42 | FULL-PARITY | 1.8 | 1.2 | 1.49 | 48 | 79 | 0.61 |
| pyquery | 83 | FULL-PARITY | 5.7 | 5.6 | 1.02 | 42 | 62 | 0.68 |
| python-dotenv | 284 | FULL-PARITY | 1.6 | 1.3 | 1.24 | 46 | 74 | 0.62 |
| python-json-logger | 133 | FULL-PARITY | 0.77 | 0.41 | 1.85 | 43 | 79 | 0.54 |
| python-tabulate | 366 | FULL-PARITY | 1.5 | 1.2 | 1.21 | 53 | 98 | 0.54 |
| pyupgrade | 1106 | FULL-PARITY | 0.97 | 0.66 | 1.46 | 35 | 66 | 0.53 |
| pyyaml | 2616 | FULL-PARITY | 5.4 | 3.0 | 1.81 | 41 | 64 | 0.64 |
| qrcode | 96 | FULL-PARITY | 1.2 | 0.71 | 1.70 | 46 | 93 | 0.49 |
| radon | 404 | FULL-PARITY | 0.41 | 0.26 | 1.57 | 35 | 65 | 0.53 |
| reorder-python-imports | 163 | FULL-PARITY | 0.41 | 0.26 | 1.57 | 35 | 42 | 0.82 |
| schema | 124 | FULL-PARITY | 0.26 | 0.26 | 1.00 | 41 | 52 | 0.79 |
| segno | 1586 | FULL-PARITY | 8.9 | 4.2 | 2.14 | 45 | 81 | 0.55 |
| simplejson | 278 | FULL-PARITY | 1.0 | 0.47 | 2.18 | 67 | 100 | 0.67 |
| slugify | 133 | FULL-PARITY | 0.71 | 0.56 | 1.27 | 40 | 58 | 0.69 |
| sortedcontainers | 296 | FULL-PARITY | 4.0 | 1.5 | 2.71 | 30 | 70 | 0.43 |
| soupsieve | 432 | FULL-PARITY | 8.7 | 7.9 | 1.11 | 41 | 79 | 0.52 |
| sqlparse | 509 | FULL-PARITY | 1.4 | 1.0 | 1.35 | 43 | 79 | 0.54 |
| tablib | 183 | FULL-PARITY | 0.81 | 0.71 | 1.14 | 47 | 65 | 0.72 |
| tenacity | 185 | FULL-PARITY | 2.3 | 2.2 | 1.04 | 40 | 93 | 0.43 |
| tinydb | 228 | FULL-PARITY | 0.46 | 0.31 | 1.48 | 34 | 62 | 0.55 |
| tomli | 18 | FULL-PARITY | 0.36 | 0.21 | 1.70 | 25 | 50 | 0.50 |
| tomlkit | 523 | FULL-PARITY | 0.66 | 0.41 | 1.60 | 41 | 84 | 0.49 |
| toolz | 188 | FULL-PARITY | 0.36 | 0.26 | 1.38 | 39 | 76 | 0.52 |
| validators | 1516 | FULL-PARITY | 0.96 | 0.56 | 1.71 | 38 | 80 | 0.48 |
| voluptuous | 181 | FULL-PARITY | 0.31 | 0.26 | 1.19 | 40 | 47 | 0.86 |
| vulture | 300 | FULL-PARITY | 1.6 | 1.3 | 1.23 | 41 | 66 | 0.62 |
| webob | 2399 | FULL-PARITY | 4.7 | 3.2 | 1.49 | 69 | 146 | 0.47 |
| werkzeug | 1018 | FULL-PARITY | 7.8 | 3.8 | 2.04 | 119 | 181 | 0.66 |
| wheel | 75 | FULL-PARITY | 0.46 | 0.31 | 1.48 | 38 | 78 | 0.49 |
| wtforms | 552 | FULL-PARITY | 0.77 | 0.56 | 1.36 | 42 | 72 | 0.59 |
| xmltodict | 134 | FULL-PARITY | 0.61 | 0.51 | 1.20 | 34 | 60 | 0.58 |

`execnet` shows `—` in the native columns on purpose: it collects 1,107 tests under pytest and 0 under the native engine, so any wall or PSS ratio computed from a 0.2 s crash is a fake speedup. Its real numbers are in the `--engine=pytest` table above (2.93×). `pygments` (0.89×) spans 1,541 custom-collected files where per-file re-import without `--preload` exceeds four-way parallelism.
