import warnings

import pytest


def test_filterwarnings_error():
    # regression: filterwarnings=error turns warnings into exceptions
    with pytest.raises(DeprecationWarning):
        warnings.warn("boom", DeprecationWarning)


def test_ini_plugin_fixture(ini_plugin_fix):
    assert ini_plugin_fix == "ini-ok"


def test_conftest_plugin_fixture(cf_plugin_fix):
    assert cf_plugin_fix == "cf-ok"


@pytest.mark.opt_skip
def test_optional_skipped():
    raise RuntimeError("must be skipped by plugin collection_modifyitems")
