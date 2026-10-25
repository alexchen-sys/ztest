#!/usr/bin/env python3
"""Release-binary sterility gate: no build-host paths inside a shipped binary.

Why this is not a plain ``grep`` over ``strings``
------------------------------------------------
``strings`` output is not line-aligned with rodata literals. A single emitted
string can concatenate several literals, e.g. a release binary legitimately
contains::

    /ztest_shim.py/tmp/ztest__getdents64 errno=ztest: discovery warning: ...

Runtime path templates (``/tmp/ztest_{d}_{d}_{x}``, ``/tmp/ztest_``, the
``/tmp/ztest_venv_<pid>`` helpers) and the production ignore-list literal
``zig-cache`` (``src/pyfiles.zig``) are *correct* content of a release binary.
A leaked ``/home/runner/work/ztest/src/main.zig`` or ``/opt/zig-0.17.0/lib/std``
is a defect. A line-oriented ``grep -v '/tmp/ztest_'`` would hide a real leak
that happens to share a line with a runtime literal, and a plain
``grep -E '/(home|mnt|opt|tmp)/|zig-cache'`` fails on the legal literals —
which is exactly the trap this gate replaces.

Rules are therefore evaluated per *occurrence*, never per line:

  leak    ``/(home|mnt|opt|root)/<path char>``   absolute build-host path
  leak    ``/tmp/`` followed by anything except ``ztest_``   (runtime namespace)
  leak    ``zig-cache`` in a path form: ``.zig-cache``, ``/zig-cache``,
          ``zig-cache/`` — a bare ``zig-cache`` token is a production literal
  leak    ``.cache/zig``                          the global zig cache
  leak    ``ztest-work``                          build work-dir marker
  leak    ``hunt-data``                           bench-artifact host marker

  allowed ``/tmp/ztest_{d}_{d}_{x}``, ``/tmp/ztest_...``, bare ``zig-cache``,
          ``/usr/bin/python3`` (documented interpreter fallback),
          ``{s}/ztest_shim.py`` / ``/ztest_shim.py`` (embedded shim name).

Usage::

    python3 scripts/check_sterility.py --self-test          # gate self-controls
    python3 scripts/check_sterility.py zig-out/bin/ztest    # scan a binary

Exit codes: 0 clean, 1 leaks found, 2 usage error or failed self-test.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MIN_STRING = 4
_PRINTABLE = frozenset(range(0x20, 0x7F)) | {0x09}

# Absolute build-host roots. `/usr/...` is deliberately absent: `/usr/bin/python3`
# is a documented runtime fallback literal, not a build-host leak.
ABS_HOST_RE = re.compile(rb"/(?:home|mnt|opt|root)/[A-Za-z0-9._~-]")
TMP_RE = re.compile(rb"/tmp/")
ZIG_CACHE_RE = re.compile(rb"zig-cache")
CACHE_ZIG_RE = re.compile(rb"\.cache/zig")
MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"ztest-work", "build work-dir marker"),
    (b"hunt-data", "bench-artifact host marker"),
)
# The only legal inhabitants of the /tmp namespace in a release binary.
RUNTIME_TMP_PREFIX = b"ztest_"

_SEP_BYTES = frozenset({0x2E, 0x2F, 0x5C})  # '.' '/' '\\'


def extract_strings(data: bytes, min_len: int = MIN_STRING) -> list[bytes]:
    """Printable ASCII runs, the same class of token ``strings`` reports."""
    out: list[bytes] = []
    start: int | None = None
    for index, byte in enumerate(data):
        if byte in _PRINTABLE:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= min_len:
                out.append(data[start:index])
            start = None
    if start is not None and len(data) - start >= min_len:
        out.append(data[start:])
    return out


def _snippet(text: bytes, start: int, end: int, pad: int = 40) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    fragment = text[lo:hi].decode("latin-1").replace("\x00", "\\0")
    return ("..." if lo else "") + fragment + ("..." if hi < len(text) else "")


def scan_string(text: bytes) -> list[tuple[str, str]]:
    """Every leak occurrence in one extracted string, as (class, snippet)."""
    findings: list[tuple[str, str]] = []

    for match in ABS_HOST_RE.finditer(text):
        findings.append(("absolute build-host path", _snippet(text, match.start(), match.end())))

    for match in TMP_RE.finditer(text):
        tail = text[match.end():]
        if not tail.startswith(RUNTIME_TMP_PREFIX):
            findings.append(("build-host /tmp path (outside the ztest_ runtime namespace)",
                             _snippet(text, match.start(), match.end())))

    for match in ZIG_CACHE_RE.finditer(text):
        prev = text[match.start() - 1] if match.start() else 0
        nxt = text[match.end()] if match.end() < len(text) else 0
        if prev in _SEP_BYTES or nxt in _SEP_BYTES:
            findings.append(("zig cache directory path", _snippet(text, match.start(), match.end())))

    for match in CACHE_ZIG_RE.finditer(text):
        findings.append(("global zig cache path", _snippet(text, match.start(), match.end())))

    for marker, label in MARKERS:
        for match in re.finditer(re.escape(marker), text):
            findings.append((label, _snippet(text, match.start(), match.end())))

    return findings


def scan_binary(path: Path, min_len: int = MIN_STRING) -> tuple[int, list[tuple[str, str]]]:
    data = path.read_bytes()
    strings = extract_strings(data, min_len)
    findings: list[tuple[str, str]] = []
    for text in strings:
        findings.extend(scan_string(text))
    return len(strings), findings


# --------------------------------------------------------------- self-test ---
# Each case is raw bytes as they would land in rodata: positive controls must be
# reported, negative controls must stay quiet. The merged-rodata cases encode
# the real failure mode this gate exists for — a runtime literal and a host path
# inside one emitted string.
_SELF_TEST_LEAKS: dict[str, bytes] = {
    "absolute /home path": b"/home/runner/work/ztest/ztest/src/main.zig",
    "absolute /opt toolchain path": b"/opt/zig-0.17.0/lib/std",
    "absolute /mnt path": b"/mnt/hunt-data/artifacts/ztest/run.jsonl",
    "absolute /root path": b"/root/build/ztest",
    "non-ztest /tmp path": b"/tmp/zig-cache/o/9f2c1",
    "dot zig-cache path": b".zig-cache/o/1234abcd/manifest",
    "relative old zig-cache path": b"zig-cache/o/1234abcd",
    "global zig cache": b"/home/u/.cache/zig/b/f1df",
    "build work-dir marker": b"ztest-work/sup-4821",
    "bench marker": b"/srv/hunt-data/artifacts",
    "runtime literal then real host path (merged rodata)":
        b"/ztest_shim.py/tmp/ztest__getdents64 errno=ztest: discovery warning: "
        b"/home/runner/work/ztest/src/main.zig",
}

_SELF_TEST_CLEAN: dict[str, bytes] = {
    "run-dir template": b"/tmp/ztest_{d}_{d}_{x}",
    "run-dir prefix": b"/tmp/ztest_",
    "concrete run dir": b"/tmp/ztest_4291_8123_0x9f2c1",
    "venv helper template": b"/tmp/ztest_venv_{d}",
    "bare ignore-list literal": b"zig-cache",
    "embedded shim name": b"/ztest_shim.py",
    "formatted shim name": b"{s}/ztest_shim.py",
    "merged rodata with runtime tmp literal":
        b"/ztest_shim.py/tmp/ztest__getdents64 errno=ztest: discovery warning: "
        b":  (coverage may be incomplete)",
    "interpreter fallback": b"/usr/bin/python3",
    "proc path": b"/proc/{d}/smaps_rollup",
}


def self_test(verbose: bool = False) -> list[str]:
    """Returns a list of failures; empty means the rules behave as documented."""
    failures: list[str] = []
    for label, blob in _SELF_TEST_LEAKS.items():
        found = scan_string(blob)
        if not found:
            failures.append(f"positive control not detected: {label} ({blob!r})")
        elif verbose:
            print(f"  ok  leak detected: {label} -> {found[0][0]}")
    for label, blob in _SELF_TEST_CLEAN.items():
        found = scan_string(blob)
        if found:
            failures.append(f"negative control flagged: {label} ({blob!r}) -> {found}")
        elif verbose:
            print(f"  ok  clean: {label}")
    # Extraction itself: the gate must see literals that `strings` sees.
    extracted = extract_strings(b"\x00\x01" + _SELF_TEST_LEAKS["absolute /home path"] + b"\x00")
    if not extracted:
        failures.append("extract_strings returned nothing for an embedded path literal")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description="sterility gate for release binaries")
    ap.add_argument("binary", nargs="?", help="binary to scan")
    ap.add_argument("--self-test", action="store_true", help="run the gate's own positive/negative controls")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--min-string", type=int, default=MIN_STRING, help="minimum printable run length (default 4)")
    ap.add_argument("--max-report", type=int, default=20, help="stop printing findings after N (default 20)")
    args = ap.parse_args()

    if args.self_test:
        failures = self_test(verbose=args.verbose)
        if failures:
            for failure in failures:
                print(f"sterility self-test FAILED: {failure}", file=sys.stderr)
            return 2
        print(f"sterility self-test: {len(_SELF_TEST_LEAKS)} leak controls and "
              f"{len(_SELF_TEST_CLEAN)} clean controls behave as documented")
        if not args.binary:
            return 0

    if not args.binary:
        ap.print_usage(sys.stderr)
        print("error: no binary to scan", file=sys.stderr)
        return 2

    path = Path(args.binary)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 2

    scanned, findings = scan_binary(path, args.min_string)
    if findings:
        print(f"::error::{path} leaks build-host paths ({len(findings)} occurrence(s))")
        for label, snippet in findings[: args.max_report]:
            print(f"  {label}: {snippet}")
        if len(findings) > args.max_report:
            print(f"  ... {len(findings) - args.max_report} more")
        return 1
    print(f"sterility: 0 matching strings in {path} ({scanned} strings scanned, {path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
