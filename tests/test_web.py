import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from relay_helper.__main__ import main
from relay_helper.config import load_config
from relay_helper.io import ConsoleError, command_lock, load_state, save_state
from relay_helper.web import (Backend, LocalServer, MAX_BODY, REMEMBER_SECONDS,
                              SESSION_SECONDS, Sessions, load_token, serve)

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

    def test_failed_creation_leaves_no_partial_token_and_can_retry(self):
        for operation in ('fsync', 'link'):
            with self.subTest(operation=operation):
                with patch('relay_helper.web.os.' + operation, side_effect=OSError('interrupted')):
                    with self.assertRaises(OSError):
                        load_token(self.path)
                self.assertFalse(self.path.exists())
                self.assertEqual(list(self.path.parent.iterdir()), [])
                token = load_token(self.path)
                self.assertEqual(load_token(self.path), token)
                self.path.unlink()

    def test_publication_exposes_only_complete_synced_private_token(self):
        publish = os.link
        sync = os.fsync
        with patch('relay_helper.web.os.fsync', wraps=sync) as synced:
            def publish_checked(source, destination):
                self.assertFalse(self.path.exists())
                self.assertEqual(Path(source).stat().st_mode & 0o777, 0o600)
                self.assertRegex(Path(source).read_text(encoding='ascii'), r'^[A-Za-z0-9_-]{43}\n$')
                synced.assert_called_once()
                publish(source, destination)

            with patch('relay_helper.web.os.link', side_effect=publish_checked):
                token = load_token(self.path)
            self.assertEqual(synced.call_count, 2)
        self.assertEqual(self.path.read_text(encoding='ascii'), token + '\n')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_creator_is_reused_without_overwrite(self):
        def another_creator(source, destination):
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                stream.write(TOKEN + '\n')
            raise FileExistsError('another process published first')

        with patch('relay_helper.web.os.link', side_effect=another_creator):
            self.assertEqual(load_token(self.path), TOKEN)
        self.assertEqual(load_token(self.path), TOKEN)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

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


class SessionTests(unittest.TestCase):
    def test_deadlines_are_absolute_and_follow_monotonic_clock(self):
        for remember, seconds in ((False, SESSION_SECONDS), (True, REMEMBER_SECONDS)):
            with self.subTest(remember=remember):
                sessions = Sessions()
                with patch('relay_helper.web.time.monotonic', return_value=100), patch('relay_helper.web.time.time', return_value=1000):
                    code, identifier, session = sessions.login(TOKEN, TOKEN, 'http://localhost:9876', remember)
                self.assertEqual(code, 200)
                self.assertEqual(session.expires_at, 1000 + seconds)
                self.assertNotIn(identifier, sessions.records)
                with patch('relay_helper.web.time.monotonic', return_value=100 + seconds - 1):
                    self.assertEqual(sessions.get(identifier, session.origin), session)
                    self.assertEqual(sessions.get(identifier, session.origin), session)
                with patch('relay_helper.web.time.monotonic', return_value=100 + seconds):
                    self.assertIsNone(sessions.get(identifier, session.origin))
                self.assertEqual(sessions.records, {})

    def test_origin_mismatch_does_not_revoke_valid_session(self):
        sessions = Sessions()
        _, identifier, session = sessions.login(TOKEN, TOKEN, 'http://localhost:9876', False)
        self.assertIsNone(sessions.get(identifier, 'http://localhost:8765'))
        sessions.revoke(identifier, 'http://127.0.0.1:9876')
        self.assertEqual(sessions.get(identifier, session.origin), session)

    def test_parallel_capacity_replacement_and_revocation(self):
        sessions = Sessions()
        origin = 'http://localhost:9876'
        with patch('relay_helper.web.MAX_SESSIONS', 8):
            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(executor.map(lambda _: sessions.login(TOKEN, TOKEN, origin, False), range(24)))
            accepted = [(identifier, session) for code, identifier, session in results if code == 200]
            self.assertEqual(len(accepted), 8)
            self.assertEqual(sum(code == 429 for code, _, _ in results), 16)
            self.assertEqual(len({identifier for identifier, _ in accepted}), 8)
            previous, session = accepted[0]
            code, identifier, new = sessions.login(TOKEN, TOKEN, origin, True, previous)
            self.assertEqual(code, 200)
            self.assertNotEqual(identifier, previous)
            self.assertNotEqual(new.csrf, session.csrf)
            self.assertIsNone(sessions.get(previous, origin))
            self.assertEqual(len(sessions.records), 8)
            with ThreadPoolExecutor(max_workers=16) as executor:
                list(executor.map(lambda _: sessions.revoke(identifier, origin), range(24)))
            self.assertIsNone(sessions.get(identifier, origin))
            self.assertEqual(len(sessions.records), 7)

    def test_failed_logins_are_bounded_and_rate_limit_recovers(self):
        sessions = Sessions()
        origin = 'http://localhost:9876'
        with patch('relay_helper.web.time.monotonic', return_value=100):
            for _ in range(5):
                self.assertEqual(sessions.login('wrong', TOKEN, origin, False)[0], 401)
            for _ in range(10):
                self.assertEqual(sessions.login(TOKEN, TOKEN, origin, False)[0], 429)
            self.assertEqual(len(sessions.failures), 5)
            self.assertEqual(sessions.records, {})
        with patch('relay_helper.web.time.monotonic', return_value=160):
            self.assertEqual(sessions.login(TOKEN, TOKEN, origin, False)[0], 200)


class BackendTests(BackendFixture):

    def test_web_probe_persists_one_count_and_one_point_per_round(self):
        self.config_path.write_text('[policy]\n[node:a]\naddress=a.test:21117\ntier=10\n', encoding='utf-8')
        with patch('relay_helper.service.probe_tcp', return_value=(12, None)) as tcp:
            for count in range(1, 8):
                code, result = self.backend.run('probe')
                self.assertEqual(code, 200)
                health = result['data']['nodes'][0]['health']
                self.assertEqual(health['successes'], count)
                self.assertEqual(len(health['samples']), min(count, 5))
                self.assertEqual(len(health['last_attempts']), 2)
                saved = load_state(self.state_path, load_config(self.config_path))
                self.assertEqual(saved['nodes']['a']['successes'], count)
            self.assertEqual(tcp.call_count, 14)

    def test_web_auto_reselects_from_manual_fallback_during_hold(self):
        self.config_path.write_text('[policy]\n[node:a]\naddress=a.test:21117\ntier=10\n'
                                    '[node:fallback]\naddress=fallback.test:21117\ntier=20\nrtt_ranked=false\n',
                                    encoding='utf-8')
        with patch('relay_helper.service.probe_tcp', return_value=(12, None)), patch('relay_helper.web.Console') as cls:
            cls.return_value.read.return_value = []
            cls.return_value.apply.return_value = True
            self.backend.run('switch', 'fallback')
            cls.return_value.read.return_value = ['fallback.test:21117']
            code, result = self.backend.run('auto')
        self.assertEqual(code, 200)
        self.assertEqual(result['data']['state']['actual'], ['a.test:21117'])
        self.assertFalse(load_state(self.state_path, load_config(self.config_path))['auto_reselect'])

    def test_first_point_ranks_and_status_hides_expired_rtt_preserving_manual_mode(self):
        self.config_path.write_text('[policy]\n[node:a]\naddress=a.test:21117\ntier=10\n', encoding='utf-8')
        with patch('relay_helper.web.time.time', return_value=1000), \
                patch('relay_helper.service.probe_tcp', side_effect=[(800, None), (12, None)]), \
                patch('relay_helper.web.Console') as cls:
            cls.return_value.read.return_value = []
            cls.return_value.apply.return_value = True
            code, result = self.backend.run('switch', 'a')
        self.assertEqual(code, 200)
        node = result['data']['nodes'][0]
        self.assertEqual(node['effective_rtt_ms'], 12)
        self.assertEqual(len(node['health']['samples']), 1)
        self.assertEqual(node['health']['last_attempts'][0]['rtt_ms'], 800)
        with patch('relay_helper.web.time.time', return_value=1120), patch('relay_helper.web.Console') as cls, \
                patch('relay_helper.service.probe_tcp') as tcp:
            cls.return_value.read.return_value = ['a.test:21117']
            code, result = self.backend.run('status')
            cls.return_value.apply.assert_not_called()
            tcp.assert_not_called()
        self.assertEqual(code, 200)
        node = result['data']['nodes'][0]
        self.assertIsNone(node['effective_rtt_ms'])
        self.assertIsNone(node['health']['rtt_ms'])
        self.assertEqual(node['health']['health'], 'unknown')
        self.assertEqual(result['data']['state']['mode'], 'manual')
        self.assertEqual(result['data']['state']['manual_target'], 'a')

    def test_web_switch_probes_only_requested_node(self):
        with self.config_path.open('a', encoding='utf-8') as stream:
            stream.write('[node:b]\naddress=b.test:21117\ntier=20\n')
        with patch('relay_helper.service.probe_tcp', return_value=(12, None)) as tcp, \
                patch('relay_helper.web.Console') as cls:
            cls.return_value.read.return_value = []
            cls.return_value.apply.return_value = True
            code, result = self.backend.run('switch', 'a')
        self.assertEqual(code, 200)
        self.assertEqual([call.args[0].id for call in tcp.call_args_list], ['a', 'a'])
        self.assertEqual(result['data']['nodes'][1]['health']['samples'], [])

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
        self.auth_headers = None

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    @property
    def origin(self):
        return 'http://127.0.0.1:{}'.format(self.server.server_address[1])

    def login(self, remember=False, headers=None):
        headers = dict(headers or {})
        headers.setdefault('Authorization', 'Bearer ' + TOKEN)
        code, response_headers, body = self.request('/api/session/login', 'POST', {'remember': remember}, headers)
        self.assertEqual(code, 200, body)
        data = json.loads(body)['data']
        return {'Cookie': response_headers['Set-Cookie'].split(';')[0], 'X-CSRF-Token': data['csrf']}, data

    def request(self, path, method='GET', body=None, headers=None, auth=False, auto_origin=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        self.addCleanup(connection.close)
        headers = dict(headers or {})
        if auth:
            if self.auth_headers is None:
                self.auth_headers, _ = self.login()
            for key, value in self.auth_headers.items():
                headers.setdefault(key, value)
        if method == 'POST' and auto_origin:
            headers.setdefault('Origin', 'http://' + headers.get('Host', self.origin[7:]))
        if isinstance(body, dict):
            body = json.dumps(body)
            headers.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode('utf-8')

    def test_loopback_binding_and_unauthorized_requests_do_no_work(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with patch.object(self.backend, 'run') as run, patch.object(self.backend, 'config') as config:
            code, _, body = self.request('/')
            self.assertEqual(code, 200)
            self.assertIn('login-form', body)
            self.assertNotIn('config-panel', body)
            for path, method in (('/api/session', 'GET'), ('/api/nodes', 'GET'), ('/api/config', 'GET'), ('/api/auto', 'POST')):
                code, headers, body = self.request(path, method)
                self.assertEqual(code, 401)
                self.assertNotIn('<html', body)
                self.assertEqual(headers['Cache-Control'], 'no-store')
            run.assert_not_called()
            config.assert_not_called()

    def test_legacy_entry_confirms_single_token_without_creating_session(self):
        for query in ('?token=', '?token=wrong', '?token=' + TOKEN + '&token=' + TOKEN,
                      '?token=%E4%B8%AD', '?token=' + TOKEN + '&extra=1', '?'):
            self.assertEqual(self.request('/' + query)[0], 401)
        code, headers, body = self.request('/?token=' + TOKEN)
        self.assertEqual(code, 200)
        self.assertIn('history.replaceState', body)
        self.assertIn('login-form', body)
        self.assertNotIn('config-panel', body)
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(self.server.sessions.records, {})
        self.assertNotIn(TOKEN, body)
        self.assertNotIn('__NONCE__', body)
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn("'unsafe-inline'", headers['Content-Security-Policy'])

    def test_api_accepts_session_and_rejects_bearer_and_query_tokens(self):
        self.assertEqual(self.request('/api/nodes?token=' + TOKEN)[0], 401)
        code, _, body = self.request('/api/nodes', auth=True)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['data']['nodes'][0]['id'], 'a')
        self.assertEqual(self.request('/api/nodes', headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request('/api/nodes', headers={'Authorization': 'Bearer ' + TOKEN})[0], 401)
        self.assertEqual(self.request('/api/nodes?', auth=True)[0], 401)

    def test_rebinding_cross_origin_and_fetch_metadata_are_rejected(self):
        with patch.object(self.backend, 'run') as run:
            for headers in ({'Host': 'evil.test:8765'}, {'Origin': 'https://evil.test'},
                            {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
                self.assertEqual(self.request('/api/auto', 'POST', {}, headers, True)[0], 403)
            run.assert_not_called()

    def test_forwarded_local_port_origin_works(self):
        headers = {'Host': '127.0.0.1:9876', 'Origin': 'http://127.0.0.1:9876', 'Sec-Fetch-Site': 'same-origin'}
        auth, _ = self.login(headers=headers)
        self.assertEqual(self.request('/api/nodes', headers=dict(headers, **auth))[0], 200)
        self.assertEqual(self.request('/api/nodes', headers=auth)[0], 401)
        self.assertEqual(self.request('/api/nodes', headers=dict(headers, **auth))[0], 200)

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

    def test_form_mapping_requires_auth_and_never_writes_files(self):
        self.assertEqual(self.request('/api/config/parse', 'POST', {'text': CONFIG})[0], 401)
        code, _, body = self.request('/api/config/parse', 'POST', {'text': CONFIG}, auth=True)
        self.assertEqual(code, 200)
        model = json.loads(body)['data']
        model['policy']['recover_after'] = 4
        draft = {'text': CONFIG, 'model': model}
        self.assertEqual(self.request('/api/config/render', 'POST', draft)[0], 401)
        with command_lock(str(self.state_path) + '.lock'):
            code, _, body = self.request('/api/config/render', 'POST', draft, auth=True)
        self.assertEqual(code, 200)
        self.assertIn('recover_after=4', json.loads(body)['data']['text'])
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(Path(str(self.config_path) + '.webui.bak').exists())

    def test_invalid_form_drafts_return_errors_and_leave_disk_unchanged(self):
        for body in ({'text': '[policy]\n'}, {'text': 1}, {'text': CONFIG, 'extra': True}):
            self.assertEqual(self.request('/api/config/parse', 'POST', body, auth=True)[0], 400)
        self.assertEqual(self.request('/api/config/render', 'POST', {'text': CONFIG, 'model': {}}, auth=True)[0], 400)
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)

    def test_login_cookie_refresh_and_session_response_hide_credentials(self):
        for remember in (False, True):
            with self.subTest(remember=remember):
                code, headers, body = self.request('/api/session/login', 'POST', {'remember': remember},
                                                  {'Authorization': 'Bearer ' + TOKEN})
                self.assertEqual(code, 200)
                cookie = headers['Set-Cookie']
                self.assertIn('; Path=/; HttpOnly; SameSite=Strict', cookie)
                self.assertNotIn('Domain=', cookie)
                self.assertNotIn('Secure', cookie)
                self.assertEqual('Max-Age=' in cookie, remember)
                if remember:
                    self.assertIn('Max-Age=' + str(REMEMBER_SECONDS), cookie)
                self.assertNotIn('Expires=', cookie)
                auth = {'Cookie': cookie.split(';')[0]}
                identifier = auth['Cookie'].split('=')[1]
                self.assertNotIn(TOKEN, cookie + body)
                self.assertNotIn(identifier, body)
                code, _, page = self.request('/', headers=auth)
                self.assertEqual(code, 200)
                self.assertIn('config-panel', page)
                code, _, status = self.request('/api/session', headers=auth)
                self.assertEqual(code, 200)
                data = json.loads(status)['data']
                self.assertEqual(data['remember'], remember)
                self.assertEqual(data['expires_at'] - data['created_at'], REMEMBER_SECONDS if remember else SESSION_SECONDS)
                self.assertNotIn(TOKEN, page + status)
                self.assertNotIn(identifier, page + status)
                self.assertNotIn('Authorization', page.split('async function request')[1].split('function controls')[0])
                self.assertEqual(self.request('/api/nodes', headers=auth)[0], 200)
                self.assertEqual(self.request('/', headers=auth)[0], 200)

    def test_unknown_expired_and_restarted_sessions_do_no_backend_work(self):
        auth, _ = self.login(remember=True)
        with patch.object(self.backend, 'run') as run, patch.object(self.backend, 'config') as config:
            fake = {'Cookie': self.server.cookie_name + '=' + 'x' * 43}
            self.assertEqual(self.request('/api/nodes', headers=fake)[0], 401)
            deadline = max(session.deadline for session in self.server.sessions.records.values())
            with patch('relay_helper.web.time.monotonic', return_value=deadline):
                self.assertEqual(self.request('/api/nodes', headers=auth)[0], 401)
                self.assertIn('login-form', self.request('/', headers=auth)[2])
            for remember in (False, True):
                auth, _ = self.login(remember)
                port = self.server.server_address[1]
                cookie_name = self.server.cookie_name
                self.stop_server()
                self.server = LocalServer(port, TOKEN, self.backend)
                self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
                self.thread.start()
                self.assertEqual(self.server.cookie_name, cookie_name)
                self.assertEqual(self.request('/api/session', headers=auth)[0], 401)
                self.assertEqual(self.request('/api/config', headers=auth)[0], 401)
                self.assertIn('login-form', self.request('/', headers=auth)[2])
            run.assert_not_called()
            config.assert_not_called()
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)
        self.assertFalse(self.state_path.exists())
        new, _ = self.login()
        self.assertNotEqual(new['Cookie'], auth['Cookie'])
        self.assertEqual(self.request('/api/nodes', headers=new)[0], 200)
        self.assertEqual(self.request('/api/nodes', headers=auth)[0], 401)

    def test_logout_revokes_replay_preserves_other_browser_and_clears_cookie(self):
        auth, _ = self.login()
        other, _ = self.login()
        self.assertNotEqual(auth['Cookie'], other['Cookie'])
        self.assertEqual(self.request('/api/session/logout', 'GET', headers=auth)[0], 404)
        code, headers, _ = self.request('/api/session/logout', 'POST', {}, auth)
        self.assertEqual(code, 200)
        self.assertEqual(headers['Set-Cookie'], self.server.cookie_name + '=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
        self.assertEqual(self.request('/api/session', headers=auth)[0], 401)
        self.assertIn('login-form', self.request('/', headers=auth)[2])
        self.assertEqual(self.request('/api/nodes', headers=other)[0], 200)

    def test_login_needs_bearer_and_boolean_json_and_has_no_business_effect(self):
        with patch.object(self.backend, 'run') as run, patch.object(self.backend, 'config') as config:
            for authorization in ('Bearer wrong', 'Basic ' + TOKEN, 'Bearer \xe9', ''):
                self.assertEqual(self.request('/api/session/login', 'POST', {'remember': False},
                                             {'Authorization': authorization})[0], 401)
            for body in ({}, {'remember': 1}, {'remember': 'false'}, {'remember': None}, {'remember': False, 'token': TOKEN}):
                self.assertEqual(self.request('/api/session/login', 'POST', body, {'Authorization': 'Bearer ' + TOKEN})[0], 400)
            self.assertEqual(self.server.sessions.records, {})
            run.assert_not_called()
            config.assert_not_called()

    def test_missing_origin_and_wrong_csrf_reject_login_and_all_writes(self):
        auth, _ = self.login()
        paths = ('/api/session/logout', '/api/probe', '/api/switch', '/api/auto', '/api/config',
                 '/api/config/parse', '/api/config/render')
        with patch.object(self.backend, 'run') as run, patch.object(self.backend, 'config') as config:
            self.assertEqual(self.request('/api/session/login', 'POST', {'remember': False},
                                         {'Authorization': 'Bearer ' + TOKEN}, auto_origin=False)[0], 403)
            for path in paths:
                self.assertEqual(self.request(path, 'POST', {}, auth, auto_origin=False)[0], 403)
                for csrf in (None, 'wrong', '\xe9'):
                    headers = {'Cookie': auth['Cookie']}
                    if csrf is not None:
                        headers['X-CSRF-Token'] = csrf
                    self.assertEqual(self.request(path, 'POST', {}, headers)[0], 403)
            run.assert_not_called()
            config.assert_not_called()
        self.assertEqual(self.request('/api/nodes', headers=auth)[0], 200)

    def test_duplicate_cookie_headers_and_auth_headers_are_rejected(self):
        auth, _ = self.login()
        self.assertEqual(self.request('/api/nodes', headers={'Cookie': auth['Cookie'] + '; ' + auth['Cookie']})[0], 401)
        self.assertEqual(self.request('/api/nodes', headers={'Cookie': auth['Cookie'] + '; ' + self.server.cookie_name + '=wrong'})[0], 401)
        for name in ('Host', 'Origin', 'X-CSRF-Token', 'Cookie', 'Authorization', 'Content-Length'):
            with self.subTest(header=name):
                path = '/api/session/login' if name == 'Authorization' else '/api/session/logout'
                content = json.dumps({'remember': False} if path.endswith('login') else {}).encode('ascii')
                headers = dict(auth, Host=self.origin[7:], Origin=self.origin,
                               Authorization='Bearer ' + TOKEN, **{'Content-Type': 'application/json', 'Content-Length': str(len(content))})
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                self.addCleanup(connection.close)
                connection.putrequest('POST', path, skip_host=True)
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.putheader(name, headers[name])
                connection.endheaders(content)
                response = connection.getresponse()
                self.assertIn(response.status, (400, 401, 403))
                response.read()
        self.assertEqual(self.request('/api/nodes', headers=auth)[0], 200)

    def test_host_ports_cross_origin_and_metadata_guard_login_too(self):
        for headers in ({'Host': 'localhost:0'}, {'Host': 'localhost:65536'}, {'Host': 'localhost:123456'},
                        {'Host': 'evil.test:8765'}, {'Origin': 'null'}, {'Origin': 'https://localhost:8765'},
                        {'Origin': 'http://127.0.0.1:9876'}, {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            self.assertEqual(self.request('/api/session/login', 'POST', {'remember': False},
                                         dict(headers, Authorization='Bearer ' + TOKEN))[0], 403)
        self.assertEqual(self.server.sessions.records, {})

    def test_session_cookie_is_distinct_for_different_instances(self):
        with LocalServer(0, TOKEN, self.backend) as other:
            self.assertNotEqual(other.cookie_name, self.server.cookie_name)


if __name__ == '__main__':
    unittest.main()
