import re

import pytest


@pytest.mark.parametrize("v", ["\u043f\u0440\u0438\u0432\u0435\u0442", "a\\b", "caf\xe9"])
def test_str(v):
    pass


@pytest.mark.parametrize("v", ["dup2", "dup2"])
def test_dup_digit(v):
    pass


@pytest.mark.parametrize("v", [1, 1])
def test_dup_int(v):
    pass


@pytest.mark.parametrize("v", [1 + 2j])
def test_complex(v):
    pass


@pytest.mark.parametrize("v", [re.compile("a.b"), re.compile(r"a\.b"), re.compile("caf\xe9")])
def test_re(v):
    pass


@pytest.mark.parametrize("v", [b"a\\b", b"\x01\xff", b"\xff"])
def test_bytes(v):
    pass


@pytest.mark.parametrize("v", [1], ids=["caf\xe9"])
def test_explicit_escaped(v):
    pass


@pytest.mark.parametrize("v", [pytest.param(2, id="\u043f\u0440\u0438\u0432\u0435\u0442")])
def test_param_id_escaped(v):
    pass


@pytest.mark.parametrize("v", [5, 5, "5_0"])
def test_dedup_collision(v):
    pass
