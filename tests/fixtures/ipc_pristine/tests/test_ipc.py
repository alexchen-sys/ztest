import json
import unittest
from unittest import mock


class TestPatchStdlibJson(unittest.TestCase):
    """regression: the test silences json.dumps via mock.patch.start();
    IPC events (the heartbeat around doCleanups, TEST_PASS) fly while the
    patch is active. The IPC core must hold pristine stdlib references and
    must not depend on user monkeypatching."""

    def test_ipc_immune(self):
        mock.patch.object(json, "dumps", return_value="{}").start()
        self.addCleanup(mock.patch.stopall)
        assert True

    def test_after_patch_restored(self):
        # the previous test's stopall ran: json is real again
        assert json.dumps({"a": 1}) == '{"a": 1}'
