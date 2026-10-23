import pytest


class CaseFile(pytest.File):
    """One file -> MANY items: custom data-driven collector."""

    def collect(self):
        for line in self.fspath.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # The data format is `case_name = value`; the same file stays a
            # valid Python module (import-safe if another hook takes it first).
            yield CaseItem.from_parent(self, name=line.split("=")[0].strip())


class CaseItem(pytest.Item):
    def __init__(self, name, parent):
        super().__init__(name, parent)

    def runtest(self):
        assert not self.name.startswith("bad")

    def repr_failure(self, excinfo):
        return f"case failed: {excinfo.value}"


def pytest_collect_file(parent, file_path):
    # .cases data files and one .py file: pytest calls this hook before the
    # native .py collection.
    if file_path.suffix == ".cases" or file_path.name == "test_generated.py":
        return CaseFile.from_parent(parent, path=file_path)
    return None
