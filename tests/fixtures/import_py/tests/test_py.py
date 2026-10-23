def test_import_py_error():
    # `import py` (the py-lib shim over _pytest._py) and py.error
    import py

    assert py.error is not None
    # py.error proxies errno names into OSError subclasses
    assert py.error.EEXIST is not None
