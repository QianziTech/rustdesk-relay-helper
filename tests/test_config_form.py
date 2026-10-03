import copy
import unittest

from relay_helper.config import config_model, parse_config, update_config_text


TEXT = ('# 管理器配置\n[policy]\nrecover_after = 6\n\n'
        '# 主节点\n[node:a]\naddress = a.test:21117\ntier = 10\n'
        'enabled = yes\nrtt_ranked = off\n\n'
        '[node:b]\naddress = [::1]:21117\ntier = 10\n')


class ConfigFormTests(unittest.TestCase):
    def test_mapping_supplies_defaults_and_typed_fields_without_changing_text(self):
        model = config_model(TEXT)
        self.assertEqual(model['policy']['connect_timeout_seconds'], 2.0)
        self.assertEqual(model['policy']['fail_after'], 3)
        self.assertEqual(model['nodes'][0]['enabled'], True)
        self.assertEqual(model['nodes'][0]['rtt_ranked'], False)
        self.assertEqual(model['nodes'][1]['address'], '[::1]:21117')
        self.assertEqual(update_config_text(TEXT, model), TEXT)

    def test_changed_fields_keep_comments_aliases_and_omitted_defaults(self):
        model = config_model(TEXT)
        model['policy']['recover_after'] = 8
        model['policy']['connect_timeout_seconds'] = .5
        model['nodes'][0]['tier'] = 20
        result = update_config_text(TEXT, model)
        self.assertIn('# 管理器配置', result)
        self.assertIn('# 主节点', result)
        self.assertIn('enabled = yes\nrtt_ranked = off', result)
        self.assertIn('recover_after = 8', result)
        self.assertIn('connect_timeout_seconds = 0.5', result)
        self.assertNotIn('fail_after', result)
        self.assertEqual(config_model(result), model)
        self.assertIn('tier = 10', TEXT)

    def test_colon_delimiters_case_and_multiline_values_round_trip(self):
        text = '[policy]\nRECOVER_AFTER:\n  6\n; 保留说明\n[node:a]\naddress: a.test:21117\ntier: 10'
        model = config_model(text)
        model['policy']['recover_after'] = 7
        model['nodes'][0]['tier'] = 12
        result = update_config_text(text, model)
        self.assertIn('RECOVER_AFTER:7\n', result)
        self.assertNotIn('  6', result)
        self.assertIn('; 保留说明', result)
        self.assertEqual(config_model(result), model)

    def test_reordering_preserves_config_order_and_node_comments(self):
        model = config_model(TEXT)
        model['nodes'].reverse()
        result = update_config_text(TEXT, model)
        self.assertLess(result.index('[node:b]'), result.index('[node:a]'))
        self.assertIn('# 主节点', result)
        self.assertEqual([node.id for node in parse_config(result).nodes], ['b', 'a'])

    def test_add_remove_and_rename_nodes(self):
        model = config_model(TEXT)
        model['nodes'].pop(0)
        model['nodes'].append({'id': 'new', 'address': 'new.test:21117', 'tier': 30,
                               'enabled': False, 'rtt_ranked': True})
        result = update_config_text(TEXT, model)
        self.assertNotIn('[node:a]', result)
        self.assertIn('[node:new]', result)
        self.assertEqual(config_model(result), model)
        model['nodes'][0]['id'] = 'renamed'
        self.assertEqual(config_model(update_config_text(result, model)), model)

    def test_invalid_models_are_rejected_by_shared_validation(self):
        base = config_model(TEXT)
        changes = [
            lambda m: m['policy'].update(recover_after=True),
            lambda m: m['policy'].update(recover_after=1.5),
            lambda m: m['policy'].update(connect_timeout_seconds=0),
            lambda m: m['policy'].update(connect_timeout_seconds=float('nan')),
            lambda m: m['nodes'][0].update(tier=-1),
            lambda m: m['nodes'][0].update(tier=True),
            lambda m: m['nodes'][0].update(enabled='false'),
            lambda m: m['nodes'][0].update(id='a\n[node:injected]'),
            lambda m: m['nodes'][0].update(address='a.test:21117\nrs evil.test:21117'),
            lambda m: m['nodes'][1].update(id='a'),
            lambda m: m['nodes'][1].update(address='a.test:21117'),
            lambda m: m['nodes'].clear(),
            lambda m: m['policy'].pop('fail_after'),
            lambda m: m['nodes'][0].update(unknown=True),
        ]
        for change in changes:
            model = copy.deepcopy(base)
            change(model)
            with self.subTest(model=model), self.assertRaises(ValueError):
                update_config_text(TEXT, model)
        self.assertEqual(config_model(TEXT), base)


if __name__ == '__main__':
    unittest.main()
