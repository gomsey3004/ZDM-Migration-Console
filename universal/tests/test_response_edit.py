import unittest
from pathlib import Path
import test_console as baseline


class ResponseEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post

    def create_response(self):
        original='# Keep this comment\nMIGRATION_METHOD=ONLINE_PHYSICAL\nTGT_DB_UNIQUE_NAME=ORIGINAL\n'
        result=self.post('/api/responses',{'name':'editable','content':original})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()['path'],original

    def read_response(self,path):
        return self.client.get('/api/responses/content',params={'path':path})

    def save(self,loaded,content):
        return self.client.put('/api/responses/content',json={'path':loaded['path'],'revision':loaded['revision'],'content':content},headers=self.headers)

    def test_load_update_backup_and_profile_path_preserved(self):
        path,original=self.create_response()
        loaded=self.read_response(path).json()
        self.assertEqual(loaded['content'],original)
        self.assertTrue(loaded['editable'])
        profile=self.post('/api/profiles',{**self.profile,'rsp':path}).json()['id']
        before=self.module.profile_signature(self.module.get_profile(profile))
        updated=original.replace('ORIGINAL','UPDATED')
        result=self.save(loaded,updated)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(Path(path).read_text(),updated)
        backup=Path(result.json()['backup'])
        self.assertEqual(backup.read_text(),original)
        self.assertEqual(backup.stat().st_mode&0o777,0o600)
        self.assertEqual(Path(path).stat().st_mode&0o777,0o600)
        self.assertEqual(self.module.get_profile(profile)['rsp'],path)
        self.assertNotEqual(self.module.profile_signature(self.module.get_profile(profile)),before)
        self.assertNotEqual(self.read_response(path).json()['revision'],loaded['revision'])
        listed=self.client.get('/api/responses').json()
        self.assertFalse(any(item['path']==str(backup) for item in listed))

    def test_changed_revision_prevents_lost_updates(self):
        path,original=self.create_response();loaded=self.read_response(path).json()
        self.assertEqual(self.save(loaded,original+'# Other editor\n').status_code,200)
        result=self.save(loaded,original+'# Stale editor\n')
        self.assertEqual(result.status_code,409,result.text)
        self.assertIn('# Other editor',Path(path).read_text())
        self.assertNotIn('# Stale editor',Path(path).read_text())

    def test_templates_are_readable_and_copyable_but_not_overwritten(self):
        folder=self.stage/'zdm26.1/template/26.1.0';folder.mkdir(parents=True)
        path=folder/'zdm_physical_template.rsp';original='MIGRATION_METHOD=\n';path.write_text(original)
        loaded=self.read_response(str(path)).json()
        self.assertFalse(loaded['editable'])
        self.assertEqual(self.save(loaded,'MIGRATION_METHOD=ONLINE_PHYSICAL\n').status_code,403)
        self.assertEqual(path.read_text(),original)
        copied=self.post('/api/responses',{'name':'template-copy','content':loaded['content']})
        self.assertEqual(copied.status_code,200,copied.text)
        self.assertNotEqual(copied.json()['path'],str(path))

    def test_editor_cannot_read_or_overwrite_other_files_or_escape_stage(self):
        other=self.stage/'private.key';other.write_text('test data')
        outside=self.base/'outside.rsp';outside.write_text('outside')
        link=self.stage/'escape.rsp';link.symlink_to(outside)
        for path in ('/etc/passwd',str(other),str(outside),str(link)):
            self.assertEqual(self.read_response(path).status_code,422)
            result=self.client.put('/api/responses/content',json={'path':path,'revision':'0'*64,'content':'changed'},headers=self.headers)
            self.assertEqual(result.status_code,422,result.text)
        self.assertEqual(outside.read_text(),'outside')

    def test_secret_validation_and_large_file_rejection(self):
        path,original=self.create_response();loaded=self.read_response(path).json()
        result=self.save(loaded,'PASSWORD=never-echo-this-secret\n')
        self.assertEqual(result.status_code,422)
        self.assertNotIn('never-echo-this-secret',result.text)
        self.assertEqual(Path(path).read_text(),original)
        malformed=self.client.put('/api/responses/content',json={'path':path,'revision':'bad','content':'PASSWORD=never-echo-this-secret'},headers=self.headers)
        self.assertEqual(malformed.status_code,422)
        self.assertNotIn('never-echo-this-secret',malformed.text)
        huge=self.stage/'huge.rsp';huge.write_bytes(b'x'*300001)
        self.assertEqual(self.read_response(str(huge)).status_code,422)


if __name__=='__main__':unittest.main()
