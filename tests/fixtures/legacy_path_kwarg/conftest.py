def pytest_ignore_collect(collection_path, path, config):
    # legacy signature: path is a py.path.local with .basename
    if path.basename == "test_gone.py":
        return True
    return None
