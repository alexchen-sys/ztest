#!/bin/sh
# smoke_test.sh <wheel> — install a ztest wheel into a throwaway venv and prove
# the installed binary actually runs a suite. Used by CI and by hand; both paths
# must stay identical, so all assertions live here and nowhere else.
set -eu

WHEEL="${1:-}"
[ -n "$WHEEL" ] || { echo "usage: $0 <path-to-wheel>" >&2; exit 1; }
[ -f "$WHEEL" ] || { echo "no such wheel: $WHEEL" >&2; exit 1; }

PYTHON="${PYTHON:-python3}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/ztest-wheel-smoke.XXXXXX")"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT INT TERM

echo "== wheel: $(basename "$WHEEL")"
echo "== python: $("$PYTHON" -V 2>&1)"
"$PYTHON" -m venv "$WORKDIR/venv"
VENV_PY="$WORKDIR/venv/bin/python"
VENV_BIN="$WORKDIR/venv/bin"

# No network, no build backend: a wheel must install from the file alone.
"$VENV_PY" -m pip install --no-index --disable-pip-version-check -q "$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"

[ -x "$VENV_BIN/ztest" ] || { echo "FAIL: $VENV_BIN/ztest missing or not executable" >&2; exit 1; }
echo "== installed: $(ls -l "$VENV_BIN/ztest" | awk '{print $1, $5" bytes"}')"
echo "== version: $("$VENV_BIN/ztest" --version)"

# Fixture: one file with two passing tests and one deliberate failure.
mkdir -p "$WORKDIR/suite"
cat > "$WORKDIR/suite/test_demo.py" <<'PYEOF'
import pytest


def test_ok_arithmetic():
    assert 1 + 1 == 2


def test_ok_raises():
    with pytest.raises(ZeroDivisionError):
        1 / 0


def test_intentional_failure():
    assert 2 + 2 == 5, "this failure is expected by the smoke test"
PYEOF

set +e
OUT="$("$VENV_BIN/ztest" "$WORKDIR/suite" 2>&1)"
RC=$?
set -e
echo "== run output:"
printf '%s\n' "$OUT" | sed 's/^/   /'
echo "== exit code: $RC"

case "$OUT" in
    *"this failure is expected by the smoke test"*) ;;
    *) echo "FAIL: the deliberate assertion message never surfaced" >&2; exit 1 ;;
esac
[ "$RC" -eq 1 ] || { echo "FAIL: a failing assertion must exit 1 (pytest's failure code), got $RC" >&2; exit 1; }
case "$OUT" in
    *"2 passed, 1 failed"*) ;;
    *) echo "FAIL: expected the summary line to report '2 passed, 1 failed'" >&2; exit 1 ;;
esac

# And a clean run must exit 0.
cat > "$WORKDIR/suite/test_demo.py" <<'PYEOF'
def test_ok_arithmetic():
    assert 1 + 1 == 2


def test_ok_string():
    assert "ztest".upper() == "ZTEST"
PYEOF
set +e
OUT2="$("$VENV_BIN/ztest" "$WORKDIR/suite" 2>&1)"
RC2=$?
set -e
echo "== clean run exit code: $RC2"
printf '%s\n' "$OUT2" | tail -n 5 | sed 's/^/   /'
[ "$RC2" -eq 0 ] || { echo "FAIL: clean suite exited $RC2" >&2; exit 1; }
case "$OUT2" in
    *"2 passed, 0 failed"*) ;;
    *) echo "FAIL: expected the summary line to report '2 passed, 0 failed'" >&2; exit 1 ;;
esac

echo "== SMOKE TEST PASSED"
