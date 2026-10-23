def pytest_collection_modifyitems(items):
    raise RuntimeError("modifyitems boom")
