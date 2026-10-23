import abc
import unittest

import pytest


class TestTranslatorBase(unittest.TestCase, abc.ABC):
    @abc.abstractmethod
    def tr_cls(self):
        ...

    def test_thing(self):
        self.assertTrue(True)


class TestConcrete(TestTranslatorBase):
    def tr_cls(self):
        return int


# target form: a bare ABC (no TestCase), pytest-style parametrize
class TestAbstractPytestBase(abc.ABC):
    @property
    @abc.abstractmethod
    def impl(self):
        ...

    @pytest.mark.parametrize("x", [1, 2])
    def test_via_impl(self, x):
        assert self.impl(x) == x


class TestConcretePytest(TestAbstractPytestBase):
    @property
    def impl(self):
        return int  # int(1) == 1, int(2) == 2
