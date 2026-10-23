# A miniature of the target legacy-tests pattern: the pytest_pycollect_makeitem
# wrapper hook turns functions with a .payloads attribute into data-driven collectors.
import pytest


class MiniItem(pytest.Item):
    def __init__(self, parent=None, function=None, payload=None, **kw):
        self._function = function
        self._payload = payload
        # name/path/fspath from from_parent are swallowed (like target PyYAMLItem)
        super().__init__(str(payload), parent)

    def runtest(self):
        self._function(self._payload)


class MiniCollector(pytest.Collector):
    def __init__(self, name, parent=None, function=None, **kw):
        self._function = function
        super().__init__(name=name, parent=parent, **kw)

    def collect(self):
        return [
            MiniItem.from_parent(parent=self, function=self._function, payload=p)
            for p in self._function.payloads
        ]


@pytest.hookimpl(hookwrapper=True)
def pytest_pycollect_makeitem(collector, name, obj):
    outcome = yield
    outcome.get_result()
    if not callable(obj) or not getattr(obj, "payloads", None):
        outcome.force_result(None)
        return
    outcome.force_result(MiniCollector.from_parent(name=name, parent=collector, function=obj))
