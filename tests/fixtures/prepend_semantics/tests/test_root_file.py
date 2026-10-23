from html.parser import HTMLParser  # stdlib, not tests/html/


def test_stdlib_html_not_shadowed():
    p = HTMLParser()
    p.feed("<b>x</b>")
    assert True
