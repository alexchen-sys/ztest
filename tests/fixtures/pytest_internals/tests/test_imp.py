from pytest import TempPathFactory, CaptureFixture, MonkeyPatch, LogCaptureFixture
from _pytest.fixtures import SubRequest
from _pytest.recwarn import WarningsRecorder

def test_imports_and_annotations(tmp_path_factory: TempPathFactory, capsys: CaptureFixture[str]):
    d = tmp_path_factory.mktemp("x")
    print("hello-out")
    out, _ = capsys.readouterr()
    assert "hello-out" in out
    assert d.name.startswith("x")

def test_subrequest_annotation(request: SubRequest):
    assert request is not None

def test_warnings_recorder_ref():
    assert WarningsRecorder is not None
