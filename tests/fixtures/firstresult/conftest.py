def pytest_ignore_collect(collection_path, config):
    # root wants to ignore the file and the directory
    if collection_path.name == "test_keep.py":
        return True
    if collection_path.name == "ignored_dir":
        return True
    return None
