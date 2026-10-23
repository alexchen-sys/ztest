#!/usr/bin/env python3
"""bench100_harness.py — in-sandbox per-suite bench: parity pass + 3 interleaved
measured pairs (clean pytest vs ztest -j4), on a 4 vCPU / 8 GiB cloud sandbox.

Per suite:
  1. clone (--depth 1, optional ref / submodules), fresh /tmp/repo + /tmp/venv
  2. install job deps, pin pytest==9.0.3, drop zzz_bench_plugin into
     site-packages (loaded only via -p, so measured pytest runs stay clean)
  3. PARITY (unmeasured, warms caches, samples peak process-tree PSS at 30/50 ms):
       pytest -p zzz_bench_plugin  (BENCH_JSONL + BENCH_COLLECT)
       ztest -j4 --llm
     -> verdict FULL-PARITY / COUNTERS-PARITY / DIVERGED / BASELINE-RED
  4. MEASURED x3, interleaved p1,z1,p2,z2,p3,z3 (uninstrumented, no PSS sampler):
       python -m pytest <dirs> -q -p no:cacheprovider -o addopts=
       ztest <dirs> -j4 --llm --python=<venv python> --timeout=300
     wall = monotonic; cgroup memory.peak delta recorded when writable.
  5. /tmp/repo and /tmp/venv are removed; receipt lands in /tmp/out/<name>/.

Usage: bench100_harness.py --job /tmp/bench/jobs/<name>.json
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
BENCH = Path(os.environ.get("BENCH_DIR", str(_HERE if (_HERE / "zzz_bench_plugin.py").exists() else "/tmp/bench")))
OUT = Path(os.environ.get("OUT_DIR", "/tmp/out"))
REPO = Path("/tmp/repo")
VENV = Path("/tmp/venv")
ZTEST = os.environ.get("ZTEST_BIN", str(BENCH / "ztest" if (BENCH / "ztest").exists() else _HERE.parent / "zig-out/bin/ztest"))
SAMPLE_INTERVAL = 0.45
PYTEST_PIN = "pytest==9.0.3"
PER_TEST_TIMEOUT = 300


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- cgroup ----------------

def cg_read(name):
    try:
        return int(Path(f"/sys/fs/cgroup/{name}").read_text().strip())
    except Exception:
        return None


def cg_peak_reset():
    try:
        Path("/sys/fs/cgroup/memory.peak").write_text("0")
        return True
    except Exception:
        return False


# ---------------- light PSS sampler (runner pgid-scoped) ----------------

def _pgid_members():
    """pid -> (ppid, pgid) from /proc/*/stat (cheap reads only)."""
    out = {}
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except Exception:
        return out
    for d in pids:
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                stat = f.read()
            rparen = stat.rfind(b")")
            fields = stat[rparen + 2:].split()
            out[int(d)] = (int(fields[1]), int(fields[2]))
        except Exception:
            continue
    return out


def _pss_kb(pid):
    try:
        with open(f"/proc/{pid}/smaps_rollup", "rb") as f:
            for line in f:
                if line.startswith(b"Pss:"):
                    return int(line.split()[1])
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for line in f:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def tree_pss_kb(root_pid):
    """Summed PSS over the runner's pgid ∪ ppid-tree. Cheap: smaps_rollup is
    read only for member pids, never for the whole /proc."""
    table = _pgid_members()
    if not table:
        return -1
    root = table.get(root_pid)
    pgid = root[1] if root else -1
    members, stack, seen = set(), [root_pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        members.add(pid)
        for cpid, (ppid, _g) in table.items():
            if ppid == pid and cpid not in seen:
                stack.append(cpid)
    for pid, (_p, g) in table.items():
        if g == pgid:
            members.add(pid)
    total = 0
    for pid in members:
        if pid in table:
            total += _pss_kb(pid)
    return total


def run_timed(tag, cmd, cwd, cap_s, out_path, env_extra=None, sample_mem=False):
    """One run: wall + exit code always. Memory only when sample_mem=True
    (parity passes): pgid-scoped PSS sampler at 0.45 s plus cgroup
    memory.peak delta when writable. Measured passes run bare so the sampler
    cannot asymmetrically tax the 4-worker arm."""
    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if env_extra:
        env.update(env_extra)
    res = {"tag": tag, "wall_s": None, "peak_pss_kb": None, "exit": None,
           "timeout": False, "samples": 0, "cg_peak_delta_kb": None}
    cg_rw = cg_peak_reset() if sample_mem else False
    cg_base = cg_read("memory.current") if sample_mem else None
    with open(out_path, "w", encoding="utf-8", errors="replace") as outf:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=outf,
                                stderr=subprocess.STDOUT, start_new_session=True)
        stop = threading.Event()
        samples = []

        def sampler():
            # Parity passes are unmeasured, so sampler cost is free: 50 ms
            # flat cadence, with the first tick after 30 ms so the bash
            # wrapper has exec'ed into the real runner before sampling.
            stop.wait(0.03)
            while not stop.is_set():
                v = tree_pss_kb(proc.pid)
                if v > 0:
                    samples.append(v)
                stop.wait(0.05 if sample_mem else SAMPLE_INTERVAL)

        th = None
        if sample_mem:
            th = threading.Thread(target=sampler, daemon=True)
        t0 = time.monotonic()
        if th:
            th.start()
        try:
            proc.wait(timeout=cap_s)
            res["exit"] = proc.returncode
        except subprocess.TimeoutExpired:
            res["timeout"] = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            res["exit"] = -9
        res["wall_s"] = round(time.monotonic() - t0, 3)
        stop.set()
        if th:
            th.join(timeout=2)
    res["samples"] = len(samples)
    res["peak_pss_kb"] = max(samples) if samples else None
    if cg_rw and cg_base is not None:
        peak = cg_read("memory.peak")
        if peak is not None:
            res["cg_peak_delta_kb"] = max(0, (peak - cg_base) // 1024)
    log(f"run {tag}: wall={res['wall_s']}s exit={res['exit']} "
        f"peak_pss={res['peak_pss_kb']}kB cg_delta={res['cg_peak_delta_kb']}kB "
        f"samples={res['samples']} timeout={res['timeout']}")
    return res


# ---------------- counters ----------------

_SUM_TOKEN_RE = re.compile(
    r"(\d+)\s+(passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?)")


def pytest_summary_counts(path):
    """pytest's own summary line — ground truth of pytest's accounting."""
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    keymap = {"passed": "passed", "failed": "failed", "skipped": "skipped",
              "deselected": "deselected", "xfailed": "xfailed",
              "xpassed": "xpassed", "error": "error", "errors": "error"}
    best = None
    for line in txt.splitlines():
        if " in " not in line or ("passed" not in line and "failed" not in line
                                  and "error" not in line):
            continue
        toks = {}
        for n, name in _SUM_TOKEN_RE.findall(line):
            k = keymap.get(name)
            if k:
                toks[k] = toks.get(k, 0) + int(n)
        if toks:
            best = toks
    return best


def ztest_summary(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("t") == "summary":
                    return rec
    except FileNotFoundError:
        pass
    return None


def count_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for ln in f if ln.strip())
    except Exception:
        return None


def merged_pytest(c):
    """xfail -> skipped, xpass -> passed (documented merge convention)."""
    if not c:
        return None
    return {"passed": c.get("passed", 0) + c.get("xpassed", 0),
            "failed": c.get("failed", 0) + c.get("error", 0),
            "skipped": c.get("skipped", 0) + c.get("xfailed", 0)}


def merged_ztest(s):
    if not s:
        return None
    return {"passed": s.get("passed", 0),
            "failed": (s.get("failed") or 0) + (s.get("crashed") or 0),
            "skipped": s.get("skipped", 0)}


def median(vals):
    vals = sorted(v for v in vals if isinstance(v, (int, float)))
    n = len(vals)
    if not n:
        return None
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


LAST_SH_TAIL = [""]


def sh(cmd, timeout, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout)
    tail = ((r.stdout or "")[-200:] + (r.stderr or "")[-400:]).replace("\n", " ")
    LAST_SH_TAIL[0] = tail
    log(f"sh {' '.join(str(c) for c in cmd[:3])}... rc={r.returncode} tail={tail[-160:]!r}")
    return r.returncode


def main():
    job = json.loads(Path(sys.argv[sys.argv.index("--job") + 1]).read_text())
    name = job["project"]
    cap_s = int(job.get("run_cap_s", 900))
    testdirs = str(job.get("test_dir") or "tests").split()
    suite_out = OUT / name
    suite_out.mkdir(parents=True, exist_ok=True)
    receipt = {"project": name, "status": "ERROR", "job": job,
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    t0 = time.monotonic()

    def finish():
        receipt["total_s"] = round(time.monotonic() - t0, 1)
        (suite_out / "receipt.json").write_text(
            json.dumps(receipt, indent=1, sort_keys=True, default=str))
        log(f"FINISH {name}: status={receipt['status']} "
            f"verdict={receipt.get('verdict')} total={receipt['total_s']}s")

    receipt["passport"] = {
        "cpu.max": Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        if Path("/sys/fs/cgroup/cpu.max").exists() else "?",
        "memory.max": cg_read("memory.max"),
        "memory.peak": "rw" if cg_peak_reset() else "ro",
        "ztest_sha256": (BENCH / "ztest.sha256").read_text().split()[0]
        if (BENCH / "ztest.sha256").exists()
        else subprocess.run(["sha256sum", ZTEST], capture_output=True, text=True).stdout.split()[0],
        "ztest_version": subprocess.run([ZTEST, "--version"], capture_output=True,
                                        text=True).stdout.strip(),
    }
    log(f"passport: {receipt['passport']}")

    # --- clone
    shutil.rmtree(REPO, ignore_errors=True)
    shutil.rmtree(VENV, ignore_errors=True)
    url = job["clone_url"]
    ref = job.get("ref") or job.get("commit") or ""
    pinned_commit = job.get("commit") or ""
    clone_cmd = ["git", "clone", "--depth", "1"]
    if ref and len(ref) != 40:
        clone_cmd += ["--branch", ref]
    clone_cmd += [url, str(REPO)]
    if sh(clone_cmd, 900) != 0:
        if not ref:
            receipt["status"] = "CLONE-FAIL"
            receipt["error_tail"] = LAST_SH_TAIL[0]
            finish()
            return 1
        # ref may be a raw commit SHA, which --branch cannot clone:
        # fall back to a plain clone + fetch of the pinned commit.
        if sh(["git", "clone", "--depth", "1", url, str(REPO)], 900) != 0:
            receipt["status"] = "CLONE-FAIL"
            receipt["error_tail"] = LAST_SH_TAIL[0]
            finish()
            return 1
        if sh(["bash", "-c",
               f"git fetch --depth 1 origin {ref} && git checkout --detach FETCH_HEAD"],
              300, cwd=REPO) != 0:
            receipt["status"] = "CLONE-FAIL"
            receipt["error_tail"] = LAST_SH_TAIL[0]
            finish()
            return 1
    if pinned_commit:
        cur_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()
        if cur_sha != pinned_commit:
            if sh(["bash", "-c",
                   f"git fetch --depth 1 origin {pinned_commit} && git checkout --detach FETCH_HEAD"],
                  300, cwd=REPO) != 0:
                receipt["status"] = "CLONE-FAIL"
                receipt["error_tail"] = LAST_SH_TAIL[0]
                finish()
                return 1
    if job.get("submodules"):
        if sh(["git", "submodule", "update", "--init", "--depth", "1"], 900,
              cwd=REPO) != 0:
            receipt["status"] = "CLONE-FAIL"
            finish()
            return 1
    receipt["head_sha"] = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
        text=True).stdout.strip()
    log(f"cloned {name} at {receipt['head_sha'][:12]}")

    # --- venv + deps
    if sh([sys.executable, "-m", "venv", str(VENV)], 300) != 0:
        receipt["status"] = "VENV-FAIL"
        finish()
        return 1
    pip = str(VENV / "bin/pip")
    py = str(VENV / "bin/python")
    # venv/bin goes on PATH before the install steps: job steps may invoke the
    # bare `python` (holidays' generate_mo_files.py), which must resolve to the
    # venv interpreter, not the sandbox's system python.
    os.environ["PATH"] = str(VENV / "bin") + os.pathsep + os.environ.get("PATH", "")
    sh([pip, "install", "-U", "-q", "pip", "setuptools", "wheel"], 600)
    for step in job["install"]:
        argv = [pip if a == "pip" else a for a in step]
        if sh(argv, 1200, cwd=REPO) != 0:
            receipt["status"] = "INSTALL-FAIL"
            receipt["error_tail"] = LAST_SH_TAIL[0]
            finish()
            return 1
    if sh([pip, "install", "-q", PYTEST_PIN], 600) != 0:
        receipt["status"] = "INSTALL-FAIL pytest"
        finish()
        return 1
    import glob as _glob
    for _sp in _glob.glob(str(VENV / "lib" / "python*" / "site-packages")):
        shutil.copy(BENCH / "zzz_bench_plugin.py", _sp)
    receipt["versions"] = {
        "python": subprocess.run([py, "-V"], capture_output=True, text=True).stdout.strip(),
        "pytest": subprocess.run([py, "-m", "pytest", "--version"],
                                 capture_output=True, text=True).stdout.strip(),
    }
    (suite_out / "pip-freeze.txt").write_text(
        subprocess.run([pip, "freeze"], capture_output=True, text=True).stdout)
    log(f"versions: {receipt['versions']}")

    py_parity = [py, "-m", "pytest", *testdirs, "-q", "--tb=short",
                 "-p", "no:cacheprovider", "-o", "addopts=",
                 "-p", "zzz_bench_plugin"]
    py_clean = [py, "-m", "pytest", *testdirs, "-q",
                "-p", "no:cacheprovider", "-o", "addopts="]
    z_base = [ZTEST, *testdirs, "-j4", f"--timeout={PER_TEST_TIMEOUT}",
              "--llm", f"--python={py}"]

    # --- PARITY pass (unmeasured wall; memory sampled here only — the
    # measured pairs below run bare so the sampler cannot tax the 4-worker arm)
    ev = suite_out / "pytest-events.jsonl"
    collect_f = suite_out / "collect-pytest.jsonl"
    plog = suite_out / "pytest-parity.log"
    zl = suite_out / "ztest-parity.jsonl"
    zcollect_f = suite_out / "collect-ztest.txt"
    for _stale in (ev, collect_f, plog, zl, zcollect_f):
        _stale.unlink(missing_ok=True)
    p_par = run_timed(f"{name}-parity-p", ["bash", "-c",
                      f"exec {' '.join(py_parity)} > {plog} 2>&1"], REPO, cap_s,
                      suite_out / "parity-p.out",
                      env_extra={"BENCH_JSONL": str(ev),
                                 "BENCH_COLLECT": str(collect_f)},
                      sample_mem=True)
    receipt["parity_py"] = {k: p_par[k] for k in
                            ("wall_s", "exit", "timeout", "peak_pss_kb")}
    z_par = run_timed(f"{name}-parity-z", ["bash", "-c",
                      f"exec {' '.join(z_base)} > {zl} 2> {suite_out}/ztest-parity.stderr"],
                      REPO, cap_s, suite_out / "parity-z.dummy",
                      sample_mem=True)
    receipt["parity_z"] = {k: z_par[k] for k in
                           ("wall_s", "exit", "timeout", "peak_pss_kb")}

    # Tiny suites finish before the sampler sees the real peak: rerun each
    # parity arm twice more and keep the max peak (memory is reported as the
    # peak over these unmeasured passes; counters/verdict come from the first).
    if (p_par["wall_s"] or 0) < 2.0:
        for extra in (2, 3):
            xp = run_timed(f"{name}-parity-p{extra}", ["bash", "-c",
                           f"exec {' '.join(py_parity)} > /dev/null 2>&1"],
                           REPO, cap_s, suite_out / f"parity-p{extra}.out",
                           env_extra={"BENCH_JSONL": "/dev/null",
                                      "BENCH_COLLECT": ""},
                           sample_mem=True)
            xz = run_timed(f"{name}-parity-z{extra}", ["bash", "-c",
                           f"exec {' '.join(z_base)} > /dev/null 2>&1"],
                           REPO, cap_s, suite_out / f"parity-z{extra}.out",
                           sample_mem=True)
            for dst, src in ((p_par, xp), (z_par, xz)):
                if src.get("peak_pss_kb") and (not dst.get("peak_pss_kb")
                                               or src["peak_pss_kb"] > dst["peak_pss_kb"]):
                    dst["peak_pss_kb"] = src["peak_pss_kb"]
                    dst["samples"] = src.get("samples", 0)
        receipt["memory_resamples"] = 2
        receipt["parity_py"]["peak_pss_kb"] = p_par["peak_pss_kb"]
        receipt["parity_z"]["peak_pss_kb"] = z_par["peak_pss_kb"]

    p_counts = pytest_summary_counts(plog)
    z_counts = ztest_summary(zl)
    receipt["pytest_counters"] = p_counts
    receipt["ztest_summary"] = z_counts
    collect_p = count_lines(collect_f)
    collect_z = (z_counts or {}).get("collected")
    with open(zcollect_f, "w", encoding="utf-8") as _zcf:
        _zc_proc = subprocess.run(
            [ZTEST, *testdirs, "--collect-only", "-q", f"--python={py}"],
            cwd=REPO, stdout=_zcf, stderr=subprocess.DEVNULL, timeout=cap_s
        )
    py_ids = {
        json.loads(ln)["nodeid"]
        for ln in (collect_f.read_text(errors="replace").splitlines() if collect_f.exists() else [])
        if ln.strip()
    }
    zt_ids = (
        {
            ln.strip()
            for ln in (zcollect_f.read_text(errors="replace").splitlines() if zcollect_f.exists() else [])
            if "::" in ln
        }
        if _zc_proc.returncode in (0, 5)
        else set()
    )
    collect_set_match = (
        (len(py_ids) == 0 and len(zt_ids) == 0 and collect_p == 0 and collect_z == 0)
        or (bool(py_ids) and bool(zt_ids) and py_ids == zt_ids)
    )
    receipt["collect"] = {"pytest": collect_p, "ztest": collect_z, "set_match": collect_set_match}

    mp, mz = merged_pytest(p_counts), merged_ztest(z_counts)
    receipt["merged"] = {"pytest": mp, "ztest": mz}
    base_failed = (mp["failed"] if mp else 1)
    classes = []
    if collect_p is None or collect_z is None or collect_p != collect_z or not collect_set_match:
        classes.append("COLLECT-DIFF")
    if not mp or not mz or mp != mz:
        classes.append("COUNT-DIFF")
    if p_par["exit"] != z_par["exit"]:
        classes.append("EXIT-DIFF")
    if p_par["exit"] not in (0, 5) or base_failed > 0:
        receipt["verdict"] = "BASELINE-RED"
    elif not classes:
        receipt["verdict"] = "FULL-PARITY"
    elif classes == ["COLLECT-DIFF"]:
        receipt["verdict"] = "COUNTERS-PARITY"
    else:
        receipt["verdict"] = "DIVERGED"
    receipt["classes"] = classes
    log(f"parity: {name} verdict={receipt['verdict']} classes={classes} "
        f"collectP={collect_p} collectZ={collect_z} mp={mp} mz={mz}")

    # --- MEASURED pass: 3 interleaved clean pairs
    runs = []
    for i in (1, 2, 3):
        plogi = suite_out / f"pytest-p{i}.log"
        rp = run_timed(f"{name}-p{i}", ["bash", "-c",
                       f"exec {' '.join(py_clean)} > {plogi} 2>&1"],
                       REPO, cap_s, suite_out / f"p{i}.out")
        rp["arm"] = "pytest"
        rp["counters"] = pytest_summary_counts(plogi)
        runs.append(rp)
        zli = suite_out / f"ztest-z{i}.jsonl"
        rz = run_timed(f"{name}-z{i}", ["bash", "-c",
                       f"exec {' '.join(z_base)} > {zli} 2> {suite_out}/ztest-z{i}.stderr"],
                       REPO, cap_s, suite_out / f"z{i}.out")
        rz["arm"] = "ztest"
        rz["counters"] = ztest_summary(zli)
        runs.append(rz)
    receipt["runs"] = runs

    pw = [r["wall_s"] for r in runs if r["arm"] == "pytest" and not r["timeout"]]
    zw = [r["wall_s"] for r in runs if r["arm"] == "ztest" and not r["timeout"]]
    # Memory comes from the instrumented parity passes (cgroup peak preferred
    # when the controller is writable, tree-PSS sampler otherwise).
    def mem_pick(run):
        return run.get("cg_peak_delta_kb") or run.get("peak_pss_kb")
    pp = [mem_pick(p_par)] if mem_pick(p_par) else []
    zp = [mem_pick(z_par)] if mem_pick(z_par) else []
    receipt["memory_source"] = ("cgroup memory.peak" if p_par.get("cg_peak_delta_kb")
                                else "tree-PSS sampler (parity pass)")
    receipt["metrics"] = {
        "p_wall_med_s": median(pw), "z_wall_med_s": median(zw),
        "p_pss_med_kb": median(pp), "z_pss_med_kb": median(zp),
        "p_exits": [r["exit"] for r in runs if r["arm"] == "pytest"],
        "z_exits": [r["exit"] for r in runs if r["arm"] == "ztest"],
        "p_timeouts": sum(1 for r in runs if r["arm"] == "pytest" and r["timeout"]),
        "z_timeouts": sum(1 for r in runs if r["arm"] == "ztest" and r["timeout"]),
    }
    m = receipt["metrics"]
    if m["p_wall_med_s"] and m["z_wall_med_s"]:
        m["wall_ratio_p_over_z"] = round(m["p_wall_med_s"] / max(m["z_wall_med_s"], 1e-9), 3)
    if m["p_pss_med_kb"] and m["z_pss_med_kb"]:
        m["pss_ratio_p_over_z"] = round(m["p_pss_med_kb"] / max(m["z_pss_med_kb"], 1e-9), 3)
    receipt["status"] = "OK"
    finish()

    shutil.rmtree(REPO, ignore_errors=True)
    shutil.rmtree(VENV, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
