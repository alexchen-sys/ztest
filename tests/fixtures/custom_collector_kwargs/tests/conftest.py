import pytest


class PayloadFile(pytest.File):
    """Collector whose __init__ declares explicit parameters without **kwargs or fspath."""

    def __init__(self, name=None, parent=None, path=None, label=None):
        super().__init__(name=name or path.name, parent=parent, path=path)
        self.label = label

    def collect(self):
        assert self.label == "root", f"label lost: {self.label!r}"
        text = self.fspath.read_text("utf-8").strip()
        yield PayloadItem.from_parent(self, name="content", payload=text, index=0)


class PlainFile(pytest.File):
    """Cooperative collector exercising item-side user kwargs independently."""

    def collect(self):
        text = self.fspath.read_text("utf-8").strip()
        yield PayloadItem.from_parent(self, name="content", payload=text, index=0)


class PayloadItem(pytest.Item):
    """Item whose custom kwargs are mandatory constructor parameters."""

    def __init__(self, name, parent, payload, index):
        super().__init__(name, parent)
        self.payload = payload
        self.index = index

    def runtest(self):
        assert self.payload == "ok", f"payload lost: {self.payload!r}"
        assert self.index == 0, self.index

    def repr_failure(self, excinfo):
        return f"payload failure: {excinfo.value}"


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".kv":
        return PayloadFile.from_parent(parent, path=file_path, label="root")
    if file_path.suffix == ".kv2":
        return PlainFile.from_parent(parent, path=file_path)
    return None
