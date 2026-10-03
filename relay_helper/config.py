"""INI configuration and relay address validation."""

import configparser
import ipaddress
import math
import re
from dataclasses import asdict, dataclass
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
    return parse_config(Path(path).read_text(encoding='utf-8'))


def parse_config(text):
    """Validate CLI files and WebUI drafts with the same rules."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
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


def config_model(text):
    """Map a valid INI draft to the form, including omitted default values."""
    config = parse_config(text)
    return {'policy': asdict(config.policy), 'nodes': [asdict(node) for node in config.nodes]}


def _model_config(model):
    if not isinstance(model, dict) or set(model) != {'policy', 'nodes'}:
        raise ValueError('表单必须包含 policy 和 nodes')
    policy, nodes = model['policy'], model['nodes']
    if not isinstance(policy, dict) or set(policy) != set(Policy.__dataclass_fields__):
        raise ValueError('表单策略字段不完整或包含未知字段')
    for name, value in policy.items():
        integer = isinstance(getattr(Policy(), name), int)
        if type(value) not in ((int,) if integer else (int, float)):
            raise ValueError('策略字段必须为' + ('整数: ' if integer else '数字: ') + name)
    if not isinstance(nodes, list) or not nodes:
        raise ValueError('表单至少需要一个节点')
    sections = [('policy', policy)]
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != set(Node.__dataclass_fields__):
            raise ValueError('节点表单字段不完整或包含未知字段')
        if (type(node['id']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', node['id'])
                or type(node['address']) is not str or type(node['tier']) is not int
                or type(node['enabled']) is not bool or type(node['rtt_ranked']) is not bool):
            raise ValueError('节点表单的 ID、地址、层级或开关类型无效')
        if node['id'] in ids:
            raise ValueError('节点 ID 重复: ' + node['id'])
        ids.add(node['id'])
        sections.append(('node:' + node['id'], {key: value for key, value in node.items() if key != 'id'}))
    text = '\n'.join(_section_text(name, values) for name, values in sections)
    return parse_config(text)


def _value_text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def _section_text(name, values):
    return '[{}]\n{}\n'.format(name, '\n'.join(key + ' = ' + _value_text(value) for key, value in values.items()))


def _update_section(lines, before, after):
    changed = {key: value for key, value in after.items() if before.get(key) != value}
    if not changed:
        return ''.join(lines)
    result, found, replacing = [lines[0]], set(), False
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', ';')):
            result.append(line)
            continue
        match = re.match(r'^([ \t]*([^:=]+?)[ \t]*[:=][ \t]*)(.*)$', line.rstrip('\r\n'))
        if match:
            key = match[2].strip().lower()
            replacing = key in changed
            if replacing:
                ending = '\r\n' if line.endswith('\r\n') else '\n'
                result.append(match[1] + _value_text(changed[key]) + ending)
                found.add(key)
            else:
                result.append(line)
        elif not replacing:
            result.append(line)
    if result and not result[-1].endswith('\n'):
        result[-1] += '\n'
    for key, value in changed.items():
        if key not in found:
            result.append(key + ' = ' + _value_text(value) + '\n')
    return ''.join(result)


def update_config_text(text, model):
    """Project a form onto its draft, retaining existing comments and defaults."""
    previous, desired = parse_config(text), _model_config(model)
    if previous == desired:
        return text
    prefix, sections, current = [], {}, None
    for line in text.splitlines(keepends=True):
        match = re.match(r'^[ \t]*\[([^\]]+)\]', line)
        if match:
            current = match[1]
            sections[current] = []
        (prefix if current is None else sections[current]).append(line)
    parts = [''.join(prefix), _update_section(sections['policy'], asdict(previous.policy), asdict(desired.policy))]
    old_nodes = {node.id: node for node in previous.nodes}
    for node in desired.nodes:
        values = {key: value for key, value in asdict(node).items() if key != 'id'}
        name = 'node:' + node.id
        parts.append(_update_section(sections[name], asdict(old_nodes[node.id]), values)
                     if node.id in old_nodes else _section_text(name, values))
    result = ''
    for part in parts:
        if result and part and not result.endswith('\n'):
            result += '\n'
        result += part
    if parse_config(result) != desired:
        raise ValueError('无法安全映射此 INI 格式，请使用文本编辑器修改')
    return result
