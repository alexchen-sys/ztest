#!/usr/bin/env python3
"""Regression tests for ``scripts/build_wheels.py`` (pure Python, no Zig).

The builder is the only place where an artifact's architecture is decided, and
the aarch64 wheel cannot be executed on the CI runner that produces it. These
tests pin the static gate that replaces execution:

* ELF identity parsing (magic, class, byte order, ``e_machine``);
* rejection of an ``x86_64`` payload under ``aarch64`` tags, even when RECORD is
  internally consistent (the failure mode the old builder shipped);
* rejection of truncated / non-ELF / 32-bit / big-endian payloads;
* architecture-bound binary discovery — a bare ``dist/ztest`` is refused;
* wheel filename and platform-tag parsing that fails with a message instead of
  an ``IndexError``/``StopIteration`` traceback.

Run directly (``python3 -m pytest scripts/test_build_wheels.py -q``) or from the
CI ``build-and-test`` job.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_wheels as bw  # noqa: E402  (path set above on purpose)

X86_64 = "x86_64"
AARCH64 = "aarch64"


def make_elf(
    machine: int,
    *,
    magic: bytes = b"\x7fELF",
    elf_class: int = bw.ELFCLASS64,
    data_encoding: int = bw.ELFDATA2LSB,
    size: int = 64,
) -> bytes:
    """A minimal ELF header carrying just the fields the gate validates."""
    header = bytearray(b"\x00" * size)
    header[0:4] = magic
    header[4] = elf_class
    header[5] = data_encoding
    header[16:18] = (2).to_bytes(2, "little")  # e_type = ET_EXEC
    if size >= bw.E_MACHINE_OFFSET + 2:
        header[bw.E_MACHINE_OFFSET:bw.E_MACHINE_OFFSET + 2] = machine.to_bytes(2, "little")
    return bytes(header)


ELF_X86_64 = make_elf(bw.EM_X86_64)
ELF_AARCH64 = make_elf(bw.EM_AARCH64)


def dist_with(tmp_path: Path, files: dict[str, bytes]) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    for name, data in files.items():
        (dist / name).write_bytes(data)
    return dist


# ------------------------------------------------------------------- ELF ---
def test_elf_machine_reads_both_architectures() -> None:
    assert bw.elf_machine(ELF_X86_64) == bw.EM_X86_64 == 62
    assert bw.elf_machine(ELF_AARCH64) == bw.EM_AARCH64 == 183


def test_check_elf_arch_accepts_matching_payload() -> None:
    assert bw.check_elf_arch(ELF_X86_64, X86_64) == bw.EM_X86_64
    assert bw.check_elf_arch(ELF_AARCH64, AARCH64) == bw.EM_AARCH64


def test_check_elf_arch_rejects_x86_64_payload_for_aarch64() -> None:
    with pytest.raises(SystemExit, match="payload is x86_64, but this wheel advertises aarch64"):
        bw.check_elf_arch(ELF_X86_64, AARCH64)


def test_check_elf_arch_rejects_aarch64_payload_for_x86_64() -> None:
    with pytest.raises(SystemExit, match="payload is aarch64, but this wheel advertises x86_64"):
        bw.check_elf_arch(ELF_AARCH64, X86_64)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "truncated ELF header"),
        (b"\x7fELF", "truncated ELF header"),
        (make_elf(bw.EM_X86_64, size=12), "truncated ELF header"),
        (make_elf(bw.EM_X86_64, size=19), "truncated ELF header"),
        (make_elf(bw.EM_X86_64, magic=b"#!/s"), "not an ELF image"),
        (make_elf(bw.EM_X86_64, elf_class=1), "is not 64-bit"),
        (make_elf(bw.EM_X86_64, data_encoding=2), "is not little-endian"),
    ],
)
def test_elf_machine_fails_closed_without_traceback(payload: bytes, message: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        bw.elf_machine(payload, "test-payload")
    assert message in str(excinfo.value)
    assert "test-payload" in str(excinfo.value)


def test_check_elf_arch_rejects_unknown_architecture() -> None:
    with pytest.raises(SystemExit, match="unknown architecture"):
        bw.check_elf_arch(ELF_X86_64, "riscv64")


# ---------------------------------------------------------- binary search ---
def test_find_binary_rejects_bare_unaddressed_binary(tmp_path: Path) -> None:
    dist = dist_with(tmp_path, {"ztest": ELF_X86_64})
    with pytest.raises(SystemExit, match="has no architecture and is rejected"):
        bw.find_binary(dist, X86_64, "0.5.1")


def test_find_binary_uses_arch_bound_file(tmp_path: Path) -> None:
    dist = dist_with(tmp_path, {f"ztest-{X86_64}": ELF_X86_64})
    data, origin = bw.find_binary(dist, X86_64, "0.5.1")
    assert data == ELF_X86_64
    assert origin == "ztest-x86_64"


def test_find_binary_rejects_mislabelled_arch_file(tmp_path: Path) -> None:
    dist = dist_with(tmp_path, {f"ztest-{AARCH64}": ELF_X86_64})
    with pytest.raises(SystemExit, match="payload is x86_64, but this wheel advertises aarch64"):
        bw.find_binary(dist, AARCH64, "0.5.1")


def test_find_binary_reads_tarball_and_checks_payload(tmp_path: Path) -> None:
    import io
    import tarfile

    dist = tmp_path / "dist"
    dist.mkdir()
    tarball = dist / "ztest-v0.5.1-linux-aarch64.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        info = tarfile.TarInfo("ztest")
        info.size = len(ELF_AARCH64)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(ELF_AARCH64))
    data, origin = bw.find_binary(dist, AARCH64, "0.5.1")
    assert data == ELF_AARCH64
    assert origin == "ztest-v0.5.1-linux-aarch64.tar.gz:ztest"

    tarball.unlink()
    with tarfile.open(tarball, "w:gz") as tf:
        info = tarfile.TarInfo("ztest")
        info.size = len(ELF_X86_64)
        tf.addfile(info, io.BytesIO(ELF_X86_64))
    with pytest.raises(SystemExit, match="payload is x86_64"):
        bw.find_binary(dist, AARCH64, "0.5.1")


# ------------------------------------------------------- filename and tags ---
def test_parse_wheel_filename_round_trip() -> None:
    name = "ztest_runner-0.5.1-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64.whl"
    python_tag, abi_tag, dist, tags = bw.parse_wheel_filename(Path(name))
    assert (python_tag, abi_tag, dist) == ("py3", "none", "ztest_runner")
    assert tags == ["manylinux_2_17_x86_64", "manylinux2014_x86_64", "musllinux_1_1_x86_64"]
    assert bw.arch_for_tags(tags, name) == X86_64


@pytest.mark.parametrize(
    "name",
    [
        "ztest_runner-0.5.1-py3-none.whl",
        "ztest_runner-0.5.1.whl",
        "ztest_runner.whl",
        "ztest_runner-0.5.1-py3-none-manylinux_2_17_x86_64.whl.off",
    ],
)
def test_parse_wheel_filename_fails_with_message(name: str) -> None:
    with pytest.raises(SystemExit, match="not a wheel filename"):
        bw.parse_wheel_filename(Path(name))


def test_arch_for_tags_rejects_duplicate_and_mixed_tags() -> None:
    duplicate = ["musllinux_1_1_x86_64", "musllinux_1_1_x86_64"]
    with pytest.raises(SystemExit, match="duplicate platform tags"):
        bw.arch_for_tags(duplicate, "dup.whl")
    mixed = ["musllinux_1_1_x86_64", "musllinux_1_1_aarch64"]
    with pytest.raises(SystemExit, match="span several architectures"):
        bw.arch_for_tags(mixed, "mixed.whl")
    with pytest.raises(SystemExit, match="no ELF architecture mapping"):
        bw.arch_for_tags(["musllinux_1_1_riscv64"], "unknown.whl")
    with pytest.raises(SystemExit, match="no platform tags"):
        bw.arch_for_tags([], "empty.whl")


# ------------------------------------------------------------ verify_wheel ---
def rewrite_payload(wheel: Path, payload: bytes) -> None:
    """Replace the executable and recompute RECORD, so only ELF can object."""
    with zipfile.ZipFile(wheel) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    script = next(name for name in members if name.endswith(".data/scripts/ztest"))
    record = next(name for name in members if name.endswith(".dist-info/RECORD"))
    members[script] = payload
    lines = []
    for name, data in members.items():
        if name == record:
            continue
        lines.append(f"{name},sha256={bw.b64_digest(data)},{len(data)}")
    lines.append(f"{record},,")
    members[record] = ("\n".join(lines) + "\n").encode()
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            bw.zip_write(zf, name, data, 0o755 if name == script else 0o644)


def build(tmp_path: Path, arch: str, payload: bytes) -> Path:
    dist = dist_with(tmp_path, {f"ztest-{arch}": payload})
    return bw.build_wheel(dist, arch, "0.5.1", "ztest long description")


def test_verify_wheel_accepts_a_consistent_wheel(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wheel = build(tmp_path, X86_64, ELF_X86_64)
    bw.verify_wheel(wheel)
    assert "ELF x86_64" in capsys.readouterr().out


def test_verify_wheel_rejects_swapped_payload_despite_valid_record(tmp_path: Path) -> None:
    wheel = build(tmp_path, AARCH64, ELF_AARCH64)
    rewrite_payload(wheel, ELF_X86_64)  # RECORD now matches this payload
    with pytest.raises(SystemExit, match="payload is x86_64, but this wheel advertises aarch64"):
        bw.verify_wheel(wheel)


def test_verify_wheel_rejects_truncated_payload(tmp_path: Path) -> None:
    wheel = build(tmp_path, X86_64, ELF_X86_64)
    rewrite_payload(wheel, b"\x7fELF")
    with pytest.raises(SystemExit, match="truncated ELF header"):
        bw.verify_wheel(wheel)


def test_verify_wheel_rejects_bad_mode_and_missing_members(tmp_path: Path) -> None:
    wheel = build(tmp_path, X86_64, ELF_X86_64)
    with zipfile.ZipFile(wheel) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            bw.zip_write(zf, name, data, 0o644)
    with pytest.raises(SystemExit, match="expected 0o755"):
        bw.verify_wheel(wheel)


def test_verify_wheel_reports_missing_member_without_stopiteration(tmp_path: Path) -> None:
    wheel = build(tmp_path, X86_64, ELF_X86_64)
    with zipfile.ZipFile(wheel) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            if name.endswith(".dist-info/WHEEL"):
                continue
            bw.zip_write(zf, name, data, 0o755 if name.endswith("/ztest") else 0o644)
    with pytest.raises(SystemExit, match="missing .dist-info/WHEEL member"):
        bw.verify_wheel(wheel)


def test_build_wheel_refuses_mismatched_payload_before_writing(tmp_path: Path) -> None:
    dist = dist_with(tmp_path, {"ztest-aarch64": ELF_X86_64})
    with pytest.raises(SystemExit, match="payload is x86_64"):
        bw.build_wheel(dist, AARCH64, "0.5.1", "ztest")
    assert not list(dist.glob("*.whl")), "no wheel may be written for a mismatched payload"


def test_metadata_requires_python_311(tmp_path: Path) -> None:
    wheel = build(tmp_path, X86_64, ELF_X86_64)
    with zipfile.ZipFile(wheel) as zf:
        metadata = zf.read(next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))).decode()
    assert "Requires-Python: >=3.11" in metadata
