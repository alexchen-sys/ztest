import warnings


def emit_deprecation() -> None:
    # a warning from a NON-test module: CPython's default filter silences it,
    # pytest per-item simplefilter("always") records it.
    warnings.warn("cached(None) is deprecated", DeprecationWarning)
