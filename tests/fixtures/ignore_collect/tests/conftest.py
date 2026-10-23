def pytest_ignore_collect(collection_path, config):
    return collection_path.name == "test_skipme.py"
