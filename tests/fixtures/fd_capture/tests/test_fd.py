import subprocess
import sys


def test_subproc():
    subprocess.run([sys.executable, "-c", "print('SUBPROC_MARKER')"], check=True)
