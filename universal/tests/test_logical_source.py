import unittest,uuid
from unittest.mock import patch
import test_console as baseline

class LogicalSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post
    def make(self,method='OFFLINE_LOGICAL',**changes):
        path=self.stage/(uuid.uuid4().hex+'.rsp');path.write_text('MIGRATION_METHOD='+method+'\nTARGETDATABASE_DBTYPE=ADBS\nSOURCEDATABASE_CONNECTIONDETAILS_HOST=db.example.com\nSOURCEDATABASE_CONNECTIONDETAILS_PORT=1521\nSOURCEDATABASE_CONNECTIONDETAILS_SERVICENAME=SRC\n')
        body={**self.profile,'rsp':str(path),'name':'Logical no SSH','source_access':'response','source_server_id':'stale-source','source_host':'stale.example.com','source_key':'/missing/key','source_auth_args':{'2':'identity_file:/missing/other'},**changes}
        result=self.post('/api/profiles',body);self.assertEqual(result.status_code,200,result.text);return result.json()['id']
    def start(self,pid,**changes):return self.post('/api/submissions',{'profile_id':pid,'evaluate':False,'confirmation':'START Logical no SSH','request_key':uuid.uuid4().hex,**changes})
    def test_online_offline_omit_source_cli_flags_and_stale_references(self):
        for method in ['ONLINE_LOGICAL','OFFLINE_LOGICAL']:
            pid=self.make(method);p=self.module.get_profile(pid)
            self.assertEqual(p['source_server_id'],'');self.assertEqual(p['source_auth_args'],{});self.assertEqual(p['source_identifier'],'')
            command=self.client.get('/api/profiles/'+pid+'/preview').json()['command']
            for flag in ['-sourcenode','-sourcedb','-sourcesid','-srcauth','-srcarg','/missing/key']:self.assertNotIn(flag,command)
    def test_absent_failed_and_expired_checks_do_not_block_logical(self):
        pid=self.make()
        with patch.object(self.module,'enqueue',return_value={'id':'test-only'}) as queue:
            self.assertEqual(self.start(pid).status_code,202)
            for result in [None,{'valid':False,'ok':False,'id':'failed'},{'valid':False,'ok':True,'id':'expired'}]:
                with patch.object(self.module,'latest_preflight',return_value=result):self.assertEqual(self.start(pid,preflight_id='old').status_code,202)
            self.assertEqual(queue.call_count,4)
    def test_checks_skip_unselected_endpoints_without_network_calls(self):
        pid=self.make()
        with patch.object(self.module,'perform_server_check',side_effect=AssertionError('Unexpected SSH')),patch.object(self.module,'perform_oci_check',side_effect=AssertionError('Unexpected API')):
            result=self.post('/api/profiles/'+pid+'/connections/check',{});self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(result.json()['advisory'])
        for endpoint in result.json()['results'].values():self.assertEqual(endpoint['steps'][0]['status'],'SKIP')
    def test_configured_source_ssh_still_emitted(self):
        pid=self.make(source_access='auto',source_server_id='',source_key=str(self.key),source_auth_args={},source_identifier='SRC')
        command=self.client.get('/api/profiles/'+pid+'/preview').json()['command'];self.assertIn('-sourcenode',command);self.assertIn('-srcauth',command)
    def test_confirmation_role_and_race_guards_remain(self):
        pid=self.make()
        with patch.object(self.module,'enqueue') as queue:
            self.assertEqual(self.start(pid,confirmation='wrong').status_code,422)
            with patch.object(self.module,'profile_signature',side_effect=['before','after']):self.assertEqual(self.start(pid).status_code,409)
            queue.assert_not_called()
    def test_physical_cannot_use_response_only_to_omit_source(self):
        path=self.stage/(uuid.uuid4().hex+'.rsp');path.write_text('MIGRATION_METHOD=ONLINE_PHYSICAL\n')
        r=self.post('/api/profiles',{**self.profile,'rsp':str(path),'source_host':'','source_access':'response'})
        self.assertEqual(r.status_code,422)
        r=self.post('/api/profiles',{**self.profile,'rsp':str(path),'source_access':'auto'});self.assertEqual(r.status_code,200,r.text)
        with patch.object(self.module,'enqueue') as queue:
            r=self.post('/api/submissions',{'profile_id':r.json()['id'],'evaluate':False,'confirmation':'START '+self.profile['name'],'request_key':uuid.uuid4().hex});self.assertEqual(r.status_code,409);queue.assert_not_called()

if __name__=='__main__':unittest.main()
