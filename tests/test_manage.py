"""Deployment transactions tested in private directories with a fake systemd."""

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('installer', Path(__file__).resolve().parents[1] / 'deploy/manage.py')
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


class FakeSystemd:
    def __init__(self):
        self.states = {u: {'active': False, 'enabled': False} for u in manager.UNITS}
        self.calls = []
        self.fail_start = False
        self.home = '/opt/rustdesk-relay-helper'

    def properties(self, unit):
        state = self.states[unit]
        return {'LoadState': 'loaded' if (manager.UNIT_DIR / unit).exists() else 'not-found',
                'ActiveState': 'active' if state['active'] else 'inactive',
                'UnitFileState': 'enabled' if state['enabled'] else 'disabled',
                'FragmentPath': str(manager.UNIT_DIR / unit), 'WorkingDirectory': self.home,
                'DropInPaths': ''}

    def snapshot(self):
        return {u: dict(s) for u, s in self.states.items()}

    def call(self, operation, *units, check=True):
        self.calls.append((operation, *units))
        for unit in units:
            if unit not in self.states:
                continue
            if operation == 'stop':
                self.states[unit]['active'] = False
            elif operation == 'start':
                if self.fail_start:
                    self.fail_start = False
                    raise RuntimeError('injected startup failure')
                self.states[unit]['active'] = unit != manager.UNITS[0]
            elif operation in ('enable', 'disable'):
                self.states[unit]['enabled'] = operation == 'enable'
        return ''

    quiesce = manager.Systemd.quiesce
    restore = manager.Systemd.restore


@unittest.skipUnless(sys.platform == 'linux', '安装管理仅支持 Linux')
class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / 'program with spaces'
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, relative in (('CONFIG_DIR', 'etc/config'), ('STATE_DIR', 'var/state'),
                               ('UNIT_DIR', 'etc/systemd'), ('LOCK_DIR', 'run/manager'), ('ENTRY', 'bin/helper')):
            value = self.root / relative
            self.stack.enter_context(patch.object(manager, name, value))
            (value.parent if name == 'ENTRY' else value).mkdir(parents=True, exist_ok=True)
        # Actual mutation stays in an unprivileged temp tree; security validation has separate tests.
        self.stack.enter_context(patch.object(manager, 'secure_path', side_effect=lambda p, **kw: Path(p)))
        self.systemd = FakeSystemd()
        self.m = manager.Manager(self.systemd)
        self.files = manager.payload(manager.SOURCE)
        self.version = self.m.prepare(self.prefix, self.files)
        (manager.CONFIG_DIR / 'config.ini').write_text('[policy]\n[node:a]\naddress=localhost:21117\ntier=10\n')
        self.python = str(Path('/usr/bin/python3').resolve())
        self.interval, self.ready = self.m.validate(self.prefix / 'releases' / self.version, self.python)

    def install(self, states=None):
        states = states or self.systemd.snapshot()
        return self.m.transaction(None, self.prefix, self.version, self.python, 8765, self.interval, states)

    def new_version(self):
        files = dict(self.files)
        files['relay_helper/__init__.py'] += b'\n# new version\n'
        return self.m.prepare(self.prefix, files)

    def test_first_install_does_not_enable_timer_or_write_business_state(self):
        record = self.install()
        self.assertEqual(self.m.read_record()['current'], self.version)
        self.assertIsNone(record['previous'])
        self.assertFalse((manager.STATE_DIR / 'state.json').exists())
        self.assertFalse(self.systemd.states[manager.UNITS[1]]['enabled'])
        self.assertNotIn(('start', manager.UNITS[0]), self.systemd.calls)
        self.assertEqual(os.readlink(self.prefix / 'current'), 'releases/' + self.version)
        self.assertEqual((manager.CONFIG_DIR / 'install.json').stat().st_mode & 0o777, 0o600)

    def test_update_restarts_web_and_timer_preserves_state_and_token(self):
        states = self.systemd.snapshot()
        for unit in (manager.UNITS[1], manager.UNITS[2]):
            states[unit] = {'active': True, 'enabled': True}
        record = self.install(states)
        business = b'{"mode":"manual","manual_target":"a","nodes":{},"last_switch":0}'
        (manager.STATE_DIR / 'state.json').write_bytes(business)
        token = b'a' * 43
        (manager.STATE_DIR / 'webui.token').write_bytes(token)
        new = self.new_version()
        self.systemd.calls.clear()
        result = self.m.transaction(record, self.prefix, new, self.python, 8765, 20,
                                    self.systemd.snapshot(), reconcile=True)
        self.assertEqual(result['previous'], self.version)
        self.assertEqual((manager.STATE_DIR / 'state.json').read_bytes(), business)
        self.assertEqual((manager.STATE_DIR / 'webui.token').read_bytes(), token)
        for unit in (manager.UNITS[1], manager.UNITS[2]):
            self.assertLess(self.systemd.calls.index(('stop', unit)), self.systemd.calls.index(('start', unit)))
            self.assertTrue(self.systemd.states[unit]['enabled'])
        self.assertIn(('start', manager.UNITS[0]), self.systemd.calls)
        self.assertFalse(self.m.pending.exists())

    def test_start_failure_restores_program_units_and_service_states(self):
        states = self.systemd.snapshot()
        states[manager.UNITS[2]] = {'active': True, 'enabled': True}
        record = self.install(states)
        old_entry = manager.ENTRY.read_bytes()
        new = self.new_version()
        self.systemd.fail_start = True
        with self.assertRaisesRegex(RuntimeError, 'startup failure'):
            self.m.transaction(record, self.prefix, new, self.python, 8765, 30, self.systemd.snapshot())
        self.assertEqual(self.m.read_record()['current'], self.version)
        self.assertEqual(manager.ENTRY.read_bytes(), old_entry)
        self.assertEqual(os.readlink(self.prefix / 'current'), 'releases/' + self.version)
        self.assertTrue(self.systemd.states[manager.UNITS[2]]['active'])
        self.assertFalse(self.m.pending.exists())

    def test_first_install_failure_removes_new_registration_and_link(self):
        states = self.systemd.snapshot()
        states[manager.UNITS[2]] = {'active': True, 'enabled': True}
        # No old service was actually running: simulate new Web start request, then its failure.
        self.systemd.fail_start = True
        with self.assertRaises(RuntimeError):
            self.install(states)
        self.assertFalse(self.m.record.exists())
        self.assertFalse((self.prefix / 'current').is_symlink())
        self.assertFalse(manager.ENTRY.exists())
        self.assertFalse(self.m.pending.exists())

    def test_busy_shared_lock_restores_stopped_services_without_publishing(self):
        record = self.install()
        new = self.new_version()
        with manager.lock(manager.STATE_DIR / 'state.json.lock'):
            with self.assertRaisesRegex(RuntimeError, '管理器忙'):
                self.m.transaction(record, self.prefix, new, self.python, 8765, 20, self.systemd.snapshot())
        self.assertEqual(self.m.read_record()['current'], self.version)
        self.assertFalse(self.m.pending.exists())

    def test_incompatible_config_blocks_before_publication(self):
        self.install()
        (manager.CONFIG_DIR / 'config.ini').write_text('[policy]\nrtt_sample_count=100\n[node:a]\naddress=a:21117\ntier=10\n')
        with self.assertRaises(subprocess.CalledProcessError):
            self.m.validate(self.prefix / 'releases' / self.version, self.python)
        self.assertEqual(self.m.read_record()['current'], self.version)

    def test_placeholder_is_not_ready_and_does_not_create_state(self):
        (manager.CONFIG_DIR / 'config.ini').write_bytes(self.files['config/relay-helper.example.ini'])
        self.assertFalse(self.m.validate(self.prefix / 'releases' / self.version, self.python, True)[1])
        with self.assertRaises(ValueError):
            self.m.validate(self.prefix / 'releases' / self.version, self.python)
        self.assertFalse((manager.STATE_DIR / 'state.json').exists())

    def test_legacy_readme_without_web_is_recognized_custom_unit_rejected(self):
        home = self.root / 'legacy'
        (home / 'relay_helper').mkdir(parents=True)
        (home / 'relay_helper/__main__.py').write_text('')
        self.systemd.home = str(home)
        manager.ENTRY.write_bytes((manager.SOURCE / 'bin/rustdesk-relay-helper').read_bytes())
        for unit in manager.UNITS[:2]:
            text = (manager.SOURCE / 'deploy/systemd' / unit).read_text().replace('/opt/rustdesk-relay-helper', str(home))
            (manager.UNIT_DIR / unit).write_text(text)
        self.assertEqual(self.m.legacy(), home)
        with (manager.UNIT_DIR / manager.UNITS[0]).open('a') as stream:
            stream.write('\nEnvironment=RELAY_HELPER_STATE=/somewhere\n')
        with self.assertRaisesRegex(ValueError, 'README 模板'):
            self.m.legacy()

    def test_purge_refuses_unknown_files(self):
        self.install()
        (manager.STATE_DIR / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(ValueError, '无关文件'):
            self.m.check_purge()

    def test_record_rejects_arbitrary_deletion_targets(self):
        record = self.install()
        record['deployment']['/etc/passwd'] = 'anything'
        self.m.record.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, '清单无效'):
            self.m.read_record()

    def test_tampered_release_and_unknown_file_block_uninstall(self):
        self.install()
        release = self.prefix / 'releases' / self.version
        (release / 'relay_helper/config.py').write_text('broken')
        with self.assertRaisesRegex(ValueError, '程序文件损坏'):
            self.m.verify_release(release)
        (release / 'relay_helper/config.py').write_bytes(self.files['relay_helper/config.py'])
        (release / 'unrelated').write_text('keep')
        with patch.object(manager, 'confirm', side_effect=[True, False]):
            with self.assertRaisesRegex(ValueError, '无关文件'):
                self.m.uninstall()
        self.assertTrue(self.m.record.exists())
        self.assertEqual((release / 'unrelated').read_text(), 'keep')

    def test_rollback_preserves_current_data_and_swaps_previous(self):
        record = self.install()
        new = self.new_version()
        record = self.m.transaction(record, self.prefix, new, self.python, 8765, 20, self.systemd.snapshot())
        data = (manager.CONFIG_DIR / 'config.ini').read_bytes()
        with patch.object(manager, 'confirm', return_value=True):
            self.m.rollback()
        self.assertEqual(self.m.read_record()['current'], self.version)
        self.assertEqual(self.m.read_record()['previous'], new)
        self.assertEqual((manager.CONFIG_DIR / 'config.ini').read_bytes(), data)

    def test_uninstall_preserves_business_data_and_download_source(self):
        self.install()
        config = (manager.CONFIG_DIR / 'config.ini').read_bytes()
        with patch.object(manager, 'confirm', side_effect=[True, False]):
            self.m.uninstall()
        self.assertFalse(manager.ENTRY.exists())
        self.assertFalse(self.prefix.exists())
        self.assertEqual((manager.CONFIG_DIR / 'config.ini').read_bytes(), config)
        self.assertTrue((manager.SOURCE / 'manage.sh').exists())

    def test_interrupted_uninstall_recovers_program_and_registration(self):
        record = self.install()
        with manager.lock(manager.STATE_DIR / 'state.json.lock'):
            pending = self.m.backup(self.systemd.snapshot(), record)
            release = self.prefix / 'releases' / self.version
            names = set(manager.REQUIRED_FILES) | {'release.json'}
            pending['uninstall_versions'] = {self.version: sorted(names)}
            folder = manager.STATE_DIR / 'backups' / pending['backup']
            for name in names:
                destination = folder / 'programs' / self.version / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((release / name).read_bytes())
            (release / 'manage.sh').unlink()
            manager.ENTRY.unlink()
            self.m.record.unlink()
            self.m.restore_backup(pending)
        self.assertEqual(self.m.read_record()['current'], self.version)
        self.m.verify_release(release)
        self.assertTrue(manager.ENTRY.exists())

    def test_purge_clears_only_known_files(self):
        self.install()
        with patch.object(manager, 'confirm', side_effect=[True, True]):
            self.m.uninstall()
        self.assertFalse(manager.CONFIG_DIR.exists())
        self.assertFalse(manager.STATE_DIR.exists())

    def test_installed_entry_runs_from_other_directory_and_ignores_pythonpath(self):
        self.install()
        result = subprocess.run(['bash', str(manager.ENTRY), 'help'], cwd=self.root,
                                env={**os.environ, 'PYTHONPATH': '/nonexistent', 'PYTHONHOME': '/nonexistent'},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reconcile', result.stdout)
        self.assertFalse(list((self.prefix / 'releases').rglob('__pycache__')))

    def test_repair_rebuilds_missing_entry_and_preserves_configuration(self):
        self.install()
        manager.ENTRY.unlink()
        config = (manager.CONFIG_DIR / 'config.ini').read_bytes()
        with patch.object(manager, 'confirm', return_value=True):
            self.m.repair()
        self.assertTrue(manager.ENTRY.exists())
        self.assertEqual((manager.CONFIG_DIR / 'config.ini').read_bytes(), config)

    def test_recover_quiescing_stage_restores_services_without_replacing_program(self):
        states = self.systemd.snapshot()
        states[manager.UNITS[2]] = {'active': True, 'enabled': True}
        self.install(states)
        record = self.m.read_record()
        self.m.pending.write_text(json.dumps({'schema': 1, 'stage': 'quiescing', 'states': states}))
        self.systemd.quiesce()
        with patch.object(manager, 'confirm', return_value=True):
            self.m.recover()
        self.assertEqual(self.m.read_record(), record)
        self.assertTrue(self.systemd.states[manager.UNITS[2]]['active'])
        self.assertFalse(self.m.pending.exists())

    def test_interrupted_current_link_publication_can_be_retried(self):
        self.install()
        (self.prefix / '.current-new').symlink_to('releases/' + self.version)
        self.m.current_link(self.prefix, self.version)
        self.assertFalse((self.prefix / '.current-new').is_symlink())
        self.assertEqual(os.readlink(self.prefix / 'current'), 'releases/' + self.version)

    def test_optional_reconcile_precedes_timer_start(self):
        states = self.systemd.snapshot()
        states[manager.UNITS[1]] = {'active': True, 'enabled': True}
        record = self.install(states)
        self.systemd.calls.clear()
        self.m.transaction(record, self.prefix, self.new_version(), self.python, 8765, 20,
                           self.systemd.snapshot(), reconcile=True)
        self.assertLess(self.systemd.calls.index(('start', manager.UNITS[0])),
                        self.systemd.calls.index(('start', manager.UNITS[1])))


@unittest.skipUnless(sys.platform == 'linux', '安装管理仅支持 Linux')
class BoundaryTests(unittest.TestCase):
    @unittest.skipUnless(manager.shutil.which('systemd-analyze'), 'systemd-analyze 不可用')
    def test_generated_units_pass_systemd_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = []
            for name, data in manager.deployment(Path('/usr/local/lib/helper with spaces'),
                                                 str(Path('/usr/bin/python3').resolve()), 20, 8765).items():
                if name.endswith(('.service', '.timer')):
                    path = root / Path(name).name
                    path.write_bytes(data)
                    units.append(str(path))
            result = subprocess.run(['systemd-analyze', 'verify', *units], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_world_writable_parent_and_relative_paths_rejected(self):
        with self.assertRaisesRegex(ValueError, '不可写'):
            manager.secure_path(Path('/tmp/installer-should-not-create'))
        with self.assertRaisesRegex(ValueError, '绝对路径'):
            manager.secure_path(Path('relative'))

    def test_archive_rejects_traversal_and_links(self):
        import io
        import tarfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.tar'
            for name, member_type in (('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)):
                with tarfile.open(path, 'w') as archive:
                    member = tarfile.TarInfo(name)
                    member.type = member_type
                    member.size = 1 if member_type == tarfile.REGTYPE else 0
                    archive.addfile(member, io.BytesIO(b'x') if member.size else None)
                with self.assertRaisesRegex(ValueError, '不安全'):
                    with manager.source_directory(str(path)):
                        self.fail('unsafe archive accepted')
            self.assertFalse((Path(folder).parent / 'escape').exists())

    def test_no_terminal_exits_without_modification(self):
        result = subprocess.run(['bash', str(manager.SOURCE / 'manage.sh')], input='', capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('交互终端', result.stderr)

    def test_systemd_paths_are_quoted_and_python_environment_is_not_inherited(self):
        files = manager.deployment(Path('/usr/local/lib/helper with spaces'), '/usr/bin/python3.12', 30, 8765)
        service = files[str(manager.UNIT_DIR / manager.UNITS[0])].decode()
        self.assertIn('WorkingDirectory=/usr/local/lib/helper with spaces/current', service)
        self.assertIn(' -B -E -s -m relay_helper', service)
        self.assertIn('UMask=0077', service)
        self.assertIn('unset PYTHONPATH PYTHONHOME', files[str(manager.ENTRY)].decode())


if __name__ == '__main__':
    unittest.main()
