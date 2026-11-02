"""Unconfigured PF is read-only; configured PF retains its privileged lock."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('disabled_network_guard', ROOT / 'core/network-guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class DisabledGuardTest(unittest.TestCase):
    def test_unconfigured_root_checks_are_read_only_disabled_and_respect_require_active(self):
        for command in (['watch'], ['status'], ['lockdown', '--if-enabled']):
            for require_active in (False, True):
                arguments = command + (['--require-active'] if require_active else [])
                output = io.StringIO()
                with self.subTest(arguments=arguments), \
                     patch.object(guard.platform, 'system', return_value='Darwin'), \
                     patch.object(guard.os, 'geteuid', return_value=0), \
                     patch.object(Path, 'exists', return_value=False), \
                     patch.object(guard, 'privileged_lock') as lock, \
                     patch.object(guard, 'read_json') as read, \
                     patch.object(guard, 'atomic_json') as write, \
                     patch.object(guard.os, 'open') as file_open, \
                     patch.object(guard, 'Guard') as operations, \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(guard.main(arguments), 1 if require_active else 0)
                    lock.assert_not_called()
                    read.assert_not_called()
                    write.assert_not_called()
                    file_open.assert_not_called()
                    operations.assert_not_called()
                self.assertEqual(json.loads(output.getvalue()),
                                 {'mode': 'disabled', 'active': False, 'packet_capture_verified': False})

    def test_existing_config_or_state_still_enters_privileged_lock(self):
        for existing in ({guard.CONFIG}, {guard.STATE}, {guard.CONFIG, guard.STATE}):
            for command in (['watch'], ['status'], ['lockdown', '--if-enabled']):
                entered = []
                output = io.StringIO()

                @contextlib.contextmanager
                def refuse_lock():
                    entered.append(True)
                    # A refused lock must remain a failure, even for status/watch.
                    raise guard.GuardError('test_lock_refused')
                    yield  # pragma: no cover

                with self.subTest(existing=existing, command=command), \
                     patch.object(guard.platform, 'system', return_value='Darwin'), \
                     patch.object(guard.os, 'geteuid', return_value=0), \
                     patch.object(Path, 'exists', lambda path: path in existing), \
                     patch.object(guard, 'privileged_lock', side_effect=refuse_lock) as lock, \
                     patch.object(guard, 'read_json') as read, \
                     patch.object(guard, 'atomic_json') as write, \
                     patch.object(guard.os, 'open') as file_open, \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(guard.main(command), 1)
                    lock.assert_called_once_with()
                    self.assertEqual(entered, [True])
                    read.assert_not_called()
                    write.assert_not_called()
                    file_open.assert_not_called()
                self.assertEqual(json.loads(output.getvalue())['mode'], 'unverified')


if __name__ == '__main__':
    unittest.main()
