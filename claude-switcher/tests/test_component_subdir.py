"""Pinned subdirectory updates use only local synthetic ZIPs and temporary state."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('components_subdir', ROOT / 'core/component-manager.py')
components = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(components)
REVISION = 'a' * 40
ARCHIVE_ROOT = 'claude-network-rules-' + REVISION
PREFIX = ARCHIVE_ROOT + '/claude-switcher/'


def fixture():
    names = ('core/claude-switch.sh', 'core/component-manager.py', 'core/stun-probe.py',
             'core/account_state.py', 'core/network-check.py', 'core/network-guard.py',
             'core/clash-policy.py', 'core/audit-contract.py', 'core/owned-policy.json',
             'core/network-monitor.py', 'app/network-notifier.swift',
             'daemons/local.claude-network-notifier.plist', 'app/main.swift',
             'daemons/claude-bodyguard.sh', 'installer/install.py', 'core/new-helper.py')
    payloads = {name: ('fixture: ' + name + '\n').encode() for name in names}
    payloads['VERSION'] = b'2.0.0\n'
    payloads['core/claude-switch.sh'] = ('#!/bin/bash\n' + '\n'.join(key + '="default"' for key in components.CONFIG_KEYS) + '\n').encode()
    manifest = {'schema': 1, 'version': '2.0.0', 'files':
                {name: hashlib.sha256(value).hexdigest() for name, value in payloads.items()}}
    encoded = (json.dumps(manifest, sort_keys=True) + '\n').encode()
    entries = [(ARCHIVE_ROOT + '/', b''), (ARCHIVE_ROOT + '/README.md', b'Rules repository\n'),
               (ARCHIVE_ROOT + '/Script.js', b'// sibling\n'), (PREFIX, b''),
               (PREFIX + 'COMPONENTS.json', encoded)]
    entries.extend((PREFIX + name, value) for name, value in payloads.items())
    return entries, encoded, manifest


def zip_bytes(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)  # Deliberate duplicate member tests.
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return stream.getvalue()


class ComponentSubdirectoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.entries, self.encoded, self.manifest = fixture()

    def tearDown(self):
        self.temp.cleanup()

    def download(self, entries=None, manifest=None):
        archive = zip_bytes(self.entries if entries is None else entries)
        encoded = self.encoded if manifest is None else manifest
        with patch.object(components, 'fetch', side_effect=[archive, encoded]) as fetch:
            root = components.download_bundle(REVISION, self.work)
        return root, fetch

    def test_download_pins_archive_and_subdir_manifest_to_same_commit(self):
        root, fetch = self.download()
        self.assertEqual(root, self.work / 'source' / ARCHIVE_ROOT / 'claude-switcher')
        self.assertEqual(components.load_bundle(root), self.manifest)
        self.assertEqual(fetch.call_args_list[0].args,
                         ('https://codeload.github.com/parafallen-maker/claude-network-rules/zip/' + REVISION,))
        self.assertEqual(fetch.call_args_list[1].args,
                         ('https://raw.githubusercontent.com/parafallen-maker/claude-network-rules/'
                          + REVISION + '/claude-switcher/COMPONENTS.json', 1024 * 1024))
        self.assertFalse((root / 'Script.js').exists())

    def test_repository_root_manifest_cannot_replace_missing_subdir(self):
        entries = [(name.replace('/claude-switcher/', '/'), data) for name, data in self.entries if name != PREFIX]
        with self.assertRaises(components.ComponentError):
            self.download(entries)

    def test_raw_manifest_must_match_selected_subdir_exactly(self):
        with self.assertRaises(components.ComponentError):
            self.download(manifest=self.encoded + b' ')

    def test_manifest_hash_validation_still_checks_every_component(self):
        entries = [(name, b'tampered\n' if name == PREFIX + 'core/new-helper.py' else data)
                   for name, data in self.entries]
        with self.assertRaisesRegex(components.ComponentError, 'SHA256'):
            self.download(entries)

    def test_unlisted_executable_and_missing_required_component_are_rejected(self):
        for added, removed in ([(PREFIX + 'core/unlisted.py', b'not covered')], []), ([], [PREFIX + 'core/network-guard.py']):
            with self.subTest(added=bool(added)):
                with tempfile.TemporaryDirectory(dir=self.work) as work:
                    entries = [(name, data) for name, data in self.entries if name not in removed] + added
                    with patch.object(components, 'fetch', side_effect=[zip_bytes(entries), self.encoded]):
                        with self.assertRaises(components.ComponentError):
                            components.download_bundle(REVISION, work)

    def test_traversal_and_sibling_symlinks_are_rejected_before_extraction(self):
        link = zipfile.ZipInfo(ARCHIVE_ROOT + '/unsafe-link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for extra in [('../escaped', b'bad'), (ARCHIVE_ROOT + '/../escaped', b'bad'),
                      (ARCHIVE_ROOT + '\\escaped', b'bad'), (link, b'../../escaped')]:
            with self.subTest(name=str(extra[0])):
                with self.assertRaises(components.ComponentError):
                    self.download(self.entries + [extra])
                self.assertFalse((self.work / 'source').exists())
        self.assertFalse((self.work / 'escaped').exists())

    def test_duplicate_members_multiple_roots_and_path_conflicts_are_rejected(self):
        for extra in [[(PREFIX + 'COMPONENTS.json', self.encoded)], [('other-root/file', b'bad')],
                      [(PREFIX + 'core', b'not a directory')]]:
            with self.subTest(extra=extra[0][0]):
                with self.assertRaises(components.ComponentError):
                    self.download(self.entries + extra)
                self.assertFalse((self.work / 'source').exists())

    def test_special_file_members_are_rejected(self):
        item = zipfile.ZipInfo(ARCHIVE_ROOT + '/special')
        item.create_system = 3
        item.external_attr = (stat.S_IFIFO | 0o600) << 16
        with self.assertRaises(components.ComponentError):
            self.download(self.entries + [(item, b'')])
        self.assertFalse((self.work / 'source').exists())

    def test_existing_extraction_target_is_never_overwritten(self):
        target = self.work / 'unrelated'
        target.mkdir()
        (target / 'sentinel').write_text('keep')
        (self.work / 'source').symlink_to(target, target_is_directory=True)
        with self.assertRaises(components.ComponentError):
            self.download()
        self.assertEqual(list(path.name for path in target.iterdir()), ['sentinel'])
        self.assertEqual((target / 'sentinel').read_text(), 'keep')

    def test_non_commit_revision_and_invalid_subdir_do_not_fetch(self):
        with patch.object(components, 'fetch') as fetch:
            for revision in ('main', 'v2.0.0', 'a' * 39, '../main', None):
                with self.assertRaises(components.ComponentError):
                    components.download_bundle(revision, self.work)
            with patch.object(components, 'SOURCE_SUBDIR', '../outside'):
                with self.assertRaises(components.ComponentError):
                    components.download_bundle(REVISION, self.work)
            fetch.assert_not_called()

    def test_stable_and_main_resolve_to_new_repository_fixed_commit(self):
        prefix = 'https://api.github.com/repos/parafallen-maker/claude-network-rules'
        release = {'tag_name': 'v2.0.0', 'draft': False, 'prerelease': False}
        with patch.object(components, 'fetch', side_effect=[json.dumps(release).encode(),
                                                          json.dumps({'sha': REVISION}).encode()]) as fetch:
            self.assertEqual(components.resolve_revision('stable'), (REVISION, '2.0.0'))
            self.assertEqual([call.args[0] for call in fetch.call_args_list],
                             [prefix + '/releases/latest', prefix + '/commits/v2.0.0'])
        with patch.object(components, 'fetch', return_value=json.dumps({'sha': REVISION}).encode()) as fetch:
            self.assertEqual(components.resolve_revision('main'), (REVISION, None))
            self.assertEqual(fetch.call_args.args[0], prefix + '/commits/main')

    def test_stable_metadata_failure_does_not_fall_back_to_main(self):
        release = {'tag_name': 'v2.0.0', 'draft': False, 'prerelease': True}
        with patch.object(components, 'fetch', return_value=json.dumps(release).encode()) as fetch:
            with self.assertRaises(components.ComponentError):
                components.resolve_revision('stable')
            self.assertEqual(fetch.call_count, 1)

    def test_cli_still_defaults_to_stable(self):
        with patch.object(components.sys, 'argv', ['component-manager.py', 'update', '--check']), \
             patch.object(components, 'run_update', return_value=0) as update:
            self.assertEqual(components.main(), 0)
            self.assertEqual(update.call_args.args[0].track, 'stable')
            self.assertTrue(update.call_args.args[0].check)

    def test_subdir_bundle_keeps_installed_paths_config_and_atomic_update(self):
        root, _ = self.download()
        base, bin_dir = self.work / 'state', self.work / 'bin'
        bin_dir.mkdir()
        original = {key: 'preserved-' + key for key in components.CONFIG_KEYS}
        script = bin_dir / 'claude-switch.sh'
        script.write_text('\n'.join(key + '="' + value + '"' for key, value in original.items()) + '\n')
        checked = []
        with components.state_lock(base):
            metadata = components.apply_bundle(root, base, bin_dir, REVISION,
                                                checker=lambda command, state: checked.append((command, state)))
        self.assertEqual(checked, [(script, base)])
        self.assertEqual(components.configs_from(script.read_text()), original)
        self.assertEqual(metadata['revision'], REVISION)
        self.assertEqual((base / 'new-helper.py').read_bytes(), (root / 'core/new-helper.py').read_bytes())
        self.assertFalse((base / 'claude-switcher').exists())
        self.assertFalse((base / 'update-journal.json').exists())
        self.assertFalse((base / 'Script.js').exists())

    def test_check_only_does_not_create_installed_state(self):
        root, _ = self.download()
        args = SimpleNamespace(base=self.work / 'state', bin_dir=self.work / 'bin', check=True, track='stable')
        with patch.object(components, 'resolve_revision', return_value=(REVISION, '2.0.0')), \
             patch.object(components, 'download_bundle', return_value=root), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(components.run_update(args), 0)
        self.assertFalse(args.base.exists())
        self.assertFalse(args.bin_dir.exists())

    def test_stable_never_automatically_downgrades_installed_version(self):
        args = SimpleNamespace(base=self.work / 'state', bin_dir=self.work / 'bin', check=True, track='stable')
        with patch.object(components, 'installed_manifest', return_value={'version': '3.0.0', 'files': {}}), \
             patch.object(components, 'resolve_revision', return_value=(REVISION, '2.0.0')), \
             patch.object(components, 'download_bundle') as download, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(components.run_update(args), 0)
            download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
