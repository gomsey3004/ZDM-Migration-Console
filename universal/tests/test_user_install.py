"""Private installation, upgrade and uninstall without system mutations."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


installer = module('user_install', ROOT / 'portable/user-install.py')
runtime = module('user_runtime', ROOT / 'portable/user-runtime.py')


class UserInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.service_available = False
        self.home = self.root / 'account'
        self.home.mkdir()
        self.app = self.home / 'zdm-gui'
        self.data = self.home / 'gui-data'
        self.zdm = {}
        for key in ('home', 'base', 'stage'):
            p = self.root / ('zdm' + key)
            p.mkdir()
            self.zdm[key] = p
        (self.zdm['home'] / 'bin').mkdir()
        for name in ('zdmcli', 'zdmservice'):
            p = self.zdm['home'] / 'bin' / name
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o700)
        self.release = self.root / 'release'
        self.release.mkdir()
        shutil.copytree(ROOT / 'backend', self.release / 'backend')
        (self.release / 'frontend/dist').mkdir(parents=True)
        (self.release / 'frontend/dist/index.html').write_text('fixture')
        (self.release / 'deploy').mkdir()
        shutil.copy2(ROOT / 'portable/user-runtime.py', self.release / 'deploy/user-runtime.py')
        shutil.copy2(ROOT / 'deploy/oci-requirements.txt', self.release / 'deploy/oci-requirements.txt')
        account = pwd.getpwuid(os.getuid())
        self.account = pwd.struct_passwd((account.pw_name, account.pw_passwd, account.pw_uid,
                                         account.pw_gid, account.pw_gecos, str(self.home), account.pw_shell))

    def install(self, *extra):
        original_read = Path.read_text
        original_run = subprocess.run
        calls = []

        def read(path, *args, **kwargs):
            return 'ID="ol"\nVERSION_ID="8.10"\n' if str(path) == '/etc/os-release' else original_read(path, *args, **kwargs)

        def run(command, **kwargs):
            calls.append(command)
            if command[1:3] == ['-m', 'venv']:
                binary = Path(command[3]) / 'bin/python'
                binary.parent.mkdir(parents=True)
                binary.symlink_to(sys.executable)
                return subprocess.CompletedProcess(command, 0)
            if command[1:3] == ['-m', 'pip']:
                return subprocess.CompletedProcess(command, 0)
            if command[:1] == ['systemctl']:
                return subprocess.CompletedProcess(command, 0 if self.service_available else 1)
            return original_run(command, **kwargs)

        args = ['user-install.py', '--yes', '--no-start', '--service', 'manual', '--app-dir', str(self.app),
                '--data-dir', str(self.data), '--tls-name', '127.0.0.1']
        for key, path in self.zdm.items():
            args.extend(['--zdm-' + key, str(path)])
        with patch.object(installer, '__file__', str(self.release / 'user-install.py')), \
             patch.object(installer.platform, 'system', return_value='Linux'), \
             patch.object(installer.platform, 'machine', return_value='x86_64'), \
             patch.object(installer.pwd, 'getpwuid', return_value=self.account), \
             patch.object(Path, 'read_text', read), \
             patch.object(installer.subprocess, 'run', side_effect=run), \
             patch.object(sys, 'argv', args + list(extra)), contextlib.redirect_stdout(io.StringIO()):
            installer.main()
        return calls

    def test_private_install_and_upgrade_keep_credentials_and_enable_tls(self):
        calls = self.install()
        config = json.loads((self.app / 'deploy/user-config.json').read_text())
        self.assertEqual(config['uid'], os.getuid())
        self.assertEqual(config['environment']['ZDM_GUI_HOSTS_READ_ONLY'], '1')
        self.assertEqual(config['port'], 8443)
        self.assertEqual(config['bind'], '127.0.0.1')
        self.assertNotIn('ZDM_GUI_PASSWORD', config['environment'])
        self.assertEqual((self.app / 'tls/gui.key').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.app / 'bin/zdm-gui-start').stat().st_mode & 0o100)
        args = runtime.command(config)
        self.assertIn('backend.app:app', args)
        self.assertIn('--ssl-keyfile', args)
        self.assertIn('--no-proxy-headers', args)
        with sqlite3.connect(self.data / 'console.sqlite') as db:
            before = db.execute('select digest from console_account').fetchone()[0]
            self.assertEqual(db.execute('select must_change_password from console_users').fetchone()[0], 1)
        calls += self.install()
        with sqlite3.connect(self.data / 'console.sqlite') as db:
            self.assertEqual(db.execute('select digest from console_account').fetchone()[0], before)
        for call in calls:
            self.assertNotIn(call[0], ('sudo', 'dnf', 'yum', 'nginx', 'firewall-cmd', 'chown'))

    def test_preflight_does_not_install_or_write(self):
        self.assertEqual(self.install('--check'), [])
        self.assertFalse(self.app.exists())
        self.assertFalse(self.data.exists())

    def test_pending_work_blocks_upgrade(self):
        self.install()
        with sqlite3.connect(self.data / 'console.sqlite') as db:
            db.execute('create table operations (state text)')
            db.execute("insert into operations values ('RUNNING')")
        with self.assertRaisesRegex(SystemExit, 'queued or running'):
            self.install()
        self.assertTrue((self.app / 'backend/app.py').exists())

    def test_user_uninstall_preserves_data_and_zdm_files(self):
        self.install()
        shim = self.root / 'shim'
        shim.mkdir()
        (shim / 'sitecustomize.py').write_text('import pwd\noriginal=pwd.getpwuid\ndef account(uid):\n p=original(uid)\n return pwd.struct_passwd((*p[:5],'+repr(str(self.home))+',p.pw_shell))\npwd.getpwuid=account\n')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'uname').write_text('#!/bin/sh\necho Linux\n')
        (bin_dir / 'uname').chmod(0o700)
        env = dict(os.environ, PYTHONPATH=str(shim), PATH=str(bin_dir)+os.pathsep+os.environ['PATH'])
        args = ['bash', str(ROOT / 'portable/uninstall.sh'), '--user', '--app-dir', str(self.app)]
        preview = subprocess.run(args + ['--dry-run'], env=env, text=True, capture_output=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(self.app.exists())
        removed = subprocess.run(args + ['--yes'], env=env, text=True, capture_output=True)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.app.exists())
        self.assertTrue((self.data / 'console.sqlite').exists())
        self.assertTrue((self.zdm['home'] / 'bin/zdmcli').exists())
        self.assertTrue(list((self.home / '.local/state/zdm-gui/uninstall-backups').iterdir()))

    def test_rejects_protected_and_nested_data_paths(self):
        with patch.object(installer.pwd, 'getpwuid', return_value=self.account):
            for app, data in [(self.home, self.data), (self.zdm['home'] / 'gui', self.data),
                              (self.app, self.app / 'backend/data')]:
                with self.subTest(app=app, data=data), self.assertRaises(SystemExit):
                    installer.check_layout(app, data, list(self.zdm.values()))

    def test_user_service_has_no_system_scope_or_user_switch(self):
        self.service_available = True
        original_which = shutil.which
        with patch.object(installer.shutil, 'which', side_effect=lambda name: '/fake/systemctl' if name == 'systemctl' else original_which(name)):
            calls = self.install('--service', 'user')
        unit = (self.home / '.config/systemd/user/zdm-gui.service').read_text()
        self.assertIn('WantedBy=default.target', unit)
        self.assertIn(str(self.app / 'deploy/user-config.json') + ' run', unit)
        self.assertNotIn('User=root', unit)
        for call in calls:
            if call[0] == 'systemctl':
                self.assertEqual(call[1], '--user')

    def test_manual_process_identity_requires_both_owner_and_token(self):
        with patch.object(runtime, 'Path') as path:
            proc = path.return_value.__truediv__.return_value
            proc.stat.return_value.st_uid = os.getuid()
            proc.__truediv__.return_value.read_bytes.return_value = b'ZDM_GUI_PROCESS_TOKEN=expected\0'
            self.assertTrue(runtime.process_matches({'pid': 12345, 'token': 'expected'}))
            self.assertFalse(runtime.process_matches({'pid': 12345, 'token': 'wrong'}))
            proc.stat.return_value.st_uid = os.getuid() + 1
            self.assertFalse(runtime.process_matches({'pid': 12345, 'token': 'expected'}))

    def test_user_service_exec_includes_the_asgi_application_and_https(self):
        config = {'uid': os.getuid(), 'app': str(self.app), 'data': str(self.data),
                  'bind': '127.0.0.1', 'port': 8443, 'environment': {}, 'service': 'user'}
        p = self.root / 'config.json'
        p.write_text(json.dumps(config))
        with patch.object(sys, 'argv', ['user-runtime.py', str(p), 'run']), \
             patch.object(runtime.os, 'chdir'), patch.object(runtime.os, 'execv', side_effect=RuntimeError('exec intercepted')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'exec intercepted'):
                runtime.main()
        self.assertIn('backend.app:app', execute.call_args.args[1])
        self.assertIn('--ssl-keyfile', execute.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
