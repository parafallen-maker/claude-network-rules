"""Precompiled installation checks run on fixtures; no installed files are touched."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prebuilt_installer', ROOT / 'installer/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
VERSION = '1.9.0'


def thin_binary(cpu=0x0100000C, order='<'):
    return struct.pack(order + 'IIIIIIII', 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0)


def fat_binary(order='>', is_64=False):
    fields = 'IIQQII' if is_64 else 'IIIII'
    header = struct.pack(order + 'II', 0xCAFEBABF if is_64 else 0xCAFEBABE, 2)
    for cpu, offset in ((0x0100000C, 4096), (0x01000007, 8192)):
        values = [cpu, 0, offset, 32, 12] + ([0] if is_64 else [])
        header += struct.pack(order + fields, *values)
    return header.ljust(4096, b'\0') + thin_binary() + b'\0' * (4096 - 32) + thin_binary(0x01000007)


def fixture_source(destination):
    destination.mkdir(parents=True)
    files = {'VERSION': VERSION.encode() + b'\n'}
    # Use the repository's component names, with small synthetic contents. This
    # includes newly required helpers without borrowing any installed user data.
    for directory in ('core', 'daemons', 'installer', 'app'):
        for path in (ROOT / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                files[path.relative_to(ROOT).as_posix()] = b'isolated fixture\n'
    for relative, content in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {'schema': 1, 'version': VERSION,
                'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    (destination / 'COMPONENTS.json').write_text(json.dumps(manifest) + '\n')
    (destination / 'SHA256SUMS').write_text(''.join(value + '  ' + name + '\n' for name, value in manifest['files'].items()))


def fixture_app(source, app):
    for bundle, executable, identifier in (
            (app, 'ClaudeSwitcher', 'local.claude-switcher'),
            (app / 'Contents/Helpers/NetworkNotifier.app', 'NetworkNotifier', 'local.claude-switcher.network-notifier')):
        (bundle / 'Contents/MacOS').mkdir(parents=True)
        binary = bundle / 'Contents/MacOS' / executable
        binary.write_bytes(thin_binary())
        binary.chmod(0o755)
        info = {'CFBundleExecutable': executable, 'CFBundleIdentifier': identifier,
                'CFBundleVersion': VERSION, 'CFBundleShortVersionString': VERSION,
                'ClaudeSwitcherVersion': VERSION}
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    resources = app / 'Contents/Resources'
    resources.mkdir()
    shutil.copytree(source, resources / 'InstallerSource')
    (resources / 'BuildInfo.json').write_text(json.dumps({
        'version': VERSION, 'components_sha256': installer.cm.digest(source / 'COMPONENTS.json')}))


class PrebuiltInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'source'
        self.app = self.base / 'Prebuilt.app'
        self.output = self.base / 'Staged.app'
        fixture_source(self.source)
        fixture_app(self.source, self.app)
        self.commands = []

        def run(command, **kwargs):
            self.commands.append([str(x) for x in command])
            return subprocess.CompletedProcess(command, 0, b'', b'')

        self.run_mock = mock.patch.object(installer, 'run', side_effect=run).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(installer, 'read_text', return_value='arm64').start()

    def test_copy_is_verified_before_and_after_without_compilation(self):
        installer.stage_prebuilt_app(self.source, self.app, self.output, VERSION)
        self.assertEqual((self.output / 'Contents/MacOS/ClaudeSwitcher').read_bytes(), thin_binary())
        self.assertEqual(sum(cmd[0] == '/usr/bin/codesign' for cmd in self.commands), 2)
        self.assertTrue(all('swiftc' not in cmd and cmd[0] != '/usr/bin/lipo' for cmd in self.commands))

    def test_build_metadata_hash_mismatch_rejected_before_copy(self):
        path = self.app / 'Contents/Resources/BuildInfo.json'
        info = json.loads(path.read_text())
        info['components_sha256'] = '0' * 64
        path.write_text(json.dumps(info))
        with self.assertRaises(installer.cm.ComponentError):
            installer.stage_prebuilt_app(self.source, self.app, self.output, VERSION)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.commands, [])

    def test_build_metadata_version_mismatch_rejected(self):
        path = self.app / 'Contents/Resources/BuildInfo.json'
        info = json.loads(path.read_text())
        info['version'] = '1.8.0'
        path.write_text(json.dumps(info))
        with self.assertRaises(installer.cm.ComponentError):
            installer.verify_prebuilt_app(self.source, self.app, VERSION)

    def test_raw_manifest_hash_must_match_even_for_equivalent_json(self):
        embedded = self.app / 'Contents/Resources/InstallerSource/COMPONENTS.json'
        embedded.write_text(json.dumps(json.loads(embedded.read_text()), indent=2))
        with self.assertRaises(installer.cm.ComponentError):
            installer.verify_prebuilt_app(self.source, self.app, VERSION)

    def test_source_and_embedded_tampering_are_each_rejected(self):
        for directory in (self.source, self.app / 'Contents/Resources/InstallerSource'):
            with self.subTest(directory=directory.name):
                target = directory / 'core/network-check.py'
                original = target.read_bytes()
                target.write_bytes(original + b'tampered')
                with self.assertRaises(installer.cm.ComponentError):
                    installer.verify_prebuilt_app(self.source, self.app, VERSION)
                target.write_bytes(original)
        self.assertEqual(self.commands, [])

    def test_missing_helper_or_wrong_bundle_identity_rejected(self):
        helper_info = self.app / 'Contents/Helpers/NetworkNotifier.app/Contents/Info.plist'
        info = plistlib.loads(helper_info.read_bytes())
        info['CFBundleIdentifier'] = 'unrelated.helper'
        helper_info.write_bytes(plistlib.dumps(info))
        with self.assertRaises(installer.cm.ComponentError):
            installer.verify_prebuilt_app(self.source, self.app, VERSION)
        helper_info.unlink()
        with self.assertRaises(installer.cm.ComponentError):
            installer.verify_prebuilt_app(self.source, self.app, VERSION)

    def test_main_full_version_is_checked(self):
        path = self.app / 'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        info['ClaudeSwitcherVersion'] = VERSION + '-old'
        path.write_bytes(plistlib.dumps(info))
        with self.assertRaises(installer.cm.ComponentError):
            installer.verify_prebuilt_app(self.source, self.app, VERSION)

    def test_links_are_not_copied_or_followed(self):
        (self.app / 'Contents/Resources/outside').symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(installer.cm.ComponentError):
            installer.stage_prebuilt_app(self.source, self.app, self.output, VERSION)
        self.assertFalse(self.output.exists())

    def test_invalid_signature_blocks_staging(self):
        self.run_mock.side_effect = installer.cm.ComponentError('fixture signature rejected')
        with self.assertRaises(installer.cm.ComponentError):
            installer.stage_prebuilt_app(self.source, self.app, self.output, VERSION)
        self.assertFalse(self.output.exists())

    def test_helper_architecture_mismatch_blocks_staging(self):
        helper = self.app / 'Contents/Helpers/NetworkNotifier.app/Contents/MacOS/NetworkNotifier'
        helper.write_bytes(thin_binary(0x01000007))
        with self.assertRaises(installer.cm.ComponentError):
            installer.stage_prebuilt_app(self.source, self.app, self.output, VERSION)
        self.assertFalse(self.output.exists())

    def test_copy_is_revalidated_for_mutation(self):
        original = installer.verify_prebuilt_app
        def verify(source, app, version):
            if app == self.output:
                (app / 'Contents/Resources/BuildInfo.json').write_text('{}')
            original(source, app, version)
        with mock.patch.object(installer, 'verify_prebuilt_app', side_effect=verify):
            with self.assertRaises(installer.cm.ComponentError):
                installer.stage_prebuilt_app(self.source, self.app, self.output, VERSION)

    def test_install_prepares_selected_app_before_entering_transaction(self):
        args = argparse.Namespace(source=self.source, home=self.base / 'user', prebuilt_app=self.app,
                                  proxy_mode='auto', proxy_group='GLOBAL', country='US',
                                  timezone='America/New_York', locale='en_US')
        class StopBeforeTransaction(Exception):
            pass
        with mock.patch.object(installer.sys, 'platform', 'darwin'), \
                mock.patch.object(installer, 'running_as_root', return_value=False), \
                mock.patch.object(installer, 'SYSTEM_JOURNAL', self.base / 'no-system-journal'), \
                mock.patch.object(installer, 'stage_prebuilt_app') as staged, \
                mock.patch.object(installer, 'compile_app') as compiled, \
                mock.patch.object(installer.cm, 'state_lock', side_effect=StopBeforeTransaction):
            with self.assertRaises(StopBeforeTransaction):
                installer.install(args)
            staged.assert_called_once()
            compiled.assert_not_called()
            staged.reset_mock()
            args.prebuilt_app = None
            with self.assertRaises(StopBeforeTransaction):
                installer.install(args)
            staged.assert_not_called()
            compiled.assert_called_once()
        self.assertFalse((args.home / '.claude-switcher').exists())


class MachoArchitectureTests(unittest.TestCase):
    def test_thin_and_universal_headers(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'binary'
            for order in ('<', '>'):
                for cpu, architecture in ((0x0100000C, 'arm64'), (0x01000007, 'x86_64')):
                    executable.write_bytes(thin_binary(cpu, order))
                    self.assertEqual(installer.macho_architectures(executable), {architecture})
                for is_64 in (False, True):
                    executable.write_bytes(fat_binary(order, is_64))
                    self.assertEqual(installer.macho_architectures(executable), {'arm64', 'x86_64'})

    def test_truncated_outside_overlapping_or_mislabeled_slices_fail(self):
        good = fat_binary()
        outside = bytearray(good)
        struct.pack_into('>I', outside, 16, len(good) + 1)
        overlapping = bytearray(good)
        struct.pack_into('>I', overlapping, 36, 4096)
        mislabeled = bytearray(good)
        struct.pack_into('>I', mislabeled, 8, 0x01000007)
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'binary'
            for malformed in (b'', b'not a Mach-O' * 4, good[:20], good[:8195], outside, overlapping, mislabeled):
                executable.write_bytes(malformed)
                with self.assertRaises(installer.cm.ComponentError):
                    installer.macho_architectures(executable)


class CompilationTests(unittest.TestCase):
    def test_host_and_universal_build_both_programs(self):
        for requested in (None, 'universal'):
            with self.subTest(architecture=requested), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                calls = []
                def run(command, **kwargs):
                    values = [str(x) for x in command]
                    calls.append(values)
                    if 'swiftc' in values:
                        path = Path(values[values.index('-o') + 1])
                    elif '-create' in values:
                        path = Path(values[values.index('-output') + 1])
                    else:
                        return subprocess.CompletedProcess(values, 0, b'', b'')
                    path.write_bytes(b'fixture executable')
                    path.chmod(0o755)
                    return subprocess.CompletedProcess(values, 0, b'', b'')
                with mock.patch.object(installer, 'read_text', return_value='x86_64'), \
                        mock.patch.object(installer, 'run', side_effect=run):
                    if requested is None:
                        installer.compile_app(root, root / 'Built.app', VERSION)
                    else:
                        installer.compile_app(root, root / 'Built.app', VERSION, arch=requested)
                swift = [call for call in calls if 'swiftc' in call]
                targets = [call[call.index('-target') + 1] for call in swift]
                self.assertEqual(targets.count('x86_64-apple-macos13.0'), 2)
                self.assertEqual(targets.count('arm64-apple-macos13.0'), 2 if requested == 'universal' else 0)
                self.assertEqual(sum('-create' in call for call in calls), 2 if requested == 'universal' else 0)
                self.assertTrue((root / 'Built.app/Contents/Helpers/NetworkNotifier.app/Contents/MacOS/NetworkNotifier').is_file())

    def test_invalid_architecture_fails_before_creating_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'Bad.app'
            with self.assertRaises(installer.cm.ComponentError):
                installer.compile_app(Path(temporary), output, VERSION, arch='other')
            self.assertFalse(output.exists())


class InstallerLauncherTests(unittest.TestCase):
    def test_direct_entry_does_not_write_into_embedded_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            for relative in ('installer/install.py', 'core/component-manager.py'):
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            # Ignore PYTHON* environment settings so the script itself must
            # prevent import caches even when it is invoked without -B.
            result = subprocess.run([sys.executable, '-E', str(source / 'installer/install.py'), '--help'],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--prebuilt-app', result.stdout)
            self.assertEqual(list(source.rglob('*.pyc')), [])

    def test_embedded_app_uses_prebuilt_path_without_swift(self):
        self.run_launcher(embedded=True)

    def test_source_kit_still_requires_swift(self):
        self.run_launcher(embedded=False)

    def run_launcher(self, embedded):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'Complete App.app'
            source = app / 'Contents/Resources/InstallerSource' if embedded else root / 'source'
            (source / 'installer').mkdir(parents=True)
            script = source / 'installer/安装.command'
            shutil.copy2(ROOT / 'installer/安装.command', script)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            capture = root / 'invocation.json'
            compiler = root / 'compiler-checked'
            fake_python = bin_dir / 'python3'
            fake_python.write_text('#!' + sys.executable + '\n' +
                'import json, os, sys\n' +
                'if sys.argv[1:] != ["--version"]:\n' +
                ' with open(os.environ["INSTALL_CAPTURE"], "w") as stream: json.dump(sys.argv[1:], stream)\n')
            fake_python.chmod(0o755)
            (bin_dir / 'curl').write_text('#!/bin/bash\nprintf \'{"country":"US","country_code":"US","country_iso":"US","timezone":"America/New_York"}\\nloc=US\\n200\\n\'\n')
            (bin_dir / 'curl').chmod(0o755)
            (bin_dir / 'clear').write_text('#!/bin/bash\nexit 0\n')
            (bin_dir / 'clear').chmod(0o755)
            startup = root / 'startup.sh'
            startup.write_text('''function [() {
    if [[ "$1" == -d && "$2" == /Applications/Claude.app ]]; then return 0; fi
    if [[ "$1" == '!' && "$2" == -d && "$3" == /Applications/Claude.app ]]; then return 1; fi
    if [[ "$1" == -d && "$2" == *io.github.clash-verge-rev.clash-verge-rev ]]; then return 1; fi
    if [[ "$1" == -d && "$2" == *com.follow.clash ]]; then return 0; fi
    builtin [ "$@"
}
command() {
    if [[ "$*" == "-v swiftc" ]]; then printf checked > "$INSTALL_COMPILER_CHECK"; return 1; fi
    builtin command "$@"
}
''')
            environment = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
                               BASH_ENV=str(startup), HOME=str(root/'home'), INSTALL_CAPTURE=str(capture), INSTALL_COMPILER_CHECK=str(compiler))
            result = subprocess.run(['/bin/bash', str(script)], env=environment, input='', text=True, capture_output=True, timeout=15)
            if embedded:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(compiler.exists())
                arguments = json.loads(capture.read_text())
                self.assertEqual(arguments[arguments.index('--prebuilt-app') + 1], str(app))
                self.assertEqual(arguments[arguments.index('--source') + 1], str(source))
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(compiler.exists())
                self.assertFalse(capture.exists())


if __name__ == '__main__':
    unittest.main()
