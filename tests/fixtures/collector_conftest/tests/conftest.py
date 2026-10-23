import pytest


class MyCollector(pytest.Collector):
    # regression: a custom collector in a conftest; it need not be
    # instantiated by ztest - what matters is that the class can be defined
    # (the conftest loads).
    pass
