import pkgutil


def test_get_data_on_module_name():
    # target FromPackageLoader(__name__): pkgutil.get_data on the module name
    data = pkgutil.get_data(__name__, "grammars/x.lark")
    assert data and b"RULE" in data


def test_get_data_on_package_name():
    data = pkgutil.get_data("tests", "grammars/x.lark")
    assert data and b"RULE" in data
