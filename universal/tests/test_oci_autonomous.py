import json
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch
from backend import connections,migration,responses
import test_console as baseline

class OciAutonomousTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post
    def setUp(self):
        iid=uuid.uuid4().hex;stamp=self.module.now();self.oid=uuid.uuid4().hex
        self.api_key=self.base/(iid+'.pem');self.api_key.write_text('private fixture content never included in RSP');self.api_key.chmod(0o600)
        self.oci={'name':'Saved PHX','profile_name':'PHX','key_id':iid,'region':'us-phoenix-1','user':'ocid1.user.oc1..exampleuser','tenancy':'ocid1.tenancy.oc1..exampletenancy','fingerprint':'ab:cd','key_file':str(self.api_key),'config_file':str(self.base/(iid+'.config'))}
        Path(self.oci['config_file']).write_text('nonsecret fixture config')
        self.sid=uuid.uuid4().hex;self.src_key=uuid.uuid4().hex
        source={'name':'Saved source','role':'source','kind':'ssh','host':'source.example.com','os_user':'opc','ssh_port':22,'ssh_key_id':self.src_key,'database_port':1521,'database_host':''}
        with self.module.db() as conn:
            conn.execute('INSERT INTO managed_keys VALUES (?,?,?,?,?,?,?)',(iid,'OCI key','oci',str(self.api_key),'ab:cd','public fixture',stamp))
            conn.execute('INSERT INTO managed_keys VALUES (?,?,?,?,?,?,?)',(self.src_key,'SSH key','ssh',str(self.key),'SHA256:fixture','public fixture',stamp))
            conn.execute('INSERT INTO oci_profiles(id,body,created) VALUES (?,?,?)',(self.oid,json.dumps(self.oci),stamp))
            conn.execute('INSERT INTO servers(id,body,updated) VALUES (?,?,?)',(self.sid,json.dumps(source),stamp))
        self.ocid='ocid1.autonomousdatabase.oc1.phx.exampledatabase'
        self.logical='MIGRATION_METHOD=OFFLINE_LOGICAL\nTARGETDATABASE_DBTYPE=ADBS\nTARGETDATABASE_OCID='+self.ocid+'\n'
        self.path=self.stage/(iid+'.rsp')
        self.path.write_text(responses.apply_oci(self.logical,{'id':self.oid,**self.oci}))
        self.passed={'ok':True,'steps':[{'name':'Fixture','status':'PASS','detail':'Passed','duration_ms':1}],'scope':'test fixture'}
    def make_profile(self,**changes):
        body={**self.__class__.profile,'name':'Autonomous target','rsp':str(self.path),'source_server_id':self.sid,'target_server_id':'','target_host':'',**changes}
        r=self.post('/api/profiles',body);self.assertEqual(r.status_code,200,r.text);return r.json()['id']
    def test_oci_snapshot_mapping_and_no_private_contents(self):
        r=self.client.get('/api/oci-profiles/'+self.oid+'/response-values')
        self.assertEqual(r.status_code,200,r.text);fields=r.json()['values']
        self.assertEqual(len(fields),5);self.assertEqual(fields['OCIAUTHENTICATIONDETAILS_REGIONID'],'us-phoenix-1')
        self.assertEqual(fields['OCIAUTHENTICATIONDETAILS_USERPRINCIPAL_PRIVATEKEYFILE'],str(self.api_key))
        self.assertNotIn('private fixture content',r.text)
        content=self.logical+'PROFILE=NONE\nOCIAUTHENTICATIONDETAILS_REGIONID=old\nOCIAUTHENTICATIONDETAILS_REGIONID=other\n'
        preview=self.post('/api/responses/oci-preview',{'content':content,'oci_profile_id':self.oid})
        self.assertEqual(preview.status_code,200,preview.text)
        text=preview.json()['content'];self.assertEqual(text.count('OCIAUTHENTICATIONDETAILS_REGIONID='),1)
        self.assertIn('PROFILE=NONE',text);self.assertNotIn('PROFILE=PHX',text)
        self.assertNotIn('private fixture content',text)
    def test_create_import_and_edit_apply_profile_with_revision_guard(self):
        r=self.post('/api/responses',{'name':'with-oci','content':self.logical,'oci_profile_id':self.oid})
        self.assertEqual(r.status_code,200,r.text);path=r.json()['path']
        loaded=self.client.get('/api/responses/content',params={'path':path}).json()
        self.assertIn('OCIAUTHENTICATIONDETAILS_USERPRINCIPAL_USERID=',loaded['content'])
        body={'path':path,'content':loaded['content']+'CUSTOM_OPTION=YES\n','revision':loaded['revision'],'oci_profile_id':self.oid}
        r=self.client.put('/api/responses/content',headers=self.headers,json=body);self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(Path(r.json()['backup']).is_file())
        self.assertEqual(self.client.put('/api/responses/content',headers=self.headers,json=body).status_code,409)
        self.assertEqual(Path(path).stat().st_mode&0o777,0o600)
    def test_builder_preview_populates_saved_oci_fields(self):
        values={'MIGRATION_METHOD':'OFFLINE_LOGICAL','DATA_TRANSFER_MEDIUM':'DBLINK','SOURCEDATABASE_ADMINUSERNAME':'SYSTEM','TARGETDATABASE_ADMINUSERNAME':'ADMIN','SOURCEDATABASE_ENVIRONMENT_DBTYPE':'ORACLE','TARGETDATABASE_DBTYPE':'ADBS','TARGETDATABASE_OCID':self.ocid,'SOURCEDATABASE_CONNECTIONDETAILS_HOST':'src.example.com','SOURCEDATABASE_CONNECTIONDETAILS_PORT':'1521','SOURCEDATABASE_CONNECTIONDETAILS_SERVICENAME':'SRC'}
        r=self.post('/api/responses/build',{'name':'logical','kind':'logical','values':values,'preview':True,'oci_profile_id':self.oid})
        self.assertEqual(r.status_code,200,r.text);self.assertIn(self.oci['tenancy'],r.json()['content'])
        self.assertNotIn('private fixture content',r.text)
    def test_physical_response_rejects_logical_oci_parameter_injection(self):
        r=self.post('/api/responses',{'name':'physical','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n','oci_profile_id':self.oid})
        self.assertEqual(r.status_code,422)
        r=self.post('/api/responses/build',{'name':'physical','kind':'physical','values':{},'preview':True,'oci_profile_id':self.oid})
        self.assertEqual(r.status_code,422)
    def test_adb_profile_ignores_stale_target_os_details_and_infers_oci(self):
        pid=self.make_profile(target_server_id='missing-stale-target',target_host='stale.example.com',target_key='/missing-target-key',target_credential='/missing-target-wallet',migration_options={'targethome':'/old/oracle/home'})
        saved=self.module.get_profile(pid)
        self.assertEqual(saved['target_server_id'],'');self.assertEqual(saved['oci_profile_id'],self.oid)
        command=self.client.get('/api/profiles/'+pid+'/preview').json()['command']
        for flag in ['-targetnode','-tgtauth','-tgtarg','-targethome','/missing-target-key']:self.assertNotIn(flag,command)
        self.assertIn('-sourcenode',command)
        info=self.client.get('/api/migration-options',params={'rsp':str(self.path)}).json()
        self.assertTrue(info['autonomous_target']);self.assertNotIn('targethome',[o['name'] for o in info['options']])
    def test_adb_preflight_source_and_oci_target_without_target_connection(self):
        pid=self.make_profile()
        body={'profile_id':pid,'evaluate':False,'confirmation':'START Autonomous target','request_key':uuid.uuid4().hex}
        with patch.object(self.module,'enqueue',return_value={'id':'advisory-start'}):self.assertEqual(self.post('/api/submissions',body).status_code,202)
        with patch.object(connections,'check_server',return_value=self.passed) as source,patch.object(connections,'check_autonomous',return_value=self.passed) as target,patch.object(connections,'check_oci',side_effect=AssertionError('Unnecessary AD-list check')):
            r=self.post('/api/profiles/'+pid+'/connections/check',{})
            self.assertEqual(r.status_code,200,r.text);check=r.json();self.assertTrue(check['valid'])
            source.assert_called_once();target.assert_called_once();self.assertEqual(target.call_args.args[1],self.ocid)
        with patch.object(self.module,'enqueue',return_value={'id':'mock-operation'}) as queue:
            r=self.post('/api/submissions',{**body,'preflight_id':check['id']});self.assertEqual(r.status_code,202,r.text)
            self.assertNotIn('-targetnode',queue.call_args.args[2])
        self.path.write_text(self.path.read_text()+'CUSTOM_OPTION=changed\n')
        with patch.object(self.module,'enqueue',return_value={'id':'advisory-start'}):self.assertEqual(self.post('/api/submissions',{**body,'preflight_id':check['id']}).status_code,202)
    def test_adb_target_failure_is_advisory_and_oci_mismatch_is_reported(self):
        pid=self.make_profile()
        with patch.object(connections,'check_server',return_value=self.passed),patch.object(connections,'check_autonomous',return_value={**self.passed,'ok':False}):
            r=self.post('/api/profiles/'+pid+'/connections/check',{});self.assertFalse(r.json()['ok'])
        with patch.object(self.module,'enqueue',return_value={'id':'advisory-start'}):self.assertEqual(self.post('/api/submissions',{'profile_id':pid,'evaluate':False,'confirmation':'START Autonomous target','request_key':uuid.uuid4().hex,'preflight_id':r.json()['id']}).status_code,202)
        self.path.write_text(self.path.read_text().replace('OCIAUTHENTICATIONDETAILS_REGIONID=us-phoenix-1','OCIAUTHENTICATIONDETAILS_REGIONID=us-ashburn-1'))
        r=self.post('/api/profiles/'+pid+'/connections/check',{});self.assertEqual(r.status_code,422);self.assertIn('Apply the selected OCI profile',r.text)
    def test_other_targets_still_require_saved_target_connection(self):
        self.path.write_text('MIGRATION_METHOD=OFFLINE_LOGICAL\nTARGETDATABASE_DBTYPE=ORACLE\n')
        pid=self.make_profile()
        with patch.object(connections,'check_server',return_value=self.passed):
            result=self.post('/api/profiles/'+pid+'/connections/check',{});self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['results']['target']['steps'][0]['status'],'SKIP')
        self.assertFalse(migration.target_info('MIGRATION_METHOD=ONLINE_PHYSICAL\nTARGETDATABASE_DBTYPE=ADBS\n')['autonomous_target'])
        self.assertFalse(migration.target_info(self.logical+'TARGETDATABASE_DBTYPE=ORACLE\n')['autonomous_target'])
    def test_autonomous_probe_is_read_only_and_checks_exact_resource(self):
        for state,ok in [('AVAILABLE',True),('STOPPED',False)]:
            with self.subTest(state=state),patch.object(connections,'command',return_value=(0,json.dumps({'id':self.ocid,'state':state}))) as cmd:
                result=connections.check_autonomous(self.oci,self.ocid);self.assertEqual(result['ok'],ok)
                args=cmd.call_args.args[0];self.assertIn('get',args);self.assertNotIn('start',args);self.assertIn(self.ocid,args)
        with patch.object(connections,'command') as cmd:
            with self.assertRaises(ValueError):connections.check_autonomous(self.oci,'invalid;command')
            cmd.assert_not_called()

if __name__=='__main__':unittest.main()
