import pytest


@pytest.fixture(params=[1, 2])
def node(request):
    # target test_parser_tree pattern: fixture writes into shared node keywords
    request.keywords["expected"] = request.param * 10
    return request.param


@pytest.fixture
def expected(request, node):
    return request.keywords["expected"]


def test_keywords_shared(node, expected):
    assert expected == node * 10


def test_pluginmanager_stub(pytestconfig):
    # target reset_logging pattern
    pm = pytestconfig.pluginmanager
    assert pm.unregister(name="logging-plugin") is None
    assert pm.hasplugin("whatever") is False
    assert pm.getplugin("whatever") is None
    pm.register(object())
