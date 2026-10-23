from testhelpers import CONSTANT  # from utils/ (pythonpath ini)
from rootmod import ROOT_MARK     # from the repo root (pythonpath ini)


def test_pythonpath_ini_utils():
    assert CONSTANT == 42


def test_pythonpath_ini_root():
    assert ROOT_MARK == "root"
