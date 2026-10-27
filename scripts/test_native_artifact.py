#!/usr/bin/env python3
"""Tests for cross-platform release artifact inspection (no native macOS needed)."""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_artifact as na  # noqa: E402


def elf(machine: int) -> bytes:
    data = bytearray(64)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", data, 18, machine)
    return bytes(data)


def macho(cputype: int, *, byteorder: str = "<", filetype: int = 2) -> bytes:
    magic = b"\xcf\xfa\xed\xfe" if byteorder == "<" else b"\xfe\xed\xfa\xcf"
    return magic + struct.pack(byteorder + "7I", cputype, 3, filetype, 0, 0, 0, 0) + b"payload"


def fat(*slices: tuple[int, bytes]) -> bytes:
    count = len(slices)
    offset = 8 + count * 20
    header = bytearray(struct.pack(">II", na.FAT_MAGIC, count))
    body = bytearray()
    for cputype, data in slices:
        header += struct.pack(">IIIII", cputype, 3, offset, len(data), 0)
        body += data
        offset += len(data)
    return bytes(header + body)


class NativeArtifactTests(unittest.TestCase):
    def test_elf_and_thin_macho(self) -> None:
        self.assertEqual(na.inspect_image(elf(na.EM_X86_64)), ("elf", "x86_64"))
        self.assertEqual(na.inspect_image(elf(na.EM_AARCH64)), ("elf", "aarch64"))
        self.assertEqual(na.inspect_image(macho(na.CPU_TYPE_X86_64)), ("macho", "x86_64"))
        self.assertEqual(na.inspect_image(macho(na.CPU_TYPE_ARM64)), ("macho", "arm64"))

    def test_bad_headers_fail_with_context(self) -> None:
        for data in (b"", b"\xcf\xfa\xed\xfe", b"not a binary", macho(7), macho(na.CPU_TYPE_ARM64, filetype=6)):
            with self.subTest(data=data), self.assertRaisesRegex(na.ArtifactError, "fixture"):
                na.inspect_image(data, "fixture")
        with self.assertRaisesRegex(na.ArtifactError, "big-endian"):
            na.inspect_image(macho(na.CPU_TYPE_ARM64, byteorder=">"))

    def test_fat_slices_and_boundaries(self) -> None:
        image = fat((na.CPU_TYPE_X86_64, macho(na.CPU_TYPE_X86_64)), (na.CPU_TYPE_ARM64, macho(na.CPU_TYPE_ARM64)))
        self.assertEqual(na.inspect_image(image), ("fat", "universal2"))
        with self.assertRaisesRegex(na.ArtifactError, "truncated"):
            na.inspect_image(image[:-2])
        with self.assertRaisesRegex(na.ArtifactError, "truncated"):
            na.inspect_image(image[:15])
        with self.assertRaisesRegex(na.ArtifactError, "contradicts"):
            na.inspect_image(fat((na.CPU_TYPE_X86_64, macho(na.CPU_TYPE_ARM64))))

    def test_release_target_requires_exact_architecture(self) -> None:
        universal = fat((na.CPU_TYPE_X86_64, macho(na.CPU_TYPE_X86_64)), (na.CPU_TYPE_ARM64, macho(na.CPU_TYPE_ARM64)))
        expected = {
            "linux-x86_64": elf(na.EM_X86_64), "linux-aarch64": elf(na.EM_AARCH64),
            "macos-x86_64": macho(na.CPU_TYPE_X86_64), "macos-arm64": macho(na.CPU_TYPE_ARM64),
            "macos-universal": universal,
        }
        for target, data in expected.items():
            self.assertEqual(na.check_release(data, target), target)
        for target, wrong in (
            ("linux-x86_64", elf(na.EM_AARCH64)),
            ("linux-aarch64", universal),
            ("macos-x86_64", macho(na.CPU_TYPE_ARM64)),
            ("macos-arm64", universal),
        ):
            with self.subTest(target=target), self.assertRaises(na.ArtifactError):
                na.check_release(wrong, target)
        with self.assertRaisesRegex(na.ArtifactError, "universal2"):
            na.check_release(macho(na.CPU_TYPE_ARM64), "macos-universal")

    def test_darwin_native_checks_require_working_tools(self) -> None:
        with mock.patch.object(na.shutil, "which", return_value=None):
            with self.assertRaisesRegex(na.ArtifactError, "codesign"):
                na.check_darwin_native(Path("ztest"), codesign=True)
            with self.assertRaisesRegex(na.ArtifactError, "otool"):
                na.check_darwin_native(Path("ztest"), frameworks=True)
        calls = []

        def run(argv: list[str], **kwargs: object) -> object:
            calls.append(argv)
            text = "Signature=adhoc\n" if argv[0] == "codesign" and "-dvvv" in argv else "/tmp/ztest:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
            return type("Result", (), {"returncode": 0, "stdout": text, "stderr": ""})()

        with mock.patch.object(na.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), mock.patch.object(na.subprocess, "run", side_effect=run):
            na.check_darwin_native(Path("ztest"), codesign=True, frameworks=True)
        self.assertTrue(any(argv[:2] == ["codesign", "--verify"] for argv in calls))

    def test_darwin_rejects_missing_signature_and_non_system_libraries(self) -> None:
        def result(stdout: str, rc: int = 0) -> object:
            return type("Result", (), {"returncode": rc, "stdout": stdout, "stderr": stdout})()
        with mock.patch.object(na.shutil, "which", return_value="/usr/bin/tool"), mock.patch.object(na.subprocess, "run", return_value=result("Signature=adhoc\n")):
            with self.assertRaisesRegex(na.ArtifactError, "otool"):
                na.check_darwin_native(Path("ztest"), frameworks=True)
        with mock.patch.object(na.shutil, "which", return_value="/usr/bin/tool"), mock.patch.object(na.subprocess, "run", return_value=result("ztest:\n\t/opt/homebrew/lib/libbad.dylib\n")):
            with self.assertRaisesRegex(na.ArtifactError, "libbad"):
                na.check_darwin_native(Path("ztest"), frameworks=True)
        with mock.patch.object(na.shutil, "which", return_value="/usr/bin/tool"), mock.patch.object(na.subprocess, "run", return_value=result("Signature=foo\n")):
            with self.assertRaisesRegex(na.ArtifactError, "adhoc"):
                na.check_darwin_native(Path("ztest"), codesign=True)

    def test_darwin_frameworks_skips_fat_architecture_headers(self) -> None:
        # `otool -L` on a universal2 (fat) binary prints one header line per slice:
        #   universal/ztest (architecture x86_64):
        #       /usr/lib/libSystem.B.dylib (compatibility version 1.0.0)
        # The gate must skip those headers, not flag the binary path itself.
        fat_listing = (
            "universal/ztest (architecture x86_64):\n"
            "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1345.100.2)\n"
            "universal/ztest (architecture arm64):\n"
            "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1345.100.2)\n"
        )
        mocked = type("Result", (), {"returncode": 0, "stdout": fat_listing, "stderr": ""})()
        with mock.patch.object(na.shutil, "which", return_value="/usr/bin/otool"), mock.patch.object(na.subprocess, "run", return_value=mocked):
            na.check_darwin_native(Path("universal/ztest"), frameworks=True)
        bad_listing = fat_listing + "\t/opt/homebrew/lib/libbad.dylib (compatibility version 1.0.0)\n"
        mocked_bad = type("Result", (), {"returncode": 0, "stdout": bad_listing, "stderr": ""})()
        with mock.patch.object(na.shutil, "which", return_value="/usr/bin/otool"), mock.patch.object(na.subprocess, "run", return_value=mocked_bad):
            with self.assertRaisesRegex(na.ArtifactError, "libbad"):
                na.check_darwin_native(Path("universal/ztest"), frameworks=True)


if __name__ == "__main__":
    unittest.main()
