# PyPI packaging for ztest

Release wheels are assembled by `scripts/build_wheels.py` from prebuilt
**static-musl Linux binaries** and **thin Mach-O Darwin binaries**. There is no
`pyproject.toml` in the repository root on purpose: ztest is a Zig program with
a Python-compatible CLI, not a Python package that compiles at install time.
The builder generates wheel metadata and verifies the payload format and
architecture before and after writing the archive.

## Why a native wheel instead of a pure-Python shim

A `py3-none-any` wheel would have to ship Python code that compiles or downloads
the binary at install time — exactly the fragility ztest exists to remove. Each
platform wheel carries the executable itself:

```
ztest_runner/__init__.py                       # __version__, binary_path()
ztest_runner-0.6.0.data/scripts/ztest          # native binary, mode 0755
ztest_runner-0.6.0.dist-info/{METADATA,WHEEL,RECORD}
```

`pip` installs `.data/scripts/*` into the environment's scripts directory, so
`pip install ztest_runner-*.whl` puts `ztest` in `<venv>/bin/ztest`. No
`console_scripts` wrapper starts Python before the native supervisor.

## Platform tags

The Linux binary is linked against musl and is fully static (`ldd` reports
"not a dynamic executable"), so it runs on glibc and musl distributions alike.
Darwin payloads are ordinary Mach-O executables using system libraries; they
are not labelled static or manylinux.

| wheel target | payload | platform tags |
| --- | --- | --- |
| `linux-x86_64` | ELF64 x86_64 | `manylinux_2_17_x86_64`, `manylinux2014_x86_64`, `musllinux_1_1_x86_64` |
| `linux-aarch64` | ELF64 aarch64 | `manylinux_2_17_aarch64`, `manylinux2014_aarch64`, `musllinux_1_1_aarch64` |
| `macos-x86_64` | thin Mach-O cputype x86_64 | `macosx_10_13_x86_64` |
| `macos-arm64` | thin Mach-O cputype arm64 | `macosx_11_0_arm64` |

Resulting filenames (version `0.6.0`):

```
ztest_runner-0.6.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64.whl
ztest_runner-0.6.0-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl
ztest_runner-0.6.0-py3-none-macosx_10_13_x86_64.whl
ztest_runner-0.6.0-py3-none-macosx_11_0_arm64.whl
```

The macOS tag is the oldest pip-accepted tag for that architecture, not a claim
about a newer SDK. `manylinux_2_17` is a compatibility floor: the static Linux
payload does not use glibc, so `auditwheel repair` is unnecessary and must not
be run on these wheels. Darwin uses `codesign`/`otool` checks on a real macOS
runner, not a Linux emulation.

## Building

With all four OS/architecture-bound tarballs in `dist/`:

```sh
python3 scripts/build_wheels.py --dist dist --verify --version 0.6.0
```

To build only the Linux pair or one target:

```sh
python3 scripts/build_wheels.py --dist dist --verify \
  --target linux-x86_64 --target linux-aarch64
python3 scripts/build_wheels.py --dist dist --verify --target macos-arm64
```

The legacy `--arch x86_64` and `--arch aarch64` spellings remain local Linux
aliases. A bare `dist/ztest` is rejected because it carries no architecture.

The builder:

1. reads each payload from `ztest-v<version>-<os>-<arch>.tar.gz` (or the
   explicit iteration names `ztest-<os>-<arch>`, `ztest-<arch>`, and
   `ztest-<version>-<os>-<arch>`);
2. checks ELF64 class, little-endian encoding and `e_machine` for Linux, or a
   32-byte Mach-O64 `MH_EXECUTE` header and exact cputype for Darwin;
3. writes deterministic zip entries with fixed timestamps, executable Unix
   mode bits and `create_system=3`;
4. emits `RECORD` digests and updates `dist/SHA256SUMS` without dropping
   entries owned by other targets;
5. with `--verify`, checks every digest, 0755 mode, WHEEL-vs-filename tags,
   duplicate/mixed target tags and payload format/architecture again.

`scripts/native_artifact.py` additionally validates FAT universal2 archives:
exactly one x86_64 and one arm64 slice, matching cputype/filetype and complete
slice bounds. It is used by Darwin release assembly; thin universal payloads
are not silently packed under an architecture-specific wheel tag.

METADATA declares `Requires-Python: >=3.11`, matching the runtime worker
interpreter requirement even though installation itself executes native code.

## Verifying an installed wheel

`packaging/pypi/smoke_test.sh <wheel>` creates a throwaway venv, installs the
wheel with `--no-index`, checks `ztest --version`, runs a two-passing/one-failing
suite (exit `1`, `2 passed, 1 failed`) and then a clean suite (exit `0`,
`2 passed, 0 failed`). CI runs this on the native Linux x86_64 and macOS arm64
targets. The other wheels still receive RECORD/mode/tag/payload checks; those
static checks are not described as native execution.

## Publishing (manual, operator-gated)

```sh
python3 scripts/build_wheels.py --dist dist --verify
python3 -m twine check dist/*.whl
python3 -m twine upload --repository pypi dist/*.whl
```

Names `ztest-runner` (distribution) and `ztest-py` (reserved fallback) are the
intended public identities. Nothing uploads automatically: publication needs
an explicit operator command and the GitHub release must exist first because
`scripts/install.sh` resolves assets from `releases/download/v<version>/`.

CI produces two release artifact sets: `ztest-dist-linux-v<version>` and
`ztest-dist-macos-v<version>`. Linux contains its two wheels and tarballs;
Darwin contains its two thin wheels, thin `x86_64`/`arm64` tarballs and a
`universal2` tarball. Each set has its own `SHA256SUMS` and `install.sh`; a
release operator merges them without overwriting either manifest. The
installer is not listed in `SHA256SUMS` (a bootstrap script cannot verify
itself); the manifest covers the binary archives.
