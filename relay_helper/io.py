"""Local hbbs console, TCP probes, atomic JSON state and command lock."""

import hashlib
import json
import math
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .config import endpoint, parse_config
from .policy import expire_health, new_health


class ConsoleError(RuntimeError):
    pass


def parse_relays(response):
    # hbbs 1.1.15 returns one address per line (including addresses without ports).
    result = response.splitlines()
    for address in result:
        if not address or address != address.strip():
            raise ConsoleError('rs 返回了无效地址列表')
        try:
            endpoint(address if ':' in address else address + ':21117')
        except ValueError as exc:
            raise ConsoleError('rs 返回了无效地址列表: ' + response) from exc
    return result


class Console:
    def __init__(self, nodes, timeout=2):
        self.allowed = {n.address for n in nodes if n.enabled}
        self.timeout = timeout

    def request(self, command, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        try:
            with socket.create_connection(('127.0.0.1', 21115), timeout) as sock:
                sock.sendall(command.encode('utf-8'))
                chunks = []
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('rs 请求超时')
                    sock.settimeout(remaining)
                    chunk = sock.recv(4096)
                    if not chunk:
                        return b''.join(chunks).decode('utf-8')
                    chunks.append(chunk)
        except (OSError, UnicodeError) as exc:
            raise ConsoleError('hbbs 实际状态未确认: ' + str(exc)) from exc

    def read(self, timeout=None):
        return parse_relays(self.request('rs', timeout))

    def apply(self, address):
        if address not in self.allowed:
            raise ValueError('只允许写入已启用配置节点的单个地址')
        if self.read() == [address]:
            return False
        self.request('rs ' + address)
        deadline = time.monotonic() + self.timeout
        last_error = 'rs 未返回唯一目标'
        while time.monotonic() < deadline:
            try:
                if self.read(max(0.001, deadline - time.monotonic())) == [address]:
                    return True
            except ConsoleError as exc:
                last_error = str(exc)
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise ConsoleError('写入后实际状态未确认: ' + last_error)


def probe_tcp(node, timeout):
    start = time.perf_counter()
    try:
        with socket.create_connection(endpoint(node.address), timeout):
            return (time.perf_counter() - start) * 1000, None
    except OSError as exc:
        return None, str(exc)


def load_state(path, config):
    if not Path(path).exists():
        state = dict(mode='auto', manual_target=None, nodes={}, last_switch=0,
                     confirmed_target=None, target=None, actual=None, actual_error=None,
                     reason='尚未执行策略')
    else:
        with Path(path).open(encoding='utf-8') as stream:
            state = json.load(stream)
        if not isinstance(state, dict) or state.get('mode') not in ('auto', 'manual'):
            raise ValueError('运行状态损坏，拒绝重置手动模式')
        if not isinstance(state.get('nodes'), dict) or not isinstance(state.get('last_switch'), (int, float)):
            raise ValueError('运行状态结构无效')
        if state['mode'] == 'manual' and not isinstance(state.get('manual_target'), str):
            raise ValueError('运行状态缺少手动目标')
    if type(state.get('auto_reselect', False)) is not bool:
        raise ValueError('运行状态自动选优标记无效')
    active = {}
    now = time.time()
    for node in config.nodes:
        health = state['nodes'].get(node.id)
        if not node.enabled or not health or health.get('address') != node.address:
            health = new_health(node.address)
        if (health.get('health') not in ('unknown', 'healthy', 'unhealthy')
                or any(not isinstance(health.get(k), int) or health[k] < 0 for k in ('successes', 'failures'))
                or not isinstance(health.get('samples'), list)
                or any(not _valid_sample(x) for x in health['samples'])):
            raise ValueError('节点运行状态无效: ' + node.id)
        last = health.get('last_probe')
        if last is not None and not _finite_nonnegative(last):
            raise ValueError('节点探测时间无效: ' + node.id)
        if health.get('rtt_ms') is not None and not _finite_nonnegative(health['rtt_ms']):
            raise ValueError('节点探测 RTT 无效: ' + node.id)
        if any(isinstance(x, (int, float)) for x in health['samples']):
            # Legacy floats have no per-point timestamps; never renew their TTL.
            health.update(samples=[], last_probe=None, successes=0, failures=0)
        health.setdefault('last_attempts', [])
        if (not isinstance(health['last_attempts'], list) or len(health['last_attempts']) > 2
                or any(not isinstance(attempt, dict) or set(attempt) != {'rtt_ms', 'error'}
                       or (attempt['rtt_ms'] is not None and not _finite_nonnegative(attempt['rtt_ms']))
                       or (attempt['error'] is not None and not isinstance(attempt['error'], str))
                       for attempt in health['last_attempts'])):
            raise ValueError('节点探测详情无效: ' + node.id)
        expire_health(health, config.policy, now)
        active[node.id] = health
    state['nodes'] = active
    return state


def _finite_nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _valid_sample(point):
    if _finite_nonnegative(point):
        return True  # Valid legacy point, discarded by the loader.
    return (isinstance(point, dict) and set(point) == {'at', 'rtt_ms'}
            and all(_finite_nonnegative(point[key]) for key in ('at', 'rtt_ms')))


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def config_document(path):
    content = Path(path).read_bytes()
    return {'text': content.decode('utf-8'), 'revision': hashlib.sha256(content).hexdigest()}


def replace_config(path, text, revision, state):
    """Caller holds the state lock; validate, back up, then atomically replace."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('WebUI 不支持修改符号链接配置；请在终端编辑')
    config = parse_config(text)
    previous = config_document(path)
    if previous['revision'] != revision:
        raise RuntimeError('配置已被其他操作修改；请重新载入后编辑')
    if state['mode'] == 'manual':
        target = state['manual_target']
        old_address = state['nodes'].get(target, {}).get('address')
        if not any(n.id == target and n.enabled and n.address == old_address for n in config.nodes):
            raise ValueError('请先恢复自动模式，再删除、禁用或修改手动目标地址')
    metadata = path.stat()
    for destination, content in ((path.with_name(path.name + '.webui.bak'), previous['text']), (path, text)):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = stream.name
                os.fchmod(stream.fileno(), metadata.st_mode & 0o777)
                if (os.getuid(), os.getgid()) != (metadata.st_uid, metadata.st_gid):
                    os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return config_document(path)


@contextmanager
def command_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise RuntimeError('管理器忙，另一个命令正在执行，请稍后重试') from exc
        try:
            yield
        finally:
            release()
