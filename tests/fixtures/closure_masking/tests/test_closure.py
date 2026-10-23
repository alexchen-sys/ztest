def test_via_fixture(works_in_py):
    assert works_in_py[0] == "checker"
    assert works_in_py[1] in ("3.6", "3.7")


def test_direct_mixed(works_in_py, each_version):
    assert works_in_py == ("checker", each_version)


def test_autouse_visible(tag_value):
    assert tag_value == "tagged"
