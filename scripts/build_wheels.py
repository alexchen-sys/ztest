#!/usr/bin/env python3
"""Assemble platform wheels for ztest from prebuilt static musl binaries.

Why wheels carry a *native binary* instead of being ``py3-none-any``: the whole
point of ztest is that the supervisor is not Python, so there is nothing to
compile at install time and nothing to fall back to. ``pip install ztest-runner``
must drop a working ``ztest`` on ``$PATH`` in under a second.

Layout produced per architecture::

    ztest_runner/__init__.py            # metadata + binary_path()
    ztest_runner-<version>.data/scripts/ztest     # mode 0755 -> <venv>/bin/ztest
    ztest_runner-<version>.dist-info/{METADATA,WHEEL,RECORD}

``.data/scripts`` is used rather than a ``console_scripts`` entry point on
purpose: an entry point would install a Python shim (a second interpreter
startup, then ``execve``), while a script payload lands directly in the scripts
directory that ``pip``, ``pipx`` and ``uv tool``/``uvx`` already put on PATH.

Platform tags: the binary is statically linked against musl and needs no shared
libraries at all, so it is honestly installable on glibc *and* musl systems.
Each wheel therefore advertises musllinux plus the two manylinux aliases so that
``pip`` on any Linux resolves it without ``--platform`` overrides.

Architecture identity: a wheel's platform tags are only a promise; the payload
is what pip installs. Before a wheel is written — and again in ``verify_wheel``
— the builder reads the ELF header of the payload and refuses any binary whose
``e_machine`` does not match the architecture the filename advertises. Without
that check an x86_64 tarball could be packaged as an aarch64 wheel and every
gate short of running it would stay green.

Usage::

    python3 scripts/build_wheels.py --dist dist
    python3 scripts/build_wheels.py --dist dist --verify
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import re
import tarfile
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "ztest-runner"
MODULE = "ztest_runner"
DEFAULT_VERSION = "0.6.0"

# Wheel target -> pip platform tags. Linux payloads are static musl; macOS
# wheels intentionally use the oldest pip-accepted macOS tag: the wheel tag is
# a compatibility floor, not a claim of a newer SDK than the payload requires.
PLATFORM_TAGS = {
    "linux-x86_64": ["manylinux_2_17_x86_64", "manylinux2014_x86_64", "musllinux_1_1_x86_64"],
    "linux-aarch64": ["manylinux_2_17_aarch64", "manylinux2014_aarch64", "musllinux_1_1_aarch64"],
    "macos-x86_64": ["macosx_10_13_x86_64"],
    "macos-arm64": ["macosx_11_0_arm64"],
}
# Backwards-compatible local iteration API: an unqualified arch means Linux.
LEGACY_ARCH_TARGETS = {"x86_64": "linux-x86_64", "aarch64": "linux-aarch64", "arm64": "macos-arm64"}

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# ELF identity. A wheel payload is an executable, not a blob: class, byte order
# and machine decide whether the installed `ztest` can even be started.
ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
ELFDATA2LSB = 1
E_MACHINE_OFFSET = 18
EM_X86_64 = 62
EM_AARCH64 = 183
ARCH_E_MACHINE = {"x86_64": EM_X86_64, "aarch64": EM_AARCH64}
E_MACHINE_NAMES = {EM_X86_64: "x86_64", EM_AARCH64: "aarch64"}

from native_artifact import ArtifactError, check_release

WHEEL_NAME_RE = re.compile(
    r"^(?P<dist>[A-Za-z0-9_.]+)-(?P<version>[^-]+)-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>.+)\.whl$"
)

METADATA_TMPL = """Metadata-Version: 2.1
Name: {name}
Version: {version}
Summary: Fast pytest-compatible test runner with a native Zig supervisor
Home-page: https://github.com/alexchen-sys/ztest
Author: Alex Chen
License: MIT
Keywords: pytest,test-runner,testing,zig,cli
Requires-Python: >=3.11
Classifier: Development Status :: 4 - Beta
Classifier: Environment :: Console
Classifier: Intended Audience :: Developers
Classifier: License :: OSI Approved :: MIT License
Classifier: Operating System :: POSIX
Classifier: Operating System :: MacOS
Classifier: Programming Language :: Python :: 3
Classifier: Topic :: Software Development :: Testing
Description-Content-Type: text/markdown

{long_description}
"""

WHEEL_TMPL = """Wheel-Version: 1.0
Generator: ztest-build-wheels {version}
Root-Is-Purelib: false
{tags}
"""

INIT_TMPL = '''"""ztest-runner — a pytest-compatible runner with a native Zig supervisor.

This package is a delivery vehicle for the ``ztest`` executable: the wheel
installs a statically linked binary into the environment's scripts directory
(``<venv>/bin/ztest``), so no Python code runs in the hot path and no compiler
is needed at install time.
"""

from __future__ import annotations

import os
import sysconfig

__version__ = "{version}"
__all__ = ["__version__", "binary_path", "BINARY_NAME"]

BINARY_NAME = "ztest"


def binary_path() -> str:
    """Absolute path to the installed ``ztest`` executable.

    Resolution mirrors how installers place `.data/scripts` payloads: the
    environment's scripts directory first (venv/pipx/uv), then the same
    directory as an absolute fallback for editable layouts.
    """
    scripts = sysconfig.get_path("scripts")
    candidate = os.path.join(scripts, BINARY_NAME)
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(
        "ztest binary is missing from {{0}}; reinstall ztest-runner".format(scripts)
    )
'''


def b64_digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def elf_machine(data: bytes, origin: str = "payload") -> int:
    """`e_machine` of a 64-bit little-endian ELF image, or SystemExit.

    Everything that makes the field meaningful is validated first: a truncated
    header, a 32-bit image, a big-endian image and a non-ELF file all fail with
    a message naming the origin, never with an IndexError/struct traceback.
    """
    if len(data) < E_MACHINE_OFFSET + 2:
        raise SystemExit(
            f"{origin}: truncated ELF header ({len(data)} bytes, need >= {E_MACHINE_OFFSET + 2})"
        )
    if data[:4] != ELF_MAGIC:
        raise SystemExit(f"{origin}: not an ELF image (magic {data[:4]!r})")
    if data[4] != ELFCLASS64:
        raise SystemExit(f"{origin}: ELF EI_CLASS={data[4]} is not 64-bit (EI_CLASS={ELFCLASS64})")
    if data[5] != ELFDATA2LSB:
        raise SystemExit(f"{origin}: ELF EI_DATA={data[5]} is not little-endian (EI_DATA={ELFDATA2LSB})")
    return int.from_bytes(data[E_MACHINE_OFFSET:E_MACHINE_OFFSET + 2], "little")


def check_elf_arch(data: bytes, arch: str, origin: str = "payload") -> int:
    """Refuse a payload whose ELF machine contradicts the advertised arch."""
    expected = ARCH_E_MACHINE.get(arch)
    if expected is None:
        raise SystemExit(f"{origin}: unknown architecture {arch!r} (known: {sorted(ARCH_E_MACHINE)})")
    machine = elf_machine(data, origin)
    if machine != expected:
        found = E_MACHINE_NAMES.get(machine, f"e_machine={machine}")
        raise SystemExit(
            f"{origin}: payload is {found}, but this wheel advertises {arch} "
            f"(e_machine {machine} != {expected})"
        )
    return machine


def check_payload_target(data: bytes, target: str, origin: str = "payload") -> str:
    """Validate ELF or thin Mach-O identity for a wheel target."""
    try:
        if target.startswith("linux-"):
            check_elf_arch(data, target.split("-", 1)[1], origin)
        else:
            check_release(data, target, origin)
        return target
    except ArtifactError as error:
        raise SystemExit(str(error)) from error


def arch_for_tags(tags: list[str], origin: str) -> str:
    """The single wheel target promised by the filename's platform tags."""
    if not tags:
        raise SystemExit(f"{origin}: no platform tags")
    duplicates = sorted({tag for tag in tags if tags.count(tag) > 1})
    if duplicates:
        raise SystemExit(f"{origin}: duplicate platform tags {duplicates}")
    arches: set[str] = set()
    for tag in tags:
        owners = [target for target, known in PLATFORM_TAGS.items() if tag in known]
        if not owners:
            raise SystemExit(f"{origin}: platform tag {tag!r} has no ELF architecture mapping")
        if len(owners) > 1:
            raise SystemExit(f"{origin}: platform tag {tag!r} maps to several architectures {owners}")
        arches.add(owners[0])
    if len(arches) != 1:
        raise SystemExit(f"{origin}: tags span several architectures {sorted(arches)}")
    return arches.pop()


def parse_wheel_filename(path: Path) -> tuple[str, str, str, list[str]]:
    """(python tag, abi tag, distribution, platform tags) from a wheel name."""
    match = WHEEL_NAME_RE.match(path.name)
    if match is None:
        raise SystemExit(
            f"{path.name}: not a wheel filename "
            "(expected <distribution>-<version>-<python>-<abi>-<platform>.whl)"
        )
    platform_tags = match.group("platform").split(".")
    return match.group("python"), match.group("abi"), match.group("dist"), platform_tags


def find_binary(dist_dir: Path, arch: str, version: str) -> tuple[bytes, str]:
    """Locate an OS/architecture-bound binary and validate its native header."""
    target = LEGACY_ARCH_TARGETS.get(arch, arch)
    if target not in PLATFORM_TAGS:
        raise SystemExit(f"unknown wheel target {arch!r}")
    os_name, payload_arch = target.split("-", 1)
    tarballs = [
        dist_dir / f"ztest-v{version}-{os_name}-{payload_arch}.tar.gz",
        dist_dir / f"ztest-{version}-{os_name}-{payload_arch}.tar.gz",
    ]
    for tb in tarballs:
        if tb.is_file():
            with tarfile.open(tb, "r:gz") as tf:
                members = [m for m in tf.getmembers() if m.isfile() and Path(m.name).name == "ztest"]
                if not members:
                    raise SystemExit(f"{tb}: no `ztest` entry inside the tarball")
                if len(members) > 1:
                    raise SystemExit(
                        f"{tb}: {len(members)} `ztest` entries inside the tarball "
                        f"({[m.name for m in members]}); refusing to guess which one to package"
                    )
                member = members[0]
                fh = tf.extractfile(member)
                assert fh is not None
                data = fh.read()
                check_payload_target(data, target, f"{tb.name}:{member.name}")
                return data, f"{tb.name}:{member.name}"
    for name in (f"ztest-{payload_arch}", f"ztest-{target}", f"ztest-{version}-{target}", f"ztest-{version}-{payload_arch}"):
        cand = dist_dir / name
        if cand.is_file():
            data = cand.read_bytes()
            check_payload_target(data, target, cand.name)
            return data, cand.name
    raise SystemExit(
        f"no prebuilt {target} binary in {dist_dir} "
        f"(expected ztest-v{version}-{os_name}-{payload_arch}.tar.gz or ztest-{payload_arch}; a bare "
        f"`ztest` has no architecture and is rejected)"
    )


def zip_write(zf: zipfile.ZipFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    # The regular-file bit (S_IFREG) must be present: pip's zip_item_is_executable()
    # does `stat.S_ISREG(mode) and mode & 0o111`, so a bare 0o755 silently loses the
    # executable bit on extraction.
    info.external_attr = ((0o100000 | (mode & 0o777)) & 0xFFFF) << 16
    info.create_system = 3  # unix: required for mode bits to survive extraction
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def build_wheel(dist_dir: Path, arch: str, version: str, long_description: str) -> Path:
    target = LEGACY_ARCH_TARGETS.get(arch, arch)
    tags = PLATFORM_TAGS[target]
    dist_info = f"{MODULE}-{version}.dist-info"
    data_dir = f"{MODULE}-{version}.data"
    wheel_name = f"{MODULE}-{version}-py3-none-{'.'.join(tags)}.whl"
    out = dist_dir / wheel_name

    binary, origin = find_binary(dist_dir, target, version)
    if not binary:
        raise SystemExit(f"{origin}: empty binary")

    init_py = INIT_TMPL.format(version=version).encode()
    metadata = METADATA_TMPL.format(
        name=DIST_NAME,
        version=version,
        RESERVED="",
        long_description=long_description,
    ).encode()
    wheel = WHEEL_TMPL.format(version=version, tags="\n".join(f"Tag: py3-none-{t}" for t in tags)).encode()

    members: list[tuple[str, bytes, int]] = [
        (f"{MODULE}/__init__.py", init_py, 0o644),
        (f"{data_dir}/scripts/ztest", binary, 0o755),
        (f"{dist_info}/METADATA", metadata, 0o644),
        (f"{dist_info}/WHEEL", wheel, 0o644),
    ]

    record_lines = []
    for name, data, _mode in members:
        record_lines.append(f"{name},sha256={b64_digest(data)},{len(data)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    record = ("\n".join(record_lines) + "\n").encode()
    members.append((f"{dist_info}/RECORD", record, 0o644))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data, mode in members:
            zip_write(zf, name, data, mode)
    out.write_bytes(buf.getvalue())
    print(f"[wheel] {out.name}  {out.stat().st_size} bytes  (binary {len(binary)} B from {origin})")
    return out


def _single_member(names: list[str], suffix: str, origin: str) -> str:
    """The one member ending in `suffix`; SystemExit when absent or ambiguous."""
    matches = [name for name in names if name.endswith(suffix)]
    if not matches:
        raise SystemExit(f"{origin}: missing {suffix} member")
    if len(matches) > 1:
        raise SystemExit(f"{origin}: ambiguous {suffix} members {matches}")
    return matches[0]


def verify_wheel(path: Path) -> None:
    """Fail loudly if RECORD, tags, payload architecture or mode are wrong."""
    python_tag, abi_tag, _dist, platform_tags = parse_wheel_filename(path)
    expected_target = arch_for_tags(platform_tags, path.name)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit(f"{path.name}: corrupt member {bad}")
        record_name = _single_member(names, ".dist-info/RECORD", path.name)
        script = _single_member(names, ".data/scripts/ztest", path.name)
        info = zf.getinfo(script)
        mode = (info.external_attr >> 16) & 0o777
        if mode != 0o755:
            raise SystemExit(f"{path.name}: payload mode is {oct(mode)}, expected 0o755")
        payload = zf.read(script)
        check_payload_target(payload, expected_target, f"{path.name}:{script}")
        recorded = {}
        for line in zf.read(record_name).decode().splitlines():
            parts = line.rsplit(",", 2)
            if len(parts) == 3 and parts[1]:
                recorded[parts[0]] = (parts[1].split("=", 1)[1], int(parts[2]))
        for name in names:
            if name.endswith("RECORD"):
                continue
            data = zf.read(name)
            if name not in recorded:
                raise SystemExit(f"{path.name}: {name} absent from RECORD")
            digest, size = recorded[name]
            if digest != b64_digest(data) or size != len(data):
                raise SystemExit(f"{path.name}: RECORD mismatch for {name}")
        wheel_meta = zf.read(_single_member(names, ".dist-info/WHEEL", path.name)).decode()
    file_tags = {f"{python_tag}-{abi_tag}-{tag}" for tag in platform_tags}
    meta_tags = {ln.split(":", 1)[1].strip()
                 for ln in wheel_meta.splitlines() if ln.startswith("Tag:")}
    if file_tags != meta_tags:
        raise SystemExit(f"{path.name}: WHEEL tags {sorted(meta_tags)} != filename tags {sorted(file_tags)}")
    native_label = ("ELF " + expected_target.split("-", 1)[1]) if expected_target.startswith("linux-") else "Mach-O"
    print(f"[verify] {path.name}: RECORD ok, mode 0755, {native_label}, "
          f"{len(names)} members, tags {sorted(platform_tags)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="build ztest platform wheels from prebuilt static binaries")
    ap.add_argument("--dist", default=str(REPO_ROOT / "dist"), help="directory holding binaries/tarballs; wheels are written here")
    ap.add_argument("--version", default=os.environ.get("ZTEST_VERSION", DEFAULT_VERSION))
    ap.add_argument("--target", "--arch", dest="targets", action="append", choices=sorted(PLATFORM_TAGS | LEGACY_ARCH_TARGETS), help="restrict to one OS/architecture target (repeatable)")
    ap.add_argument("--verify", action="store_true", help="re-verify wheels after writing")
    ap.add_argument("--sums", default="SHA256SUMS", help="file name for the checksum manifest inside --dist ('' disables)")
    args = ap.parse_args()

    dist_dir = Path(args.dist)
    if not dist_dir.is_dir():
        raise SystemExit(f"--dist {dist_dir} is not a directory")
    version = args.version
    readme = REPO_ROOT / "README.md"
    long_description = readme.read_text(encoding="utf-8") if readme.is_file() else "ztest"

    targets = args.targets or list(PLATFORM_TAGS)
    built = [build_wheel(dist_dir, target, version, long_description) for target in targets]

    if args.sums:
        sums_path = dist_dir / args.sums
        existing: dict[str, str] = {}
        if sums_path.is_file():
            for line in sums_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    digest, _, name = line.partition("  ")
                    existing[name.strip()] = digest.strip()
        for whl in built:
            existing[whl.name] = hashlib.sha256(whl.read_bytes()).hexdigest()
        sums_path.write_text("".join(f"{d}  {n}\n" for n, d in sorted(existing.items(), key=lambda kv: kv[0])),
                             encoding="utf-8")
        print(f"[sums] {sums_path} ({len(existing)} entries)")

    if args.verify:
        for whl in built:
            verify_wheel(whl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
