import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from relay_helper.__main__ import main
from relay_helper.config import load_config
from relay_helper.io import ConsoleError, command_lock, load_state, save_state
from relay_helper.web import Backend, LocalServer, MAX_BODY, load_token, serve

CONFIG = '[policy]\nrecover_after=1\nrtt_sample_count=1\n[node:a]\naddress=a.test:21117\ntier=10\n'
TOKEN = 'test_only_' + 'x' * 34


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'webui.token'

    def test_generated_token_is_private_and_survives_restart(self):
        token = load_token(self.path)
        self.assertEqual(len(token), 43)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_token(self.path), token)

    def test_loose_permissions_and_short_token_rejected(self):
        load_token(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            load_token(self.path)
        self.path.chmod(0o600)
        self.path.write_text('weak\n', encoding='ascii')
        with self.assertRaises(ValueError):
            load_token(self.path)

    def test_symlink_is_never_followed(self):
        target = self.path.with_name('original')
        target.write_text(TOKEN, encoding='ascii')
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            load_token(self.path)
        self.assertEqual(target.read_text(encoding='ascii'), TOKEN)

    def test_reserved_paths_and_invalid_ports_rejected_before_write(self):
        for port in (0, 65536):
            with self.assertRaises(ValueError):
                serve(self.path, self.path.with_name('state.json'), port)
        with self.assertRaises(ValueError):
            serve(self.path, self.path.with_name('state.json'), token_file=self.path)
        self.assertFalse(self.path.exists())

    def test_cli_passes_paths_to_web_adapter(self):
        with patch('relay_helper.web.serve', return_value=0) as run, patch('relay_helper.__main__.setup_logging'):
            self.assertEqual(main(['--config', 'test.ini', '--state', 'test.json',
                                   'web', '--port', '8899', '--token-file', str(self.path)]), 0)
        run.assert_called_once_with('test.ini', 'test.json', 8899, str(self.path))


class BackendFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / 'config.ini'
        self.state_path = Path(self.tmp.name) / 'state.json'
        self.config_path.write_text(CONFIG, encoding='utf-8')
        self.config_path.chmod(0o640)
        self.backend = Backend(self.config_path, self.state_path)


class BackendTests(BackendFixture):

    def test_probe_switch_auto_share_persistent_mode_and_confirmation(self):
        with patch('relay_helper.service.probe_tcp', return_value=(12, None)), patch('relay_helper.web.Console') as cls:
            console = cls.return_value
            console.read.return_value = []
            console.apply.return_value = True
            code, result = self.backend.run('probe')
            self.assertEqual(code, 200)
            self.assertEqual(result['data']['nodes'][0]['health']['health'], 'healthy')
            console.read.assert_not_called()
            console.apply.assert_not_called()
            code, result = self.backend.run('switch', 'a')
            self.assertEqual(result['data']['state']['mode'], 'manual')
            self.assertEqual(result['data']['state']['actual'], ['a.test:21117'])
            console.apply.assert_called_with('a.test:21117')
            self.assertEqual(load_state(self.state_path, load_config(self.config_path))['mode'], 'manual')
            self.backend.run('auto')
            self.assertEqual(load_state(self.state_path, load_config(self.config_path))['mode'], 'auto')

    def test_failed_switch_confirmation_preserves_manual_intent(self):
        with patch('relay_helper.service.probe_tcp', return_value=(12, None)), patch('relay_helper.web.Console') as cls:
            cls.return_value.read.return_value = []
            cls.return_value.apply.side_effect = ConsoleError('not confirmed')
            code, result = self.backend.run('switch', 'a')
        self.assertEqual(code, 502)
        self.assertFalse(result['ok'])
        state = load_state(self.state_path, load_config(self.config_path))
        self.assertEqual(state['mode'], 'manual')
        self.assertEqual(state['last_switch'], 0)
        self.assertIsNone(state['actual'])

    def test_failed_status_is_unknown_and_never_writes_hbbs(self):
        with patch('relay_helper.web.Console') as cls:
            cls.return_value.read.side_effect = ConsoleError('offline')
            code, result = self.backend.run('status')
            cls.return_value.apply.assert_not_called()
        self.assertEqual(code, 502)
        self.assertIsNone(result['data']['state']['actual'])
        self.assertEqual(result['data']['state']['actual_error'], 'offline')

    def test_invalid_config_keeps_original_and_creates_no_backup(self):
        document = self.backend.config()
        with self.assertRaises(ValueError):
            self.backend.config(dict(document, text=CONFIG.replace('tier=10', 'tier=-1')))
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)
        self.assertFalse(Path(str(self.config_path) + '.webui.bak').exists())

    def test_valid_config_is_backed_up_and_conflicts_are_rejected(self):
        document = self.backend.config()
        changed = CONFIG.replace('tier=10', 'tier=20')
        updated = self.backend.config(dict(document, text=changed))
        self.assertNotEqual(updated['revision'], document['revision'])
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), changed)
        backup = Path(str(self.config_path) + '.webui.bak')
        self.assertEqual(backup.read_text(encoding='utf-8'), CONFIG)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o640)
        with self.assertRaises(RuntimeError):
            self.backend.config(document)
        self.assertEqual(backup.read_text(encoding='utf-8'), CONFIG)

    def test_manual_target_cannot_be_disabled_removed_or_readdressed(self):
        config = load_config(self.config_path)
        state = load_state(self.state_path, config)
        state.update(mode='manual', manual_target='a')
        save_state(self.state_path, state)
        document = self.backend.config()
        for text in (CONFIG + 'enabled=false\n', CONFIG.replace('a.test', 'b.test'),
                     CONFIG.replace('node:a', 'node:b')):
            with self.assertRaises(ValueError):
                self.backend.config(dict(document, text=text))
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)

    def test_operations_and_config_share_cli_lock(self):
        with command_lock(str(self.state_path) + '.lock'):
            for operation in (lambda: self.backend.run('nodes'), self.backend.config):
                with self.assertRaisesRegex(RuntimeError, '管理器忙'):
                    operation()


class HTTPTests(BackendFixture):
    def setUp(self):
        super().setUp()
        self.server = LocalServer(0, TOKEN, self.backend)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, method='GET', body=None, headers=None, auth=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        if auth:
            headers['Authorization'] = 'Bearer ' + TOKEN
        if isinstance(body, dict):
            body = json.dumps(body)
            headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode('utf-8')

    def test_loopback_binding_and_unauthorized_requests_do_no_work(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with patch.object(self.backend, 'run') as run, patch.object(self.backend, 'config') as config:
            for path, method in (('/', 'GET'), ('/api/nodes', 'GET'), ('/api/config', 'GET'), ('/api/auto', 'POST')):
                code, headers, body = self.request(path, method)
                self.assertEqual(code, 401)
                self.assertNotIn('<html', body)
                self.assertEqual(headers['Cache-Control'], 'no-store')
            run.assert_not_called()
            config.assert_not_called()

    def test_entry_requires_single_correct_token_and_does_not_embed_secret(self):
        for query in ('', '?token=wrong', '?token=' + TOKEN + '&token=' + TOKEN, '?token=%E4%B8%AD'):
            self.assertEqual(self.request('/' + query)[0], 401)
        code, headers, body = self.request('/?token=' + TOKEN)
        self.assertEqual(code, 200)
        self.assertIn('history.replaceState', body)
        self.assertNotIn(TOKEN, body)
        self.assertNotIn('__NONCE__', body)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn("'unsafe-inline'", headers['Content-Security-Policy'])

    def test_api_accepts_bearer_and_rejects_query_tokens(self):
        self.assertEqual(self.request('/api/nodes?token=' + TOKEN)[0], 401)
        code, _, body = self.request('/api/nodes', auth=True)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['data']['nodes'][0]['id'], 'a')
        self.assertEqual(self.request('/api/nodes', headers={'Authorization': 'Bearer wrong'})[0], 401)

    def test_rebinding_cross_origin_and_fetch_metadata_are_rejected(self):
        with patch.object(self.backend, 'run') as run:
            for headers in ({'Host': 'evil.test:8765'}, {'Origin': 'https://evil.test'},
                            {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
                self.assertEqual(self.request('/api/auto', 'POST', {}, headers, True)[0], 403)
            run.assert_not_called()

    def test_forwarded_local_port_origin_works(self):
        headers = {'Host': '127.0.0.1:9876', 'Origin': 'http://127.0.0.1:9876', 'Sec-Fetch-Site': 'same-origin'}
        self.assertEqual(self.request('/api/nodes', headers=headers, auth=True)[0], 200)

    def test_write_request_type_size_and_shape_are_checked(self):
        self.assertEqual(self.request('/api/probe', 'POST', '{}', auth=True)[0], 415)
        self.assertEqual(self.request('/api/probe', 'POST', '[]', {'Content-Type': 'application/json'}, True)[0], 400)
        self.assertEqual(self.request('/api/switch', 'POST', {'address': 'evil.test:21117'}, auth=True)[0], 400)
        code, _, _ = self.request('/api/config', 'POST', '', {'Content-Length': str(MAX_BODY + 1)}, True)
        self.assertEqual(code, 413)
        self.assertEqual(self.request('/api/auto', auth=True)[0], 404)

    def test_config_write_requires_auth_and_checks_revision(self):
        _, _, body = self.request('/api/config', auth=True)
        draft = json.loads(body)['data']
        draft['text'] = CONFIG.replace('tier=10', 'tier=30')
        self.assertEqual(self.request('/api/config', 'POST', draft)[0], 401)
        self.assertEqual(self.request('/api/config', 'POST', draft, auth=True)[0], 200)
        self.assertEqual(self.request('/api/config', 'POST', draft, auth=True)[0], 409)

    def test_requests_never_log_entry_token(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            self.request('/?token=' + TOKEN)
            self.request('/?token=wrong')
        self.assertEqual(stream.getvalue(), '')

    def test_busy_http_operation_returns_conflict(self):
        with command_lock(str(self.state_path) + '.lock'):
            code, _, body = self.request('/api/nodes', auth=True)
        self.assertEqual(code, 409)
        self.assertIn('管理器忙', json.loads(body)['error'])


if __name__ == '__main__':
    unittest.main()
