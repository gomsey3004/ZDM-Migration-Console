import unittest
from pathlib import Path
from unittest.mock import patch
import test_console as baseline
from backend import response_files

class ResponseDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post
    def setUp(self):
        self.query=patch.object(self.module,'query_jobs',return_value={'raw':'No scheduled jobs found in the repository.','jobs':[]});self.query.start();self.addCleanup(self.query.stop)
    def create(self):
        r=self.post('/api/responses',{'name':'delete-test','content':'# preserve\nMIGRATION_METHOD=ONLINE_PHYSICAL\n'});self.assertEqual(r.status_code,200,r.text)
        p=r.json()['path'];return self.client.get('/api/responses/content',params={'path':p}).json()
    def delete(self,r,**overrides):return self.post('/api/responses/delete',{'path':r['path'],'revision':r['revision'],'confirmation':'DELETE '+Path(r['path']).name,**overrides})
    def restore(self,iid,name):return self.post('/api/responses/deleted/'+iid+'/restore',{'confirmation':'RESTORE '+name})
    def test_delete_restore_bytes_and_list(self):
        r=self.create();p=Path(r['path']);original=p.read_bytes();result=self.delete(r);self.assertEqual(result.status_code,200,result.text);iid=result.json()['id']
        self.assertFalse(p.exists());self.assertFalse(any(x['path']==str(p) for x in self.client.get('/api/responses').json()))
        self.assertTrue(any(x['id']==iid for x in self.client.get('/api/responses/deleted').json()))
        self.assertEqual(self.restore(iid,p.name).status_code,200);self.assertEqual(p.read_bytes(),original);self.assertEqual(p.stat().st_mode&0o777,0o600)
    def test_templates_protected(self):
        for relative in ['zdm_logical_template.rsp','templates/example.rsp','zdm26.1/example.rsp']:
            p=self.stage/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('MIGRATION_METHOD=ONLINE_PHYSICAL\n')
            r=self.client.get('/api/responses/content',params={'path':str(p)}).json();self.assertTrue(r['template']);self.assertEqual(self.delete(r).status_code,403);self.assertTrue(p.exists())
    def test_confirmation_revision_and_symlink_guards(self):
        r=self.create();self.assertEqual(self.delete(r,confirmation='DELETE wrong').status_code,422);self.assertEqual(self.delete(r,revision='0'*64).status_code,409)
        link=self.stage/'linked.rsp';link.symlink_to(r['path']);self.assertEqual(self.delete({**r,'path':str(link)}).status_code,403);self.assertTrue(Path(r['path']).exists())
    def test_profile_reference_blocks(self):
        r=self.create();self.assertEqual(self.post('/api/profiles',{**self.profile,'rsp':r['path']}).status_code,200);self.assertEqual(self.delete(r).status_code,409)
    def test_jobs_guard_active_resumable_and_terminal(self):
        r=self.create();p=Path(r['path'])
        for status in ['RUNNING','PAUSED','FAILED']:
            raw=f'Job ID: 99\nJob Type: "MIGRATE"\nCurrent status: {status}\nScheduled job command: zdmcli migrate database -rsp {p}\n'
            with patch.object(self.module,'query_jobs',return_value={'raw':raw}):self.assertEqual(self.delete(r).status_code,409)
        raw=f'Job ID: 99\nJob Type: "MIGRATE"\nCurrent status: SUCCEEDED\nScheduled job command: zdmcli migrate database -rsp {p}\n'
        with patch.object(self.module,'query_jobs',return_value={'raw':raw}):self.assertEqual(self.delete(r).status_code,200)
    def test_restore_no_overwrite_and_tampered_archive(self):
        r=self.create();p=Path(r['path']);iid=self.delete(r).json()['id'];p.write_text('replacement')
        self.assertEqual(self.restore(iid,p.name).status_code,409);self.assertEqual(p.read_text(),'replacement');p.unlink()
        response_files.trash_path(iid).write_text('tampered');self.assertEqual(self.restore(iid,p.name).status_code,409);self.assertFalse(p.exists())
    def test_database_failure_restores_original(self):
        r=self.create();p=Path(r['path']);original=p.read_bytes()
        with self.module.db() as conn:
            conn.execute("CREATE TRIGGER fail_response_delete BEFORE INSERT ON deleted_responses BEGIN SELECT RAISE(FAIL,'fixture'); END")
        try:
            with self.assertRaises(Exception):self.delete(r)
            self.assertEqual(p.read_bytes(),original)
        finally:
            with self.module.db() as conn:conn.execute('DROP TRIGGER fail_response_delete')

if __name__=='__main__':unittest.main()
