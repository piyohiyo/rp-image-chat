import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    import ide_poke
else:
    ide_poke = None


@unittest.skipUnless(ide_poke is not None, "Antigravity integration requires Windows")
class HubServerTests(unittest.TestCase):
    def setUp(self):
        patches = {
            "hub": mock.patch.object(ide_poke, "_hub", None),
            "servers": mock.patch.object(ide_poke, "_language_servers", return_value=[("test-token", [1111, 2222])]),
            "call": mock.patch.object(ide_poke, "_call"),
        }
        for name, patch in patches.items():
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def test_checks_next_port_after_unauthorized_response(self):
        self.call.side_effect = [(401, "unauthorized"), (200, {})]
        self.assertEqual(ide_poke.hub_server(), (2222, "test-token"))
        self.assertEqual([call.args[0] for call in self.call.call_args_list], [1111, 2222])

    def test_checks_remaining_ports_after_other_http_errors(self):
        self.servers.return_value = [("test-token", [1111, 2222, 3333])]
        self.call.side_effect = [(404, "not found"), (503, "unavailable"), (200, {})]
        self.assertEqual(ide_poke.hub_server(), (3333, "test-token"))

    def test_checks_next_port_after_connection_or_json_errors(self):
        self.servers.return_value = [("test-token", [1111, 2222, 3333])]
        self.call.side_effect = [ConnectionResetError(), ValueError("invalid JSON"), (200, {})]
        self.assertEqual(ide_poke.hub_server(), (3333, "test-token"))

    def test_checks_next_language_server(self):
        self.servers.return_value = [("first-token", [1111, 1112]), ("second-token", [2222])]
        responses = {1111: (401, "unauthorized"), 1112: (404, "not found"), 2222: (200, {})}
        self.call.side_effect = lambda port, *args, **kwargs: responses[port]
        self.assertEqual(ide_poke.hub_server(), (2222, "second-token"))

    def test_new_conversation_uses_the_later_valid_port(self):
        def respond(port, token, method, body, timeout=15):
            if method == "ReadProjects":
                return (401, "unauthorized") if port == 1111 else (200, {})
            self.assertEqual((port, method), (2222, "StartCascade"))
            return 200, {"cascadeId": "new-conversation"}

        self.call.side_effect = respond
        with mock.patch.object(ide_poke, "find_project", return_value="existing-project"), mock.patch.object(ide_poke, "_servers", {}):
            conversation, _ = ide_poke.new_conversation(r"C:\example")
        self.assertEqual(conversation, "new-conversation")

    def test_returns_none_when_no_port_responds_successfully(self):
        self.call.side_effect = [(401, "unauthorized"), ConnectionResetError()]
        self.assertIsNone(ide_poke.hub_server())
        self.assertIsNone(ide_poke._hub)

    def test_reuses_a_valid_cached_server(self):
        ide_poke._hub = (9999, "cached-token")
        self.call.return_value = (200, {})
        self.assertEqual(ide_poke.hub_server(), (9999, "cached-token"))
        self.servers.assert_not_called()

    def test_rediscovers_after_cached_server_stops_accepting_requests(self):
        ide_poke._hub = (9999, "cached-token")
        self.servers.return_value = [("new-token", [2222])]
        self.call.side_effect = [(401, "unauthorized"), (200, {})]
        self.assertEqual(ide_poke.hub_server(), (2222, "new-token"))
        self.assertEqual(ide_poke._hub, (2222, "new-token"))


if __name__ == "__main__":
    unittest.main()
