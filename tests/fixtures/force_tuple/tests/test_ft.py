import uuid

import pytest


@pytest.mark.parametrize(
    ("value",),
    [
        (uuid.UUID("12345678-1234-5678-1234-567812345678"),),
        ("2bc1c94f-0deb-43e9-92a1-4775189ec9f8",),
    ],
)
def test_tuple_form_unpacks(value):
    # validators pattern: ("value",) argnames force tuple-unpacking
    assert not isinstance(value, tuple)
    assert str(value).replace("-", "")


@pytest.mark.parametrize("value", [(1, 2), (3, 4)])
def test_string_form_keeps_value_asis(value):
    # string argname: no force_tuple - the tuple value arrives whole
    assert isinstance(value, tuple)
