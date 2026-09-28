"""Local hbbs console, TCP probes, atomic JSON state and command lock."""

import json
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .config import endpoint
from .policy import new_health


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
    active = {}
    for node in config.nodes:
        health = state['nodes'].get(node.id)
        if not node.enabled or not health or health.get('address') != node.address:
            health = new_health(node.address)
        if (health.get('health') not in ('unknown', 'healthy', 'unhealthy')
                or any(not isinstance(health.get(k), int) or health[k] < 0 for k in ('successes', 'failures'))
                or not isinstance(health.get('samples'), list)
                or any(not isinstance(x, (int, float)) or x < 0 for x in health['samples'])):
            raise ValueError('节点运行状态无效: ' + node.id)
        health['samples'] = health['samples'][-config.policy.rtt_sample_count:]
        active[node.id] = health
    state['nodes'] = active
    return state


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
