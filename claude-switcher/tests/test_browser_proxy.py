"""AdsPower integration uses an isolated loopback API, never real browser data."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('browser_proxy', ROOT / 'core/browser-proxy.py')
browser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browser)


class FakeAPI:
    def __init__(self):
        self.profile = {'profile_id': 'test_profile', 'name': 'Test profile',
                        'user_proxy_config': {'proxy_soft': 'other', 'proxy_type': 'socks5',
                                              'proxy_host': 'old.invalid', 'proxy_port': '1080',
                                              'proxy_user': 'old-user', 'proxy_password': 'old-password'},
                        'fingerprint_config': {'webrtc': 'real', 'ua': 'KEEP_UA', 'canvas': 'KEEP_CANVAS'},
                        'password': 'ACCOUNT_SECRET', 'cookie': 'COOKIE_SECRET', 'username': 'KEEP_ACCOUNT'}
        self.original = copy.deepcopy(self.profile)
        self.running = False
        self.calls = []
        self.updates = []
        self.mode = ''
        self.list_count = 0
        self.proxy_pool = {}

    def respond(self, method, path, body):
        if path.startswith('/api/v2/browser-profile/active?'):
            return {'status': 'Active' if self.running else 'Inactive'}
        if path == '/api/v2/browser-profile/list':
            self.list_count += 1
            if self.mode == 'race' and self.list_count == 2:
                self.profile['user_proxy_config']['proxy_host'] = 'external.invalid'
            return {'list': [copy.deepcopy(self.profile)], 'page': 1, 'limit': 100}
        if path == '/api/v1/user/update':
            self.updates.append(copy.deepcopy(body))
            if 'proxyid' in body:
                self.profile['fbcc_proxy_acc_id'] = body['proxyid']
                self.profile['user_proxy_config'] = copy.deepcopy(self.proxy_pool[body['proxyid']])
            if 'user_proxy_config' in body:
                self.profile['user_proxy_config'] = copy.deepcopy(body['user_proxy_config'])
                self.profile['fbcc_proxy_acc_id'] = ''
            if 'fingerprint_config' in body and not (self.mode == 'partial' and len(self.updates) == 1):
                self.profile['fingerprint_config'].update(body['fingerprint_config'])
            if self.mode == 'external' and len(self.updates) == 1:
                self.profile['user_proxy_config']['proxy_port'] = '9999'
            return {}
        raise AssertionError('Unexpected API route: ' + path)


class BrowserProxyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / '.claude-switcher'
        self.api = FakeAPI()
        api = self.api

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.serve('GET')

            def do_POST(self):
                self.serve('POST')

            def serve(self, method):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(raw) if raw else None
                api.calls.append((method, self.path, body, self.headers.get('Authorization')))
                if api.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://unreachable.invalid/stolen')
                    self.end_headers()
                    return
                if api.mode == 'auth':
                    self.send_response(403)
                    self.end_headers()
                    return
                value = {'code': 0, 'data': api.respond(method, self.path, body)}
                if api.mode == 'business_error':
                    value = {'code': -1, 'data': {}, 'msg': 'API_SECRET ACCOUNT_SECRET COOKIE_SECRET'}
                payload = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.options = {'endpoint': self.address, 'api_key': 'API_SECRET', 'profile_id': 'test_profile', 'port': 7898}
        self.client = browser.Client(self.address, 'API_SECRET', interval=0)
        self.journal = browser.Journal(self.base, self.address, 'test_profile')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_action(self, action):
        return browser.execute(action, self.options, self.base, self.client)

    def test_configure_readback_and_restore_preserve_unowned_fields(self):
        expected = browser.snapshot(self.api.profile)
        result = self.run_action('configure')
        self.assertTrue(result['ok'])
        self.assertTrue(result['complete'])
        self.assertTrue(result['changed'])
        self.assertEqual(self.api.updates[0]['fingerprint_config'], {'webrtc': 'disabled'})
        self.assertEqual(set(self.api.updates[0]), {'user_id', 'user_proxy_config', 'fingerprint_config'})
        self.assertEqual(self.api.profile['fingerprint_config']['ua'], 'KEEP_UA')
        self.assertEqual(self.api.profile['fingerprint_config']['canvas'], 'KEEP_CANVAS')
        self.assertEqual(self.api.profile['username'], 'KEEP_ACCOUNT')
        self.assertTrue(all(call[3] == 'Bearer API_SECRET' for call in self.api.calls))
        self.assertFalse(self.run_action('configure')['changed'])
        self.assertTrue(self.run_action('restore')['changed'])
        self.assertEqual(browser.snapshot(self.api.profile), expected)
        self.assertFalse(self.journal.path.exists())
        self.assertTrue(all('start' not in call[1] and 'stop' not in call[1] for call in self.api.calls))

    def test_missing_webrtc_query_does_not_blindly_modify_fingerprint(self):
        del self.api.profile['fingerprint_config']
        result = self.run_action('configure')
        self.assertTrue(result['settings']['proxy_configured'])
        self.assertFalse(result['complete'])
        self.assertTrue(result['manual_required'])
        self.assertNotIn('fingerprint_config', self.api.updates[0])
        self.run_action('restore')
        self.assertNotIn('fingerprint_config', self.api.updates[-1])

    def test_running_profile_rejected_before_mutation(self):
        self.api.running = True
        with self.assertRaises(browser.BrowserError):
            self.run_action('configure')
        self.assertEqual(self.api.updates, [])
        self.assertFalse(self.journal.path.exists())

    def test_partial_update_rolls_back_original_values(self):
        expected = browser.snapshot(self.api.profile)
        self.api.mode = 'partial'
        with self.assertRaises(browser.BrowserError) as caught:
            self.run_action('configure')
        self.assertTrue(caught.exception.details['rolled_back'])
        self.assertEqual(browser.snapshot(self.api.profile), expected)
        self.assertFalse(self.journal.path.exists())

    def test_external_change_during_write_keeps_recovery_record(self):
        self.api.mode = 'external'
        with self.assertRaises(browser.BrowserError) as caught:
            self.run_action('configure')
        self.assertTrue(caught.exception.details['recovery_required'])
        self.assertEqual(len(self.api.updates), 1)
        self.assertEqual(self.api.profile['user_proxy_config']['proxy_port'], '9999')
        self.assertTrue(self.journal.path.exists())

    def test_restore_refuses_later_external_change(self):
        self.run_action('configure')
        self.api.profile['fingerprint_config']['webrtc'] = 'forward'
        with self.assertRaises(browser.BrowserError):
            self.run_action('restore')
        self.assertEqual(len(self.api.updates), 1)
        self.assertTrue(self.journal.path.exists())

    def test_changed_during_preflight_does_not_write(self):
        self.api.mode = 'race'
        with self.assertRaises(browser.BrowserError):
            self.run_action('configure')
        self.assertEqual(self.api.updates, [])
        self.assertFalse(self.journal.path.exists())

    def test_recovery_file_is_minimal_and_private(self):
        output = self.run_action('configure')
        saved = self.journal.path.read_text()
        self.assertIn('old-password', saved)  # Required to restore the old proxy.
        for secret in ('API_SECRET', 'ACCOUNT_SECRET', 'COOKIE_SECRET', 'KEEP_UA', 'KEEP_ACCOUNT'):
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, json.dumps(output))
        self.assertEqual(self.journal.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.journal.directory.stat().st_mode & 0o777, 0o700)
        self.journal.path.chmod(0o644)
        with self.assertRaises(browser.BrowserError):
            self.run_action('restore')
        self.assertEqual(len(self.api.updates), 1)

    def test_no_proxy_restore_and_saved_proxy_id_restore(self):
        self.api.profile['user_proxy_config'] = {'proxy_soft': 'no_proxy'}
        self.run_action('configure')
        self.run_action('restore')
        self.assertEqual(self.api.updates[-1]['user_proxy_config'], {'proxy_soft': 'no_proxy'})
        self.api.profile['user_proxy_config'] = copy.deepcopy(self.api.original['user_proxy_config'])
        self.api.profile['fbcc_proxy_acc_id'] = 'saved123'
        self.api.proxy_pool['saved123'] = copy.deepcopy(self.api.profile['user_proxy_config'])
        self.run_action('configure')
        self.run_action('restore')
        self.assertEqual(self.api.updates[-1]['proxyid'], 'saved123')
        self.assertNotIn('user_proxy_config', self.api.updates[-1])

    def test_status_and_list_are_redacted_and_do_not_create_state(self):
        output = [self.run_action('status'), self.run_action('list')]
        self.assertFalse(output[0]['complete'])
        self.assertEqual(output[1]['profiles'], [{'profile_id': 'test_profile', 'name': 'Test profile'}])
        self.assertFalse(self.base.exists())
        self.assertNotIn('old.invalid', json.dumps(output))
        self.assertNotIn('old-password', json.dumps(output))

    def test_redirect_is_not_followed_and_errors_are_redacted(self):
        for mode in ('redirect', 'auth', 'business_error'):
            self.api.mode = mode
            with self.assertRaises(browser.BrowserError) as caught:
                self.run_action('list')
            self.assertNotIn('API_SECRET', str(caught.exception))
            self.assertNotIn('ACCOUNT_SECRET', str(caught.exception))
        self.assertEqual(len(self.api.calls), 3)

    def test_only_literal_loopback_endpoints_are_allowed(self):
        for address in ('https://127.0.0.1:50325', 'http://example.com', 'http://127.0.0.2',
                        'http://127.0.0.1:0', 'http://127.0.0.1/path', 'http://user:pass@localhost', 'http://localhost?token=secret'):
            with self.assertRaises(browser.BrowserError):
                browser.Client(address)
        self.assertEqual(browser.endpoint('http://localhost:50325/'), browser.DEFAULT_ENDPOINT)
        self.assertEqual(browser.endpoint('http://[::1]:50325'), 'http://[::1]:50325')

    def test_shared_lock_blocks_mutation(self):
        state = browser.state_module()
        with state.state_lock(self.base):
            with self.assertRaises(browser.BrowserError):
                self.run_action('configure')
        self.assertEqual(self.api.calls, [])

    def test_symlink_journal_directory_is_rejected(self):
        self.base.mkdir()
        target = Path(self.temp.name) / 'unrelated'
        target.mkdir()
        self.journal.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(browser.BrowserError):
            self.run_action('configure')
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.api.updates, [])

    def test_cli_secret_uses_stdin_and_api_error_does_not_echo_response(self):
        self.api.mode = 'business_error'
        env = dict(os.environ, HOME=self.temp.name)
        process = subprocess.run([sys.executable, str(ROOT / 'core/browser-proxy.py'), 'list'],
                                 input=json.dumps(self.options), text=True, capture_output=True, env=env)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(json.loads(process.stdout)['ok'])
        self.assertEqual(process.stderr, '')
        for secret in ('API_SECRET', 'ACCOUNT_SECRET', 'COOKIE_SECRET'):
            self.assertNotIn(secret, process.stdout)


if __name__ == '__main__':
    unittest.main()
