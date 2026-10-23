def is_palindrome(text):
    return text == text[::-1]


def test_palindrome():
    assert is_palindrome("level")


def test_not_palindrome():
    assert not is_palindrome("ztest")
