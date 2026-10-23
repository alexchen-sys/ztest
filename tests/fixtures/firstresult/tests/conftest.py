def pytest_ignore_collect(collection_path, config):
    # pluggy firstresult: hookimpls run LIFO (leaf first); the first non-None
    # result wins - leaf False must override root True for the file.
    if collection_path.name == "test_keep.py":
        return False
    return None
