#!/bin/sh
# ztest installer — POSIX sh, no dependencies beyond curl/wget + tar + sha256sum.
#
#   curl -LsSf https://github.com/alexchen-sys/ztest/releases/latest/download/install.sh | sh
#   curl -LsSf https://raw.githubusercontent.com/alexchen-sys/ztest/main/scripts/install.sh | sh
#   sh install.sh --version 0.5.1
#   sh install.sh --system                 # /usr/local/bin instead of ~/.local/bin
#   sh install.sh --from-dir ./dist        # offline: use a local release directory
#
# The first URL is a real release asset: the CI `dist/` that becomes the
# published release contains `install.sh` next to the tarballs, wheels and
# SHA256SUMS. The raw URL tracks `main` and works before a release exists.
#
# Environment:
#   ZTEST_VERSION       release to install (default: 0.5.1)
#   ZTEST_INSTALL_DIR   destination directory (default: ~/.local/bin, or /usr/local/bin with --system)
#   ZTEST_DOWNLOAD_BASE base URL for release assets (default: the GitHub release for $ZTEST_VERSION)
#   ZTEST_NO_VERIFY     set to 1 to skip SHA256SUMS verification (discouraged)
#
# Exit codes: 0 ok, 1 usage/environment error, 2 unsupported platform,
#             3 download failed, 4 checksum mismatch, 5 install failed.
set -eu

SCRIPT_VERSION="0.5.1"
DEFAULT_VERSION="0.5.1"
REPO="alexchen-sys/ztest"
BINARY_NAME="ztest"

usage() {
    cat <<'EOF'
Install ztest — a pytest-compatible test runner with a native Zig supervisor.

Usage: install.sh [OPTIONS]

Options:
  --version <VERSION>   release to install (default: 0.5.1)
  --system             install into /usr/local/bin (needs root or sudo)
  --install-dir <DIR>  install into DIR (overrides ZTEST_INSTALL_DIR)
  --from-dir <DIR>     install from a local release directory (offline)
  --force              replace a file at the target path that is not a ztest binary
  --no-verify          skip SHA256SUMS verification
  --print-version      print the installer version and the default release
  -h, --help           show this help

Environment: ZTEST_VERSION, ZTEST_INSTALL_DIR, ZTEST_DOWNLOAD_BASE, ZTEST_NO_VERIFY
EOF
}

say() { printf '%s\n' "$*" >&2; }
die() { say "error: $1"; exit "${2:-1}"; }

VERSION="${ZTEST_VERSION:-$DEFAULT_VERSION}"
MODE="user"
INSTALL_DIR_ARG=""
FROM_DIR=""
VERIFY=1
FORCE=0
if [ "${ZTEST_NO_VERIFY:-0}" = "1" ]; then VERIFY=0; fi

while [ $# -gt 0 ]; do
    case "$1" in
        --version)
            [ $# -ge 2 ] || die "--version needs a value" 1
            VERSION="$2"; shift 2 ;;
        --version=*) VERSION="${1#*=}"; shift ;;
        --system) MODE="system"; shift ;;
        --install-dir)
            [ $# -ge 2 ] || die "--install-dir needs a value" 1
            INSTALL_DIR_ARG="$2"; shift 2 ;;
        --install-dir=*) INSTALL_DIR_ARG="${1#*=}"; shift ;;
        --from-dir)
            [ $# -ge 2 ] || die "--from-dir needs a value" 1
            FROM_DIR="$2"; shift 2 ;;
        --from-dir=*) FROM_DIR="${1#*=}"; shift ;;
        --force) FORCE=1; shift ;;
        --no-verify) VERIFY=0; shift ;;
        --print-version)
            say "ztest-install $SCRIPT_VERSION (default release $DEFAULT_VERSION)"
            exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" 1 ;;
    esac
done

# ---------------------------------------------------------------- platform ---
OS="$(uname -s)"
case "$OS" in
    Linux) ;;
    *) die "unsupported OS: $OS (ztest ships static Linux binaries only)" 2 ;;
esac

MACHINE="$(uname -m)"
case "$MACHINE" in
    x86_64|amd64) ARCH="x86_64" ;;
    aarch64|arm64) ARCH="aarch64" ;;
    *) die "unsupported architecture: $MACHINE (supported: x86_64, aarch64)" 2 ;;
esac

ASSET="${BINARY_NAME}-v${VERSION}-linux-${ARCH}.tar.gz"

# -------------------------------------------------------------- destination ---
if [ -n "$INSTALL_DIR_ARG" ]; then
    DEST="$INSTALL_DIR_ARG"
elif [ -n "${ZTEST_INSTALL_DIR:-}" ]; then
    DEST="$ZTEST_INSTALL_DIR"
elif [ "$MODE" = "system" ]; then
    DEST="/usr/local/bin"
else
    DEST="${HOME}/.local/bin"
fi

NEW_DEST="$DEST/${BINARY_NAME}.new"

SUDO=""
# Escalate only when the destination directory itself exists and is not
# writable. A missing ~/.local/bin is creatable by the user; using sudo for
# mkdir would leave a root-owned ~/.local behind.
if [ -d "$DEST" ] && [ ! -w "$DEST" ] && [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        say "warning: $DEST is not writable and sudo is unavailable"
    fi
fi

# ------------------------------------------------------------------ fetch ---
TMPDIR_INSTALL="$(mktemp -d "${TMPDIR:-/tmp}/ztest-install.XXXXXX")" || die "mktemp failed" 1
cleanup() {
    rm -rf "$TMPDIR_INSTALL"
    # A failed install/mv must not leave a root-owned `.new` behind.
    if [ -e "$NEW_DEST" ]; then $SUDO rm -f "$NEW_DEST" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

if [ -n "$FROM_DIR" ]; then
    [ -f "$FROM_DIR/$ASSET" ] || die "local release asset not found: $FROM_DIR/$ASSET" 3
    cp "$FROM_DIR/$ASSET" "$TMPDIR_INSTALL/$ASSET"
    if [ -f "$FROM_DIR/SHA256SUMS" ]; then cp "$FROM_DIR/SHA256SUMS" "$TMPDIR_INSTALL/SHA256SUMS"; fi
else
    BASE="${ZTEST_DOWNLOAD_BASE:-https://github.com/${REPO}/releases/download/v${VERSION}}"
    BASE="${BASE%/}"
    fetch() {
        # $1 = url, $2 = output path
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$2" "$1"
        elif command -v wget >/dev/null 2>&1; then
            wget -q -O "$2" "$1"
        else
            die "need curl or wget to download ztest" 3
        fi
    }
    say "downloading ${ASSET} (v${VERSION}, ${ARCH})"
    fetch "$BASE/$ASSET" "$TMPDIR_INSTALL/$ASSET" || die "download failed: $BASE/$ASSET" 3
    if [ "$VERIFY" = "1" ]; then
        fetch "$BASE/SHA256SUMS" "$TMPDIR_INSTALL/SHA256SUMS" \
            || die "download failed: $BASE/SHA256SUMS (use --no-verify to skip)" 3
    fi
fi

# ----------------------------------------------------------------- verify ---
if [ "$VERIFY" = "1" ]; then
    [ -f "$TMPDIR_INSTALL/SHA256SUMS" ] || die "SHA256SUMS missing (use --no-verify to skip)" 4
    # Fixed-field match, not a regex: $ASSET embeds $VERSION, which comes from the
    # user's environment. `grep -E` would interpret `.`/`+`/`[` in it as regex
    # syntax and could silently match a different asset (or nothing).
    EXPECTED="$(awk -v want="$ASSET" '
        { name = $2; sub(/^\*/, "", name); if (name == want) { print $1; exit } }
    ' "$TMPDIR_INSTALL/SHA256SUMS")"
    [ -n "$EXPECTED" ] || die "$ASSET is not listed in SHA256SUMS" 4
    if command -v sha256sum >/dev/null 2>&1; then
        ACTUAL="$(sha256sum "$TMPDIR_INSTALL/$ASSET" | cut -d' ' -f1)"
    elif command -v shasum >/dev/null 2>&1; then
        ACTUAL="$(shasum -a 256 "$TMPDIR_INSTALL/$ASSET" | cut -d' ' -f1)"
    else
        die "need sha256sum or shasum to verify the download" 4
    fi
    [ "$EXPECTED" = "$ACTUAL" ] || die "checksum mismatch for $ASSET (expected $EXPECTED, got $ACTUAL)" 4
    say "checksum verified: $ASSET"
fi

# ---------------------------------------------------------------- install ---
mkdir -p "$TMPDIR_INSTALL/extract"
tar -xzf "$TMPDIR_INSTALL/$ASSET" -C "$TMPDIR_INSTALL/extract" || die "failed to extract $ASSET" 5
if [ -f "$TMPDIR_INSTALL/extract/$BINARY_NAME" ]; then
    NEW_BIN="$TMPDIR_INSTALL/extract/$BINARY_NAME"
else
    NEW_BIN="$(find "$TMPDIR_INSTALL/extract" -type f -name "$BINARY_NAME" | head -n1)"
    [ -n "$NEW_BIN" ] || die "no $BINARY_NAME inside $ASSET" 5
fi
chmod 755 "$NEW_BIN"

# A directory on the target path cannot be atomically replaced by a file; `mv`
# would move the binary *into* it and the script would report success.
if [ -d "$DEST/$BINARY_NAME" ]; then
    die "$DEST/$BINARY_NAME is a directory; move it away first" 5
fi

# Refuse to clobber anything that is not a ztest binary — a predictable,
# documented failure beats silently overwriting an unrelated tool. An existing
# ztest (any version) is upgraded in place; --force overrides the refusal.
if [ -e "$DEST/$BINARY_NAME" ]; then
    if "$DEST/$BINARY_NAME" --version 2>/dev/null | grep -q '^ztest '; then
        :
    elif [ "$FORCE" = "1" ]; then
        say "warning: replacing $DEST/$BINARY_NAME (not a ztest binary; --force given)"
    else
        die "$DEST/$BINARY_NAME exists and is not a ztest binary; refusing to overwrite (use --force)" 5
    fi
fi

$SUDO mkdir -p "$DEST" || die "could not create $DEST" 5
$SUDO install -m 0755 "$NEW_BIN" "$NEW_DEST" || die "could not write to $DEST" 5

# Pre-flight before the atomic replace: a payload that does not report the
# version we asked for must never displace a working installation.
STAGED_VERSION="$("$NEW_DEST" --version 2>&1 || true)"
case "$STAGED_VERSION" in
    "ztest ${VERSION} ("*) ;;
    *) die "the $ASSET binary did not report 'ztest ${VERSION} (...)' (got: ${STAGED_VERSION:-<no output>}); nothing was installed" 5 ;;
esac

$SUDO mv -f "$NEW_DEST" "$DEST/$BINARY_NAME" || die "could not replace $DEST/$BINARY_NAME" 5

# "Installed" is a verified fact, not an assumption: the file on the final path
# must be executable and report the version that was just installed.
[ -x "$DEST/$BINARY_NAME" ] || die "$DEST/$BINARY_NAME is not executable after install" 5
INSTALLED_VERSION="$("$DEST/$BINARY_NAME" --version 2>&1 || true)"
case "$INSTALLED_VERSION" in
    "ztest ${VERSION} ("*) ;;
    *) die "$DEST/$BINARY_NAME did not report 'ztest ${VERSION} (...)' after install (got: ${INSTALLED_VERSION:-<no output>})" 5 ;;
esac

say ""
say "installed ztest to $DEST/$BINARY_NAME"
printf '%s\n' "$INSTALLED_VERSION" | sed 's/^/  /' >&2

case ":${PATH}:" in
    *":$DEST:"*) say "ztest is already on PATH" ;;
    *)
        say ""
        say "add it to PATH:"
        say "  echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.profile   # or ~/.zshrc / ~/.bashrc"
        say "then restart the shell."
        ;;
esac
