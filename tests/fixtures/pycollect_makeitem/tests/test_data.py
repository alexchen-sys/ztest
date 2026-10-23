def test_render(payload):
    assert isinstance(payload, str)
    assert payload == payload.upper().lower().upper()


test_render.payloads = ["PAYLOAD-A", "PAYLOAD-B"]


def test_plain():
    assert True
