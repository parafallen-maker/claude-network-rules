import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('environment', ROOT/'core/environment-config.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.clash = self.home/'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
        self.script = self.clash/'profiles/Script.js'
        self.script.parent.mkdir(parents=True)
        self.original = 'function main(config) { config.custom = 42; return config; }\n'
        self.script.write_text(self.original)
        self.base = self.home/'.claude-switcher'
        self.base.mkdir()
        (self.base/'webrtc-policy').write_text('reject\n')
        shell = self.home/'.local/bin/claude-switch.sh'
        shell.parent.mkdir(parents=True)
        shell.write_text('PROXY_GROUP="test-proxy"\nPROXY_MODE="clash-verge"\nCOUNTRY_TARGET="US"\nTZ_TARGET="America/Chicago"\nLOCALE_TARGET="en_US"\n')
        self.options = dict(m.DEFAULTS, proxy_group='test-proxy')

    def execute(self, action, options=None, activation=None):
        return m.execute(action, options or self.options, self.home,
                         activation=activation or (lambda *a: (True, True, '')))

    def test_status_and_plan_are_read_only(self):
        before = sorted(str(p) for p in self.home.rglob('*'))
        self.assertEqual(self.execute('status')['region']['timezone'], 'America/Chicago')
        self.assertEqual(len(self.execute('plan')['changes']), 5)
        self.assertEqual(before, sorted(str(p) for p in self.home.rglob('*')))
        self.assertEqual(self.script.read_text(), self.original)

    def test_group_change_and_invalid_types_rejected(self):
        for change in ({'proxy_group':'other'}, {'browser_port':True}, {'auto_group':1}, {'extra':True}):
            with self.assertRaises(m.Error):
                self.execute('plan', dict(self.options, **change))

    def test_apply_and_restore_private_backup(self):
        result = self.execute('apply')
        self.assertTrue(result['complete'])
        self.assertEqual((self.base/m.BACKUP).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.base/m.SETTINGS).stat().st_mode & 0o777, 0o600)
        with patch.object(m, 'restart_clash'):
            result = self.execute('restore')
        self.assertFalse(result['complete'])
        self.assertEqual(self.script.read_text(), self.original)
        self.assertFalse((self.base/m.SETTINGS).exists())
        self.assertFalse((self.base/m.BACKUP).exists())

    def test_inactive_rolls_back_and_keeps_recovery(self):
        with self.assertRaises(m.Error):
            self.execute('apply', activation=lambda *a: (False, False, 'not loaded'))
        self.assertEqual(self.script.read_text(), self.original)
        self.assertFalse((self.base/m.SETTINGS).exists())
        self.assertTrue((self.base/m.BACKUP).exists())
        with self.assertRaises(m.Error):
            self.execute('apply')
        with patch.object(m, 'restart_clash'):
            self.execute('restore')

    def test_new_reject_group_is_partial(self):
        result = self.execute('apply', activation=lambda *a: (True, False, 'choose node'))
        self.assertFalse(result['complete'])
        self.assertTrue(result['active'])
        self.assertIn('choose node', result['manual_required'])

    def test_external_changes_never_overwritten(self):
        self.execute('apply')
        self.script.write_text(self.script.read_text()+'// external change\n')
        changed = self.script.read_text()
        with patch.object(m, 'restart_clash') as restart:
            with self.assertRaises(m.Error):
                self.execute('restore')
            restart.assert_not_called()
        self.assertEqual(changed, self.script.read_text())
        with self.assertRaises(m.Error):
            self.execute('apply', dict(self.options, browser_port=7899))

    def test_symlink_refused(self):
        self.script.unlink()
        target = self.home/'outside'
        target.write_text(self.original)
        self.script.symlink_to(target)
        with self.assertRaises(m.Error):
            self.execute('apply')
        self.assertEqual(target.read_text(), self.original)

    def test_generated_script_multiple_subscriptions_and_conflicts(self):
        source = m.policy.script_text(self.script, 'test-proxy', 'reject', self.options)
        js = source + '''
const assert = require('assert');
const config = {rules:['MATCH,DIRECT'],proxies:[{name:'node-a',type:'ss'},{name:'direct',type:'direct'}],dns:{listen:'127.0.0.1:1053','nameserver-policy':{'example.com':'system'}}};
const first=main(config);
assert.equal(first.custom,42); assert.equal(first.rules.length,51);
assert.deepEqual(first['proxy-groups'][0].proxies,['REJECT','node-a']);
assert.equal(first.listeners[0].listen,'127.0.0.1');
assert.equal(first.dns.nameserver[0],'https://1.1.1.1/dns-query#test-proxy');
assert.deepEqual(first.dns['nameserver-policy'],{});
assert.equal(first.dns.listen,'127.0.0.1:1053');
const existing={name:'test-proxy',type:'select',proxies:['node-b']};
const second=main({'proxy-groups':[existing],'proxy-providers':{provider:{type:'http'}}});
assert.strictEqual(second['proxy-groups'][0],existing);
const third=main({'proxy-providers':{provider:{type:'http'}}});
assert.deepEqual(third['proxy-groups'][0].use,['provider']);
assert.deepEqual(third['proxy-groups'][0].proxies,['REJECT']);
assert.throws(()=>main({'mixed-port':7898}));
assert.throws(()=>main({dns:{listen:'127.0.0.1:7898'}}));
assert.throws(()=>main({'external-controller':'127.0.0.1:7898'}));
assert.throws(()=>main({listeners:[{name:'other',port:7898}]}));
assert.throws(()=>main({listeners:[{name:'adspower-claude-local',listen:'0.0.0.0',port:7898,type:'http',proxy:'test-proxy'}]}));
const repeat=main(first); assert.equal(repeat.listeners.length,1);
'''
        subprocess.run(['node', '-e', js], check=True, capture_output=True)

    def test_disabling_managed_features_preserves_original_dns(self):
        opts = dict(self.options, enable_browser_proxy=False, configure_dns=False)
        source = m.policy.script_text(self.script, 'test-proxy', 'reject', opts)
        js = source + '''
const assert=require('assert');const dns={nameserver:['system']};
const result=main({dns,listeners:[{name:'adspower-claude-local',type:'http',listen:'127.0.0.1',port:7898,proxy:'test-proxy'},{name:'other',port:8000}]});
assert.strictEqual(result.dns,dns);assert.equal(result.listeners.length,1);assert.equal(result.listeners[0].name,'other');
'''
        subprocess.run(['node','-e',js],check=True,capture_output=True)

    def test_second_apply_partial_write_rolls_back_previous_config(self):
        self.execute('apply')
        before_script = self.script.read_text()
        before_settings = (self.base/m.SETTINGS).read_text()
        real_write = m.cm.atomic_write
        triggered = False
        def fail_once(path, data, mode=0o600):
            nonlocal triggered
            if Path(path) == self.base/m.SETTINGS and not triggered:
                triggered = True
                raise OSError('injected write failure')
            return real_write(path, data, mode)
        with patch.object(m.cm, 'atomic_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.execute('apply', dict(self.options, browser_port=7899))
        self.assertEqual(self.script.read_text(), before_script)
        self.assertEqual((self.base/m.SETTINGS).read_text(), before_settings)
        with patch.object(m, 'restart_clash'):
            self.execute('restore')
        self.assertEqual(self.script.read_text(), before_script)

    def test_installed_escaped_group(self):
        shell = self.home/'.local/bin/claude-switch.sh'
        shell.write_text(m.cm.inject_configs(shell.read_text(), {'PROXY_GROUP':'Proxy $ One'}))
        self.assertEqual(m.installed(self.home)['PROXY_GROUP'],'Proxy $ One')
        with self.assertRaises(m.Error):
            m.settings(dict(self.options, proxy_group='AI & Code'), 'AI & Code')

    def generated_yaml(self, options=None):
        options = options or self.options
        marker = json.dumps(json.dumps(options,ensure_ascii=False),ensure_ascii=False)
        return 'claude-switcher-environment: '+marker+'\n'+            'dns:\n  enable: true\n  enhanced-mode: fake-ip\n  respect-rules: true\n  nameserver:\n  - https://1.1.1.1/dns-query#test-proxy\n  nameserver-policy: {}\n  fallback: []\n  direct-nameserver: []\n'+            'listeners:\n- name: adspower-claude-local\n  type: http\n  listen: 127.0.0.1\n  port: 7898\n  proxy: test-proxy\n'

    def test_exact_generated_fields_and_disabled_listener(self):
        path = self.clash/'clash-verge.yaml'
        path.write_text(self.generated_yaml())
        with patch.object(m.socket, 'create_connection'):
            self.assertTrue(m.generated_fields(self.clash,self.options))
            path.write_text(self.generated_yaml()+'- name: other\n  type: http\n  port: 8888\n  users:\n  - username: sample\n    password: sample\n')
            self.assertTrue(m.generated_fields(self.clash,self.options))
            path.write_text(self.generated_yaml().replace('  port: 7898', '  port: 17898').replace('  proxy: test-proxy', '  proxy: DIRECT')+'- name: other-test-proxy\n  port: 7898\n')
            self.assertFalse(m.generated_fields(self.clash,self.options))
            path.write_text(self.generated_yaml().replace('  - https://1.1.1.1/dns-query#test-proxy', '  - https://wrong.example/dns-query'))
            self.assertFalse(m.generated_fields(self.clash,self.options))
            disabled = dict(self.options, configure_dns=False,enable_browser_proxy=False)
            path.write_text(self.generated_yaml(disabled))
            self.assertFalse(m.generated_fields(self.clash,disabled))
            path.write_text(self.generated_yaml(disabled).split('listeners:')[0]+'listeners: []\n')
            self.assertTrue(m.generated_fields(self.clash,disabled))

    def test_pending_transactions_block_apply(self):
        for name in ('install-journal.json','update-journal.json','account-transaction.json'):
            (self.base/name).write_text('{}')
            with self.assertRaises(m.Error):
                self.execute('apply')
            (self.base/name).unlink()

    def test_shell_forwarding_keeps_json_stdin(self):
        result = subprocess.run(['/bin/bash',str(ROOT/'core/claude-switch.sh'),'environment-config','status'],input='{}',text=True,capture_output=True,env={'HOME':str(self.home),'PATH':'/usr/bin:/bin:/opt/homebrew/bin'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['settings']['proxy_group'],'test-proxy')


if __name__ == '__main__':
    unittest.main()
