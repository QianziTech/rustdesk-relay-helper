import unittest
from dataclasses import replace

from relay_helper.config import Config, Node, Policy
from relay_helper.policy import choose, effective_rtt, expire_health, new_health, ranked_candidates, record_probe


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(Policy(), (
            Node('a', 'a.test:21117', 10),
            Node('b', 'b.test:21117', 10),
            Node('fallback', 'fallback.test:21117', 10, rtt_ranked=False),
            Node('lower', 'lower.test:21117', 20),
        ))
        self.state = dict(mode='auto', manual_target=None, last_switch=0,
                          confirmed_target=None, nodes={n.id: new_health(n.address) for n in self.config.nodes})

    def healthy(self, node, rtt=50):
        for _ in range(6):
            record_probe(self.state['nodes'][node], rtt, None, 1000, self.config.policy)

    def selected(self, current='a', now=1000):
        actual = [next(n.address for n in self.config.nodes if n.id == current)] if current else []
        node, _ = choose(self.config, self.state, actual, now)
        return node.id if node else None

    def test_unknown_start_preserves_actual(self):
        self.assertIsNone(self.selected())

    def test_new_node_first_success_is_healthy_and_ranked(self):
        record_probe(self.state['nodes']['a'], 10, None, 1000, self.config.policy)
        self.assertEqual(self.state['nodes']['a']['successes'], 1)
        self.assertEqual(self.selected(None), 'a')

    def test_failure_threshold_and_hold_bypass(self):
        self.healthy('a')
        self.healthy('lower', 100)
        self.state['last_switch'] = 999
        for _ in range(2):
            record_probe(self.state['nodes']['a'], None, 'down', 1000, self.config.policy)
            self.assertEqual(self.selected(), 'a')
        record_probe(self.state['nodes']['a'], None, 'down', 1000, self.config.policy)
        self.assertEqual(self.selected(), 'lower')

    def test_display_tier_does_not_override_lowest_rtt(self):
        self.healthy('a', 200)
        self.healthy('lower', 1)
        self.assertEqual(self.selected(), 'lower')
        self.assertEqual(self.selected('lower'), 'lower')
        self.config = replace(self.config, nodes=tuple(replace(n, tier=0 if n.id == 'a' else 1000)
                                                     for n in self.config.nodes))
        self.assertEqual(self.selected(None), 'lower')

    def test_rtt_margin_and_hold(self):
        self.healthy('a', 50)
        self.healthy('b', 31)
        self.assertEqual(self.selected(), 'a')
        self.healthy('b', 30)
        self.state['last_switch'] = 900
        self.assertEqual(self.selected(), 'a')
        self.state['last_switch'] = 700
        self.assertEqual(self.selected(), 'b')

    def test_auto_reselection_uses_global_rtt_despite_display_tier_hold_and_margin(self):
        self.healthy('a', 50)
        self.healthy('b', 49)
        self.healthy('fallback', 1)
        self.healthy('lower', 1)
        self.state.update(last_switch=999, auto_reselect=True)
        self.assertEqual(self.selected('fallback'), 'lower')
        self.assertEqual(self.selected('a'), 'lower')
        self.assertEqual(self.selected('lower'), 'lower')
        self.state.update(mode='manual', manual_target='fallback')
        self.assertEqual(self.selected('fallback'), 'fallback')

    def test_median_not_single_outlier(self):
        self.healthy('a', 50)
        self.healthy('b', 60)
        record_probe(self.state['nodes']['b'], 1, None, 1000, self.config.policy)
        self.assertEqual(effective_rtt(self.state['nodes']['b'], self.config.policy, 1000), 60)
        self.assertEqual(self.selected(), 'a')

    def test_partial_window_uses_all_available_samples(self):
        record_probe(self.state['nodes']['a'], 10, None, 1000, self.config.policy)
        record_probe(self.state['nodes']['a'], 30, None, 1000, self.config.policy)
        self.healthy('fallback', 200)
        self.assertEqual(effective_rtt(self.state['nodes']['a'], self.config.policy, 1000), 20)
        self.assertEqual(self.selected(None), 'a')

    def test_full_window_evicts_oldest_point(self):
        health = self.state['nodes']['a']
        for index, value in enumerate((1000, 10, 20, 30, 40, 50)):
            record_probe(health, value, None, 1000 + index, self.config.policy)
        self.assertEqual([point['rtt_ms'] for point in health['samples']], [10, 20, 30, 40, 50])
        self.assertEqual(effective_rtt(health, self.config.policy, 1005), 30)

    def test_expiry_at_two_minutes_applies_to_ranked_and_fallback_nodes(self):
        self.healthy('a')
        self.healthy('fallback')
        self.assertEqual(self.selected(None, 1119.999), 'a')
        self.assertIsNone(self.selected(None, 1120))
        for node in ('a', 'fallback'):
            self.assertEqual(self.state['nodes'][node]['health'], 'unknown')
            self.assertEqual(self.state['nodes'][node]['samples'], [])
            self.assertIsNone(self.state['nodes'][node]['rtt_ms'])

    def test_expired_point_is_not_renewed_by_new_success(self):
        health = self.state['nodes']['a']
        record_probe(health, 1, None, 900, self.config.policy)
        record_probe(health, 100, None, 1000, self.config.policy)
        self.assertEqual(effective_rtt(health, self.config.policy, 1020), 100)
        record_probe(health, 200, None, 1020, self.config.policy)
        self.assertEqual([point['at'] for point in health['samples']], [1000, 1020])

    def test_gap_breaks_recovery_without_bypassing_unhealthy_state(self):
        health = self.state['nodes']['a']
        for _ in range(3):
            record_probe(health, None, 'down', 1000, self.config.policy)
        for _ in range(5):
            record_probe(health, 10, None, 1000, self.config.policy)
        record_probe(health, 10, None, 1120, self.config.policy)
        self.assertEqual(health['successes'], 1)
        self.assertEqual(health['health'], 'unhealthy')
        self.assertIsNone(self.selected(None, 1120))
        for _ in range(5):
            record_probe(health, 10, None, 1120, self.config.policy)
        self.assertEqual(self.selected(None, 1120), 'a')

    def test_future_points_and_failure_streak_are_discarded_on_clock_rollback(self):
        health = self.state['nodes']['a']
        record_probe(health, 10, None, 1000, self.config.policy)
        record_probe(health, None, 'down', 1000, self.config.policy)
        expire_health(health, self.config.policy, 999)
        self.assertEqual(health['health'], 'unknown')
        self.assertEqual(health['samples'], [])
        self.assertEqual(health['failures'], 0)

    def test_expired_manual_target_is_not_applied_or_failed_over(self):
        self.healthy('a')
        record_probe(self.state['nodes']['b'], 1, None, 1120, self.config.policy)
        self.state.update(mode='manual', manual_target='a')
        self.assertIsNone(self.selected('a', 1120))
        self.assertEqual(self.state['manual_target'], 'a')

    def test_fallback_is_automatic_and_does_not_compete_on_rtt(self):
        self.healthy('fallback', 1)
        self.healthy('lower', 1)
        self.assertEqual(self.selected(None), 'lower')
        self.healthy('a', 500)
        self.assertEqual(self.selected(None), 'lower')
        self.assertEqual(self.selected('fallback'), 'lower')
        self.state['last_switch'] = 900
        self.assertEqual(self.selected('fallback'), 'fallback')

    def test_cross_tier_rtt_switch_still_respects_hold_and_margin(self):
        self.healthy('a', 50)
        self.healthy('lower', 31)
        self.assertEqual(self.selected(), 'a')
        self.healthy('lower', 30)
        self.state['last_switch'] = 900
        self.assertEqual(self.selected(), 'a')
        self.state['last_switch'] = 700
        self.assertEqual(self.selected(), 'lower')

    def test_rank_order_excludes_disabled_failed_and_insufficient_samples(self):
        self.healthy('a', 80)
        self.healthy('b', 10)
        self.healthy('lower', 1)
        self.healthy('fallback', .1)
        self.config = replace(self.config, nodes=tuple(replace(n, enabled=False) if n.id == 'lower' else n
                                                     for n in self.config.nodes))
        self.assertEqual([n.id for n in ranked_candidates(self.config, self.state, 1000)], ['b', 'a', 'fallback'])
        for _ in range(3):
            record_probe(self.state['nodes']['b'], None, 'down', 1000, self.config.policy)
        self.assertEqual([n.id for n in ranked_candidates(self.config, self.state, 1000)], ['a', 'fallback'])
        self.state['nodes']['a']['samples'] = []
        self.assertEqual([n.id for n in ranked_candidates(self.config, self.state, 1000)], ['fallback'])
        self.assertEqual(self.selected(None), 'fallback')

    def test_equal_rtt_and_unranked_fallbacks_use_config_order_not_tier(self):
        self.healthy('a', 10)
        self.healthy('b', 10)
        self.config = replace(self.config, nodes=tuple(replace(n, tier=0 if n.id == 'b' else 1000)
                                                     for n in self.config.nodes))
        self.assertEqual(self.selected(None), 'a')
        self.assertEqual(self.selected('b'), 'b')
        self.config = replace(self.config, nodes=tuple(replace(n, rtt_ranked=False) for n in self.config.nodes))
        self.assertEqual(self.selected(None), 'a')

    def test_pure_priority_configuration_order(self):
        self.config = replace(self.config, nodes=tuple(replace(n, rtt_ranked=False) for n in self.config.nodes))
        self.healthy('a', 500)
        self.healthy('b', 1)
        self.assertEqual(self.selected('b'), 'a')

    def test_recovery_requires_six_successes_and_hold(self):
        self.healthy('a')
        self.healthy('lower')
        for _ in range(3):
            record_probe(self.state['nodes']['a'], None, 'down', 1000, self.config.policy)
        for _ in range(5):
            record_probe(self.state['nodes']['a'], 1, None, 1000, self.config.policy)
        self.assertEqual(self.selected('lower'), 'lower')
        record_probe(self.state['nodes']['a'], 1, None, 1000, self.config.policy)
        self.state['last_switch'] = 800
        self.assertEqual(self.selected('lower'), 'lower')
        self.assertEqual(self.selected('lower', 1100), 'a')

    def test_all_failed_never_clears_actual(self):
        for health in self.state['nodes'].values():
            for _ in range(3):
                record_probe(health, None, 'down', 1000, self.config.policy)
        self.assertIsNone(self.selected())

    def test_manual_never_fails_over(self):
        self.healthy('a')
        self.healthy('b', 1)
        self.state.update(mode='manual', manual_target='a')
        self.assertEqual(self.selected(), 'a')
        for _ in range(3):
            record_probe(self.state['nodes']['a'], None, 'down', 1000, self.config.policy)
        self.assertIsNone(self.selected())

    def test_manual_first_failure_does_not_reapply_historical_healthy_target(self):
        self.healthy('a')
        self.healthy('b', 1)
        self.state.update(mode='manual', manual_target='a')
        record_probe(self.state['nodes']['a'], None, 'down', 1000, self.config.policy)
        self.assertEqual(self.state['nodes']['a']['health'], 'healthy')
        self.assertIsNone(self.selected(None))
        self.assertEqual(self.state['manual_target'], 'a')

    def test_hbbs_restart_reapplies_confirmed_target_during_hold(self):
        self.healthy('a', 100)
        self.healthy('b', 1)
        self.state.update(last_switch=999, confirmed_target='a.test:21117')
        self.assertEqual(self.selected(None), 'a')

    def test_disabled_node_not_selected(self):
        self.healthy('a', 1)
        self.healthy('lower', 100)
        self.config = replace(self.config, nodes=tuple(replace(n, enabled=False) if n.id == 'a' else n for n in self.config.nodes))
        self.assertEqual(self.selected(), 'lower')


if __name__ == '__main__':
    unittest.main()
