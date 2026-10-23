import pytest

from tests.conftest import TextFile, TextItem


def pytest_collect_file(parent, file_path):
    if file_path.suffix == ".txt":
        return TextFile.from_parent(parent, path=file_path)
    return None


teardown_seen = []


def pytest_runtest_teardown(item, nextitem):
    if isinstance(item, TextItem):
        teardown_seen.append(item.name)
