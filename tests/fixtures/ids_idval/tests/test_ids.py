import pytest


def _caf(v):
    return "caf\xe9"


def _bytes_id(v):
    return b"\xffz"


def _unsupported(v):
    return ["not", "an", "id"]


@pytest.mark.parametrize("v", [1], ids=_caf)
def test_callable_unicode(v):
    pass


@pytest.mark.parametrize("v", [2], ids=_bytes_id)
def test_callable_bytes(v):
    pass


@pytest.mark.parametrize("v", [7], ids=_unsupported)
def test_callable_unsupported(v):
    pass


@pytest.fixture(params=[1], ids=_caf)
def fx_caf(request):
    return request.param


def test_fixture_unicode(fx_caf):
    assert fx_caf == 1


@pytest.fixture(params=[1], ids=[b"\xffz"])
def fx_bytes(request):
    return request.param


def test_fixture_bytes(fx_bytes):
    assert fx_bytes == 1


@pytest.mark.parametrize("v", [10, 20, 30], ids=(f"g{i}" for i in range(3)))
def test_gen_ids(v):
    pass
