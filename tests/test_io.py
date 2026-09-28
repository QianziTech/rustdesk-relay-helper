import socket
import socketserver
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from relay_helper.config import Config, Node, Policy, endpoint, load_config
from relay_helper.io import Console, ConsoleError, command_lock, load_state, parse_relays, probe_tcp, save_state


@contextmanager
def fake_console(response):
    commands = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            command = self.request.recv(1024).decode()
            commands.append(command)
            value = response(command, commands)
            try:
                self.request.sendall(value.encode())
            except OSError:
                pass  # A deliberate client timeout closes the test connection.

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    connect = socket.create_connection

    def redirected(address, timeout):
        assert address == ('127.0.0.1', 21115)
        return connect(server.server_address, timeout)

    try:
        with patch('relay_helper.io.socket.create_connection', side_effect=redirected):
            yield commands
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.console = Console([Node('a', 'a.test:21117', 10)], timeout=0.4)

    def test_normal_eof_empty_is_empty_list(self):
        with fake_console(lambda cmd, seen: ''):
            self.assertEqual(self.console.read(), [])

    def test_line_separated_output(self):
        with fake_console(lambda cmd, seen: 'a.test:21117\nb.test:21117\n'):
            self.assertEqual(self.console.read(), ['a.test:21117', 'b.test:21117'])

    def test_timeout_is_not_empty(self):
        def slow(cmd, seen):
            time.sleep(0.2)
            return ''
        self.console.timeout = 0.04
        with fake_console(slow):
            with self.assertRaises(ConsoleError):
                self.console.read()

    def test_connection_failure_is_not_empty(self):
        with patch('relay_helper.io.socket.create_connection', side_effect=ConnectionRefusedError('refused')):
            with self.assertRaises(ConsoleError):
                self.console.read()

    def test_delayed_write_confirmed_on_new_connection(self):
        def delayed(cmd, seen):
            if cmd.startswith('rs '):
                return ''
            return 'a.test:21117\n' if len(seen) >= 4 else 'old.test:21117\n'
        with fake_console(delayed) as commands:
            self.assertTrue(self.console.apply('a.test:21117'))
        self.assertEqual(commands[:4], ['rs', 'rs a.test:21117', 'rs', 'rs'])

    def test_matching_value_skips_write(self):
        with fake_console(lambda cmd, seen: 'a.test:21117\n') as commands:
            self.assertFalse(self.console.apply('a.test:21117'))
        self.assertEqual(commands, ['rs'])

    def test_wrong_or_multiple_addresses_cannot_confirm(self):
        self.console.timeout = 0.12
        with fake_console(lambda cmd, seen: '' if cmd.startswith('rs ') else 'a.test:21117\nb.test:21117\n'):
            with self.assertRaises(ConsoleError):
                self.console.apply('a.test:21117')

    def test_malformed_response_rejected(self):
        for text in ['error: unavailable\n', 'a.test:21117,b.test:21117', '\n']:
            with self.subTest(text=text), self.assertRaises(ConsoleError):
                parse_relays(text)

    def test_unconfigured_target_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.console.apply('other.test:21117')

    def test_real_tcp_probe(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            rtt, error = probe_tcp(Node('a', '127.0.0.1:' + str(listener.getsockname()[1]), 10), 1)
            self.assertIsNone(error)
            self.assertGreaterEqual(rtt, 0)


class ConfigStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config = Config(Policy(), (Node('a', 'a.test:21117', 10),))

    def test_example_loads(self):
        config = load_config(Path(__file__).resolve().parents[1] / 'config/relay-helper.example.ini')
        self.assertEqual(config.policy.fail_after, 3)
        self.assertFalse(config.nodes[-1].rtt_ranked)

    def test_defaults_and_fractional_timeout(self):
        file = self.path / 'config.ini'
        file.write_text('[policy]\nconnect_timeout_seconds = 0.2\n[node:a]\naddress = a.test:21117\ntier = 10\n', encoding='utf-8')
        config = load_config(file)
        self.assertTrue(config.nodes[0].rtt_ranked)
        self.assertEqual(config.policy.connect_timeout_seconds, 0.2)

    def test_invalid_config(self):
        file = self.path / 'config.ini'
        for entry in ['fail_after = 0', 'recover_after = -1', 'connect_timeout_seconds = nan', 'rtt_sample_count = 1.5', 'typo = 2']:
            file.write_text('[policy]\n' + entry + '\n[node:a]\naddress = a.test:21117\ntier = 10\n', encoding='utf-8')
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                load_config(file)

    def test_address_validation(self):
        self.assertEqual(endpoint('[::1]:21117'), ('::1', 21117))
        for value in ['a:1,b:2', 'a:1\nrs b:2', 'a:0', 'a:65536', 'a', '::1:21117']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                endpoint(value)

    def test_manual_mode_persists_and_address_change_resets_health(self):
        path = self.path / 'state.json'
        state = load_state(path, self.config)
        state.update(mode='manual', manual_target='a')
        state['nodes']['a']['health'] = 'healthy'
        save_state(path, state)
        loaded = load_state(path, self.config)
        self.assertEqual(loaded['mode'], 'manual')
        self.assertEqual(loaded['manual_target'], 'a')
        altered = replace(self.config, nodes=(Node('a', 'new.test:21117', 10),))
        self.assertEqual(load_state(path, altered)['nodes']['a']['health'], 'unknown')
        self.assertEqual(list(self.path.glob('*.tmp')), [])

    def test_corrupt_state_does_not_reset_manual_mode(self):
        path = self.path / 'state.json'
        path.write_text('{"mode": "manual"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            load_state(path, self.config)

    def test_lock_exclusion_and_release(self):
        path = self.path / 'state.lock'
        with command_lock(path):
            with self.assertRaisesRegex(RuntimeError, '管理器忙'):
                with command_lock(path):
                    self.fail('second lock acquired')
        with command_lock(path):
            pass


if __name__ == '__main__':
    unittest.main()
