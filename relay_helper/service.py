"""Shared operations for CLI and future callers."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .io import ConsoleError, probe_tcp
from .policy import choose, record_probe

LOG = logging.getLogger('rustdesk-relay-helper')


def probe(config, state):
    enabled = [node for node in config.nodes if node.enabled]
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(enabled)))) as pool:
        results = pool.map(lambda node: probe_tcp(node, config.policy.connect_timeout_seconds), enabled)
        for node, (rtt, error) in zip(enabled, results):
            health = state['nodes'][node.id]
            previous = health['health']
            record_probe(health, rtt, error, time.time(), config.policy)
            LOG.info('probe node=%s source=CNHZ rtt_ms=%s health=%s error=%s',
                     node.id, rtt, health['health'], error)
            if health['health'] != previous:
                LOG.info('health node=%s %s -> %s', node.id, previous, health['health'])


def read_actual(state, console):
    state['actual_checked_at'] = time.time()
    try:
        state['actual'] = console.read()
        state['actual_error'] = None
    except ConsoleError as exc:
        state['actual'] = None
        state['actual_error'] = str(exc)
        raise
    return state['actual']


def reconcile(config, state, console):
    actual = read_actual(state, console)
    if state.get('pending_target') and actual == [state['pending_target']]:
        state['confirmed_target'] = state.pop('pending_target')
        state['last_switch'] = time.time()
        LOG.info('switch target=%s reason=previous_write_now_confirmed', state['confirmed_target'])
    target, reason = choose(config, state, actual, time.time())
    state['reason'] = reason
    state['target'] = target.address if target else None
    if target is None:
        LOG.warning('%s', reason)
        return
    try:
        changed = console.apply(target.address)
    except ConsoleError as exc:
        state['actual'] = None
        state['actual_error'] = str(exc)
        state['pending_target'] = target.address
        LOG.error('%s', exc)
        raise
    state['actual'] = [target.address]
    state['actual_error'] = None
    state['actual_checked_at'] = time.time()
    state['confirmed_target'] = target.address
    if changed:
        state['last_switch'] = time.time()
        LOG.info('switch target=%s reason=%s', target.address, reason)
    state['pending_target'] = None


def set_manual(config, state, node_id):
    node = next((n for n in config.nodes if n.id == node_id and n.enabled), None)
    if node is None:
        raise ValueError('节点不存在或已禁用: ' + node_id)
    if state['nodes'][node.id]['health'] != 'healthy':
        raise ValueError('节点最近状态不健康；请先执行 probe 积累恢复计数')
    state['mode'] = 'manual'
    state['manual_target'] = node.id
    LOG.info('mode=manual target=%s', node.id)
