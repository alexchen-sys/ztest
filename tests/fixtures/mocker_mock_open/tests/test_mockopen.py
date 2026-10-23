def test_mock_open_reads(mocker):
    m = mocker.mock_open(read_data=b"payload")
    with m("some/file.bin", "rb") as fh:
        assert fh.read() == b"payload"


def test_mock_open_as_context_manager(mocker):
    with mocker.mock_open(read_data=b"abc")() as fh:
        assert fh.read() == b"abc"
