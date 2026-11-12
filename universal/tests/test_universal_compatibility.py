import unittest
from unittest.mock import patch

from backend import compatibility, migration, wallets


def detected(release, migrate=(), resume=(), modify=False):
    return {
        'release': release,
        'supported': True,
        'documentation': f'https://docs.oracle.com/{release}/',
        'capabilities': {
            'migrate_database': True,
            'migrate_switches': list(migrate),
            'resume_job': True,
            'resume_switches': list(resume),
            'modify_job': modify,
            'modify_switches': [],
        },
    }


class UniversalCompatibilityTests(unittest.TestCase):
    def test_supported_release_lines_ignore_cpat_version(self):
        for release in ('21.4.10', '21.5.0', '26.1.0'):
            result = compatibility.parse_build(
                f'full version: "{release}"\npatch version: "x"\nCPAT build version: 99.9.0\n'
            )
            self.assertTrue(result['supported'])
            self.assertEqual(result['release'], '.'.join(release.split('.')[:2]))

    def test_unknown_release_is_rejected(self):
        self.assertFalse(compatibility.parse_build('full version: "23.1.0"')['supported'])

    def test_older_release_hides_newer_switches(self):
        current = detected('21.5', migrate={'rsp', 'schedule', 'pauseafter', 'eval'})
        with patch('backend.compatibility.current', return_value=current):
            _, _, rows = migration.available_options()
            names = {row['name'] for row in rows}
            self.assertIn('schedule', names)
            self.assertNotIn('jobtag', names)
            self.assertNotIn('sourcesyswallet', names)
            self.assertNotIn('DB_NAME_CHECK', next(row for row in rows if row['name'] == 'ignore')['choices'] if 'ignore' in names else [])

    def test_261_exposes_only_observed_options(self):
        current = detected('26.1', migrate={'rsp', 'jobtag', 'sourcesyswallet', 'ignore'})
        with patch('backend.compatibility.current', return_value=current):
            _, _, rows = migration.available_options()
            names = {row['name'] for row in rows}
            self.assertIn('jobtag', names)
            self.assertIn('sourcesyswallet', names)
            self.assertNotIn('restoreuser', names)

    def test_wallet_catalog_follows_cli_switches(self):
        old = detected('21.4', migrate={'rsp'})
        new = detected('26.1', migrate={'rsp', 'sourcesyswallet'})
        with patch('backend.compatibility.current', return_value=old):
            self.assertNotIn('SOURCE_SYS_PHYSICAL', wallets.available_catalog())
            self.assertIn('WALLET_SOURCEADMIN', wallets.available_catalog())
        with patch('backend.compatibility.current', return_value=new):
            self.assertIn('SOURCE_SYS_PHYSICAL', wallets.available_catalog())


if __name__ == '__main__':
    unittest.main()
