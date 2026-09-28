import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from relay_helper.__main__ import main
from relay_helper.config import Config, Node, Policy
from relay_helper.io import Console, ConsoleError, load_state, save_state
from relay_helper.policy import record_probe
from relay_helper.service import reconcile, set_manual

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
            record_probe(self.state['nodes']['a'], 10, None, 1000, self.config.policy)

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
            record_probe(self.state['nodes']['b'], 10, None, 1000, self.config.policy)
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
