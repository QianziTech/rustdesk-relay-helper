"""Optional browser regression: python tests/browser_login.py (Playwright + Chromium)."""

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_helper.web import Backend, LocalServer, REMEMBER_SECONDS
from relay_helper.config import load_config
from relay_helper.io import load_state

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

TOKEN = 'browser_test_only_' + 'x' * 32
CONFIG = '[policy]\nrecover_after=1\nrtt_sample_count=1\n[node:a]\naddress=a.test:21117\ntier=10\n'


@unittest.skipUnless(sync_playwright and shutil.which('chromium'), 'requires Playwright and Chromium')
class BrowserLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / 'config.ini'
        self.config_path.write_text(CONFIG, encoding='utf-8')
        self.backend = Backend(self.config_path, Path(self.tmp.name) / 'state.json')
        console = patch('relay_helper.web.Console')
        fake = console.start().return_value
        self.console = fake
        self.addCleanup(console.stop)
        fake.read.return_value = ['a.test:21117']
        fake.apply.return_value = True
        probe = patch('relay_helper.service.probe_tcp', return_value=(12, None))
        self.tcp = probe.start()
        self.addCleanup(probe.stop)
        self.start_server()
        self.addCleanup(self.stop_server)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(executable_path=shutil.which('chromium'),
                                                       headless=True, args=['--no-sandbox'])
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def start_server(self, port=0):
        self.server = LocalServer(port, TOKEN, self.backend)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:{}'.format(self.server.server_address[1])

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def restart_server(self):
        port = self.server.server_address[1]
        self.stop_server()
        self.start_server(port)

    def login(self, page=None):
        page = page or self.page
        page.locator('#login-token').fill(TOKEN)
        page.locator('#login-submit').click()
        expect(page.locator('#config-panel')).to_be_visible()
        expect(page.locator('#refresh')).to_be_enabled()

    def test_confirm_login_refresh_logout_and_remembered_restart(self):
        page = self.page
        page.goto(self.url)
        self.assertEqual(page.locator('#config-panel').count(), 0)
        page.locator('#login-token').fill('wrong')
        page.locator('#login-submit').click()
        expect(page.locator('#notice')).to_contain_text('无效访问 token')
        self.assertEqual(self.server.sessions.records, {})
        page.goto(self.url + '/?token=' + TOKEN)
        expect(page).to_have_url(self.url + '/')
        expect(page.locator('#login-token')).to_have_value(TOKEN)
        expect(page.locator('#login-remember')).not_to_be_checked()
        self.assertEqual(self.server.sessions.records, {})
        self.login()
        cookies = self.context.cookies()
        self.assertEqual(len(cookies), 1)
        self.assertTrue(cookies[0]['httpOnly'])
        self.assertEqual(cookies[0]['sameSite'], 'Strict')
        self.assertEqual(cookies[0]['expires'], -1)
        self.assertEqual(page.evaluate('document.cookie'), '')
        self.assertEqual(page.evaluate('[localStorage.length, sessionStorage.length]'), [0, 0])
        page.reload()
        expect(page.locator('#refresh')).to_be_enabled()
        tab = self.context.new_page()
        tab.goto(self.url)
        expect(tab.locator('#refresh')).to_be_enabled()
        independent = self.browser.new_context()
        other = independent.new_page()
        other.goto(self.url)
        self.login(other)
        page.locator('#logout').click()
        expect(page.locator('#login-submit')).to_be_visible()
        self.assertEqual(self.context.cookies(), [])
        tab.locator('#refresh').click()
        expect(tab.locator('#login-panel')).to_be_visible()
        other.reload()
        expect(other.locator('#refresh')).to_be_enabled()
        page.locator('#login-remember').check()
        self.login()
        cookie = self.context.cookies()[0]
        expires = page.evaluate('Date.now() / 1000') + REMEMBER_SECONDS
        self.assertLess(abs(cookie['expires'] - expires), 10)
        page.reload()
        expect(page.locator('#refresh')).to_be_enabled()
        self.restart_server()
        page.reload()
        expect(page.locator('#login-submit')).to_be_visible()
        expect(page.locator('#login-token')).to_have_value('')
        expect(page.locator('#login-remember')).not_to_be_checked()
        self.assertEqual(page.locator('#config-panel').count(), 0)
        self.login()
        self.assertEqual(self.errors, [])

    def test_reauthentication_keeps_form_and_text_drafts_without_replaying_writes(self):
        page = self.page
        requests = []
        page.on('request', lambda request: requests.append((urlsplit(request.url).path, request.method, request.headers)))
        page.goto(self.url)
        self.login()
        page.locator('#config-panel summary').click()
        tier = page.locator('[data-field=tier]')
        expect(tier).to_have_value('10')
        tier.fill('21')
        self.restart_server()
        page.locator('#preview-config').click()
        expect(page.locator('#login-panel')).to_be_visible()
        expect(tier).to_have_value('21')
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)
        count = len(requests)
        self.login()
        expect(page.locator('#login-panel')).to_be_hidden()
        expect(tier).to_have_value('21')
        self.assertEqual([path for path, _, _ in requests[count:]], ['/api/session/login'])
        page.locator('#preview-config').click()
        expect(page.locator('#diff')).to_contain_text('tier=21')
        page.locator('#text-mode').click()
        draft = page.locator('#config-text').input_value() + '# retained draft\n'
        page.locator('#config-text').fill(draft)
        self.restart_server()
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#save-config').click()
        expect(page.locator('#login-panel')).to_be_visible()
        expect(page.locator('#config-text')).to_have_value(draft)
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)
        count = len(requests)
        self.login()
        expect(page.locator('#config-text')).to_have_value(draft)
        self.assertEqual([path for path, _, _ in requests[count:]], ['/api/session/login'])
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), CONFIG)
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#save-config').click()
        expect(page.locator('#notice')).to_contain_text('配置已校验并保存')
        self.assertEqual(self.config_path.read_text(encoding='utf-8'), draft)
        for path, method, headers in requests:
            if path.startswith('/api/') and path != '/api/session/login':
                self.assertNotIn('authorization', headers)
                if method == 'POST':
                    self.assertIn('x-csrf-token', headers)
        self.assertEqual(self.errors, [])

    def test_probe_counts_rounds_and_failed_healthy_node_can_retry_switch(self):
        self.config_path.write_text('[policy]\n[node:a]\naddress=a.test:21117\ntier=10\n', encoding='utf-8')
        page = self.page
        page.goto(self.url)
        self.login()
        for count in (1, 2):
            page.locator('#probe').click()
            expect(page.locator('#nodes tr').locator('td').nth(3)).to_have_text(f'{count} / 0')
            expect(page.locator('#notice')).to_contain_text('探测已完成')
            state = load_state(self.backend.state_path, load_config(self.config_path))
            self.assertEqual(len(state['nodes']['a']['samples']), count)
            self.assertEqual(self.tcp.call_count, 2 * count)
        self.tcp.side_effect = [(12, None), (None, 'temporary timeout')]
        page.locator('#probe').click()
        expect(page.locator('#nodes tr').locator('td').nth(3)).to_have_text('0 / 1')
        expect(page.locator('#nodes tr button')).to_be_enabled()
        self.tcp.side_effect = None
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#nodes tr button').click()
        expect(page.locator('#notice')).to_contain_text('手动模式已应用')
        expect(page.locator('#mode')).to_have_text('手动固定')
        expect(page.locator('#nodes tr').locator('td').nth(3)).to_have_text('1 / 0')
        self.assertEqual(self.tcp.call_count, 8)
        self.assertEqual(self.errors, [])

    def test_tier_sorting_during_edit_preserves_focus_and_same_tier_order(self):
        text = ('[policy]\n# keep policy comment\n'
                '[node:b]\naddress=b.test:21117\ntier=30\n'
                '[node:c]\naddress=c.test:21117\ntier=10\n'
                '[node:a]\naddress=a.test:21117\ntier=20\n'
                '[node:d]\naddress=d.test:21117\ntier=10\nenabled=false\n')
        self.config_path.write_text(text, encoding='utf-8')
        page = self.page
        page.goto(self.url)
        self.login()
        expect(page.locator('#nodes tr').locator('td:first-child')).to_have_text([
            'cc.test:21117', 'dd.test:21117', 'aa.test:21117', 'bb.test:21117'])
        page.locator('#config-panel summary').click()
        ids = page.locator('#node-fields [data-field=id]')
        expect(ids.nth(3)).to_have_value('b')
        order = lambda: ids.evaluate_all('(inputs) => inputs.map(input => input.value)')
        self.assertEqual(order(), ['c', 'd', 'a', 'b'])
        tier = page.locator('#node-fields [data-field=tier]').nth(3).element_handle()
        tier.fill('5')
        self.assertEqual(order(), ['b', 'c', 'd', 'a'])
        self.assertTrue(tier.evaluate('(input) => document.activeElement === input'))
        tier.fill('')
        self.assertEqual(order(), ['b', 'c', 'd', 'a'])
        tier.fill('40')
        self.assertEqual(order(), ['c', 'd', 'a', 'b'])
        self.assertTrue(tier.evaluate('(input) => document.activeElement === input'))
        page.locator('#node-fields [data-action=down]').nth(0).click()
        self.assertEqual(order(), ['d', 'c', 'a', 'b'])
        expect(page.locator('#node-fields [data-action=down]').nth(1)).to_be_disabled()
        page.locator('#add-node').click()
        self.assertEqual(order(), ['d', 'c', 'a', '', 'b'])
        new = page.locator('#node-fields .node-form').nth(3)
        new.locator('[data-field=id]').fill('new')
        new.locator('[data-field=address]').fill('new.test:21117')
        new.locator('[data-field=tier]').fill('0')
        self.assertEqual(order(), ['new', 'd', 'c', 'a', 'b'])
        page.locator('#text-mode').click()
        expect(page.locator('#config-text')).to_be_visible()
        draft = page.locator('#config-text').input_value()
        self.assertIn('# keep policy comment', draft)
        self.assertEqual([node.id for node in load_config(self.config_path).nodes], ['b', 'c', 'a', 'd'])
        page.locator('#form-mode').click()
        expect(ids.nth(0)).to_have_value('new')
        self.assertEqual(order(), ['new', 'd', 'c', 'a', 'b'])
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#save-config').click()
        expect(page.locator('#notice')).to_contain_text('配置已校验并保存')
        self.assertEqual([node.id for node in load_config(self.config_path).nodes], ['new', 'd', 'c', 'a', 'b'])
        page.locator('#load-config').click()
        expect(page.locator('#notice')).to_contain_text('配置已载入')
        self.assertEqual(order(), ['new', 'd', 'c', 'a', 'b'])
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
