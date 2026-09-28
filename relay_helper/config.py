"""INI configuration and relay address validation."""

import configparser
import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


def endpoint(address):
    """Require one host:port or [IPv6]:port, never a console command/list."""
    if not address or any(c.isspace() for c in address) or ',' in address:
        raise ValueError('relay 地址必须是单个 host:port 或 [IPv6]:port')
    if address.startswith('['):
        match = re.fullmatch(r'\[([^\]]+)\]:(\d+)', address)
        if not match:
            raise ValueError('IPv6 地址必须使用 [IPv6]:port')
        host, port = match.groups()
        ipaddress.IPv6Address(host)
    else:
        match = re.fullmatch(r'([A-Za-z0-9_.-]+):(\d+)', address)
        if not match:
            raise ValueError('relay 地址必须包含主机和端口')
        host, port = match.groups()
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError('端口必须在 1–65535 之间')
    if len(address.encode('utf-8')) > 1000:
        raise ValueError('relay 地址过长')
    return host, port


@dataclass(frozen=True)
class Policy:
    probe_interval_seconds: int = 20
    connect_timeout_seconds: float = 2.0
    fail_after: int = 3
    recover_after: int = 6
    minimum_hold_seconds: float = 300.0
    rtt_sample_count: int = 5
    rtt_switch_margin_ms: float = 20.0


@dataclass(frozen=True)
class Node:
    id: str
    address: str
    tier: int
    enabled: bool = True
    rtt_ranked: bool = True


@dataclass(frozen=True)
class Config:
    policy: Policy
    nodes: Tuple[Node, ...]


def load_config(path):
    parser = configparser.ConfigParser(interpolation=None)
    with Path(path).open(encoding='utf-8') as stream:
        parser.read_file(stream)
    if parser.defaults():
        raise ValueError('不支持 INI DEFAULT，请在具体节内配置')
    if not parser.has_section('policy'):
        raise ValueError('缺少 [policy]')
    defaults = Policy()
    values = {}
    for name in parser['policy']:
        if name not in defaults.__dataclass_fields__:
            raise ValueError('未知策略参数: ' + name)
    for name in defaults.__dataclass_fields__:
        default = getattr(defaults, name)
        value = type(default)(parser['policy'].get(name, default))
        minimum = 0 if name in ('minimum_hold_seconds', 'rtt_switch_margin_ms') else 1e-9
        if not math.isfinite(value) or value < minimum:
            raise ValueError('策略参数取值不合法: ' + name)
        values[name] = value
    nodes = []
    addresses = set()
    for section in parser.sections():
        if section == 'policy':
            continue
        if not section.startswith('node:') or not re.fullmatch(r'[A-Za-z0-9_-]+', section[5:]):
            raise ValueError('节点节应为 [node:<id>]，id 仅含字母、数字、下划线或连字符')
        entry = parser[section]
        if set(entry) - {'address', 'tier', 'enabled', 'rtt_ranked'}:
            raise ValueError('未知节点配置项: ' + section)
        address = entry['address']
        endpoint(address)
        if address in addresses:
            raise ValueError('节点地址重复: ' + address)
        addresses.add(address)
        nodes.append(Node(section[5:], address, entry.getint('tier'),
                          entry.getboolean('enabled', fallback=True),
                          entry.getboolean('rtt_ranked', fallback=True)))
        if nodes[-1].tier is None or nodes[-1].tier < 0:
            raise ValueError('节点必须配置非负整数 tier')
    if not nodes:
        raise ValueError('至少配置一个节点')
    return Config(Policy(**values), tuple(nodes))
