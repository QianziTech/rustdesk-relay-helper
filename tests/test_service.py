import contextlib
import io
import copy
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from relay_helper.__main__ import main, show_nodes
from relay_helper.config import Config, Node, Policy
from relay_helper.io import Console, ConsoleError, load_state, save_state
from relay_helper.policy import record_probe
from relay_helper.service import operate, probe, probe_node, reconcile, set_manual

from test_io import fake_console


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.json'
        self.config = Config(Policy(), (Node('a', 'a.test:21117', 10),))
        self.state = load_state(self.path, self.config)

    def healthy(self):
        for _ in range(6):
            record_probe(self.state['nodes']['a'], 10, None, time.time(), self.config.policy)

    def test_no_healthy_node_preserves_actual(self):
        console = Mock()
        console.read.return_value = ['external:21117']
        reconcile(self.config, self.state, console)
        console.apply.assert_not_called()
        self.assertEqual(self.state['actual'], ['external:21117'])

    def test_failed_read_prevents_write(self):
        self.healthy()
        console = Mock()
        console.read.side_effect = ConsoleError('offline')
        with self.assertRaises(ConsoleError):
            reconcile(self.config, self.state, console)
        console.apply.assert_not_called()
        self.assertIsNone(self.state['actual'])

    def test_failed_confirmation_preserves_switch_time(self):
        self.healthy()
        console = Mock()
        console.read.return_value = []
        console.apply.side_effect = ConsoleError('not confirmed')
        with self.assertRaises(ConsoleError):
            reconcile(self.config, self.state, console)
        self.assertEqual(self.state['last_switch'], 0)
        self.assertIsNone(self.state['actual'])
        self.assertIsNone(self.state['confirmed_target'])
        console.read.return_value = ['a.test:21117']
        console.apply.side_effect = None
        console.apply.return_value = False
        reconcile(self.config, self.state, console)
        self.assertGreater(self.state['last_switch'], 0)

    def test_restart_reapplies_manual_target(self):
        self.healthy()
        set_manual(self.config, self.state, 'a')
        save_state(self.path, self.state)
        state = load_state(self.path, self.config)
        def response(cmd, commands):
            return 'a.test:21117\n' if cmd == 'rs' and 'rs a.test:21117' in commands else ''
        with fake_console(response) as commands:
            reconcile(self.config, state, Console(self.config.nodes))
        self.assertEqual(state['mode'], 'manual')
        self.assertEqual(state['actual'], ['a.test:21117'])
        self.assertIn('rs a.test:21117', commands)

    def test_late_confirmation_starts_hold_before_next_decision(self):
        self.config = Config(Policy(), (Node('a', 'a.test:21117', 10), Node('b', 'b.test:21117', 20)))
        self.state = load_state(self.path, self.config)
        self.healthy()
        for _ in range(6):
            record_probe(self.state['nodes']['b'], 10, None, time.time(), self.config.policy)
        self.state['pending_target'] = 'b.test:21117'
        console = Mock()
        console.read.return_value = ['b.test:21117']
        console.apply.return_value = False
        reconcile(self.config, self.state, console)
        console.apply.assert_called_once_with('b.test:21117')
        self.assertGreater(self.state['last_switch'], 0)

    def test_manual_requires_health(self):
        with self.assertRaises(ValueError):
            set_manual(self.config, self.state, 'a')
        self.assertEqual(self.state['mode'], 'auto')

    def test_cli_hides_expired_values_even_without_loading_again(self):
        record_probe(self.state['nodes']['a'], 10, None, 1000, self.config.policy)
        output = io.StringIO()
        with patch('relay_helper.__main__.time.time', return_value=1120), contextlib.redirect_stdout(output):
            show_nodes(self.config, self.state)
        self.assertIn('unknown\t0/0\t-\t-', output.getvalue())

    def test_double_probe_uses_second_value_and_counts_one_round(self):
        for values in ((800, 12), (12, 800)):
            with self.subTest(values=values):
                state = load_state(self.path, self.config)
                with patch('relay_helper.service.probe_tcp', side_effect=[(value, None) for value in values]) as tcp:
                    probe(self.config, state)
                health = state['nodes']['a']
                self.assertEqual(tcp.call_count, 2)
                self.assertEqual(health['rtt_ms'], values[1])
                self.assertEqual(health['successes'], 1)
                self.assertEqual(len(health['samples']), 1)
                self.assertEqual([attempt['rtt_ms'] for attempt in health['last_attempts']], list(values))

    def test_either_attempt_failure_counts_one_failed_round(self):
        combinations = (((None, 'timeout'), (12, None)),
                        ((12, None), (None, 'refused')),
                        ((None, 'timeout'), (None, 'refused')))
        for values in combinations:
            with self.subTest(values=values):
                state = load_state(self.path, self.config)
                with patch('relay_helper.service.probe_tcp', side_effect=values) as tcp:
                    probe(self.config, state)
                health = state['nodes']['a']
                self.assertEqual(tcp.call_count, 2)
                self.assertIsNone(health['rtt_ms'])
                self.assertEqual(health['failures'], 1)
                self.assertEqual(health['samples'], [])
                self.assertEqual(health['health'], 'unknown')
                self.assertIn('第', health['error'])

    def test_failure_and_recovery_count_rounds_not_connections(self):
        with patch('relay_helper.service.probe_tcp', return_value=(None, 'down')):
            for _ in range(3):
                probe(self.config, self.state)
        self.assertEqual(self.state['nodes']['a']['health'], 'unhealthy')
        with patch('relay_helper.service.probe_tcp', return_value=(10, None)):
            for _ in range(5):
                probe(self.config, self.state)
            self.assertEqual(self.state['nodes']['a']['health'], 'unhealthy')
            probe(self.config, self.state)
        self.assertEqual(self.state['nodes']['a']['health'], 'healthy')

    def test_round_counts_continue_after_thresholds(self):
        with patch('relay_helper.service.probe_tcp', return_value=(10, None)) as tcp:
            for count in range(1, 9):
                probe(self.config, self.state)
                self.assertEqual(self.state['nodes']['a']['successes'], count)
                self.assertEqual(len(self.state['nodes']['a']['samples']), min(count, 5))
            self.assertEqual(tcp.call_count, 16)
        with patch('relay_helper.service.probe_tcp', return_value=(None, 'down')) as tcp:
            for count in range(1, 6):
                probe(self.config, self.state)
                self.assertEqual(self.state['nodes']['a']['failures'], count)
                self.assertEqual(self.state['nodes']['a']['successes'], 0)
            self.assertEqual(tcp.call_count, 10)

    def test_resume_auto_reselects_once_then_respects_hold_and_margin(self):
        config = Config(Policy(), (Node('a', 'a.test:21117', 10),
                                  Node('b', 'b.test:21117', 10),
                                  Node('fallback', 'fallback.test:21117', 10, rtt_ranked=False),
                                  Node('lower', 'lower.test:21117', 20)))
        state = load_state(self.path, config)
        console = Mock()
        console.read.return_value = []
        console.apply.return_value = True
        latencies = {'a': 10, 'b': 15, 'fallback': 1, 'lower': 1}
        with patch('relay_helper.service.probe_tcp', side_effect=lambda node, timeout: (latencies[node.id], None)):
            operate(config, state, console, 'switch', 'fallback')
            console.read.return_value = ['fallback.test:21117']
            operate(config, state, console, 'auto')
            console.apply.assert_called_with('lower.test:21117')
            self.assertEqual(state['mode'], 'auto')
            self.assertFalse(state['auto_reselect'])
            # The explicit resume bypass applies once, subsequent automatic runs keep the contract.
            console.read.return_value = ['b.test:21117']
            operate(config, state, console, 'reconcile')
            console.apply.assert_called_with('b.test:21117')
            state['last_switch'] = 0
            operate(config, state, console, 'auto')
            console.apply.assert_called_with('b.test:21117')

    def test_resume_auto_retries_after_console_failure_across_reload(self):
        self.state.update(mode='manual', manual_target='a')
        console = Mock()
        console.read.side_effect = ConsoleError('offline')
        with patch('relay_helper.service.probe_tcp', return_value=(10, None)):
            with self.assertRaises(ConsoleError):
                operate(self.config, self.state, console, 'auto')
            self.assertTrue(self.state['auto_reselect'])
            save_state(self.path, self.state)
            state = load_state(self.path, self.config)
            console.read.side_effect = None
            console.read.return_value = ['external:21117']
            console.apply.return_value = True
            operate(self.config, state, console, 'reconcile')
        console.apply.assert_called_once_with('a.test:21117')
        self.assertFalse(state['auto_reselect'])

    def test_resume_without_candidates_keeps_reselection_until_next_probe(self):
        self.state.update(mode='manual', manual_target='a')
        console = Mock()
        console.read.return_value = ['external:21117']
        with patch('relay_helper.service.probe_tcp', return_value=(None, 'down')):
            operate(self.config, self.state, console, 'auto')
        console.apply.assert_not_called()
        self.assertTrue(self.state['auto_reselect'])
        self.assertEqual(self.state['actual'], ['external:21117'])
        with patch('relay_helper.service.probe_tcp', return_value=(10, None)):
            operate(self.config, self.state, console, 'reconcile')
        console.apply.assert_called_once_with('a.test:21117')
        self.assertFalse(self.state['auto_reselect'])

    def test_switch_probes_only_target_and_preserves_other_node_samples(self):
        config = Config(self.config.policy, self.config.nodes + (Node('b', 'b.test:21117', 20),))
        state = load_state(self.path, config)
        record_probe(state['nodes']['b'], 100, None, time.time(), config.policy)
        before = copy.deepcopy(state['nodes']['b'])
        console = Mock()
        console.read.return_value = []
        console.apply.return_value = True
        with patch('relay_helper.service.probe_tcp', return_value=(10, None)) as tcp:
            operate(config, state, console, 'switch', 'a')
        self.assertEqual([call.args[0].id for call in tcp.call_args_list], ['a', 'a'])
        self.assertEqual(state['nodes']['b'], before)
        self.assertEqual(state['mode'], 'manual')
        console.apply.assert_called_once_with('a.test:21117')

    def test_invalid_and_disabled_switch_target_does_no_network_io(self):
        config = Config(self.config.policy, self.config.nodes + (Node('b', 'b.test:21117', 20, enabled=False),))
        for target in ('missing', 'b'):
            with self.subTest(target=target), patch('relay_helper.service.probe_tcp') as tcp:
                console = Mock()
                with self.assertRaises(ValueError):
                    operate(config, self.state, console, 'switch', target)
                tcp.assert_not_called()
                console.read.assert_not_called()
                console.apply.assert_not_called()

    def test_failed_target_probe_cannot_switch_using_previous_health(self):
        self.healthy()
        console = Mock()
        with patch('relay_helper.service.probe_tcp', side_effect=[(10, None), (None, 'timeout')]):
            with self.assertRaises(ValueError):
                operate(self.config, self.state, console, 'switch', 'a')
        self.assertEqual(self.state['mode'], 'auto')
        self.assertEqual(self.state['nodes']['a']['failures'], 1)
        console.apply.assert_not_called()

    def test_real_double_probe_opens_two_independent_tcp_connections(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(2)
            accepted = []

            def accept():
                for _ in range(2):
                    connection, address = listener.accept()
                    accepted.append(address)
                    connection.close()

            thread = threading.Thread(target=accept)
            thread.start()
            try:
                result = probe_node(Node('a', '127.0.0.1:' + str(listener.getsockname()[1]), 10), 1)
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(accepted), 2)
            self.assertIsNone(result[1])
            self.assertEqual(result[0], result[2][1]['rtt_ms'])

    def test_cli_probe_never_contacts_hbbs_and_auto_restores_mode(self):
        config_path = Path(self.tmp.name) / 'config.ini'
        config_path.write_text('[policy]\nrecover_after=1\nrtt_sample_count=1\n[node:a]\naddress=a.test:21117\ntier=10\n', encoding='utf-8')
        args = ['--config', str(config_path), '--state', str(self.path)]
        with patch('relay_helper.service.probe_tcp', return_value=(10, None)), patch('relay_helper.__main__.Console') as console_class, patch('relay_helper.__main__.setup_logging'), contextlib.redirect_stdout(io.StringIO()):
            console = console_class.return_value
            console.read.return_value = []
            console.apply.return_value = True
            self.assertEqual(main(args + ['probe']), 0)
            console.read.assert_not_called()
            console.apply.assert_not_called()
            self.assertEqual(main(args + ['switch', 'a']), 0)
            self.assertEqual(load_state(self.path, self.config)['mode'], 'manual')
            self.assertEqual(main(args + ['auto']), 0)
            self.assertEqual(load_state(self.path, self.config)['mode'], 'auto')


if __name__ == '__main__':
    unittest.main()
