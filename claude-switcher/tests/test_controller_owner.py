"""Controller fallback must target the configuration owner when run as root."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('controller_owner_policy', ROOT / 'core/clash-policy.py')
policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy)


class ControllerOwnerTest(unittest.TestCase):
    def test_root_fallback_uses_config_directory_owner_not_process_uid(self):
        config_dir = Path('/Users/test-owner/Library/Application Support/test-clash')
        expected = '/var/run/clash-verge-service/users/501/verge-mihomo.sock'
        root_socket = '/var/run/clash-verge-service/users/0/verge-mihomo.sock'
        values = {'external-controller-unix': '/tmp/expired-test-clash.sock',
                  'secret': 'test-only-secret', 'external-controller': '127.0.0.1:19090'}
        checked_paths = []

        def exists(path):
            checked_paths.append(str(path))
            # Even if UID 0 also has a socket, it is the wrong account's service.
            return str(path) in (expected, root_socket)

        with patch.object(policy.os, 'getuid', return_value=0), \
             patch.object(policy, 'read_yaml_scalar', side_effect=lambda path, key: values[key]), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=501)), \
             patch.object(Path, 'exists', exists):
            self.assertEqual(policy.controller_config(config_dir), (('unix', expected, None), 'test-only-secret'))
        self.assertIn(expected, checked_paths)
        self.assertNotIn(root_socket, checked_paths)


if __name__ == '__main__':
    unittest.main()
