#!/usr/bin/env python3
"""Deterministic bundler and freshness tests for the generated shim."""
from __future__ import annotations

import ast
import importlib.util
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bundle_shim  # noqa: E402


class BundleShimTests(unittest.TestCase):
    def test_real_bundle_is_fresh_and_compiles(self) -> None:
        self.assertEqual(bundle_shim.generate(), bundle_shim.BUNDLE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            py_compile.compile(str(bundle_shim.BUNDLE), cfile=str(Path(tmp) / "shim.pyc"), doraise=True)

    def test_manifest_is_exact_and_ordered(self) -> None:
        self.assertEqual(len(bundle_shim.MODULE_ORDER), len(set(bundle_shim.MODULE_ORDER)))
        self.assertEqual(bundle_shim.MODULE_ORDER, sorted(bundle_shim.MODULE_ORDER))
        self.assertEqual(
            {path.stem for path in bundle_shim.MODULES_DIR.glob("[0-9][0-9]_*.py")},
            set(bundle_shim.MODULE_ORDER),
        )

    def test_bundle_and_sections_have_identical_ast(self) -> None:
        payload = "".join(
            "".join(bundle_shim.module_path(name).read_text(encoding="utf-8").splitlines(keepends=True)[5:])
            for name in bundle_shim.MODULE_ORDER
        )
        self.assertEqual(ast.dump(ast.parse(payload)), ast.dump(ast.parse(bundle_shim.generate())))


class ProjectDetectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = bundle_shim.MODULES_DIR / "_project_detect.py"
        spec = importlib.util.spec_from_file_location("_project_detect_test", path)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_project(self, name: str, package: str) -> None:
        (self.root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
        package_dir = self.root / "src" / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")

    def test_distribution_name_is_normalized_and_src_layout_found(self) -> None:
        self.write_project("Pygments-Extras", "pygments_extras")
        self.assertEqual(self.module.detect_project_package(self.root), "pygments_extras")

    def test_src_layout_has_precedence_and_missing_package_fails_soft(self) -> None:
        self.write_project("demo", "demo")
        (self.root / "demo.py").write_text("", encoding="utf-8")
        self.assertEqual(self.module.detect_project_package(self.root), "demo")
        (self.root / "src" / "demo" / "__init__.py").unlink()
        (self.root / "demo.py").unlink()
        self.assertIsNone(self.module.detect_project_package(self.root))

    def test_oversized_and_malformed_metadata_fail_soft(self) -> None:
        (self.root / "pyproject.toml").write_bytes(b"x" * (self.module.MAX_CONFIG_BYTES + 1))
        self.assertIsNone(self.module.detect_project_package(self.root))
        (self.root / "pyproject.toml").write_text("[project", encoding="utf-8")
        self.assertIsNone(self.module.detect_project_package(self.root))


if __name__ == "__main__":
    unittest.main()
