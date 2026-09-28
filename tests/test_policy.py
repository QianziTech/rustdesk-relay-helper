import unittest
from dataclasses import replace

from relay_helper.config import Config, Node, Policy
from relay_helper.policy import choose, effective_rtt, new_health, record_probe


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

    def test_six_successes_required(self):
        for _ in range(5):
            record_probe(self.state['nodes']['a'], 10, None, 1, self.config.policy)
        self.assertIsNone(self.selected(None))
        record_probe(self.state['nodes']['a'], 10, None, 1, self.config.policy)
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

    def test_tier_beats_rtt(self):
        self.healthy('a', 200)
        self.healthy('lower', 1)
        self.assertEqual(self.selected(), 'a')
        self.assertEqual(self.selected('lower'), 'a')

    def test_rtt_margin_and_hold(self):
        self.healthy('a', 50)
        self.healthy('b', 31)
        self.assertEqual(self.selected(), 'a')
        self.healthy('b', 30)
        self.state['last_switch'] = 900
        self.assertEqual(self.selected(), 'a')
        self.state['last_switch'] = 700
        self.assertEqual(self.selected(), 'b')

    def test_median_not_single_outlier(self):
        self.healthy('a', 50)
        self.healthy('b', 60)
        record_probe(self.state['nodes']['b'], 1, None, 1000, self.config.policy)
        self.assertEqual(effective_rtt(self.state['nodes']['b'], self.config.policy), 60)
        self.assertEqual(self.selected(), 'a')

    def test_insufficient_samples_not_ranked(self):
        self.config = replace(self.config, policy=replace(self.config.policy, rtt_sample_count=8))
        self.healthy('a')
        self.healthy('fallback', 200)
        self.assertIsNone(effective_rtt(self.state['nodes']['a'], self.config.policy))
        self.assertEqual(self.selected(None), 'fallback')

    def test_fallback_is_automatic_and_does_not_compete_on_rtt(self):
        self.healthy('fallback', 1)
        self.healthy('lower', 1)
        self.assertEqual(self.selected(None), 'fallback')
        self.healthy('a', 500)
        self.assertEqual(self.selected(None), 'a')
        self.assertEqual(self.selected('fallback'), 'a')
        self.state['last_switch'] = 900
        self.assertEqual(self.selected('fallback'), 'fallback')

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
        self.state['last_switch'] = 900
        self.assertEqual(self.selected('lower'), 'lower')
        self.assertEqual(self.selected('lower', 1200), 'a')

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
