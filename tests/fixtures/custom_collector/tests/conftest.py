import pytest


class TextFile(pytest.File):
    def collect(self):
        yield TextItem.from_parent(self, name="content")


class TextItem(pytest.Item):
    def __init__(self, name, parent):
        super().__init__(name, parent)
        self.data = self.fspath.read_text("utf-8").strip()

    def runtest(self):
        assert self.data == "ok"

    def repr_failure(self, excinfo):
        return f"custom failure: {excinfo.value}"
