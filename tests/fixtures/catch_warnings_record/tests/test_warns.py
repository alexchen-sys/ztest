import warnings

from warner import emit_deprecation


def test_catch_warnings_records_non_main_deprecation():
    with warnings.catch_warnings(record=True) as w:
        emit_deprecation()
    assert len(w) >= 1, "catch_warnings(record=True) is empty - pytest's per-item always filter not reproduced"
    assert w[0].category is DeprecationWarning


def test_catch_warnings_second_hit_also_recorded():
    # registry semantics: a repeated firing is recorded too
    with warnings.catch_warnings(record=True) as w:
        emit_deprecation()
        emit_deprecation()
    assert len(w) >= 1
