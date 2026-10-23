import unittest


class BaseTemplate(unittest.TestCase):
    __test__ = False  # pytest: do not collect the template class

    def test_template(self):
        raise NotImplementedError


class TestConcrete(BaseTemplate):
    __test__ = True  # explicit re-enable

    def test_template(self):
        assert True


def test_helper():
    raise AssertionError("must not be collected")


test_helper.__test__ = False


def test_real():
    assert True
