"""Isolated regression tests: no real network, process signals or installed files."""
import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('network_monitor_under_test', ROOT / 'core/network-monitor.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

NODE_A, NODE_B = m.digest('test-node-a'), m.digest('test-node-b')
IP_A, IP_B = m.digest('test-exit-a'), m.digest('test-exit-b')


def local(node=NODE_A):
    return {'ok': True, 'node': node, 'port': 17890}


def sample(ip=IP_A, country='US'):
    return {'ip': ip, 'country': country}


def args():
    return SimpleNamespace(owner_uid=501, user_home=Path('/Users/test-owner'),
                           proxy_mode='clash-verge', proxy_group='test-group', expected_country='US')


class StateTest(unittest.TestCase):
    def establish(self, now=100.0):
        state = m.MonitorState()
        with patch.object(m.time, 'time', return_value=now):
            self.assertIsNone(state.local(local()))
        self.assertIsNone(state.sample(state.generation, sample(), now, 'US'))
        self.assertTrue(state.ready(now))
        return state

    def test_startup_requires_local_and_fresh_exit_evidence(self):
        state = m.MonitorState()
        self.assertFalse(state.ready(100))
        state.sample(state.generation, sample(), 100, 'US')
        self.assertFalse(state.ready(100))
        self.assertIsNone(state.local(local()))
        self.assertFalse(state.ready(100))
        self.assertIsNone(state.sample(state.generation, sample(), 100, 'US'))
        self.assertTrue(state.ready(100))

    def test_saved_baseline_is_not_startup_readiness(self):
        state = self.establish()
        restarted = m.MonitorState(state.private())
        self.assertFalse(restarted.ready(101))
        restarted.local(local())
        self.assertFalse(restarted.ready(101))
        self.assertEqual(restarted.sample(restarted.generation, sample(IP_B), 101, 'US'), 'exit_ip_changed')
        self.assertTrue(restarted.ready(101))

    def test_node_change_invalidates_baseline_and_does_not_double_count_ip(self):
        state = self.establish()
        old_generation = state.generation
        self.assertEqual(state.local(local(NODE_B)), 'node_changed')
        self.assertGreater(state.generation, old_generation)
        self.assertIsNone(state.ip)
        self.assertFalse(state.ready(101))
        self.assertIsNone(state.sample(state.generation, sample(IP_B), 102, 'US'))
        self.assertTrue(state.ready(102))
        self.assertIsNone(state.local(local(NODE_B)))

    def test_same_node_exit_change_is_reported_once(self):
        state = self.establish()
        self.assertIsNone(state.local(local()))
        self.assertEqual(state.sample(state.generation, sample(IP_B), 105, 'US'), 'exit_ip_changed')
        self.assertIsNone(state.sample(state.generation, sample(IP_B), 110, 'US'))
        self.assertTrue(state.ready(110))

    def test_disconnect_is_deduplicated_and_reconnect_needs_fresh_sample(self):
        state = self.establish()
        generation = state.generation
        self.assertEqual(state.local({'ok': False}), 'vpn_disconnected')
        self.assertGreater(state.generation, generation)
        self.assertFalse(state.ready(101))
        self.assertIsNone(state.local({'ok': False}))
        self.assertIsNone(state.local(local()))
        self.assertFalse(state.ready(102))
        self.assertIsNone(state.sample(state.generation, sample(), 103, 'US'))
        self.assertTrue(state.ready(103))
        self.assertEqual(state.local({'ok': False}), 'vpn_disconnected')

    def test_late_sample_before_disconnect_is_ignored_after_reconnect(self):
        state = self.establish()
        old_generation = state.generation
        state.local({'ok': False})
        state.local(local())
        self.assertIsNone(state.sample(old_generation, sample(IP_B), 103, 'US'))
        self.assertFalse(state.ready(103))
        self.assertEqual(state.ip, IP_A)
        self.assertEqual(state.sample(state.generation, sample(IP_B), 104, 'US'), 'exit_ip_changed')

    def test_late_sample_from_previous_node_is_ignored(self):
        state = self.establish()
        old_generation = state.generation
        state.local(local(NODE_B))
        self.assertIsNone(state.sample(old_generation, sample(IP_B), 105, 'US'))
        self.assertIsNone(state.ip)
        self.assertFalse(state.ready(105))

    def test_wrong_country_immediately_blocks_and_deduplicates_fault(self):
        state = self.establish()
        self.assertEqual(state.sample(state.generation, sample(IP_B, 'CN'), 105, 'US'), 'monitor_unavailable')
        self.assertFalse(state.ready(105))
        self.assertEqual(state.ip, IP_A)
        self.assertEqual(state.last_exit, 100)
        self.assertIsNone(state.sample(state.generation, sample(IP_B, 'CN'), 110, 'US'))
        self.assertIsNone(state.sample(state.generation, sample(IP_A), 111, 'US'))
        self.assertTrue(state.ready(111))

    def test_transient_probe_failure_keeps_only_unexpired_evidence(self):
        state = self.establish()
        self.assertIsNone(state.sample(state.generation, None, 105, 'US'))
        self.assertTrue(state.ready(120))
        self.assertIsNone(state.stale(120, 90))
        self.assertFalse(state.ready(120.001))
        self.assertEqual(state.stale(120.001, 90), 'monitor_unavailable')
        self.assertIsNone(state.stale(125, 90))
        self.assertIsNone(state.sample(state.generation, sample(), 126, 'US'))
        self.assertTrue(state.ready(126))

    def test_new_node_gets_its_own_probe_grace_period(self):
        state = self.establish()
        with patch.object(m.time, 'time', return_value=200):
            self.assertEqual(state.local(local(NODE_B)), 'node_changed')
        self.assertIsNone(state.stale(200, 90))
        self.assertIsNone(state.stale(220, 90))
        self.assertEqual(state.stale(220.001, 90), 'monitor_unavailable')

    def test_clock_rollback_never_marks_future_evidence_ready(self):
        state = self.establish()
        self.assertFalse(state.ready(99.999))

    def test_persisted_baselines_contain_hashes_only(self):
        value = self.establish().private()
        self.assertEqual(value['schema'], 1)
        for key in ('node', 'ip'):
            self.assertRegex(value[key], r'^[a-f0-9]{64}$')
        text = json.dumps(value)
        self.assertNotIn('test-node', text)
        self.assertNotIn('test-exit', text)


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.proxies = {
            'test-group': {'type': 'Selector', 'all': ['nested'], 'now': 'nested'},
            'nested': {'type': 'URLTest', 'all': ['leaf-a', 'leaf-b'], 'now': 'leaf-a', 'history': [{'delay': 1}]},
            'leaf-a': {'id': 'id-a', 'type': 'Vless', 'provider-name': 'provider-a',
                       'server': 'proxy.invalid', 'port': 443, 'history': [{'delay': 1}], 'delay': 1},
            'leaf-b': {'id': 'id-b', 'type': 'Vless', 'server': 'other.invalid', 'port': 443},
        }

    def fingerprint(self, value=None):
        return m.node_fingerprint(value or self.proxies, 'test-group')

    def test_delay_history_and_unselected_node_do_not_change_identity(self):
        baseline = self.fingerprint()
        self.proxies['leaf-a']['delay'] = 999
        self.proxies['leaf-a']['history'] = [{'delay': 999, 'time': 'later'}]
        self.proxies['nested']['history'] = []
        self.proxies['leaf-b']['server'] = 'unused.invalid'
        self.assertEqual(baseline, self.fingerprint())

    def test_selected_chain_and_connection_identity_change_fingerprint(self):
        baseline = self.fingerprint()
        for key, value in [('id', 'new-id'), ('type', 'Trojan'), ('provider-name', 'new-provider'),
                           ('server', 'changed.invalid'), ('port', 8443)]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.proxies)
                changed['leaf-a'][key] = value
                self.assertNotEqual(baseline, self.fingerprint(changed))
        self.proxies['nested']['now'] = 'leaf-b'
        self.assertNotEqual(baseline, self.fingerprint())

    def test_direct_reject_and_nonproxy_selections_are_rejected(self):
        for kind in m.NON_PROXIES:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                changed = copy.deepcopy(self.proxies)
                changed['leaf-a']['type'] = kind.upper()
                self.fingerprint(changed)

    def test_cycles_missing_nodes_and_empty_selection_are_rejected(self):
        for selected in ('test-group', 'missing', ''):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                changed = copy.deepcopy(self.proxies)
                changed['nested']['now'] = selected
                self.fingerprint(changed)


class ProbeTest(unittest.TestCase):
    def test_exit_probe_is_explicit_loopback_proxy_and_scrubs_proxy_environment(self):
        response = SimpleNamespace(returncode=0, stdout=b'ip=1.1.1.1\nloc=US\n')
        with patch.object(m.subprocess, 'run', return_value=response) as run:
            self.assertEqual(m.exit_sample(17890), {'ip': m.digest('1.1.1.1'), 'country': 'US'})
        command = run.call_args.args[0]
        self.assertEqual(command[0], '/usr/bin/curl')
        self.assertEqual(command[command.index('--proxy') + 1], 'http://127.0.0.1:17890')
        self.assertEqual(command[command.index('--noproxy') + 1], '')
        self.assertEqual(command[command.index('--max-time') + 1], '5')
        self.assertEqual(run.call_args.kwargs['timeout'], 6)
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
        self.assertEqual(run.call_count, 1)

    def test_failed_invalid_and_oversized_exit_response_have_no_direct_fallback(self):
        responses = [
            SimpleNamespace(returncode=7, stdout=b'ip=1.1.1.1\nloc=US\n'),
            SimpleNamespace(returncode=0, stdout=b'ip=127.0.0.1\nloc=US\n'),
            SimpleNamespace(returncode=0, stdout=b'ip=192.168.1.1\nloc=US\n'),
            SimpleNamespace(returncode=0, stdout=b'ip=invalid\nloc=US\n'),
            SimpleNamespace(returncode=0, stdout=b'ip=1.1.1.1\nloc=us\n'),
            SimpleNamespace(returncode=0, stdout=b'ip=1.1.1.1\n'),
            SimpleNamespace(returncode=0, stdout=b'\xff'),
            SimpleNamespace(returncode=0, stdout=b'x' * 8193),
        ]
        for response in responses:
            with self.subTest(response=response), patch.object(m.subprocess, 'run', return_value=response) as run:
                self.assertIsNone(m.exit_sample(17890))
                self.assertEqual(run.call_count, 1)
        with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 6)) as run:
            self.assertIsNone(m.exit_sample(17890))
            self.assertEqual(run.call_count, 1)

    def test_local_probe_has_total_deadline_and_rejects_invalid_evidence(self):
        result = SimpleNamespace(returncode=0, stdout=json.dumps(local()).encode())
        with patch.object(m.subprocess, 'run', return_value=result) as run:
            self.assertEqual(m.local_snapshot(args()), local())
        self.assertEqual(run.call_args.kwargs['timeout'], m.LOCAL_TIMEOUT)
        self.assertIn('probe-local', run.call_args.args[0])
        for value in ({'ok': True, 'node': NODE_A, 'port': True},
                      {'ok': True, 'node': NODE_A, 'port': 65536},
                      {'ok': True, 'node': 'raw-name', 'port': 17890}, {'ok': False}):
            with self.subTest(value=value), patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())):
                self.assertEqual(m.local_snapshot(args()), {'ok': False})
        with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe-local', m.LOCAL_TIMEOUT)):
            self.assertEqual(m.local_snapshot(args()), {'ok': False})


class ProcessTest(unittest.TestCase):
    def test_process_selection_is_owner_scoped_and_matches_only_actual_app_path(self):
        with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='123\n124\n')) as run:
            self.assertEqual(m.claude_pids(501), [123, 124])
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['/usr/bin/pgrep', '-u', '501', '-f'])
        pattern = command[4]
        self.assertIsNotNone(re.search(pattern, '/Applications/Claude.app/Contents/MacOS/Claude'))
        self.assertIsNotNone(re.search(pattern, '/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper'))
        for unrelated in ('/Applications/ClaudeSwitcher.app/Contents/MacOS/ClaudeSwitcher',
                          '/bin/sh -c /Applications/Claude.app/Contents/MacOS/Claude',
                          '/Applications/ClaudeXapp/Contents/MacOS/Claude',
                          '/Users/test/Claude.app/Contents/MacOS/Claude'):
            self.assertIsNone(re.search(pattern, unrelated), unrelated)

    def test_process_inspection_failure_is_not_reported_as_no_processes(self):
        with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=2, stdout='')):
            with self.assertRaises(ValueError):
                m.claude_pids(501)

    def test_stop_freezes_all_matched_processes_before_killing_and_rechecks(self):
        with patch.object(m, 'claude_pids', side_effect=[[123, 124], [123, 124], []]) as pids, \
             patch.object(m.os, 'kill') as kill, patch.object(m.time, 'sleep'):
            self.assertEqual(m.stop_claude(501), (True, False))
        self.assertEqual(kill.call_args_list, [call(123, signal.SIGSTOP), call(124, signal.SIGSTOP),
                                              call(123, signal.SIGKILL), call(124, signal.SIGKILL)])
        self.assertEqual(pids.call_count, 3)

    def test_no_processes_means_no_claim_of_stopping_claude(self):
        with patch.object(m, 'claude_pids', return_value=[]), patch.object(m.os, 'kill') as kill:
            self.assertEqual(m.stop_claude(501), (False, False))
        kill.assert_not_called()

    def test_process_exiting_during_signal_is_harmless(self):
        with patch.object(m, 'claude_pids', side_effect=[[123], [123], []]), \
             patch.object(m.os, 'kill', side_effect=ProcessLookupError), patch.object(m.time, 'sleep'):
            self.assertEqual(m.stop_claude(501), (True, False))

    def test_surviving_processes_report_stop_failure(self):
        with patch.object(m, 'claude_pids', return_value=[123]), \
             patch.object(m.os, 'kill'), patch.object(m.time, 'sleep'):
            self.assertEqual(m.stop_claude(501), (False, True))

    def test_signal_permission_failure_reports_failure_without_crashing_monitor(self):
        with patch.object(m, 'claude_pids', return_value=[123]), \
             patch.object(m.os, 'kill', side_effect=PermissionError), patch.object(m.time, 'sleep'):
            self.assertEqual(m.stop_claude(501), (False, True))

    def test_process_inspection_error_during_stop_is_reported_as_failure(self):
        for sequence in ([ValueError('inspection failed')],
                         [[123], subprocess.TimeoutExpired('pgrep', 1)],
                         [[123], [123], OSError('inspection failed')]):
            with self.subTest(sequence=sequence), patch.object(m, 'claude_pids', side_effect=sequence), \
                 patch.object(m.os, 'kill'), patch.object(m.time, 'sleep'):
                self.assertEqual(m.stop_claude(501), (False, True))


class MonitorLoopTest(unittest.TestCase):
    def test_failed_stop_on_ip_change_blocks_readiness_and_retries_without_duplicate_event(self):
        # Run the real coordinator/state machine through five deterministic polls.
        # Every external effect is replaced; no thread, network, root file or signal.
        clock = {'index': 0}
        times = [100, 101, 106, 107, 108]
        samples = iter([sample(), sample(IP_B)])
        statuses, events, stop_times = [], [], []

        class EndPolls(Exception):
            pass

        def now():
            return times[clock['index']]

        def sleep(_):
            clock['index'] += 1
            if clock['index'] == len(times):
                raise EndPolls()

        def stop(_):
            stop_times.append(now())
            if now() == 107:
                return False, True
            if now() == 108:
                return True, False
            return False, False

        def submit(function, port):
            self.assertIs(function, m.exit_sample)
            self.assertEqual(port, 17890)
            result = next(samples)
            return SimpleNamespace(done=lambda: True, result=lambda: result)

        def write(path, value, mode):
            if path.name.startswith('status-'):
                statuses.append(copy.deepcopy(value))

        pool = SimpleNamespace(submit=submit, shutdown=lambda **kwargs: None)
        patches = [
            patch.object(m.os, 'geteuid', return_value=0),
            patch.object(m.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir='/Users/test-owner')),
            patch.object(m, 'secure_directory'), patch.object(m.os, 'open', return_value=12345),
            patch.object(m.os, 'close'), patch.object(m.fcntl, 'flock'),
            patch.object(m, 'read_root_json', side_effect=FileNotFoundError),
            patch.object(m, 'write_root_json', side_effect=write),
            patch.object(m, 'local_snapshot', return_value=local()),
            patch.object(m, 'stop_claude', side_effect=stop),
            patch.object(m, 'publish_event', side_effect=lambda *values: events.append(values)),
            patch.object(m.time, 'time', side_effect=now),
            patch.object(m.time, 'monotonic', side_effect=now),
            patch.object(m.time, 'sleep', side_effect=sleep),
            patch.object(m.concurrent.futures, 'ThreadPoolExecutor', return_value=pool),
            patch.object(m.subprocess, 'run', side_effect=AssertionError('external command is not allowed in this test')),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with self.assertRaises(EndPolls):
                m.run_monitor(args())
        by_time = {status['heartbeat']: status for status in statuses}
        self.assertTrue(by_time[101]['ready'])
        self.assertEqual(by_time[101]['configuration'], m.digest(['clash-verge', 'test-group', 'US']))
        self.assertFalse(by_time[107]['ready'], 'a failed stop must block the protected-launch health check')
        self.assertIn(108, stop_times, 'the next poll must retry stopping after a failed signal')
        self.assertEqual(events, [(501, 'exit_ip_changed', False, True)])


class StorageTest(unittest.TestCase):
    def test_atomic_state_and_public_status_use_expected_permissions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'RUNTIME', Path(tmp)):
            for name, mode in [('state-501.json', 0o600), ('status-501.json', 0o644), ('events-501.json', 0o644)]:
                path = Path(tmp) / name
                m.write_root_json(path, {'schema': 1, 'value': 'old'}, mode)
                m.write_root_json(path, {'schema': 1, 'value': 'new'}, mode)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                self.assertEqual(json.loads(path.read_text())['value'], 'new')
            self.assertEqual(len(list(Path(tmp).iterdir())), 3)

    def test_state_write_refuses_symlink_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'RUNTIME', Path(tmp)):
            target = Path(tmp) / 'target'
            target.write_text('original')
            link = Path(tmp) / 'state-501.json'
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                m.write_root_json(link, {'schema': 1}, 0o600)
            self.assertEqual(target.read_text(), 'original')

    def test_root_reader_refuses_symlinks_owners_and_loose_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{"schema":1}')
            link = Path(tmp) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                m.read_root_json(link)
            for uid, mode, private in [(501, 0o600, True), (0, 0o644, True), (0, 0o666, False)]:
                info = SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_size=12)
                with self.subTest(uid=uid, mode=mode), patch.object(m.os, 'fstat', return_value=info):
                    with self.assertRaises(ValueError):
                        m.read_root_json(path, private=private)
            info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_size=12)
            with patch.object(m.os, 'fstat', return_value=info):
                self.assertEqual(m.read_root_json(path, private=True), {'schema': 1})

    def test_runtime_directory_must_be_root_owned_and_not_writable_by_others(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'RUNTIME', Path(tmp)):
            for uid, mode in [(501, stat.S_IFDIR | 0o755), (0, stat.S_IFDIR | 0o777),
                              (0, stat.S_IFLNK | 0o755)]:
                info = SimpleNamespace(st_mode=mode, st_uid=uid)
                with self.subTest(uid=uid, mode=mode), patch.object(Path, 'lstat', return_value=info):
                    with self.assertRaises(ValueError):
                        m.secure_directory()
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)):
                m.secure_directory()

    def test_status_rejects_stale_wrong_owner_or_unready_evidence(self):
        valid = {'schema': 1, 'owner_uid': 501, 'heartbeat': 100, 'ready': True, 'last_exit_check': 90}
        with patch.object(m.time, 'time', return_value=100):
            with patch.object(m, 'read_root_json', return_value=valid):
                self.assertEqual(m.read_status(501), valid)
            for changes in ({'heartbeat': 93.999}, {'heartbeat': 101}, {'owner_uid': 502},
                            {'ready': False}, {'last_exit_check': 79.999}, {'last_exit_check': 101}, {'schema': 2}):
                with self.subTest(changes=changes), patch.object(m, 'read_root_json', return_value={**valid, **changes}):
                    with self.assertRaises(ValueError):
                        m.read_status(501)

    def test_status_binds_evidence_to_current_start_configuration_and_process(self):
        configuration = m.digest(['clash-verge', 'test-group', 'US'])
        valid = {'schema': 1, 'owner_uid': 501, 'heartbeat': 100, 'ready': True,
                 'last_exit_check': 99, 'configuration': configuration, 'pid': 123}
        required = {'min_heartbeat': 100, 'expected_configuration': configuration, 'expected_pid': 123}
        with patch.object(m.time, 'time', return_value=101):
            with patch.object(m, 'read_root_json', return_value=valid):
                self.assertEqual(m.read_status(501, **required), valid)
            # These records are otherwise fresh and ready: each must be rejected
            # because it belongs to an earlier start, different target, or old PID.
            for changes in ({'heartbeat': 99.999}, {'pid': 122}, {'pid': None},
                            {'configuration': m.digest(['clash-verge', 'test-group', 'JP'])},
                            {'configuration': m.digest(['clash-verge', 'other-group', 'US'])},
                            {'configuration': m.digest(['flclash', 'test-group', 'US'])}):
                with self.subTest(changes=changes), patch.object(m, 'read_root_json', return_value={**valid, **changes}):
                    with self.assertRaises(ValueError):
                        m.read_status(501, **required)

    def test_event_contains_stop_failure_but_no_raw_address_or_node(self):
        with patch.object(m, 'write_root_json') as write, patch.object(m.subprocess, 'run'):
            m.publish_event(501, 'node_changed', False, True)
        path, envelope, mode = write.call_args.args
        self.assertEqual(path.name, 'events-501.json')
        self.assertEqual(mode, 0o644)
        event = envelope['latest']
        self.assertEqual(envelope['schema'], 1)
        self.assertEqual(event['kind'], 'node_changed')
        self.assertTrue(event['stop_failed'])
        self.assertFalse(event['claude_stopped'])
        self.assertEqual(set(event), {'id', 'time', 'kind', 'claude_stopped', 'stop_failed'})
        self.assertRegex(event['id'], r'^[a-f0-9]{32}$')


if __name__ == '__main__':
    unittest.main()
