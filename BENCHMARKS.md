# Benchmarks

ztest is measured against stock pytest on real open-source suites in isolated cloud sandboxes, with the two runners interleaved so machine load hits both equally: **ztest 0.5.0** (`ReleaseSafe`, Zig 0.17.0) and **pytest 9.0.3** on **4 vCPU / 8 GiB** (cgroup v2 quota `cpu.max=400000/100000`, `memory.max=8589934592`).

All ratios are **pytest/ztest**: a wall ratio above 1.0 means ztest finished faster; a peak-PSS ratio above 1.0 means ztest used less memory. A PSS ratio of 0.555 means ztest's median peak footprint is 1/0.555 ≈ 1.8× pytest's — ztest used more memory.

## Method

- One hundred real PyPI suites plus a nine-suite parallel cross-check. Every suite is cloned at bench time and installed with its declared test dependencies in a fresh venv; exact repository URLs, 40-char commit SHAs, and test commands for all 100 suites are recorded in [`benchmarks/manifest-100.json`](benchmarks/manifest-100.json), driven by [`benchmarks/bench100_harness.py`](benchmarks/bench100_harness.py).
- Per suite: one unmeasured **parity pass** per side, then three interleaved measured pairs (`p1, z1, p2, z2, p3, z3`). Time is the median of the three uninstrumented runs; memory is the peak process-tree PSS from the separate parity pass (sampled every 50 ms after a 30 ms initial delay via `/proc/<pid>/smaps_rollup` across all descendants; the sampler never runs during measured timing passes; fast suites are sampled up to three times).
- Measured commands: `python -m pytest <dirs> -q -p no:cacheprovider -o addopts=` and `ztest <dirs> -j4 --llm --python=<venv python> --timeout=300`. Project `addopts` are cleared in the pytest arm to match the shim.
- Parity compares the exit status, collected count, and collected nodeid sets (`collect-pytest.jsonl` vs `collect-ztest.txt`) plus the merged outcome counters (`xfail` folds into `skipped`, `xpass` into `passed`; a crashed worker counts as `failed`). `FULL-PARITY` means identical exit status, identical collected count and nodeid set, and identical merged outcome counters. Suites whose serial pytest baseline exits non-zero in the sandbox are reported as `BASELINE-RED`.

## The 100-suite bench

90 FULL-PARITY · 6 DIVERGED · 4 BASELINE-RED. Raw `passed` totals over all 100 suites: **117,233 under pytest, 116,745 under ztest**. The 488-test delta decomposes across five suites: execnet 482 (boundary tier, see below), platformdirs 3, freezegun 1, num2words 1, astroid 1.

Medians over the 90 FULL-PARITY suites: wall ratio **1.465** (ztest finishes ~1.5× faster), peak-PSS ratio **0.555** (four pre-warmed workers cost ~1.8× the peak memory of a single serial interpreter). By size stratum (pytest outcome total): wall 1.29 / PSS 0.57 under 300 tests (n=46), wall 1.57 / PSS 0.53 for 300–1500 (n=31), wall 1.71 / PSS 0.75 above 1500 (n=13) — parallelism gains grow with suite size while the relative memory overhead shrinks. Of the 90 full-parity suites, `ztest -j4` is faster than serial pytest in 88, slower in one (pygments, 0.89), and tied in one (schema, 0.264 s under both); aggregate wall time over the full-parity set is 573.0 s vs 364.4 s (1.57×).

### The six classified divergences

| Suite | Class | Substance |
| --- | --- | --- |
| execnet | boundary tier | its conftest imports the self-hosting `pytester` plugin; ztest reports 10 collection errors (`ModuleNotFoundError: pytester`), runs zero tests (pytest collects 1,107) |
| freezegun | runner identity | `freezegun` inspects the call stack for `_pytest` frames (`test_time_freeze_stays_across_tests`) |
| num2words | file isolation | serial pytest runs all files in one process, so `tests/test_ar.py` (importing `lang_AR`) globally raises `decimal.getcontext().prec` before `tests/test_bn.py` runs; ztest runs each file in a fresh worker with the default `prec=28`, where `test_bn.py::test_parse_number` fails just as it does under `pytest tests/test_bn.py` in isolation |
| platformdirs | plugin fixture | three tests need the package's own `pytest11` entry-point fixture |
| poetry-core | parallel exposure | pytest's ini deselects 9 integration tests via `-m "not integration"`; without `-m`, they collide on shared `fixtures/*/dist/` directories (6) and C-extension build subprocesses (3) |
| astroid | runner stack depth | one test in 2,576 expects a recursion guard at pytest's stack depth; ztest's shallower stack finishes the call |

### The four red baselines (environment, not parity)

Identical counters under both runners; each fails for a sandbox reason:

| Suite | Failing | Cause |
| --- | ---: | --- |
| pypdf | 39 | tests download PDFs from arxiv.org (30 `URLError` 403, 9 `RemoteDisconnected`); sandbox egress is allow-listed. Serial pytest waits out the retry back-off one test at a time (median 1,418 s); ztest's four workers overlap the same back-off (544 s) |
| fpdf2 | 5 | four warning-capture asserts (`caplog.text` empty) plus one test fetching an image over the network |
| jedi | 1 | `test_complete_expanduser` expects a non-empty `$HOME` |
| pyopenssl | 1 | asserts the system CA-path layout |

### Per-suite table

| Suite | Tests | Verdict | pytest wall s | ztest -j4 wall s | wall p/z | pytest peak PSS MiB | ztest peak PSS MiB | PSS p/z |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| add-trailing-comma | 148 | FULL-PARITY | 0.3 | 0.2 | 1.47 | 35 | 63 | 0.56 |
| apispec | 642 | FULL-PARITY | 1.0 | 0.6 | 1.57 | 57 | 147 | 0.38 |
| arrow | 1904 | FULL-PARITY | 2.5 | 1.2 | 2.03 | 141 | 144 | 0.98 |
| astroid | 2576 | DIVERGED | 46.9 | 14.8 | 3.16 | 1173 | 1132 | 1.04 |
| autoflake | 178 | FULL-PARITY | 1.1 | 1.0 | 1.05 | 47 | 67 | 0.70 |
| backoff | 96 | FULL-PARITY | 0.4 | 0.3 | 1.38 | 38 | 59 | 0.64 |
| black | 617 | FULL-PARITY | 51.0 | 42.2 | 1.21 | 455 | 316 | 1.44 |
| bleach | 440 | FULL-PARITY | 0.5 | 0.4 | 1.41 | 39 | 80 | 0.48 |
| boltons | 525 | FULL-PARITY | 2.1 | 1.0 | 2.19 | 59 | 171 | 0.34 |
| cachetools | 366 | FULL-PARITY | 0.4 | 0.3 | 1.57 | 34 | 67 | 0.51 |
| chardet | 12066 | FULL-PARITY | 76.9 | 44.9 | 1.71 | 609 | 600 | 1.01 |
| charset-normalizer | 620 | FULL-PARITY | 3.0 | 1.7 | 1.81 | 464 | 507 | 0.92 |
| cleo | 250 | FULL-PARITY | 0.8 | 0.5 | 1.65 | 39 | 74 | 0.53 |
| configupdater | 167 | FULL-PARITY | 0.6 | 0.6 | 1.09 | 49 | 68 | 0.72 |
| convertdate | 146 | FULL-PARITY | 9.6 | 6.6 | 1.45 | 31 | 55 | 0.55 |
| croniter | 248 | FULL-PARITY | 3.6 | 2.3 | 1.56 | 31 | 87 | 0.35 |
| dateparser | 24976 | FULL-PARITY | 41.2 | 15.6 | 2.64 | 525 | 443 | 1.18 |
| diskcache | 249 | FULL-PARITY | 35.4 | 16.9 | 2.09 | 284 | 342 | 0.83 |
| docstring_parser | 254 | FULL-PARITY | 0.4 | 0.2 | 1.70 | 33 | 64 | 0.52 |
| emoji | 102 | FULL-PARITY | 18.0 | 16.8 | 1.07 | 78 | 127 | 0.61 |
| execnet | 1107 | DIVERGED | 45.3 | 0.2 | 211.67 † | 142 | 55 | 2.58 † |
| fastjsonschema | 631 | FULL-PARITY | 14.1 | 13.4 | 1.05 | 4039 | 3983 | 1.01 |
| feedparser | 4305 | FULL-PARITY | 5.2 | 3.6 | 1.46 | 68 | 91 | 0.75 |
| flake8 | 468 | FULL-PARITY | 1.0 | 0.5 | 2.08 | 53 | 79 | 0.67 |
| flask | 494 | FULL-PARITY | 1.4 | 0.7 | 2.13 | 53 | 101 | 0.53 |
| flask-cors | 99 | FULL-PARITY | 0.7 | 0.6 | 1.08 | 39 | 107 | 0.36 |
| flask-login | 107 | FULL-PARITY | 0.7 | 0.6 | 1.18 | 44 | 48 | 0.91 |
| fpdf2 | 2276 | BASELINE-RED | 81.2 | 50.0 | 1.62 | 281 | 410 | 0.69 |
| freezegun | 149 | DIVERGED | 0.9 | 0.6 | 1.62 | 43 | 99 | 0.43 |
| funcy | 260 | FULL-PARITY | 0.3 | 0.3 | 1.19 | 33 | 67 | 0.49 |
| glom | 202 | FULL-PARITY | 0.6 | 0.4 | 1.49 | 49 | 83 | 0.59 |
| h11 | 78 | FULL-PARITY | 0.3 | 0.3 | 1.19 | 40 | 77 | 0.52 |
| holidays | 8156 | FULL-PARITY | 25.8 | 12.4 | 2.09 | 354 | 196 | 1.80 |
| iniconfig | 54 | FULL-PARITY | 0.2 | 0.2 | 1.30 | 25 | 39 | 0.65 |
| installer | 150 | FULL-PARITY | 0.4 | 0.3 | 1.57 | 34 | 64 | 0.53 |
| itsdangerous | 297 | FULL-PARITY | 0.7 | 0.4 | 1.60 | 37 | 68 | 0.54 |
| jedi | 4032 | BASELINE-RED | 72.4 | 53.5 | 1.35 | 324 | 374 | 0.87 |
| jinja | 911 | FULL-PARITY | 1.6 | 0.7 | 2.43 | 59 | 94 | 0.62 |
| jmespath | 992 | FULL-PARITY | 0.9 | 0.6 | 1.49 | 29 | 61 | 0.48 |
| jsonpatch | 206 | FULL-PARITY | 1.3 | 1.2 | 1.09 | 46 | 57 | 0.82 |
| jsonref | 107 | FULL-PARITY | 0.3 | 0.3 | 1.19 | 34 | 72 | 0.47 |
| jupyter-core | 101 | FULL-PARITY | 1.2 | 0.8 | 1.43 | 60 | 79 | 0.76 |
| lark | 1329 | FULL-PARITY | 8.1 | 7.8 | 1.04 | 70 | 80 | 0.88 |
| loguru | 1678 | FULL-PARITY | 74.9 | 31.3 | 2.39 | 94 | 155 | 0.60 |
| markdown | 926 | FULL-PARITY | 1.2 | 0.8 | 1.49 | 37 | 78 | 0.48 |
| markupsafe | 80 | FULL-PARITY | 0.3 | 0.3 | 1.19 | 23 | 41 | 0.57 |
| marshmallow | 1192 | FULL-PARITY | 1.1 | 0.5 | 2.17 | 54 | 100 | 0.54 |
| mistune | 1324 | FULL-PARITY | 4.9 | 1.6 | 3.01 | 50 | 117 | 0.43 |
| more-itertools | 765 | FULL-PARITY | 13.6 | 7.7 | 1.77 | 112 | 246 | 0.46 |
| nbformat | 195 | FULL-PARITY | 1.2 | 0.8 | 1.52 | 83 | 163 | 0.51 |
| num2words | 1494 | DIVERGED | 1.7 | 1.1 | 1.56 | 51 | 97 | 0.53 |
| parse | 114 | FULL-PARITY | 0.3 | 0.2 | 1.23 | 31 | 57 | 0.55 |
| parsel | 395 | FULL-PARITY | 0.8 | 0.7 | 1.23 | 49 | 80 | 0.61 |
| parsimonious | 86 | FULL-PARITY | 0.4 | 0.4 | 1.14 | 26 | 56 | 0.46 |
| parso | 1985 | FULL-PARITY | 5.8 | 4.6 | 1.26 | 60 | 95 | 0.62 |
| path | 116 | FULL-PARITY | 4.4 | 4.4 | 1.01 | 36 | 39 | 0.91 |
| pathspec | 1072 | FULL-PARITY | 0.8 | 0.3 | 2.44 | 36 | 65 | 0.55 |
| petl | 1127 | FULL-PARITY | 2.9 | 2.0 | 1.45 | 75 | 113 | 0.67 |
| platformdirs | 2391 | DIVERGED | 3.5 | 1.4 | 2.58 | 47 | 80 | 0.58 |
| poetry-core | 2957 | DIVERGED | 16.5 | 13.0 | 1.27 † | 110 | 176 | 0.63 † |
| prompt-toolkit | 156 | FULL-PARITY | 10.4 | 10.0 | 1.04 | 63 | 78 | 0.80 |
| puremagic | 95 | FULL-PARITY | 0.5 | 0.4 | 1.12 | 32 | 60 | 0.53 |
| pycodestyle | 775 | FULL-PARITY | 1.1 | 0.6 | 1.82 | 32 | 64 | 0.50 |
| pyflakes | 795 | FULL-PARITY | 1.2 | 0.5 | 2.27 | 41 | 78 | 0.53 |
| pygments | 5346 | FULL-PARITY | 14.6 | 16.4 | 0.89 | 177 | 109 | 1.63 |
| pyjwt | 476 | FULL-PARITY | 6.0 | 4.9 | 1.22 | 49 | 113 | 0.43 |
| pyopenssl | 435 | BASELINE-RED | 5.6 | 4.2 | 1.35 | 60 | 88 | 0.68 |
| pyparsing | 2154 | FULL-PARITY | 46.3 | 33.0 | 1.40 | 365 | 367 | 0.99 |
| pypdf | 2267 | BASELINE-RED | 1417.9 | 543.5 | 2.61 | 2198 | 2473 | 0.89 |
| pyproject-hooks | 42 | FULL-PARITY | 1.8 | 1.2 | 1.49 | 48 | 79 | 0.61 |
| pyquery | 83 | FULL-PARITY | 5.7 | 5.6 | 1.02 | 42 | 62 | 0.68 |
| python-dotenv | 285 | FULL-PARITY | 1.6 | 1.3 | 1.24 | 46 | 74 | 0.62 |
| python-json-logger | 133 | FULL-PARITY | 0.8 | 0.4 | 1.85 | 43 | 79 | 0.54 |
| python-tabulate | 366 | FULL-PARITY | 1.5 | 1.2 | 1.21 | 53 | 98 | 0.54 |
| pyupgrade | 1106 | FULL-PARITY | 1.0 | 0.7 | 1.46 | 35 | 66 | 0.53 |
| pyyaml | 2616 | FULL-PARITY | 5.4 | 3.0 | 1.81 | 41 | 64 | 0.64 |
| qrcode | 97 | FULL-PARITY | 1.2 | 0.7 | 1.70 | 46 | 93 | 0.49 |
| radon | 404 | FULL-PARITY | 0.4 | 0.3 | 1.57 | 35 | 65 | 0.53 |
| reorder-python-imports | 163 | FULL-PARITY | 0.4 | 0.3 | 1.57 | 35 | 42 | 0.82 |
| schema | 124 | FULL-PARITY | 0.3 | 0.3 | 1.00 | 41 | 52 | 0.79 |
| segno | 1586 | FULL-PARITY | 8.9 | 4.2 | 2.14 | 45 | 81 | 0.55 |
| simplejson | 278 | FULL-PARITY | 1.0 | 0.5 | 2.18 | 67 | 100 | 0.67 |
| slugify | 138 | FULL-PARITY | 0.7 | 0.6 | 1.27 | 40 | 58 | 0.69 |
| sortedcontainers | 296 | FULL-PARITY | 4.0 | 1.5 | 2.71 | 30 | 70 | 0.43 |
| soupsieve | 432 | FULL-PARITY | 8.7 | 7.9 | 1.11 | 41 | 79 | 0.52 |
| sqlparse | 509 | FULL-PARITY | 1.4 | 1.0 | 1.35 | 43 | 79 | 0.54 |
| tablib | 183 | FULL-PARITY | 0.8 | 0.7 | 1.14 | 47 | 65 | 0.72 |
| tenacity | 185 | FULL-PARITY | 2.3 | 2.2 | 1.04 | 40 | 93 | 0.43 |
| tinydb | 228 | FULL-PARITY | 0.5 | 0.3 | 1.48 | 34 | 62 | 0.55 |
| tomli | 18 | FULL-PARITY | 0.4 | 0.2 | 1.70 | 25 | 50 | 0.50 |
| tomlkit | 523 | FULL-PARITY | 0.7 | 0.4 | 1.60 | 41 | 84 | 0.49 |
| toolz | 188 | FULL-PARITY | 0.4 | 0.3 | 1.38 | 39 | 76 | 0.52 |
| validators | 1516 | FULL-PARITY | 1.0 | 0.6 | 1.71 | 38 | 80 | 0.48 |
| voluptuous | 181 | FULL-PARITY | 0.3 | 0.3 | 1.19 | 40 | 47 | 0.86 |
| vulture | 300 | FULL-PARITY | 1.6 | 1.3 | 1.23 | 41 | 66 | 0.62 |
| webob | 2399 | FULL-PARITY | 4.7 | 3.2 | 1.49 | 69 | 146 | 0.47 |
| werkzeug | 1018 | FULL-PARITY | 7.8 | 3.8 | 2.04 | 119 | 181 | 0.66 |
| wheel | 75 | FULL-PARITY | 0.5 | 0.3 | 1.48 | 38 | 78 | 0.49 |
| wtforms | 552 | FULL-PARITY | 0.8 | 0.6 | 1.36 | 42 | 72 | 0.59 |
| xmltodict | 134 | FULL-PARITY | 0.6 | 0.5 | 1.20 | 34 | 60 | 0.58 |

† Collected sets differ; shown for completeness. `pygments` (0.89×) spans 1,541 custom-collected files where per-file re-import without `--preload` exceeds 4-way parallelism.

## Parallel pytest cross-check (pytest-xdist)

Nine suites measured on the same 4 vCPU / 8 GiB stand with four arms interleaved round-robin (`serial pytest`, `pytest -n4`, `ztest -j1`, `ztest -j4`), cold round discarded, warm medians reported. Outcome counters of serial pytest and `ztest -j4` match on all nine suites.

| Suite | Tests | pytest serial s | pytest -n4 s | ztest -j1 s | ztest -j4 s | parallel speedup vs serial | xdist / ztest |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| arrow | 1904 | 1.8 | red | 1.6 | 0.9 | 2.04x ztest -j4 | — |
| boltons | 525 | 1.7 | 1.1 | 1.8 | 0.8 | 2.11x ztest -j4 | 1.38x |
| dateparser | 24976 | 30.3 | red | 30.7 | 8.9 | 3.43x ztest -j4 | — |
| iniconfig | 54 | 0.2 | 0.4 | 0.1 | 0.1 | 1.89x ztest -j4 | 4.00x |
| pathspec | 1072 | 0.6 | red | 0.4 | 0.2 | 2.64x ztest -j4 | — |
| pygments | 5346 | 10.4 | 6.3 | 40.6 | 10.6 | 1.65x pytest -n4 | 0.59x |
| pyjwt | 476 | 5.6 | 4.8 | 5.9 | 4.9 | 1.18x pytest -n4 | 0.98x |
| python-dotenv | 285 | 1.3 | 1.2 | 1.2 | 1.0 | 1.31x ztest -j4 | 1.20x |
| simplejson | 278 | 0.7 | 0.7 | 0.9 | 0.4 | 1.96x ztest -j4 | 1.75x |

Wall times in seconds, warm medians. `Tests` is the serial pytest outcome total (`passed + failed + skipped + xfailed`, including `subtests` items on pathspec and dateparser). `red` marks non-zero exit under `pytest -n4`: arrow (`Different tests were collected between gw0 and gw1`), pathspec (29 regex-flag failures), and dateparser (3 `test_weekday_with_modifier` failures). Among the six green xdist suites, `ztest -j4` wins four and `pytest -n4` wins two; on the three where xdist fails, `ztest -j4` passes green at 2.04×–3.43× over serial pytest.
