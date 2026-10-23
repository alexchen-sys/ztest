# Contributing

Thanks for considering a contribution. This document describes the development
loop and the bar a change has to clear.

## Setup

You need Zig 0.17.0 and Python 3.11 or newer. The behavioral gate runs under
stock pytest 9, so install it too:

```sh
git clone https://github.com/alexchen-sys/ztest.git
cd ztest
zig build
pip install "pytest>=9,<10"
```

The binary lands at `zig-out/bin/ztest`.

## Development loop

```sh
zig build test                                                        # native Zig suite
ZTEST_BIN=zig-out/bin/ztest python3 -m pytest tests/test_oss_regressions.py -q
```

The native suite covers the supervisor: framing, peer authentication, process
containment, CLI parsing. The Python gate runs the real binary against fixture
trees under `tests/fixtures/`, one tree per compatibility bug that was confirmed
against a real open-source suite.

## Reporting bugs

Open an issue with the smallest test file that reproduces the behavior, the
`ztest --version` output, and what pytest does on the same file. A fixture that
fails under ztest and passes under pytest is the most useful bug report this
project can receive.

## Submitting changes

- Bug fixes land test-first: add a fixture under `tests/fixtures/` that fails,
  then fix the shim or the supervisor until it passes. The gate above must stay
  green, and so must `zig build test`.
- If your change adds or removes a guard tagged with an `Invariants:` key, run
  `python3 scripts/check_docs.py`; CI runs it too.
- Keep the compatibility claims in `README.md` accurate: if a change widens or
  narrows the supported pytest surface, update the README in the same commit.
- Code comments and commit messages are written in English.
- Do not bump the version in released artifacts; releases are cut by the
  maintainer.

## Style

Zig code follows `zig fmt`. Python shim code targets CPython 3.11+ with no
third-party dependencies — the shim must stay embeddable and importable from a
frozen binary, so the standard library only.
