"""Pure health transitions and relay selection; no network or filesystem I/O."""

from statistics import median

SAMPLE_TTL_SECONDS = 120


def new_health(address):
    return dict(address=address, health='unknown', successes=0, failures=0,
                samples=[], last_probe=None, rtt_ms=None, error=None, source='CNHZ',
                last_attempts=[])


def fresh_samples(health, policy, now):
    return [point for point in health['samples']
            if 0 <= now - point['at'] < SAMPLE_TTL_SECONDS][-policy.rtt_sample_count:]


def expire_health(health, policy, now):
    health['samples'] = fresh_samples(health, policy, now)
    last = health.get('last_probe')
    if last is None or not 0 <= now - last < SAMPLE_TTL_SECONDS:
        health.update(successes=0, failures=0, rtt_ms=None, last_attempts=[])
    if not health['samples'] and health['health'] == 'healthy':
        health['health'] = 'unknown'
        health['successes'] = 0


def record_probe(health, rtt_ms, error, now, policy):
    expire_health(health, policy, now)
    health.update(last_probe=now, rtt_ms=rtt_ms, error=error, source='CNHZ')
    if rtt_ms is None:
        health['successes'] = 0
        health['failures'] += 1
        if health['failures'] >= policy.fail_after:
            health['health'] = 'unhealthy'
            health['samples'] = []
    else:
        health['failures'] = 0
        health['successes'] += 1
        health['samples'] = (health['samples'] + [dict(at=now, rtt_ms=rtt_ms)])[-policy.rtt_sample_count:]
        if health['health'] != 'unhealthy' or health['successes'] >= policy.recover_after:
            health['health'] = 'healthy'


def effective_rtt(health, policy, now):
    points = fresh_samples(health, policy, now)
    if health['health'] != 'healthy' or not points:
        return None
    return median(point['rtt_ms'] for point in points)


def ranked_candidates(config, state, now):
    """Order healthy RTT candidates, then healthy unranked fallbacks; ignore tier."""
    for health in state['nodes'].values():
        expire_health(health, config.policy, now)
    healthy = [node for node in config.nodes
               if node.enabled and state['nodes'][node.id]['health'] == 'healthy']
    ranked = [node for node in healthy
              if node.rtt_ranked and effective_rtt(state['nodes'][node.id], config.policy, now) is not None]
    ranked.sort(key=lambda node: effective_rtt(state['nodes'][node.id], config.policy, now))
    return ranked + [node for node in healthy if not node.rtt_ranked]


def choose(config, state, actual, now):
    nodes = [node for node in config.nodes if node.enabled]
    health = state['nodes']
    policy = config.policy
    for entry in health.values():
        expire_health(entry, policy, now)
    if state['mode'] == 'manual':
        target = next((n for n in nodes if n.id == state['manual_target']), None)
        if target is None:
            return None, '手动目标已删除或禁用；保留实际值'
        if health[target.id]['health'] != 'healthy' or health[target.id].get('rtt_ms') is None:
            return None, '手动目标探测失败或非健康；保留固定模式并提示故障'
        return target, '手动固定'

    candidates = ranked_candidates(config, state, now)
    if not candidates:
        return None, '无已验证可用节点；保留实际值'
    best = candidates[0]
    if state.get('auto_reselect'):
        return best, '恢复自动模式，重新按有效 RTT 选择最优节点'
    # When hbbs has restarted, retain the last confirmed choice during hold time.
    current_address = actual[0] if len(actual) == 1 else state.get('confirmed_target') if not actual else None
    current = next((n for n in nodes if n.address == current_address), None)
    if current is None or health[current.id]['health'] == 'unhealthy':
        return best, '初始化选择或故障切换'
    if health[current.id]['health'] == 'unknown':
        return None, '当前节点健康未知；等待探测确认'
    if best.id == current.id:
        return current, '保持当前最优节点'
    if now - state.get('last_switch', 0) < policy.minimum_hold_seconds:
        return current, '保持期内，暂不主动切换'
    if current.rtt_ranked:
        current_rtt = effective_rtt(health[current.id], policy, now)
        if current_rtt is None:
            return best, '当前节点无有效 RTT；选择可用候选'
        if not best.rtt_ranked:
            return current, '当前排名节点健康'
        best_rtt = effective_rtt(health[best.id], policy, now)
        if best_rtt >= current_rtt or current_rtt - best_rtt < policy.rtt_switch_margin_ms:
            return current, 'RTT 样本不足或改善未达到阈值'
        return best, 'RTT 改善达到阈值'
    return best, 'RTT 排名节点可用或配置顺序兜底'
