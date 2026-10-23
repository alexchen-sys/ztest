import os


def test_one():
    assert True


def test_two():
    assert True


def test_three_terminates_the_worker():
    # The whole process group disappears here: no FILE_DONE is ever sent.
    os._exit(7)


def test_four_never_runs():
    assert True
