"""Exercise the uninstaller's read-only preview and protected-path checks."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'portable' / 'uninstall.sh'


class UninstallerPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / 'gui'
        (self.app / 'backend').mkdir(parents=True)
        (self.app / 'backend/app.py').write_text('# fixture\n')
        (self.app / 'frontend/dist').mkdir(parents=True)
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / 'console.sqlite').write_bytes(b'preserve this fixture')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'commands.log'
        for name, text in {
            'uname': '#!/bin/sh\necho Linux\n',
            'systemctl': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_LOG"\n'
                         'case "$*" in *WorkingDirectory*) echo "$TEST_APP";; '
                         '*zdmservice-autostart.service*LoadState*) echo "${TEST_AUTOSTART:-not-found}";; esac\n',
            # macOS versions do not all support realpath -m.
            'realpath': '#!/usr/bin/env python3\nimport os,sys\nprint(os.path.realpath(sys.argv[-1]))\n',
        }.items():
            p = self.bin / name
            p.write_text(text)
            p.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                        TEST_LOG=str(self.log), TEST_APP=str(self.app))

    def run_preview(self, *args):
        return subprocess.run(['bash', str(SCRIPT), '--dry-run', '--data-dir', str(self.data), *args],
                              env=self.env, text=True, capture_output=True)

    def test_detected_installation_is_not_modified(self):
        result = self.run_preview()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.app), result.stdout)
        self.assertIn('No changes made', result.stdout)
        self.assertEqual((self.data / 'console.sqlite').read_bytes(), b'preserve this fixture')
        self.assertTrue((self.app / 'backend/app.py').exists())
        self.assertNotIn('stop', self.log.read_text())
        self.assertNotIn('disable', self.log.read_text())

    def test_data_inside_application_is_preserved(self):
        result = self.run_preview('--data-dir', str(self.app / 'data'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('only code and virtual environments', result.stdout)

    def test_protected_or_broad_application_paths_are_rejected(self):
        for path in ('/', '/u01', '/u01/app', '/u01/app/zdmhome', '/u01/app/zdmhome/gui'):
            with self.subTest(path=path):
                result = self.run_preview('--app-dir', path)
                self.assertNotEqual(result.returncode, 0)

    def test_missing_application_markers_are_rejected(self):
        result = self.run_preview('--app-dir', str(self.data))
        self.assertNotEqual(result.returncode, 0)

    def test_data_inside_removable_component_is_rejected(self):
        result = self.run_preview('--data-dir', str(self.app / 'backend/data'))
        self.assertNotEqual(result.returncode, 0)

    def test_absent_autostart_is_not_offered_for_removal(self):
        result = self.run_preview('--remove-zdm-autostart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('not installed; nothing to remove', result.stdout)
        self.assertNotIn('will ask', result.stdout)
        self.assertNotIn('disable', self.log.read_text())

    def test_installed_autostart_is_detected_before_prompt(self):
        self.env['TEST_AUTOSTART'] = 'loaded'
        result = self.run_preview()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('will ask whether to remove', result.stdout)
        self.assertNotIn('disable', self.log.read_text())

    def test_explicit_autostart_preview_does_not_change_service(self):
        self.env['TEST_AUTOSTART'] = 'loaded'
        result = self.run_preview('--remove-zdm-autostart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Remove: ZDM auto-start boot unit', result.stdout)
        self.assertNotIn('disable', self.log.read_text())

    def test_autostart_removal_never_stops_zdm_and_aborts_if_disable_fails(self):
        function = re.search(r'^remove_zdm_autostart\(\)\{.*?^}', SCRIPT.read_text(), re.M | re.S).group()
        for disable_fails in (False, True):
            with self.subTest(disable_fails=disable_fails):
                self.log.write_text('')
                mock = '''
fail(){ echo "$*" >&2; exit 1; }
systemctl(){
  printf 'systemctl %s\\n' "$*" >> "$TEST_LOG"
  if [ "$1" = disable ]; then return "$TEST_DISABLE_STATUS"; fi
  if [ "$1" = is-enabled ]; then return 1; fi
  return 99
}
rm(){ printf 'rm %s\\n' "$*" >> "$TEST_LOG"; }
'''
                result = subprocess.run(['bash', '-c', 'set -e\n'+mock+function+'\nremove_zdm_autostart'],
                                        env=dict(self.env, TEST_DISABLE_STATUS=str(int(disable_fails))),
                                        text=True, capture_output=True)
                log = self.log.read_text()
                self.assertEqual(result.returncode == 0, not disable_fails)
                self.assertNotIn('--now', log)
                self.assertNotIn('systemctl stop', log)
                self.assertEqual('rm -f -- /etc/systemd/system/zdmservice-autostart.service' in log,
                                 not disable_fails)


if __name__ == '__main__':
    unittest.main()
