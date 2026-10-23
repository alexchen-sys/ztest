def test_tmpdir_py_path_api(tmpdir):
    with tmpdir.as_cwd():
        pass
    f = tmpdir.join("a.bin")
    f.write_binary(b"\x00\x01")
    assert f.read_binary() == b"\x00\x01"
    g = tmpdir.join("note.txt")
    g.write_text("hello", "utf-8")
    assert g.read_text("utf-8") == "hello"
    assert isinstance(tmpdir.dirname, str) and tmpdir.dirname
    assert g.basename == "note.txt"
    assert g.purebasename == "note"
    assert g.ext == ".txt"
    assert tmpdir.check(dir=True) and not tmpdir.check(file=True)
    ensured = tmpdir.ensure("deep", "sub", dir=True)
    assert ensured.check(dir=True)
    ef = tmpdir.ensure("made.txt")
    assert ef.check(file=True)
