def test_roundtrip(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("hello ztest")
    assert target.read_text() == "hello ztest"
