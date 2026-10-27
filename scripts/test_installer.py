#!/usr/bin/env python3
"""Offline installer tests: no network, no real ztest, no privileged writes."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts/install.sh"


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_obj.cleanup)
        self.tmp = Path(self.tmp_obj.name)
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        for name in ("uname", "mktemp", "tar", "cp", "chmod", "mkdir", "install", "mv", "rm", "id", "grep", "sed", "cut", "find", "awk", "gzip"):
            target = Path("/usr/bin") / name
            if not target.exists():
                target = Path("/bin") / name
            if target.exists():
                (self.tools / name).symlink_to(target)
        # Real uname is retained for Linux tests. Darwin tests replace this link.
        self.assets = self.tmp / "dist"
        self.assets.mkdir()

    def tool(self, name: str, body: str) -> None:
        path = self.tools / name
        if path.is_symlink() or path.exists():
            path.unlink()
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def asset(self, version: str = "0.6.0", os_name: str = "linux", arch: str = "x86_64", text: str | None = None) -> str:
        name = f"ztest-v{version}-{os_name}-{arch}.tar.gz"
        binary = self.tmp / "ztest-fixture"
        binary.write_text(f"#!/bin/sh\nif [ \"$1\" = --version ]; then echo '{text or ('ztest ' + version + ' (fixture)')}'; else exit 0; fi\n", encoding="utf-8")
        with tarfile.open(self.assets / name, "w:gz") as tf:
            tf.add(binary, arcname="ztest")
        digest = hashlib.sha256((self.assets / name).read_bytes()).hexdigest()
        (self.assets / "SHA256SUMS").write_text(f"{digest}  {name}\n", encoding="utf-8")
        return name

    def run_install(self, *args: str, sha: str = "sha256sum") -> subprocess.CompletedProcess[str]:
        for old_tool in ("sha256sum", "shasum", "missing-sha-tool"):
            path = self.tools / old_tool
            if path.exists() or path.is_symlink():
                path.unlink()
        self.tool(sha, 'if [ "$1" = "-a" ]; then shift 2; fi\nexec /usr/bin/sha256sum "$@"\n')
        env = os.environ.copy()
        env["PATH"] = str(self.tools)
        env["HOME"] = str(self.tmp / "home")
        env["ZTEST_NO_NETWORK"] = "1"
        return subprocess.run(["/bin/sh", str(INSTALLER), "--from-dir", str(self.assets), *args], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    def test_linux_happy_path_and_version(self) -> None:
        self.asset()
        dest = self.tmp / "bin"
        result = self.run_install("--install-dir", str(dest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.check_output([dest / "ztest", "--version"], text=True).strip(), "ztest 0.6.0 (fixture)")

    def test_darwin_arm64_uses_macos_asset_and_shasum(self) -> None:
        self.tool("uname", 'case "$1" in -s) echo Darwin;; -m) echo arm64;; *) exit 2;; esac\n')
        self.asset(os_name="macos", arch="arm64")
        result = self.run_install("--install-dir", str(self.tmp / "bin"), sha="shasum")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("macos-arm64", result.stderr)

    def test_checksum_mismatch_is_rejected(self) -> None:
        name = self.asset()
        (self.assets / "SHA256SUMS").write_text(f"{'0' * 64}  {name}\n", encoding="utf-8")
        result = self.run_install("--install-dir", str(self.tmp / "bin"))
        self.assertEqual(result.returncode, 4)
        self.assertIn("checksum mismatch", result.stderr)

    def test_manifest_must_contain_selected_asset(self) -> None:
        self.asset()
        (self.assets / "SHA256SUMS").write_text("", encoding="utf-8")
        result = self.run_install("--install-dir", str(self.tmp / "bin"))
        self.assertEqual(result.returncode, 4)
        self.assertIn("not listed", result.stderr)

    def test_missing_manifest_and_sha_tool_are_hard_errors(self) -> None:
        self.asset()
        (self.assets / "SHA256SUMS").unlink()
        result = self.run_install("--install-dir", str(self.tmp / "bin"))
        self.assertEqual(result.returncode, 4)
        self.assertIn("SHA256SUMS missing", result.stderr)
        self.asset()
        result = self.run_install("--install-dir", str(self.tmp / "bin"), sha="missing-sha-tool")
        self.assertEqual(result.returncode, 4)
        self.assertIn("sha256sum or shasum", result.stderr)

    def test_staged_version_mismatch_preserves_existing_binary(self) -> None:
        self.asset(text="ztest 0.5.1 (wrong)")
        dest = self.tmp / "bin"
        dest.mkdir()
        old = dest / "ztest"
        old.write_text("#!/bin/sh\necho 'ztest 0.6.0 (existing)'\n", encoding="utf-8")
        old.chmod(0o755)
        result = self.run_install("--install-dir", str(dest))
        self.assertEqual(result.returncode, 5)
        self.assertIn("nothing was installed", result.stderr)
        self.assertIn("existing", subprocess.check_output([old, "--version"], text=True))

    def test_unsupported_os_and_no_network_escape(self) -> None:
        self.tool("uname", 'case "$1" in -s) echo FreeBSD;; -m) echo amd64;; *) exit 2;; esac\n')
        result = self.run_install("--install-dir", str(self.tmp / "bin"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported OS", result.stderr)
        # No curl/wget in the test PATH: an accidental network path must fail before download.
        self.tool("uname", 'case "$1" in -s) echo Linux;; -m) echo x86_64;; *) exit 2;; esac\n')
        env = os.environ.copy()
        env.update(PATH=str(self.tools), HOME=str(self.tmp), ZTEST_NO_NETWORK="1")
        result = subprocess.run(["/bin/sh", str(INSTALLER), "--install-dir", str(self.tmp / "bin")], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("offline", result.stderr)


if __name__ == "__main__":
    unittest.main()
