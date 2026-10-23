import os
import subprocess
import sys


def test_no_pythonpath_env_pollution():
    # pytest never writes os.environ["PYTHONPATH"] (rootdir goes to sys.path)
    assert not os.environ.get("PYTHONPATH")


def test_subprocess_sees_clean_pythonpath():
    out = subprocess.run(
        [sys.executable, "-c", "import os; print(repr(os.environ.get('PYTHONPATH')))"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0
    assert "None" in out.stdout or out.stdout.strip() == "''"
