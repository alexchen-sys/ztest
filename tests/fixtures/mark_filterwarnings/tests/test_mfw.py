import warnings

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def test_module_filterwarnings_mark():
    # a module-level filterwarnings mark applies for
    # the item's lifetime - UserWarning is filtered out, RuntimeWarning shows.
    with warnings.catch_warnings(record=True) as rec:
        warnings.warn("hidden", UserWarning)
        warnings.warn("visible", RuntimeWarning)
    assert [str(w.message) for w in rec] == ["visible"]
