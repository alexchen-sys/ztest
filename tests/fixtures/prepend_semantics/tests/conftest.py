# regression: a package-tree conftest; the test subpackage html/ must not
# shadow stdlib html (pytest prepend mode inserts the first non-package ancestor).
from html.parser import HTMLParser


class _P(HTMLParser):
    pass
