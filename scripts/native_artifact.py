#!/usr/bin/env python3
"""Architecture and host-tool validation for Linux and macOS release binaries.

The byte-level gates work on every build host. Signature/framework checks use
Apple's tools and therefore belong on a Darwin runner, never in a cross-build.
"""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
ELFDATA2LSB = 1
E_MACHINE_OFFSET = 18
EM_X86_64 = 62
EM_AARCH64 = 183
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe": "<",  # MH_MAGIC_64, little endian
    b"\xfe\xed\xfa\xcf": ">",  # MH_CIGAM_64, big endian
}
FAT_MAGIC = 0xCAFEBABE
CPU_TYPE_X86_64 = 0x01000007
CPU_TYPE_ARM64 = 0x0100000C
CPUTYPE_NAMES = {CPU_TYPE_X86_64: "x86_64", CPU_TYPE_ARM64: "arm64"}
ELF_NAMES = {EM_X86_64: "x86_64", EM_AARCH64: "aarch64"}
TARGETS = ("linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-arm64", "macos-universal")


class ArtifactError(ValueError):
    """A release payload is malformed or contradicts its target."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise ArtifactError(message)


def inspect_image(data: bytes, origin: str = "payload") -> tuple[str, str]:
    """Return (image kind, architecture): elf, macho, or fat/universal2."""
    if len(data) < 4:
        raise ArtifactError(f"{origin}: truncated binary header ({len(data)} bytes)")
    if data[:4] == ELF_MAGIC:
        _need(len(data) >= E_MACHINE_OFFSET + 2, f"{origin}: truncated ELF header")
        _need(data[4] == ELFCLASS64, f"{origin}: ELF is not 64-bit")
        _need(data[5] == ELFDATA2LSB, f"{origin}: ELF is not little-endian")
        machine = int.from_bytes(data[E_MACHINE_OFFSET:E_MACHINE_OFFSET + 2], "little")
        _need(machine in ELF_NAMES, f"{origin}: unsupported ELF e_machine={machine}")
        return "elf", ELF_NAMES[machine]
    if data[:4] in MACHO_MAGICS:
        order = MACHO_MAGICS[data[:4]]
        _need(order == "<", f"{origin}: big-endian Mach-O is unsupported")
        _need(len(data) >= 32, f"{origin}: truncated Mach-O header ({len(data)} bytes, need 32)")
        cputype, _cpusubtype, filetype = struct.unpack_from(order + "3I", data, 4)
        _need(cputype in CPUTYPE_NAMES, f"{origin}: unsupported Mach-O cputype={cputype:#x}")
        _need(filetype == 2, f"{origin}: Mach-O filetype={filetype} is not MH_EXECUTE (2)")
        return "macho", CPUTYPE_NAMES[cputype]
    if data[:4] == struct.pack(">I", FAT_MAGIC):
        _need(len(data) >= 8, f"{origin}: truncated FAT header")
        count = struct.unpack_from(">I", data, 4)[0]
        _need(1 <= count <= 16, f"{origin}: invalid FAT slice count {count}")
        _need(len(data) >= 8 + count * 20, f"{origin}: truncated FAT architecture table")
        arches = []
        for index in range(count):
            cputype, _subtype, offset, size, _align = struct.unpack_from(">5I", data, 8 + index * 20)
            _need(cputype in CPUTYPE_NAMES, f"{origin}: unsupported FAT cputype={cputype:#x}")
            _need(offset >= 8 + count * 20 and offset + size <= len(data), f"{origin}: truncated FAT slice {index}")
            kind, arch = inspect_image(data[offset:offset + size], f"{origin}:slice[{index}]")
            _need(kind == "macho" and arch == CPUTYPE_NAMES[cputype], f"{origin}: FAT slice {index} contradicts its cputype")
            arches.append(arch)
        _need(len(set(arches)) == count, f"{origin}: duplicate FAT architectures")
        _need(set(arches) == {"x86_64", "arm64"} and count == 2, f"{origin}: FAT is not universal2 (slices {arches})")
        return "fat", "universal2"
    raise ArtifactError(f"{origin}: unsupported binary magic {data[:4]!r}")


def check_release(data: bytes, target: str, origin: str = "payload") -> str:
    """Fail closed on cross-architecture packaging and partial universal binaries."""
    kind, arch = inspect_image(data, origin)
    if target not in TARGETS:
        raise ArtifactError(f"unknown release target {target!r}; choose from {TARGETS}")
    expected_os, expected_arch = target.split("-", 1)
    if expected_os == "linux":
        _need(kind == "elf" and arch == expected_arch, f"{origin}: {target} requires ELF/{expected_arch}, got {kind}/{arch}")
    elif target == "macos-universal":
        _need(kind == "fat" and arch == "universal2", f"{origin}: macos-universal requires FAT/universal2, got {kind}/{arch}")
    elif expected_os == "macos":
        _need(kind == "macho" and arch == expected_arch, f"{origin}: {target} requires thin Mach-O/{expected_arch}, got {kind}/{arch}")
    else:
        raise ArtifactError(f"unsupported release target {target!r}")
    return target


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    _need(result.returncode == 0, f"{argv[0]} failed ({result.returncode}): {(result.stdout + result.stderr).strip()}")
    return result


def check_darwin_native(path: Path, *, codesign: bool = False, frameworks: bool = False) -> None:
    """Run explicitly requested Apple-host gates; missing tools are hard errors."""
    if codesign:
        _need(shutil.which("codesign") is not None, "codesign is required on this Darwin runner")
        _run(["codesign", "--verify", "--strict", "--verbose=4", str(path)])
        description = _run(["codesign", "-dvvv", str(path)])
        _need("Signature=adhoc" in description.stdout + description.stderr, f"{path}: codesign did not report adhoc signature")
    if frameworks:
        _need(shutil.which("otool") is not None, "otool is required on this Darwin runner")
        linked = _run(["otool", "-L", str(path)]).stdout.splitlines()[1:]
        _need(bool(linked), f"{path}: otool -L returned no linkage information")
        for line in linked:
            library = line.strip().split(" (", 1)[0]
            # otool -L on a fat (universal2) binary prints one header per slice:
            #   <path> (architecture x86_64):
            # These lines are not libraries; skip them instead of flagging the path itself.
            if " (architecture " in line and line.rstrip().endswith(":"):
                continue
            _need(library.startswith(("/usr/lib/", "/System/Library/Frameworks/")), f"{path}: non-system dependency {library}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--codesign", action="store_true", help="verify strict adhoc signature with Apple codesign")
    parser.add_argument("--frameworks", action="store_true", help="allow only system libraries reported by Apple otool")
    args = parser.parse_args()
    try:
        data = args.binary.read_bytes()
        kind, arch = inspect_image(data, str(args.binary))
        check_release(data, args.target, str(args.binary))
        check_darwin_native(args.binary, codesign=args.codesign, frameworks=args.frameworks)
    except (OSError, ArtifactError) as error:
        print(f"native artifact: FAIL: {error}", file=sys.stderr)
        return 1
    print(f"native artifact: {args.binary}: {kind}/{arch}, target {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
