def test_fixture_function_definition_stub():
    # the attribute exists and is fit for isinstance.
    import sys

    ffd = sys.modules["_pytest.fixtures"].FixtureFunctionDefinition
    assert isinstance(ffd, type)
    assert not isinstance(lambda: None, ffd)
