"""Conservative project-package detection for the ztest zygote.

Distribution metadata and import names are separate namespaces. Names are
resolved to importable top-level packages in the project tree; nothing here
imports project code. The zygote alone performs the best-effort import.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

MAX_CONFIG_BYTES = 256 * 1024
MAX_ANCESTOR_HOPS = 4


def _read_name(pyproject: Path) -> str | None:
    try:
        with pyproject.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return None
        document = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        return None
    for table in (document.get("project"), document.get("tool", {}).get("poetry")):
        if isinstance(table, dict):
            name = table.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _import_roots(root: Path) -> list[Path]:
    """Prefer src layout, then the checkout root, without following outside it."""
    return [root / "src", root] if (root / "src").is_dir() else [root]


def _package_for_distribution(root: Path, distribution: str) -> str | None:
    """Find the unique importable top-level package matching normalized metadata."""
    normalized = distribution.replace("-", "_").replace(".", "_").casefold()
    matches: set[str] = set()
    try:
        for base in _import_roots(root):
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                if not child.name.isidentifier():
                    continue
                if child.name.casefold() != normalized:
                    continue
                if (child / "__init__.py").is_file():
                    matches.add(child.name)
            # Single-module distributions are importable too.
            candidate = base / f"{distribution.replace('-', '_').replace('.', '_')}.py"
            if candidate.is_file() and candidate.stem.isidentifier():
                matches.add(candidate.stem)
    except OSError:
        return None
    return matches.pop() if len(matches) == 1 else None


def _top_level_package(root: Path) -> str | None:
    """Fallback for projects without metadata: one plausible top-level package."""
    matches: set[str] = set()
    try:
        for base in _import_roots(root):
            for child in base.iterdir():
                if child.name in {"test", "tests", "docs", "examples", "benchmarks", "scripts", "build", "dist"}:
                    continue
                if child.is_dir() and (child / "__init__.py").is_file() and child.name.isidentifier():
                    matches.add(child.name)
    except OSError:
        return None
    return matches.pop() if len(matches) == 1 else None


def detect_project_package(start: str | os.PathLike[str] | None = None, *, max_hops: int = MAX_ANCESTOR_HOPS) -> str | None:
    """Resolve an importable project package, with bounded, side-effect-free discovery."""
    try:
        current = Path(start or os.getcwd()).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    fallback: str | None = None
    for _ in range(max(0, max_hops) + 1):
        name = _read_name(current / "pyproject.toml")
        if name is not None:
            return _package_for_distribution(current, name)
        if fallback is None:
            fallback = _top_level_package(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return fallback


def import_project_package(name: str | None) -> bool:
    """Best-effort import; only validated dotted identifiers reach the import system."""
    if not name or not all(part.isidentifier() for part in name.split(".")):
        return False
    try:
        __import__(name)
    except Exception:
        return False
    return True


__all__ = ["detect_project_package", "import_project_package"]
