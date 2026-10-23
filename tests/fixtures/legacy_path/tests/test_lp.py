def test_strpath_and_write_ensure(tmpdir):
    # LegacyPath must expose strpath (str(self)) and
    # write(..., ensure=True) with parent creation.
    assert tmpdir.strpath == str(tmpdir)
    f = tmpdir.join("deep/deeper/f.txt")
    f.write("data", ensure=True)
    assert f.read() == "data"


def test_tmpdir_factory_mktemp_legacy(tmpdir_factory):
    # regression: tmpdir_factory.mktemp(...).strpath
    d = tmpdir_factory.mktemp("venv_path")
    assert d.strpath == str(d)
    sub = d.join("x/y.txt")
    sub.write("v", ensure=True)
    assert sub.read() == "v"
