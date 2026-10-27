# ztest shim source module: 87_failure_report
# Part of the generated bundle src/shim/ztest_shim.py (ADR-006 v2).
# Edit this module, then regenerate with: python3 scripts/bundle_shim.py
# Provides/requires are documented by the section banner of the original
# monolith kept verbatim below; no cross-module import machinery exists.
# The tail of a captured stream carried into a failure record. A test can log
# megabytes; the report keeps the end of the stream, which is what explains
# the failure.
_CAPTURE_REPORT_CHARS = 65536

# Stdlib unittest driver frames (run/testPartExecutor/_callTestMethod and the
# result/suite plumbing) are runner machinery, not user code. When a plain
# function test executes through the _FunctionTestCase adapter these frames
# would otherwise dominate the rendered traceback with six lines of
# unittest/case.py internals.
_UNITTEST_FRAME_RE = re.compile(
    r"(^|[\\/])unittest[\\/](case|result|suite|runner|main|loader|_log)\.py$"
)


def _is_shim_frame(path: str) -> bool:
    return os.path.basename(path) == _SHIM_BASENAME


def _is_internal_runner_frame(path: str) -> bool:
    return _is_shim_frame(path) or bool(_UNITTEST_FRAME_RE.search(path))


def _strip_shim_frames(text: str) -> str:
    """Remove the runner's own frames from a formatted traceback.

    A report shows the test's frames; the kernel's runtime path (its code is
    unpacked into a per-process temporary directory) must not surface in user
    output. Frame blocks are the ``File "..."`` header plus the indented
    source/caret lines that belong to it.
    """
    if _SHIM_BASENAME not in text and "unittest" not in text:
        return text
    kept: list[str] = []
    dropping = False
    for line in text.splitlines():
        match = _TB_FRAME_RE.match(line)
        if match is not None:
            dropping = _is_internal_runner_frame(match.group("path"))
            if dropping:
                continue
        elif dropping:
            if line[:1] in (" ", "\t") and line.strip():
                continue
            dropping = False
        kept.append(line)
    return "\n".join(kept)


def _capture_sections(out_txt: str, err_txt: str) -> str:
    """The captured-output block pytest appends to a failure report."""
    chunks: list[str] = []
    for label, text in (("stdout", out_txt), ("stderr", err_txt)):
        if not text:
            continue
        if len(text) > _CAPTURE_REPORT_CHARS:
            text = "... output truncated ...\n" + text[-_CAPTURE_REPORT_CHARS:]
        if not text.endswith("\n"):
            text += "\n"
        chunks.append(f"{f' Captured {label} call ':-^80}\n{text}")
    return "\n".join(chunks)


def _sanitize_frame_data(data: dict[str, Any]) -> dict[str, Any]:
    """Scrub the runner's internal paths from the textual frame fields."""
    for key in ("tb", "msg"):
        value = data.get(key)
        if isinstance(value, str) and (_SHIM_BASENAME in value or "unittest" in value):
            data[key] = _strip_shim_frames(value)
    return data


def _report_failure(ipc: IpcChannel, item: TestItemSpec, exc: BaseException, out_txt: str, err_txt: str, dur_ms: int, error_name: str | None = None) -> None:
    tb_entries = traceback.extract_tb(exc.__traceback__)
    filtered = [f for f in tb_entries if not _is_internal_runner_frame(f.filename)]
    last_frame = filtered[-1] if filtered else (tb_entries[-1] if tb_entries else None)
    loc = f"{last_frame.filename}:{last_frame.lineno}" if last_frame else str(item.filepath)
    short_tb = "\n".join(
        f"  File \"{f.filename}\", line {f.lineno}, in {f.name}\n    {f.line or ''}"
        for f in (filtered or tb_entries)[-6:]
    )
    msg = str(exc)
    if len(msg) > 256_000:
        msg = msg[:256_000] + "... [truncated]"
    full_tb = f"{short_tb}\n{type(exc).__name__}: {msg}"
    captured = _capture_sections(out_txt, err_txt)
    if captured:
        # Captured output belongs to the failure context: without it a
        # failure caused by what the test printed is undebuggable.
        msg = f"{msg}\n\n{captured}"
        if len(msg) > 256_000:
            msg = msg[:256_000] + "... [truncated]"
        full_tb = f"{full_tb}\n{captured}"
    ipc.send_event(
        "TEST_FAIL",
        nodeid=item.nodeid,
        loc=loc,
        error=error_name or type(exc).__name__,
        msg=msg,
        # The traceback is cut under the 16 MiB payload ceiling — a giant
        # traceback must not convert FAILED into CRASHED (PayloadTooLarge).
        tb=full_tb[:12_000_000],
        stdout_tail=out_txt[-500:] if out_txt else "",
        stderr_tail=err_txt[-500:] if err_txt else "",
        duration_ms=dur_ms)


