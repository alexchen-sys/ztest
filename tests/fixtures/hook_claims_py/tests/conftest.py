import pytest


def pytest_collect_file(parent, file_path):
    # regression: the hook claims EVERYTHING in the directory, including *.py.
    if file_path.parent.name == "claimed" and file_path.name != "conftest.py":
        return ClaimedFile.from_parent(parent, path=file_path)
    return None


class ClaimedFile(pytest.File):
    def collect(self):
        yield ClaimedItem.from_parent(self, name="")


class ClaimedItem(pytest.Item):
    def runtest(self):
        pass

    def repr_failure(self, excinfo):
        return str(excinfo.value)
