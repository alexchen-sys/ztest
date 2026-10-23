#!/usr/bin/env python3
"""Cross-check between the code and docs/INVARIANTS.md.

Guards that are non-obvious carry a comment ending in `Invariants: KEY, KEY`.
Every key cited from the code must exist in docs/INVARIANTS.md, and every
relative markdown link under docs/ must resolve. Exits non-zero on a violation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "INVARIANTS.md"

TAG_RE = re.compile(r"Invariants:\s*([A-Z0-9][A-Z0-9_+\-*, ]*?)[.\s]*$")
ENTRY_RE = re.compile(r"^-\s+`([A-Z0-9][A-Z0-9_+\-]*)`")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def registry_keys() -> set[str]:
    keys: set[str] = set()
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        m = ENTRY_RE.match(line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def cited_keys() -> dict[str, list[str]]:
    cited: dict[str, list[str]] = {}
    for path in sorted(ROOT.glob("src/**/*")):
        if path.suffix not in (".py", ".zig") or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not (stripped.startswith("#") or stripped.startswith("//")):
                continue
            m = TAG_RE.search(stripped)
            if not m:
                continue
            for chunk in m.group(1).split(","):
                for key in chunk.split("+"):
                    key = key.strip().rstrip(".")
                    if key:
                        cited.setdefault(key, []).append(f"{path.relative_to(ROOT)}:{lineno}")
    return cited


def broken_doc_links() -> list[str]:
    broken: list[str] = []
    for md in sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md", ROOT / "CONTRIBUTING.md"]:
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            for target in LINK_RE.findall(line):
                if "://" in target or target.startswith("#") or target.startswith("mailto:"):
                    continue
                path_part = target.split("#")[0]
                if not path_part:
                    continue
                if not (md.parent / path_part).resolve().exists():
                    broken.append(f"{md.relative_to(ROOT)}:{lineno} -> {target}")
    return broken


def main() -> int:
    keys = registry_keys()
    cited = cited_keys()
    missing = {k: v for k, v in cited.items() if k not in keys}
    links = broken_doc_links()
    ok = True
    if missing:
        print("keys cited from code but missing from docs/INVARIANTS.md:")
        for key, sites in sorted(missing.items()):
            print(f"  {key}  ({len(sites)} site(s), first: {sites[0]})")
        ok = False
    else:
        print(f"invariant keys: {len(cited)} cited from code, all present in the registry "
              f"({len(keys)} entries).")
    if links:
        print("broken relative links in docs:")
        for item in links:
            print(f"  {item}")
        ok = False
    else:
        print("relative links in docs: all resolve.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
