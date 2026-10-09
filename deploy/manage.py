"""Interactive local deployment manager; no relay selection logic lives here."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

SOURCE = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path('/etc/rustdesk-relay-helper')
STATE_DIR = Path('/var/lib/rustdesk-relay-helper')
UNIT_DIR = Path('/etc/systemd/system')
LOCK_DIR = Path('/run/rustdesk-relay-helper')
ENTRY = Path('/usr/local/bin/rustdesk-relay-helper')
UNITS = ('rustdesk-relay-helper.service', 'rustdesk-relay-helper.timer',
         'rustdesk-relay-helper-web.service')
REQUIRED_FILES = ('manage.sh', 'deploy/manage.py', 'config/relay-helper.example.ini',
                'relay_helper/__main__.py', 'relay_helper/config.py', 'relay_helper/io.py',
                'relay_helper/service.py', 'relay_helper/policy.py', 'relay_helper/web.py',
                'relay_helper/__init__.py', 'relay_helper/login.html', 'relay_helper/webui.html', 'LICENSE',
                'bin/rustdesk-relay-helper', *(str(Path('deploy/systemd') / u) for u in UNITS))
MANIFEST = 'install.json'
DEFAULT_PREFIX = '/usr/local/lib/rustdesk-relay-helper'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def ask(prompt, default=''):
    value = input(prompt + (f' [{default}]' if default else '') + ': ').strip()
    return value or default


def confirm(prompt):
    return ask(prompt + '（输入 yes 确认，其余取消）') == 'yes'


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('文件缺失或不是普通文件: ' + str(path))
    return path


def secure_path(path, directory=False, repair_leaf=False):
    """Check every existing ancestor; never chmod another owner's parent."""
    path = Path(path)
    if not path.is_absolute() or any(c in str(path) for c in '\n\r\x00%\\'):
        raise ValueError('必须使用不含换行、反斜杠或 % 的绝对路径')
    if '..' in path.parts:
        raise ValueError('路径不能包含 ..')
    for part in reversed((path,) + tuple(path.parents)):
        if part.is_symlink():
            raise ValueError('路径中存在符号链接: ' + str(part))
        if not part.exists():
            continue
        info = part.stat()
        if info.st_uid != 0 or (info.st_mode & 0o022 and not (repair_leaf and part == path)):
            raise ValueError('路径必须属于 root 且组/其他用户不可写: ' + str(part))
        # POSIX ACLs can grant write access beyond the mode's other bits.
        try:
            if 'system.posix_acl_access' in os.listxattr(part):
                raise ValueError('受管路径不支持扩展 ACL，请选择其他目录: ' + str(part))
        except OSError as exc:
            raise ValueError('无法检查路径 ACL: ' + str(part)) from exc
    if directory and path.exists() and not path.is_dir():
        raise ValueError('目录路径被文件占用: ' + str(path))
    return path


def mkdir(path, mode=0o750):
    secure_path(path, directory=True)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def atomic(path, data, mode=0o600):
    secure_path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def lock(path):
    secure_path(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('管理器忙，请稍后重试；不要删除锁文件') from exc
        yield
    finally:
        os.close(fd)


def run(args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True,
                          timeout=60, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
                                           'LANG': 'C.UTF-8'})


class Systemd:
    def call(self, *args, check=True):
        return run(['/usr/bin/systemctl', *args], check=check).stdout.strip()

    def properties(self, unit):
        result = self.call('show', unit, '--no-pager',
                           '--property=LoadState,ActiveState,UnitFileState,FragmentPath,WorkingDirectory,DropInPaths')
        return dict(line.split('=', 1) for line in result.splitlines() if '=' in line)

    def snapshot(self):
        states = {}
        for unit in UNITS:
            props = self.properties(unit)
            if props.get('UnitFileState') not in ('enabled', 'disabled', 'static', '', 'not-found'):
                raise ValueError('不支持的单元状态（例如 masked）: ' + unit)
            states[unit] = {'enabled': props.get('UnitFileState') == 'enabled',
                            'active': props.get('ActiveState') in ('active', 'activating')}
        return states

    def quiesce(self):
        for unit in (UNITS[1], UNITS[2]):
            if self.properties(unit).get('LoadState') != 'not-found':
                self.call('stop', unit)
        # A running oneshot may be applying a relay; let it finish before maintenance.
        deadline = time.monotonic() + 60
        while self.properties(UNITS[0]).get('ActiveState') in ('active', 'activating', 'deactivating'):
            if time.monotonic() >= deadline:
                raise RuntimeError('relay 任务仍在执行，取消维护，请稍后重试')
            time.sleep(.2)

    def restore(self, states, reconcile=False):
        self.call('daemon-reload')
        for unit in (UNITS[2], UNITS[1]):
            if self.properties(unit).get('LoadState') == 'not-found':
                if states[unit]['active'] or states[unit]['enabled']:
                    raise RuntimeError('原服务缺失: ' + unit)
                continue
            self.call('enable' if states[unit]['enabled'] else 'disable', unit)
        # Do not start the timer until the optional one-shot completes: both use the same lock.
        for unit in (UNITS[2], UNITS[1]):
            if unit == UNITS[1] and reconcile:
                self.call('start', UNITS[0])
            if states[unit]['active']:
                self.call('start', unit)
                if self.properties(unit).get('ActiveState') != 'active':
                    raise RuntimeError('服务启动失败: ' + unit)


def payload(source):
    source = Path(source)
    files = {}
    for name in REQUIRED_FILES:
        path = source / name
        if any(p.is_symlink() for p in (path,) + tuple(list(path.parents)[:len(Path(name).parts)])):
            raise ValueError('源文件不能经过符号链接: ' + name)
        files[name] = regular(path).read_bytes()
    return files


@contextlib.contextmanager
def source_directory(value):
    path = Path(value).expanduser().absolute()
    if path.is_dir():
        yield path
        return
    regular(path)
    # Unpack only small regular files/directories, no symlinks, hardlinks or devices.
    with tempfile.TemporaryDirectory(prefix='relay-helper-source-') as temporary:
        root = Path(temporary)
        with tarfile.open(path, 'r:*') as archive:
            members = archive.getmembers()
            if len(members) > 10000 or sum(m.size for m in members) > 32 * 1024 * 1024:
                raise ValueError('安装包过大')
            for member in members:
                relative = Path(member.name)
                if relative.is_absolute() or '..' in relative.parts or not (member.isfile() or member.isdir()):
                    raise ValueError('安装包包含不安全路径或链接')
                target = root / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as stream:
                        target.write_bytes(stream.read())
        candidates = [root] + [p for p in root.iterdir() if p.is_dir()]
        matches = [p for p in candidates if (p / 'manage.sh').is_file()]
        if len(matches) != 1:
            raise ValueError('安装包必须包含唯一的完整仓库根目录')
        yield matches[0]


def version_id(files):
    return digest(b''.join(name.encode() + b'\0' + files[name] for name in sorted(files)))[:20]


def systemd_quote(value):
    return '"' + str(value).replace('$', '$$').replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def deployment(prefix, python, interval, port):
    home = prefix / 'current'
    entry = ('#!/bin/bash\nset -euo pipefail\nunset PYTHONPATH PYTHONHOME\n'
             'cd -- ' + shell_quote(str(home)) + '\nexec ' + shell_quote(python)
             + ' -B -E -s -m relay_helper --config ' + shell_quote(str(CONFIG_DIR / 'config.ini'))
             + ' --state ' + shell_quote(str(STATE_DIR / 'state.json')) + ' "$@"\n')
    common = ('WorkingDirectory=' + str(home) + '\n'
              'Environment=PYTHONDONTWRITEBYTECODE=1\nUMask=0077\n'
              'StateDirectory=rustdesk-relay-helper\nStateDirectoryMode=0750\n')
    command = (systemd_quote(python) + ' -B -E -s -m relay_helper --config '
               + systemd_quote(CONFIG_DIR / 'config.ini') + ' --state '
               + systemd_quote(STATE_DIR / 'state.json'))
    unit = '[Unit]\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\n'
    service = unit + 'Type=oneshot\n' + common + 'ExecStart=' + command + ' reconcile\n'
    web = (unit + 'Type=simple\n' + common + 'ExecStart=' + command + ' web --port ' + str(port)
           + '\nRestart=on-failure\nRestartSec=3s\nNoNewPrivileges=true\nPrivateTmp=true\n'
           '[Install]\nWantedBy=multi-user.target\n')
    timer = ('[Unit]\nDescription=RustDesk relay helper schedule\n[Timer]\nOnBootSec=20s\n'
             'OnUnitInactiveSec=' + str(interval) + 's\nAccuracySec=1s\nUnit=' + UNITS[0]
             + '\n[Install]\nWantedBy=timers.target\n')
    return {str(ENTRY): entry.encode(), str(UNIT_DIR / UNITS[0]): service.encode(),
            str(UNIT_DIR / UNITS[1]): timer.encode(), str(UNIT_DIR / UNITS[2]): web.encode()}


def shell_quote(value):
    import shlex
    return shlex.quote(value)


class Manager:
    def __init__(self, systemd=None):
        self.systemd = systemd or Systemd()
        self.record = CONFIG_DIR / MANIFEST
        self.pending = CONFIG_DIR / 'maintenance.json'

    def read_record(self):
        if not self.record.exists():
            return None
        secure_path(self.record)
        data = json.loads(regular(self.record).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != 1:
            raise ValueError('安装记录格式不支持')
        prefix = self.prefix(data['prefix'])
        for key in ('current', 'previous'):
            value = data.get(key)
            if value is not None and not re.fullmatch('[a-f0-9]{20}', value):
                raise ValueError('安装版本记录无效')
        if not data.get('current'):
            raise ValueError('安装记录缺少当前版本')
        secure_path(prefix / 'releases', directory=True)
        secure_path(Path(data['python']))
        if not 1 <= data['port'] <= 65535:
            raise ValueError('Web 端口无效')
        if set(data['deployment']) != {str(ENTRY), *(str(UNIT_DIR / u) for u in UNITS)}:
            raise ValueError('受管部署文件清单无效')
        return data

    def prefix(self, value):
        path = secure_path(Path(value), directory=True)
        protected = (CONFIG_DIR, STATE_DIR, UNIT_DIR, ENTRY.parent, Path('/'), Path('/usr'), Path('/usr/local'))
        if any(path == p or path in p.parents or p in path.parents for p in protected[:4]):
            raise ValueError('程序目录不能与系统配置、状态或入口目录重叠')
        if path in protected[4:]:
            raise ValueError('不能将系统根目录作为程序目录')
        return path

    def legacy(self):
        clues = ENTRY.exists() or any((UNIT_DIR / u).exists() for u in UNITS) or (CONFIG_DIR / 'config.ini').exists()
        if not clues:
            return None
        service = UNIT_DIR / UNITS[0]
        timer = UNIT_DIR / UNITS[1]
        if not ENTRY.exists() and not service.exists() and not timer.exists() and not (UNIT_DIR / UNITS[2]).exists():
            print('发现保留配置，将校验后复用。')
            return None
        if not ENTRY.exists() or not service.exists() or not timer.exists():
            raise ValueError('发现旧部署残留，缺少入口/service/timer，需人工核对；不覆盖')
        home = self.systemd.properties(UNITS[0]).get('WorkingDirectory', '')
        if not home:
            raise ValueError('无法识别旧部署程序目录')
        secure_path(Path(home), directory=True)
        for unit in UNITS:
            path = UNIT_DIR / unit
            if not path.exists():
                continue  # WebUI is optional in README installations.
            secure_path(path)
            props = self.systemd.properties(unit)
            if props.get('DropInPaths') or props.get('FragmentPath') != str(path):
                raise ValueError('旧部署存在自定义单元或 drop-in，需人工核对: ' + unit)
            text = regular(path).read_text()
            template = (SOURCE / 'deploy/systemd' / unit).read_text()
            if text != template.replace('/opt/rustdesk-relay-helper', home):
                raise ValueError('旧部署单元不是 README 模板，需人工核对: ' + unit)
        expected = (SOURCE / 'bin/rustdesk-relay-helper').read_bytes()
        secure_path(ENTRY)
        if regular(ENTRY).read_bytes() != expected:
            raise ValueError('旧部署入口已修改，需人工核对')
        regular(Path(home) / 'relay_helper/__main__.py')
        return Path(home)

    def check_units(self, record, repair=False):
        for unit in UNITS:
            props = self.systemd.properties(unit)
            if props.get('LoadState') != 'not-found' and props.get('FragmentPath') != str(UNIT_DIR / unit):
                raise ValueError('单元来自其他目录，需人工核对: ' + unit)
            if props.get('DropInPaths'):
                raise ValueError('存在管理员 drop-in，保留并请人工核对: ' + unit)
        for name, expected in record['deployment'].items():
            path = Path(name)
            secure_path(path, repair_leaf=repair)
            if repair and path.exists():
                os.chmod(regular(path), 0o755 if path == ENTRY else 0o644)
            if not repair and (not path.exists() or digest(regular(path).read_bytes()) != expected):
                raise ValueError('受管文件缺失或已修改，先使用修复核对: ' + name)

    def validate(self, release, python, allow_placeholder=False):
        # Validate with the target version in an isolated subprocess, never import it into this manager.
        config = CONFIG_DIR / 'config.ini'
        if not config.exists():
            raise ValueError('配置缺失，请重新配置或从备份恢复')
        secure_path(config)
        code = ('import sys; sys.path.insert(0,sys.argv[1]); from relay_helper.config import load_config,endpoint; '
                'from relay_helper.io import load_state; c=load_config(sys.argv[2]); '
                'load_state(sys.argv[3],c); '
                'print(c.policy.probe_interval_seconds); '
                'print(int(any(n.enabled and endpoint(n.address)[0].lower().endswith(("example.com","example.org","example.net")) for n in c.nodes))); '
                'print(int(any(n.enabled for n in c.nodes)))')
        result = run([python, '-I', '-B', '-c', code, str(release), str(config), str(STATE_DIR / 'state.json')])
        interval, placeholder, enabled = result.stdout.strip().splitlines()
        ready = placeholder == '0' and enabled == '1'
        if not ready and not allow_placeholder:
            raise ValueError('配置含示例地址或没有启用节点，请先配置真实节点')
        return int(interval), ready

    def prepare(self, prefix, files):
        mkdir(prefix, 0o755)
        releases = prefix / 'releases'
        mkdir(releases, 0o755)
        version = version_id(files)
        target = releases / version
        if target.exists():
            self.verify_release(target, files)
            return version
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=releases) as temporary:
            root = Path(temporary)
            os.chmod(root, 0o755)
            for name, content in files.items():
                dest = root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                for parent in dest.parents:
                    if parent == root:
                        break
                    os.chmod(parent, 0o755)
                dest.write_bytes(content)
                os.chmod(dest, 0o755 if name == 'manage.sh' else 0o644)
            atomic(root / 'release.json', json.dumps({name: digest(data) for name, data in files.items()}).encode(), 0o644)
            os.rename(root, target)
        return version

    def verify_release(self, release, files=None):
        secure_path(release, directory=True)
        saved = json.loads(regular(release / 'release.json').read_text())
        expected_names = set(REQUIRED_FILES)
        if not isinstance(saved, dict) or set(saved) != expected_names:
            raise ValueError('版本文件清单无效')
        for name, expected in saved.items():
            path = release / name
            secure_path(path)
            if digest(regular(path).read_bytes()) != expected:
                raise ValueError('程序文件损坏: ' + name)
            if files is not None and files[name] != path.read_bytes():
                raise ValueError('同版本目录内容不一致')
        return saved

    def current_link(self, prefix, version):
        path = prefix / 'current'
        if path.exists() or path.is_symlink():
            if not path.is_symlink() or not re.fullmatch('releases/[a-f0-9]{20}', os.readlink(path)):
                raise ValueError('current 不是受管版本链接')
        temporary = prefix / '.current-new'
        if temporary.exists() or temporary.is_symlink():
            if (temporary.is_symlink() and temporary.lstat().st_uid == os.getuid()
                    and re.fullmatch('releases/[a-f0-9]{20}', os.readlink(temporary))):
                temporary.unlink()  # Recover our interrupted atomic link publication.
            else:
                raise ValueError('发现未知临时版本链接，请人工核对')
        temporary.symlink_to('releases/' + version)
        os.replace(temporary, path)

    def backup(self, states, record):
        mkdir(STATE_DIR / 'backups', 0o700)
        folder = STATE_DIR / 'backups' / (str(time.time_ns()))
        mkdir(folder, 0o700)
        paths = [ENTRY, *(UNIT_DIR / u for u in UNITS), self.record]
        saved = {}
        for index, path in enumerate(paths):
            secure_path(path)
            if path.exists():
                info = regular(path).stat()
                dest = folder / str(index)
                atomic(dest, path.read_bytes())
                saved[str(path)] = {'file': str(index), 'mode': stat.S_IMODE(info.st_mode)}
            else:
                saved[str(path)] = None
        for name in ('config.ini',):
            if (CONFIG_DIR / name).exists():
                atomic(folder / name, regular(CONFIG_DIR / name).read_bytes())
        for name in ('state.json', 'webui.token'):
            if (STATE_DIR / name).exists():
                secure_path(STATE_DIR / name)
                atomic(folder / name, regular(STATE_DIR / name).read_bytes())
        pending = {'schema': 1, 'backup': folder.name, 'saved': saved, 'states': states,
                   'record': record, 'stage': 'prepared'}
        atomic(self.pending, json.dumps(pending).encode())
        return pending

    def restore_backup(self, pending):
        if pending.get('schema') != 1 or not re.fullmatch('[0-9]+', pending['backup']):
            raise ValueError('维护记录无效')
        allowed = {str(ENTRY), *(str(UNIT_DIR / u) for u in UNITS), str(self.record)}
        if set(pending['saved']) != allowed:
            raise ValueError('维护文件清单无效')
        folder = STATE_DIR / 'backups' / pending['backup']
        secure_path(folder, directory=True)
        for name, saved in pending['saved'].items():
            path = Path(name)
            secure_path(path)
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                if not re.fullmatch('[0-9]+', saved['file']) or saved['mode'] not in (0o600, 0o640, 0o644, 0o755):
                    raise ValueError('备份元数据无效')
                atomic(path, regular(folder / saved['file']).read_bytes(), saved['mode'])
        record = pending['record']
        if pending.get('uninstall_versions'):
            if record is None:
                raise ValueError('卸载恢复缺少安装记录')
            prefix = self.prefix(record['prefix'])
            mkdir(prefix, 0o755)
            mkdir(prefix / 'releases', 0o755)
            for version, names in pending['uninstall_versions'].items():
                if not re.fullmatch('[a-f0-9]{20}', version) or set(names) != set(REQUIRED_FILES) | {'release.json'}:
                    raise ValueError('卸载版本清单无效')
                for name in names:
                    destination = prefix / 'releases' / version / name
                    mkdir(destination.parent, 0o755)
                    original = folder / 'programs' / version / name
                    secure_path(original)
                    atomic(destination, regular(original).read_bytes(), 0o755 if name == 'manage.sh' else 0o644)
        if record is None:
            # The new release remains available for a retry; it is not a live installation.
            if self.record.exists():
                raise ValueError('恢复后不应有安装记录')
            if pending.get('new_prefix'):
                prefix = self.prefix(pending['new_prefix'])
                link = prefix / 'current'
                if link.is_symlink() and re.fullmatch('releases/[a-f0-9]{20}', os.readlink(link)):
                    link.unlink()
        else:
            record = self.read_record()
            prefix = self.prefix(record['prefix'])
            self.verify_release(prefix / 'releases' / record['current'])
            self.current_link(prefix, record['current'])
        # Business data is preserved: a resumed timer may already have written newer state.

    def transaction(self, record, prefix, version, python, port, interval, states, reconcile=False, original_states=None):
        previous = None if record is None else record['current']
        new = {'schema': 1, 'prefix': str(prefix), 'current': version,
               'previous': previous if previous != version else record.get('previous'),
               'python': python, 'port': port, 'updated_at': time.time(), 'deployment': {}}
        files = deployment(prefix, python, interval, port)
        if self.pending.exists():
            marker = json.loads(regular(self.pending).read_text())
            if original_states is None or marker.get('stage') != 'quiescing':
                raise RuntimeError('存在未完成维护，请先选择恢复')
        pending = None
        original_states = original_states or self.systemd.snapshot()
        atomic(self.pending, json.dumps({'schema': 1, 'stage': 'quiescing', 'states': original_states}).encode())
        try:
            self.systemd.quiesce()
            with lock(STATE_DIR / 'state.json.lock'):
                interval, ready = self.validate(prefix / 'releases' / version, python, True)
                if not ready and (reconcile or any(s['active'] or s['enabled'] for s in states.values())):
                    raise ValueError('配置未完成，不能启动服务')
                files = deployment(prefix, python, interval, port)
                pending = self.backup(original_states, record)
                pending['new_prefix'] = str(prefix)
                atomic(self.pending, json.dumps(pending).encode())
                self.current_link(prefix, version)
                for name, data in files.items():
                    atomic(Path(name), data, 0o755 if name == str(ENTRY) else 0o644)
                    new['deployment'][name] = digest(data)
                atomic(self.record, json.dumps(new, ensure_ascii=False, indent=2).encode())
                pending['stage'] = 'published'
                atomic(self.pending, json.dumps(pending).encode())
            self.systemd.restore(states, reconcile)
            self.pending.unlink()
        except BaseException:
            if pending is not None:
                try:
                    self.systemd.quiesce()
                    with lock(STATE_DIR / 'state.json.lock'):
                        self.restore_backup(pending)
                    self.systemd.restore(original_states)
                    self.pending.unlink()
                    print('维护失败，已恢复原部署；业务数据保留。')
                except BaseException as recovery_error:
                    print('自动恢复未完成，保留维护记录；下次运行选择恢复:', recovery_error)
            else:
                # No published changes; restore any service stopped before a busy lock/timeout.
                self.systemd.restore(original_states)
                self.pending.unlink(missing_ok=True)
            raise
        return new

    def status(self):
        record = self.read_record()
        if self.pending.exists():
            print('存在未完成维护，请选择恢复。')
        if record:
            print('已安装版本:', record['current'], '\n程序目录:', record['prefix'])
            try:
                prefix = Path(record['prefix'])
                if os.readlink(prefix / 'current') != 'releases/' + record['current']:
                    raise ValueError('当前链接与安装记录不一致')
                self.verify_release(prefix / 'releases' / record['current'])
                self.check_units(record)
                for path, mode in ((CONFIG_DIR, 0o750), (STATE_DIR, 0o750),
                                   (CONFIG_DIR / 'config.ini', 0o640), (self.record, 0o600),
                                   (STATE_DIR / 'state.json', 0o600), (STATE_DIR / 'webui.token', 0o600)):
                    if path.exists():
                        secure_path(path)
                        if stat.S_IMODE(path.stat().st_mode) != mode:
                            raise ValueError('权限不符合要求: ' + str(path))
                interval, ready = self.validate(prefix / 'releases' / record['current'], record['python'], True)
                print('文件与配置校验通过；配置完成:', ready, '期望 timer 间隔:', interval)
                timer_text = regular(UNIT_DIR / UNITS[1]).read_text()
                match = re.search(r'^OnUnitInactiveSec=(\d+)s$', timer_text, re.MULTILINE)
                if not match or int(match[1]) != interval:
                    print('timer 间隔与配置不一致，请使用修复同步。')
                print('WebUI 配置监听地址: http://127.0.0.1:' + str(record['port']))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                print('检查发现问题:', exc)
        else:
            legacy = self.legacy()
            print('发现 README 手动部署: ' + str(legacy) if legacy else '尚未安装')
        for unit in UNITS:
            print(unit, self.systemd.properties(unit))
        state = STATE_DIR / 'state.json'
        if state.exists():
            value = json.loads(regular(state).read_text())
            print('业务模式:', value.get('mode'), '手动目标:', value.get('manual_target'))
        else:
            print('尚无业务状态；不创建状态文件。')
        print('hbbs 只读检查（不探测节点、不写 relay）：')
        # Local socket only; do not use CLI which creates a lock file for status.
        import socket
        try:
            with socket.create_connection(('127.0.0.1', 21115), 2) as sock:
                sock.settimeout(2)
                sock.sendall(b'rs')
                data = bytearray()
                deadline = time.monotonic() + 2
                while True:
                    sock.settimeout(max(.001, deadline - time.monotonic()))
                    block = sock.recv(4096)
                    if not block:
                        break
                    data.extend(block)
                    if len(data) > 65536 or time.monotonic() >= deadline:
                        raise ValueError('响应过大或读取超时')
                print('实际 relay:', data.decode() or '(空列表)')
        except (OSError, ValueError, UnicodeError) as exc:
            print('实际状态未确认:', exc)

    def configure(self, source):
        path = CONFIG_DIR / 'config.ini'
        if path.exists():
            secure_path(path)
            print('保留已有配置:', path)
            return
        print('配置真实节点；留空节点 ID 可暂不配置，部署后所有服务保持关闭。')
        nodes = []
        while True:
            identifier = ask('节点 ID（字母/数字/下划线/连字符，空值结束）')
            if not identifier:
                break
            if not re.fullmatch('[A-Za-z0-9_-]+', identifier):
                print('节点 ID 格式无效')
                continue
            address = ask('relay 地址 host:port 或 [IPv6]:port')
            ranked = ask('参与 RTT 排名？yes/no', 'yes') == 'yes'
            nodes.append(f'[node:{identifier}]\naddress={address}\ntier={len(nodes) * 10}\nenabled=true\nrtt_ranked={str(ranked).lower()}\n')
        example = (source / 'config/relay-helper.example.ini').read_text()
        text = example.split('[node:', 1)[0] + '\n'.join(nodes) if nodes else example
        atomic(path, text.encode(), 0o640)
        print('配置已写入:', path, '（会在部署前校验，无效时修正此文件后重新运行）')

    def install(self):
        record = self.read_record()
        if record:
            print('已经安装，请选择修复或更新。')
            return
        legacy = self.legacy()
        if legacy:
            print('将接管 README 部署，保留原源码目录及全部业务数据:', legacy)
        prefix = self.prefix(ask('程序安装目录', DEFAULT_PREFIX))
        if prefix.exists() and any(prefix.iterdir()):
            # A previously prepared but unpublished install can be resumed.
            if {p.name for p in prefix.iterdir()} != {'releases'}:
                raise ValueError('首次安装请选择空目录；不覆盖已有文件')
            for release in (prefix / 'releases').iterdir():
                self.verify_release(release)
        files = payload(SOURCE)
        print('待部署版本:', version_id(files))
        if legacy:
            print('接管将部署上述版本，完整重启原先运行的 WebUI/timer，原源码目录保留。')
        python = str(Path('/usr/bin/python3').resolve())
        if not confirm(f'安装到 {prefix}，保留已有配置；默认不启用 timer'):
            return
        with self.maintenance():
            version = self.prepare(prefix, files)
            self.configure(SOURCE)
            interval, ready = self.validate(prefix / 'releases' / version, python, True)
            states = self.systemd.snapshot()
            if not ready and any(v['active'] or v['enabled'] for v in states.values()):
                raise ValueError('配置未完成且已有服务启用，先修正配置；不停止现有部署')
            if ready and not states[UNITS[2]]['active'] and confirm('安装后启动 WebUI（仅本机 8765）'):
                states[UNITS[2]] = {'active': True, 'enabled': True}
            self.transaction(None, prefix, version, python, 8765, interval, states)
            print('安装完成。再次管理: bash', prefix / 'current/manage.sh')
            print('初次自动执行仍须现场验收后按 README 启用 timer。')

    @contextlib.contextmanager
    def maintenance(self):
        mkdir(CONFIG_DIR)
        mkdir(STATE_DIR)
        mkdir(LOCK_DIR)
        with lock(LOCK_DIR / 'manage.lock'):
            if self.pending.exists():
                raise RuntimeError('存在未完成维护，请先选择恢复')
            yield

    def update(self):
        record = self.read_record()
        if not record:
            print('先选择安装接管旧部署，再更新。')
            return
        value = ask('新版完整目录或 tar 安装包', str(SOURCE))
        with source_directory(value) as source:
            files = payload(source)
            version = version_id(files)
            print('当前版本:', record['current'], '目标版本:', version)
            if not confirm('更新并完整恢复原先运行的 WebUI/timer，保留配置、状态和 token'):
                return
            reconcile = confirm('更新成功后立即执行一次新版 relay reconcile（可能写入 hbbs）')
            with self.maintenance():
                record = self.read_record()
                self.check_units(record)
                prefix = Path(record['prefix'])
                version = self.prepare(prefix, files)
                interval, _ = self.validate(prefix / 'releases' / version, record['python'])
                states = self.systemd.snapshot()
                self.transaction(record, prefix, version, record['python'], record['port'], interval, states, reconcile)
                print('更新成功，原先运行的 WebUI/timer 已使用新版；WebUI 需重新登录。')

    def repair(self):
        record = self.read_record()
        if not record:
            print('请先安装或接管，缺少记录时不猜测修复范围。')
            return
        prefix = Path(record['prefix'])
        print('修复受管文件/权限和单元；保留配置、状态、token，不覆盖管理员 drop-in。')
        if not confirm('确认修复当前版本'):
            return
        with self.maintenance():
            record = self.read_record()
            states = self.systemd.snapshot()
            atomic(self.pending, json.dumps({'schema': 1, 'stage': 'quiescing', 'states': states}).encode())
            try:
                self.systemd.quiesce()
                with lock(STATE_DIR / 'state.json.lock'):
                    self.check_units(record, repair=True)
                    release = prefix / 'releases' / record['current']
                    try:
                        self.verify_release(release)
                    except (OSError, ValueError):
                        files = payload(SOURCE)
                        if version_id(files) != record['current']:
                            raise ValueError('当前版本副本损坏；请下载同版修复或选择更新')
                        for name, data in files.items():
                            secure_path(release / name)
                            mkdir((release / name).parent, 0o755)
                            atomic(release / name, data, 0o755 if name == 'manage.sh' else 0o644)
                        atomic(release / 'release.json', json.dumps({n: digest(d) for n, d in files.items()}).encode(), 0o644)
                    for name in self.verify_release(release):
                        os.chmod(release / name, 0o755 if name == 'manage.sh' else 0o644)
                    for path, mode in ((CONFIG_DIR / 'config.ini', 0o640), (STATE_DIR / 'state.json', 0o600),
                                       (STATE_DIR / 'webui.token', 0o600)):
                        if path.exists():
                            secure_path(path, repair_leaf=True)
                            if path.name == 'webui.token' and not re.fullmatch(rb'[A-Za-z0-9_-]{32,128}\n?', regular(path).read_bytes()):
                                raise ValueError('token 内容无效，不自动轮换')
                            os.chmod(regular(path), mode)
                    interval, ready = self.validate(release, record['python'], True)
                    if not ready and any(s['active'] for s in states.values()):
                        raise ValueError('配置未完成，不能重启现有服务')
                self.transaction(record, prefix, record['current'], record['python'], record['port'], interval, states, original_states=states)
            except BaseException:
                if not self.pending.exists() or json.loads(self.pending.read_text()).get('stage') == 'quiescing':
                    self.systemd.restore(states)
                    self.pending.unlink(missing_ok=True)
                raise
            print('修复完成。')

    def rollback(self):
        record = self.read_record()
        if not record or not record.get('previous'):
            raise ValueError('没有受管上一版可回滚')
        if not confirm('回滚到 ' + record['previous'] + '，保留当前业务数据，不撤销 hbbs 已写入值'):
            return
        with self.maintenance():
            record = self.read_record()
            prefix = Path(record['prefix'])
            target = prefix / 'releases' / record['previous']
            self.check_units(record)
            self.verify_release(target)
            interval, _ = self.validate(target, record['python'])
            self.transaction(record, prefix, record['previous'], record['python'], record['port'], interval,
                             self.systemd.snapshot())
            print('回滚完成。')

    def recover(self):
        if not self.pending.exists():
            print('没有未完成维护。')
            return
        secure_path(self.pending)
        if not confirm('恢复中断前的部署及服务状态；保留当前业务数据'):
            return
        mkdir(LOCK_DIR)
        with lock(LOCK_DIR / 'manage.lock'):
            pending = json.loads(regular(self.pending).read_text())
            self.systemd.quiesce()
            with lock(STATE_DIR / 'state.json.lock'):
                if pending.get('stage') != 'quiescing':
                    self.restore_backup(pending)
            self.systemd.restore(pending['states'])
            self.pending.unlink()
            print('恢复完成。')

    def uninstall(self):
        record = self.read_record()
        if not record:
            raise ValueError('没有安装记录；先接管旧部署，不猜测卸载范围')
        print('仅删除受管程序、入口和单元；不删除下载目录，不修改 hbbs。')
        if not confirm('确认卸载，默认保留配置、状态、token 和备份'):
            return
        purge = confirm('同时彻底清除配置、状态、token 和全部受管备份（不可恢复）')
        with self.maintenance():
            record = self.read_record()
            self.check_units(record)
            prefix = Path(record['prefix'])
            # Preflight exact inventories before stopping anything. Unknown files block deletion.
            releases = prefix / 'releases'
            inventories = {}
            for release in releases.iterdir():
                if not re.fullmatch('[a-f0-9]{20}', release.name):
                    raise ValueError('程序目录含无关文件，取消卸载: ' + str(release))
                known = self.verify_release(release)
                expected = set(known) | {'release.json'}
                actual = {str(p.relative_to(release)) for p in release.rglob('*') if not p.is_dir()}
                if actual != expected or any(p.is_symlink() for p in release.rglob('*')):
                    raise ValueError('程序版本含无关文件，取消卸载')
                inventories[release] = expected
            if {p.name for p in prefix.iterdir()} != {'releases', 'current'}:
                raise ValueError('程序根目录含无关文件，取消卸载')
            if os.readlink(prefix / 'current') != 'releases/' + record['current']:
                raise ValueError('当前版本链接不一致')
            if purge:
                self.check_purge()
            states = self.systemd.snapshot()
            pending = None
            atomic(self.pending, json.dumps({'schema': 1, 'stage': 'quiescing', 'states': states}).encode())
            try:
                self.systemd.quiesce()
                with lock(STATE_DIR / 'state.json.lock'):
                    pending = self.backup(states, record)
                    pending['uninstall_versions'] = {r.name: sorted(names) for r, names in inventories.items()}
                    folder = STATE_DIR / 'backups' / pending['backup']
                    for release, names in inventories.items():
                        for name in names:
                            destination = folder / 'programs' / release.name / name
                            mkdir(destination.parent, 0o700)
                            atomic(destination, regular(release / name).read_bytes())
                    atomic(self.pending, json.dumps(pending).encode())
                    for unit in (UNITS[1], UNITS[2]):
                        self.systemd.call('disable', unit)
                    for path in (ENTRY, *(UNIT_DIR / u for u in UNITS)):
                        secure_path(path)
                        path.unlink(missing_ok=True)
                    self.systemd.call('daemon-reload')
                    # Remove only enumerated files; rmdir fails on any unexpected new file.
                    for release, names in inventories.items():
                        for name in names:
                            secure_path(release / name)
                            (release / name).unlink()
                        for directory in sorted((p for p in release.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                            directory.rmdir()
                        release.rmdir()
                    (prefix / 'current').unlink()
                    releases.rmdir()
                    prefix.rmdir()
                    self.record.unlink()
                    self.pending.unlink()
            except BaseException:
                if pending is None:
                    self.systemd.restore(states)
                    self.pending.unlink(missing_ok=True)
                else:
                    print('卸载未完成，保留维护记录和备份；请选择恢复并核对程序文件。')
                raise
            if purge:
                self.check_purge()
                for directory in (CONFIG_DIR, STATE_DIR):
                    # Preflight excludes unknown files; only known manager-owned entries remain.
                    for path in sorted(directory.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                        secure_path(path)
                        path.rmdir() if path.is_dir() else path.unlink()
                    directory.rmdir()
        print('卸载完成。' + ('已清除数据。' if purge else f'数据保留于 {CONFIG_DIR} 和 {STATE_DIR}。'))

    def check_purge(self):
        config_names = {'config.ini', 'config.ini.webui.bak', MANIFEST, 'manage.lock', 'maintenance.json'}
        state_names = {'state.json', 'state.json.lock', 'webui.token', 'backups'}
        for directory, names in ((CONFIG_DIR, config_names), (STATE_DIR, state_names)):
            for path in directory.iterdir():
                secure_path(path)
                if path.name not in names:
                    raise ValueError('数据目录含无关文件，拒绝彻底清除: ' + str(path))
        backups = STATE_DIR / 'backups'
        if backups.exists():
            for folder in backups.iterdir():
                if not re.fullmatch('[0-9]+', folder.name) or not folder.is_dir():
                    raise ValueError('备份目录含未知条目')
                for path in folder.iterdir():
                    secure_path(path)
                    if path.name == 'programs':
                        for release in path.iterdir():
                            if not re.fullmatch('[a-f0-9]{20}', release.name) or not release.is_dir():
                                raise ValueError('卸载备份版本目录无效')
                            allowed = set(REQUIRED_FILES) | {'release.json'}
                            actual = set()
                            for entry in release.rglob('*'):
                                secure_path(entry)
                                if not entry.is_dir():
                                    regular(entry)
                                    actual.add(str(entry.relative_to(release)))
                            if actual != allowed:
                                raise ValueError('卸载备份包含无关文件')
                    elif not path.is_file() or not (path.name.isdigit() or path.name in ('config.ini', 'state.json', 'webui.token')):
                        raise ValueError('备份目录含无关文件')


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print('请在交互终端运行 bash manage.sh。', file=sys.stderr)
        return 2
    if sys.version_info < (3, 9) or sys.platform != 'linux':
        print('需要 Linux 和 Python 3.9+。', file=sys.stderr)
        return 2
    manager = Manager()
    actions = {'1': manager.install, '2': manager.status, '3': manager.repair,
               '4': manager.update, '5': manager.rollback, '6': manager.uninstall, '7': manager.recover}
    while True:
        try:
            record = manager.read_record()
            if record:
                summary = '已安装 ' + record['current'] + '，目录 ' + record['prefix']
            elif ENTRY.exists() or any((UNIT_DIR / u).exists() for u in UNITS):
                summary = '发现已有手动部署，请选择状态核对或安装接管'
            else:
                summary = '未安装（已有配置将保留）'
        except (OSError, ValueError, KeyError) as exc:
            summary = '安装记录无法读取或无效: ' + str(exc)
        print('\nRustDesk Relay Helper 安装管理：' + summary + '\n1. 安装/接管 README 部署\n2. 查看状态\n'
              '3. 修复\n4. 更新\n5. 回滚上一版\n6. 卸载\n7. 恢复中断维护\n0. 退出')
        choice = ask('请选择')
        if choice == '0':
            return 0
        if choice not in actions:
            print('输入无效。')
            continue
        if choice != '2' and os.getuid() != 0:
            print('此操作需要 root 权限。请在确认来源后运行 sudo bash', SOURCE / 'manage.sh')
            continue
        try:
            if choice != '2':
                manager.systemd.call('show', '--property=Version')
                secure_path(Path('/usr/bin/python3').resolve())
                os.umask(0o077)
            actions[choice]()
        except KeyboardInterrupt:
            print('\n已取消；如维护中断，请选择恢复。')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            print('操作未完成:', exc)
            if isinstance(exc, subprocess.CalledProcessError):
                print(exc.stderr.strip())
        try:
            input('按 Enter 返回菜单……')
        except (KeyboardInterrupt, EOFError):
            return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\n已退出。')
