import io
import json
import logging
import logging.config

EXT_MARK = "ext-ok"


def my_default(obj):
    return f"default:{obj!r}"


LOGGING_CONFIG = {
    "version": 1,
    "formatters": {
        "default": {
            "()": "logging.Formatter",
        },
        "jsonish": {
            "()": "tests.test_dictcfg.Jsonish",
            "json_default": "ext://tests.test_dictcfg.my_default",
            "static": {"k": "ext://tests.test_dictcfg.EXT_MARK"},
        },
    },
    "handlers": {
        "buf": {"class": "logging.StreamHandler", "formatter": "jsonish", "stream": "ext://sys.stdout"},
    },
    "root": {"handlers": ["buf"], "level": "INFO"},
}


class Jsonish(logging.Formatter):
    def __init__(self, *a, json_default=None, static=None, **kw):
        super().__init__()
        self.json_default = json_default
        self.static = static

    def format(self, record):
        return json.dumps({"m": record.getMessage(), "k": (self.static or {}).get("k")})


def test_ext_reference_resolves():
    logging.config.dictConfig(LOGGING_CONFIG)
    fmt = logging.root.handlers[0].formatter
    assert fmt.json_default is my_default, "ext://tests.test_dictconfig.* does not resolve"
    assert fmt.static["k"] == "ext-ok"
