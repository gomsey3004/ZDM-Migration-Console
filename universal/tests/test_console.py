import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import adapter

FIXTURE='''zdm.example.com: Audit ID: 8
Job ID: 31
User: zdmuser
Job Type: "EVAL"
Source node: source.example.com
Target node: target.example.com
Database name: PROD
Current status: SUCCEEDED
Result file path: /u01/app/zdmbase/chkbase/scheduled/job-31.log
ZDM_VALIDATE_TGT ...................... COMPLETED
ZDM_VALIDATE_SRC ...................... COMPLETED

Job ID: 32
Job Type: "MIGRATE"
Source node:
Target node: target.example.com
Current status: PAUSED
ZDM_MONITOR_GG_LAG ...................... PAUSED
'''

class AdapterTests(unittest.TestCase):
    def test_multiple_jobs_phases_and_blank_fields(self):
        jobs=adapter.parse_jobs(FIXTURE)
        self.assertEqual([j['id'] for j in jobs],[31,32])
        self.assertEqual(jobs[0]['status'],'SUCCEEDED')
        self.assertEqual(jobs[0]['type'],'EVAL')
        self.assertEqual(len(jobs[0]['phases']),2)
        self.assertEqual(jobs[1]['source'],'')
        self.assertEqual(jobs[1]['phases'][0]['status'],'PAUSED')

    def test_empty_repository(self):
        self.assertEqual(adapter.parse_jobs('No scheduled jobs found in the repository.'),[])

    def test_secrets_redacted(self):
        text=adapter.redact('PASSWORD=hidden\napi_token: other\n-----BEGIN RSA PRIVATE KEY-----\nkey\n-----END RSA PRIVATE KEY-----')
        self.assertNotIn('hidden',text)
        self.assertNotIn('other',text)
        self.assertNotIn('\nkey\n',text)

    def test_paths_cannot_escape_with_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'allowed';root.mkdir()
            outside=Path(tmp)/'secret';outside.write_text('private')
            (root/'link').symlink_to(outside)
            with self.assertRaises(ValueError):adapter.safe_path(str(root/'link'),[root])
            with self.assertRaises(ValueError):adapter.safe_path('relative.rsp',[root])

    def test_shell_syntax_not_a_host(self):
        for value in ['host; touch /tmp/x','$(id)','-bad','two hosts']:
            with self.assertRaises(ValueError):adapter.word(value,'host')

class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temp.name)
        cls.stage=cls.root/'stage';cls.stage.mkdir()
        cls.base=cls.root/'base';cls.base.mkdir()
        cls.home=cls.root/'home';cls.home.mkdir()
        cls.rsp=cls.stage/'physical.rsp';cls.rsp.write_text('MIGRATION_METHOD=ONLINE_PHYSICAL\n')
        cls.key=cls.base/'id_rsa';cls.key.write_text('test fixture, not an actual key')
        cls.patches=[patch.object(adapter,'STAGE',cls.stage),patch.object(adapter,'BASE',cls.base),patch.object(adapter,'HOME',cls.home),
                     patch.dict(os.environ,{'ZDM_GUI_PASSWORD':'test-only-password','ZDM_GUI_ALLOWED_IPS':'testclient'})]
        for p in cls.patches:p.start()
        from backend import app as module
        cls.module=module
        cls.patches.append(patch.object(module,'DATA',cls.root/'data'))
        cls.patches[-1].start()
        cls.calls=[]
        def fake(args,**kwargs):
            cls.calls.append(args)
            if args==['status']:return 'Running: true\n'
            if args==['-build']:return 'full version: "26.1.0"'
            if args[:2]==['query','job']:
                if '-jobid' in args:
                    return FIXTURE.split('Job ID: 32')[1].join(['Job ID: 32','']) if args[-1]=='32' else FIXTURE.split('Job ID: 32')[0]
                return FIXTURE
            if args[:2]==['migrate','database']:return 'Operation "zdmcli migrate database" scheduled with the job ID "43".'
            return 'Operation completed successfully.'
        cls.patches.append(patch.object(adapter,'run',side_effect=fake));cls.patches[-1].start()
        from fastapi.testclient import TestClient
        cls.client=TestClient(module.app)
        cls.client.__enter__()
        cls.client.auth=('admin','test-only-password')
        cls.headers={'X-ZDM-Request':'console'}
        cls.profile={'name':'Test profile','source_host':'source.example.com','source_identifier':'PROD',
                     'rsp':str(cls.rsp),'source_key':str(cls.key),'target_key':str(cls.key)}

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None,None,None)
        for p in reversed(cls.patches):p.stop()
        cls.temp.cleanup()

    def post(self,path,body):return self.client.post(path,json=body,headers=self.headers)
    def profile_id(self):
        res=self.post('/api/profiles',self.profile)
        self.assertEqual(res.status_code,200,res.text)
        return res.json()['id']

    def test_auth_and_ip_restriction(self):
        self.assertEqual(self.client.get('/api/jobs',auth=('admin','wrong')).status_code,401)
        with patch.dict(os.environ,{'ZDM_GUI_ALLOWED_IPS':'192.0.2.1'}):
            self.assertEqual(self.client.get('/api/jobs').status_code,403)

    def test_csrf_guard(self):
        self.assertEqual(self.client.post('/api/profiles',json=self.profile).status_code,403)
        self.assertEqual(self.client.post('/api/profiles',json=self.profile,headers={**self.headers,'Origin':'http://evil.example'}).status_code,403)

    def test_service_and_job_monitoring(self):
        self.assertTrue(self.client.get('/api/health').json()['running'])
        self.assertEqual(len(self.client.get('/api/jobs').json()['jobs']),2)
        self.assertEqual(self.client.get('/api/jobs/32').json()['job']['status'],'PAUSED')

    def test_profile_rejects_command_injection_and_missing_files(self):
        self.assertEqual(self.post('/api/profiles',{**self.profile,'source_host':'host;id'}).status_code,422)
        self.assertEqual(self.post('/api/profiles',{**self.profile,'rsp':'/etc/shadow'}).status_code,422)

    def test_response_file_permissions_and_secrets(self):
        self.assertEqual(self.post('/api/responses',{'name':'a','content':'PASSWORD=secret'}).status_code,422)
        res=self.post('/api/responses',{'name':'valid','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n'})
        self.assertEqual(res.status_code,200,res.text)
        self.assertEqual(Path(res.json()['path']).stat().st_mode&0o777,0o600)

    def test_wallet_references_are_not_plaintext_secrets(self):
        content='WALLET_OCIAUTHTOKEN=/u01/app/wallets/oci\nWALLET_AMAZONS3SECRET=/u01/app/wallets/s3\n'
        self.assertEqual(self.post('/api/responses',{'name':'wallets','content':content}).status_code,200)
        self.assertEqual(self.post('/api/responses',{'name':'invalid','content':'WALLET_OCIAUTHTOKEN=plaintext-secret'}).status_code,422)

    def test_migration_requires_confirmation(self):
        pid=self.profile_id()
        res=self.post('/api/submissions',{'profile_id':pid,'evaluate':False,'request_key':'migration-confirm-key'})
        self.assertEqual(res.status_code,422)
        preview=self.client.get('/api/profiles/'+pid+'/preview').json()['command']
        self.assertIn('-eval',preview)
        self.assertIn('-srcauth zdmauth',preview)

    def test_password_change_validation_does_not_echo_secrets(self):
        secret='never-echo-this-secret'
        res=self.post('/api/account/password',{'current_password':secret,'new_password':{'bad':secret},'confirm_password':secret})
        self.assertEqual(res.status_code,422)
        self.assertNotIn(secret,res.text)
        cases=[('short','short'),('a-new-test-password','different-confirmation'),('test-only-password','test-only-password')]
        for new,confirmation in cases:
            res=self.post('/api/account/password',{'current_password':'test-only-password','new_password':new,'confirm_password':confirmation})
            self.assertEqual(res.status_code,422,res.text)
        res=self.post('/api/account/password',{'current_password':'wrong','new_password':'a-new-test-password','confirm_password':'a-new-test-password'})
        self.assertEqual(res.status_code,403)

    def test_password_change_persists_and_revokes_old_password(self):
        new='Temporary Unicode passphrase café 45'
        body={'current_password':'test-only-password','new_password':new,'confirm_password':new}
        old_row=None
        with self.module.db() as conn:
            old_row=dict(conn.execute('SELECT * FROM console_account WHERE id=1').fetchone())
        res=self.post('/api/account/password',body)
        self.assertEqual(res.status_code,200,res.text)
        try:
            self.assertNotIn(new,res.text)
            self.assertEqual(self.client.get('/api/account').status_code,401)
            self.client.auth=('admin',new)
            self.assertEqual(self.client.get('/api/account').status_code,200)
            from backend import auth
            with self.module.db() as conn:
                auth.initialize(conn,'other-user','different-bootstrap-value',self.module.now())
                row=dict(conn.execute('SELECT * FROM console_account WHERE id=1').fetchone())
                events=conn.execute('SELECT event FROM account_events').fetchall()
            self.assertTrue(auth.authenticate(self.module.DATA/'console.sqlite','admin',new))
            self.assertFalse(auth.authenticate(self.module.DATA/'console.sqlite','admin','different-bootstrap-value'))
            self.assertNotEqual(old_row['salt'],row['salt'])
            self.assertNotIn(new,json.dumps(row))
            self.assertNotIn('test-only-password',json.dumps(row))
            self.assertIn('password_changed',[e['event'] for e in events])
        finally:
            self.client.auth=('admin',new)
            restored=self.post('/api/account/password',{'current_password':new,'new_password':'test-only-password','confirm_password':'test-only-password'})
            self.assertEqual(restored.status_code,200,restored.text)
            self.client.auth=('admin','test-only-password')

    def test_job_state_guard(self):
        res=self.post('/api/jobs/31/actions',{'action':'resume','confirmation':'RESUME 31','request_key':'state-guard-test-key'})
        self.assertEqual(res.status_code,409)
        self.assertEqual(self.post('/api/jobs/32/actions',{'action':'abort','confirmation':'wrong','request_key':'confirmation-test-key'}).status_code,422)

    def test_abort_failed_job_requires_confirmation_and_fresh_state(self):
        body={'action':'abort','confirmation':'ABORT 31','request_key':'abort-failed-job-test'}
        with patch.object(self.module,'enqueue',return_value={'id':'test-abort'}) as queued:
            with patch.object(self.module,'query_jobs',return_value={'jobs':[{'id':31,'status':'FAILED'}]}) as query:
                response=self.post('/api/jobs/31/actions',body)
                self.assertEqual(response.status_code,202,response.text)
                query.assert_called_once_with(31)
                queued.assert_called_once_with(body['request_key'],'abort',['abort','job','-jobid','31'],job_id=31)
            queued.reset_mock()
            self.assertEqual(self.post('/api/jobs/31/actions',{**body,'confirmation':'wrong'}).status_code,422)
            with patch.object(self.module,'query_jobs',return_value={'jobs':[{'id':31,'status':'SUCCEEDED'}]}):
                self.assertEqual(self.post('/api/jobs/31/actions',body).status_code,409)
            queued.assert_not_called()

    def test_delete_job_confirmation_states_and_command(self):
        body={'action':'delete','confirmation':'DELETE 31','request_key':'delete-confirmed-job-31'}
        with patch.object(self.module,'enqueue',return_value={'id':'test-delete'}) as queued:
            for state in ['FAILED','SUCCEEDED','ABORTED','TERMINATED','REVERTED']:
                with self.subTest(state=state),patch.object(self.module,'query_jobs',return_value={'jobs':[{'id':31,'status':state}]}):
                    response=self.post('/api/jobs/31/actions',body)
                    self.assertEqual(response.status_code,202,response.text)
                    queued.assert_called_with(body['request_key'],'delete',['delete','job','-jobids','31'],job_id=31)
            queued.reset_mock()
            for state in ['EXECUTING','SCHEDULED','PAUSED','SUSPENDING','SUSPENDED','UNKNOWN']:
                with self.subTest(state=state),patch.object(self.module,'query_jobs',return_value={'jobs':[{'id':31,'status':state}]}):
                    self.assertEqual(self.post('/api/jobs/31/actions',body).status_code,409)
            with patch.object(self.module,'query_jobs',return_value={'jobs':[]}):
                self.assertEqual(self.post('/api/jobs/31/actions',body).status_code,409)
            self.assertEqual(self.post('/api/jobs/31/actions',{**body,'confirmation':'DELETE 32'}).status_code,422)
            queued.assert_not_called()

    def test_delete_job_persists_and_retries_once(self):
        body={'action':'delete','confirmation':'DELETE 31','request_key':'delete-idempotent-job-31'}
        first=self.post('/api/jobs/31/actions',body)
        self.assertEqual(first.status_code,202,first.text)
        # A repeat must succeed even after the vendor no longer returns the job.
        with patch.object(self.module,'query_jobs',side_effect=AssertionError('duplicate queried deleted job')):
            self.assertEqual(self.post('/api/jobs/31/actions',body).json(),first.json())
        for _ in range(100):
            row=next(o for o in self.client.get('/api/operations').json() if o['id']==first.json()['id'])
            if row['state'] not in {'QUEUED','RUNNING'}:break
            time.sleep(.02)
        self.assertEqual(row['state'],'COMPLETED',row)
        self.assertEqual(row['kind'],'delete')
        self.assertEqual(sum(args==['delete','job','-jobids','31'] for args in self.calls),1)

    def test_submission_idempotency_and_persistence(self):
        pid=self.profile_id()
        body={'profile_id':pid,'evaluate':True,'request_key':'repeatable-eval-request'}
        first=self.post('/api/submissions',body)
        self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(first.json(),self.post('/api/submissions',body).json())
        for _ in range(100):
            operations=self.client.get('/api/operations').json()
            row=next(o for o in operations if o['id']==first.json()['id'])
            if row['state'] not in {'QUEUED','RUNNING'}:break
            time.sleep(.02)
        self.assertEqual(row['state'],'SUBMITTED',row)
        self.assertEqual(row['job_id'],43)
        self.assertEqual(sum(args[:2]==['migrate','database'] for args in self.calls),1)

if __name__=='__main__':unittest.main()
