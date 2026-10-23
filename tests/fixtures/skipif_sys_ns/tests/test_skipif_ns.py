import pytest


# the skipif string uses sys without importing sys in
# the module - pytest evaluates it in a namespace with os/sys/platform/sysconfig.
@pytest.mark.skipif("sys.platform == 'win32'", reason="windows only")
def test_skipif_sys_string():
    assert True


@pytest.mark.xfail("sys.platform == 'win9x'", reason="never")
def test_xfail_sys_string():
    assert True
