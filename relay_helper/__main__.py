"""python -m relay_helper"""

import argparse
import configparser
import datetime
import logging
import logging.handlers
import os
from pathlib import Path
import subprocess
import sys

from .config import load_config
from .io import Console, command_lock, load_state, save_state
from .policy import effective_rtt
from .service import LOG, probe, read_actual, reconcile, set_manual


def setup_logging():
    handler = (logging.handlers.SysLogHandler(address='/dev/log')
               if Path('/dev/log').exists() else logging.StreamHandler())
    handler.setFormatter(logging.Formatter('rustdesk-relay-helper: %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def timestamp(value):
    return datetime.datetime.fromtimestamp(value).astimezone().isoformat(timespec='seconds') if value else '-'


def show_nodes(config, state):
    print('ID\t启用\ttier\tRTT排名\t健康\t成功/失败\t最近RTT(ms)\t有效RTT(ms)\t探测源\t探测时间\t地址')
    for node in config.nodes:
        health = state['nodes'][node.id]
        raw = health.get('rtt_ms')
        ranked = effective_rtt(health, config.policy) if node.rtt_ranked else None
        print('\t'.join(map(str, [node.id, node.enabled, node.tier, node.rtt_ranked,
                                   health['health'], f"{health['successes']}/{health['failures']}",
                                   '-' if raw is None else round(raw, 2),
                                   '-' if ranked is None else round(ranked, 2),
                                   health.get('source', 'CNHZ'), timestamp(health.get('last_probe')), node.address])))
        if health.get('error'):
            print('  探测错误: ' + health['error'])


def show_status(config, state):
    print('模式: ' + state['mode'])
    print('手动节点: ' + str(state.get('manual_target') or '-'))
    actual = state.get('actual')
    print('实际 relay: ' + ('未确认' if actual is None else ', '.join(actual) or '(空列表)'))
    print('实际读取时间: ' + timestamp(state.get('actual_checked_at')))
    if state.get('actual_error'):
        print('错误: ' + state['actual_error'])
    print('目标: ' + str(state.get('target') or '-'))
    print('上次确认目标: ' + str(state.get('confirmed_target') or '-'))
    print('上次决策原因: ' + state.get('reason', '-'))
    print('上次成功切换: ' + timestamp(state.get('last_switch')))
    show_nodes(config, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description='RustDesk OSS relay 管理器（CNHZ 宿主机）')
    parser.add_argument('--config', default=os.environ.get('RELAY_HELPER_CONFIG', '/etc/rustdesk-relay-helper/config.ini'))
    parser.add_argument('--state', default=os.environ.get('RELAY_HELPER_STATE', '/var/lib/rustdesk-relay-helper/state.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'nodes', 'probe', 'auto', 'reconcile'):
        sub.add_parser(command)
    sub.add_parser('switch').add_argument('id')
    history = sub.add_parser('history')
    history.add_argument('--lines', type=int, default=50)
    args = parser.parse_args(argv)
    setup_logging()
    try:
        if args.command == 'history':
            if args.lines < 1:
                raise ValueError('--lines 必须是正整数')
            return subprocess.run(['journalctl', '-t', 'rustdesk-relay-helper', '-n', str(args.lines), '--no-pager'], check=False).returncode
        with command_lock(str(args.state) + '.lock'):
            config = load_config(args.config)
            state = load_state(args.state, config)
            console = Console(config.nodes, config.policy.connect_timeout_seconds)
            if args.command == 'nodes':
                show_nodes(config, state)
                return 0
            if args.command == 'status':
                try:
                    read_actual(state, console)
                finally:
                    show_status(config, state)
                return 0
            try:
                probe(config, state)
                if args.command == 'switch':
                    set_manual(config, state, args.id)
                elif args.command == 'auto':
                    state.update(mode='auto', manual_target=None)
                    LOG.info('mode=auto')
                if args.command != 'probe':
                    reconcile(config, state, console)
            finally:
                save_state(args.state, state)
            show_status(config, state)
        return 0
    except (OSError, ValueError, KeyError, configparser.Error, RuntimeError) as exc:
        LOG.error('%s', exc)
        print('错误: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
