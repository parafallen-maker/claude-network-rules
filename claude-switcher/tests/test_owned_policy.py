import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('policy', ROOT / 'core/clash-policy.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class OwnedPolicyTest(unittest.TestCase):
    def setUp(self):
        self.rules = []
        for kind, value in m.expected_rules():
            typ, payload, proxy = {
                'port': ('AND', f'((Network,udp) && (DstPort,{value}))', 'REJECT'),
                'domain': ('DomainSuffix', value, 'test-proxy'),
                'cidr': ('IPCIDR', value, 'DIRECT'),
                'match': ('Match', '', 'test-proxy'),
                'geosite': ('GeoSite', value, 'DIRECT'),
                'geoip': ('GeoIP', str(value).upper(), 'DIRECT'),
            }[kind]
            self.rules.append(dict(type=typ, payload=payload, proxy=proxy))
        self.state = {'/configs': {'mode': 'rule'}, '/rules': {'rules': self.rules},
                      '/proxies': {'proxies': {'test-proxy': {'all': ['node'], 'now': 'node'},
                                               'node': {'type': 'Vless', 'udp': True}}}}

    def verify(self):
        with patch.object(m, 'controller_config', return_value=(None, None)), patch.object(m, 'api_get', side_effect=lambda a,b,p: self.state[p]):
            return m.verify_runtime(Path('.'), 'test-proxy', 'reject')

    def rejects(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.verify()

    def test_exact_table_passes(self):
        self.assertEqual(len(self.verify()['rules']['rules']), 51)

    def test_extra_direct_rejected(self):
        self.rules.insert(-1, dict(type='DomainSuffix', payload='example.com', proxy='DIRECT'))
        self.rejects()

    def test_match_direct_rejected(self):
        self.rules[-1]['proxy'] = 'DIRECT'
        self.rejects()

    def test_stun_policy_mismatch_rejected(self):
        self.rules[0]['proxy'] = 'test-proxy'
        self.rejects()

    def test_disabled_rule_rejected(self):
        self.rules[3]['extra'] = {'disabled': True}
        self.rejects()

    def test_domestic_direct_rule_rejected_if_changed(self):
        self.rules[-2]['proxy'] = 'test-proxy'
        self.rejects()

    def test_domestic_direct_rule_rejected_if_moved_above_claude(self):
        self.rules.insert(3, self.rules.pop(-2))
        self.rejects()

    def test_global_mode_rejected(self):
        self.state['/configs']['mode'] = 'global'
        self.rejects()

    def test_direct_node_rejected(self):
        self.state['/proxies']['proxies']['node']['type'] = 'Direct'
        self.rejects()

    def test_public_cidr_rejected(self):
        value = json.loads((ROOT/'core/owned-policy.json').read_text())
        value['local_cidrs'] = ['0.0.0.0/0']
        with patch.object(Path, 'read_text', return_value=json.dumps(value)), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.rule_strings('test-proxy', 'reject')

    def test_script_replaces_routes_preserves_other_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'Script.js'
            path.write_text('function main(config) { config.extra = 42; return config; }\n')
            first = m.script_text(path, 'test-proxy', 'reject')
            path.write_text(first)
            self.assertEqual(first, m.script_text(path, 'test-proxy', 'reject'))
            js = first + '''\nconst assert = require('assert');
const config = {mode:'global',rules:['MATCH,DIRECT'],dns:{enable:true},'proxy-groups':[{name:'test-proxy'}]};
const result = main(config);
assert.equal(result.extra,42);assert.deepEqual(result.dns,{enable:true});
assert.equal(result.mode,'rule');assert.equal(result.rules.length,51);
assert.equal(result.rules.at(-1),'MATCH,test-proxy');assert(!result.rules.includes('MATCH,DIRECT'));
'''
            subprocess.run(['node', '-e', js], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
