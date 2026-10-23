import pytest


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".tau":
        return TauFile.from_parent(parent, path=file_path)
    return None


class TauFile(pytest.File):
    def collect(self):
        yield TauItem.from_parent(self, name="content", fspath=str(self.path))


class TauItem(pytest.Item):
    def __init__(self, name, parent=None, fspath=None, **kw):
        super().__init__(name=name, parent=parent, **kw)
        self.fspath = fspath

    def runtest(self):
        assert "ok" in open(self.fspath).read()

    def repr_failure(self, excinfo):
        return str(excinfo.value)
