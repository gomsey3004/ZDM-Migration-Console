import json
import re
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch,MagicMock

from backend import adapter,migration,interactive
import test_console as baseline


class MigrationOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline.ApiTests.setUpClass.__func__(cls)
        cls.client.base_url='https://testserver'
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post

    def test_all_noninteractive_option_groups_generate_separate_arguments(self):
        rsp=self.stage/'logical-all-options.rsp';rsp.write_text('MIGRATION_METHOD=ONLINE_LOGICAL\n')
        values={'targethome':'/u02/app/oracle/product/19/dbhome_1','advisor_mode':'ignoreadvisor','genfixup':'NO',
                'ignore':['WARNING','PATCH_CHECK'],'ignoremissingpatches':'123,456','schedule':'PAUSE','jobtag':'MY_JOB',
                'pauseafter':'ZDM_CONFIGURE_DG_SRC','stopafter':'ZDM_CONFIGURE_DG_TGT','incrementalinterval':'30',
                'backupuser':'backupuser','restoreuser':'restoreuser','dvowner':'dvuser','imagetype':'MY_IMAGE',
                'useractiondata':'migration=Finance; environment=Test'}
        args=adapter.migration_args({**self.profile,'rsp':str(rsp),'migration_options':values},False)
        for name in values:
            if name=='advisor_mode':self.assertIn('-ignoreadvisor',args)
            else:self.assertIn('-'+name,args)
        self.assertEqual(args[args.index('-ignore')+1],'WARNING,PATCH_CHECK')
        self.assertIn(values['useractiondata'],args)
        self.assertNotIn('-eval',args)
        evaluated=adapter.migration_args({**self.profile,'rsp':str(rsp),'migration_options':values},True)
        self.assertIn('-eval',evaluated);self.assertIn('-schedule',evaluated);self.assertIn('-stopafter',evaluated)

    def test_authentication_modes_are_exclusive_and_plugin_overrides_are_validated(self):
        cases=[('root','-srcroot'),('credential','-srccred'),('user','-srcuser'),('sudo','-srcsudouser')]
        for mode,flag in cases:
            args=adapter.migration_args({**self.profile,'source_auth_mode':mode,'source_credential':'REGISTERED'},True)
            self.assertIn(flag,args);self.assertNotIn('-srcauth',args)
        args=adapter.migration_args({**self.profile,'source_auth_plugin':'sudoasuser','source_ssh_port':2222,
                                     'source_auth_args':{'4':'custom_property:some value'}},True)
        self.assertIn('-srcarg15',args);self.assertIn('ssh_port=2222',args)
        self.assertIn('custom_property:some value',args)
        for override in ({'1':'password:do-not-save'},{'16':'field:value'},{'2':'identity_file:/etc/shadow'},{'4':'field:value\n-injected'}):
            with self.assertRaises(ValueError):adapter.migration_args({**self.profile,'source_auth_args':override},True)

    def test_logical_response_only_and_database_source_connections(self):
        rsp=self.stage/'logical-options.rsp';rsp.write_text('MIGRATION_METHOD=OFFLINE_LOGICAL\nSOURCEDATABASE_ENVIRONMENT_DBTYPE=ADBS\n')
        profile={**self.profile,'rsp':str(rsp),'source_host':'','source_identifier':'','identifier_type':'none','source_auth_mode':'none'}
        args=adapter.migration_args(profile,True)
        self.assertNotIn('-sourcedb',args);self.assertNotIn('-sourcenode',args);self.assertNotIn('-srcauth',args)
        server=self.post('/api/connections',{'name':'Source ADB','role':'source','kind':'database','host':'source.example.com','database_port':1522})
        self.assertEqual(server.status_code,200,server.text)
        result=self.post('/api/profiles',{**profile,'source_server_id':server.json()['id']})
        self.assertEqual(result.status_code,200,result.text)
        with self.assertRaises(ValueError):adapter.migration_args({**self.profile,'source_auth_mode':'none'},True)

    def test_password_flags_never_accept_password_values(self):
        flags=['tdekeystorepasswd','tgttdekeystorepasswd','tdemasterkey','backuppasswd','okvkeystorepasswd','tgtokvkeystorepasswd']
        args=adapter.migration_args({**self.profile,'migration_options':{flag:True for flag in flags}},True)
        for flag in flags:
            self.assertIn('-'+flag,args)
            with self.assertRaises(ValueError):migration.clean_options({flag:'never-save-this-password'})
        for values in ({'incrementalinterval':0},{'schedule':'2026-01-01T12:00'},
                       {'ignore':['ALL','WARNING']},{'advisor_mode':'skipadvisor','genfixup':'YES'},
                       {'tdekeystorepasswd':True,'tdekeystorewallet':'/u01/app/wallet'},
                       {'useractiondata':'password=never-save-this-password'},{'arbitrary-shell-command':'id'}):
            with self.assertRaises(ValueError):migration.clean_options(values)

    def test_wallet_options_and_documented_aliases(self):
        wallet=self.base/'test-wallet';wallet.mkdir();(wallet/'cwallet.sso').write_bytes(b'test wallet fixture')
        for name in [row['name'] for row in migration.OPTIONS if row['type']=='wallet']:
            args=adapter.migration_args({**self.profile,'migration_options':{name:str(wallet)}},True)
            self.assertIn('-'+name,args);self.assertIn(str(wallet.resolve()),args)
        args=adapter.migration_args({**self.profile,'migration_options':{'tdewallet':str(wallet)}},True)
        self.assertIn('-tdekeystorewallet',args);self.assertNotIn('-tdewallet',args)

    def test_physical_database_identifier_is_required(self):
        for values in ({'source_identifier':''},{'source_identifier':'PROD','identifier_type':'none'}):
            with self.assertRaisesRegex(ValueError,'physical migration requires a database identifier'):
                adapter.migration_args({**self.profile,**values},True)
        for kind,flag in [('sourcedb','-sourcedb'),('sourcesid','-sourcesid')]:
            args=adapter.migration_args({**self.profile,'source_identifier':'PROD','identifier_type':kind},True)
            self.assertIn(flag,args);self.assertEqual(args[args.index(flag)+1],'PROD')

    def test_diagnostic_modes_preview_and_real_migration_guard(self):
        rsp=self.stage/'logical-diagnostic-options.rsp';rsp.write_text('MIGRATION_METHOD=ONLINE_LOGICAL\n')
        result=self.post('/api/profiles',{**self.profile,'rsp':str(rsp)});pid=result.json()['id']
        for mode,flag in [('evaluate','-eval'),('advisor','-advisor'),('summary','-summary'),('listphases','-listphases')]:
            preview=self.client.get('/api/profiles/'+pid+'/preview',params={'mode':mode})
            self.assertEqual(preview.status_code,200,preview.text)
            self.assertIn(flag,preview.json()['command'])
        result=self.post('/api/submissions',{'profile_id':pid,'mode':'migrate','evaluate':True,'request_key':'cannot-bypass-confirmation'})
        self.assertEqual(result.status_code,422)
        with patch.object(self.module,'enqueue',return_value={'id':'mock-logical-start'}) as queue:
            result=self.post('/api/submissions',{'profile_id':pid,'mode':'migrate','evaluate':True,'confirmation':'START '+self.profile['name'],'request_key':'logical-advisory-checks'})
            self.assertEqual(result.status_code,202,result.text)
            self.assertEqual(queue.call_args.args[1],'migrate')
            self.assertNotIn('-eval',queue.call_args.args[2])

    def test_actions_follow_current_response_file_and_physical_cpat_is_omitted(self):
        rsp=self.stage/'mode-dependent.rsp';rsp.write_text('MIGRATION_METHOD=ONLINE_PHYSICAL\n')
        body={**self.profile,'rsp':str(rsp),'migration_options':{'advisor_mode':'skipadvisor','genfixup':'NO'}}
        result=self.post('/api/profiles',body);self.assertEqual(result.status_code,200,result.text)
        pid=result.json()['id']
        def listed():return next(profile for profile in self.client.get('/api/profiles').json() if profile['id']==pid)
        self.assertEqual(listed()['migration_kind'],'physical')
        self.assertNotIn('advisor',listed()['available_actions'])
        args=adapter.migration_args(body,True)
        self.assertNotIn('-skipadvisor',args);self.assertNotIn('-genfixup',args)
        self.assertIn('-listphases',adapter.migration_args(body,mode='listphases'))
        for mode in ('advisor','summary'):
            preview=self.client.get('/api/profiles/'+pid+'/preview',params={'mode':mode})
            self.assertEqual(preview.status_code,422,preview.text)
            submitted=self.post('/api/submissions',{'profile_id':pid,'mode':mode,'request_key':'physical-rejected-'+mode})
            self.assertEqual(submitted.status_code,422,submitted.text)
        options=self.client.get('/api/migration-options',params={'rsp':str(rsp)}).json()
        self.assertFalse(migration.CPAT_OPTIONS&{row['name'] for row in options['options']})
        rsp.write_text('MIGRATION_METHOD=OFFLINE_LOGICAL\n')
        self.assertEqual(listed()['migration_kind'],'logical')
        self.assertIn('advisor',listed()['available_actions'])
        self.assertEqual(self.client.get('/api/profiles/'+pid+'/preview',params={'mode':'advisor'}).status_code,200)
        options=self.client.get('/api/migration-options',params={'rsp':str(rsp)}).json()
        self.assertTrue(migration.CPAT_OPTIONS<={row['name'] for row in options['options']})
        # A stale logical preview cannot be used after the response file becomes physical.
        rsp.write_text('MIGRATION_METHOD=OFFLINE_PHYSICAL\n')
        self.assertEqual(self.post('/api/submissions',{'profile_id':pid,'mode':'advisor','request_key':'stale-logical-preview'}).status_code,422)

    def test_missing_or_conflicting_method_does_not_enable_cpat(self):
        for content in ('','# A template\nMIGRATION_METHOD=\n','MIGRATION_METHOD=ONLINE_LOGICAL\nMIGRATION_METHOD=ONLINE_PHYSICAL\n'):
            self.assertEqual(migration.method_info(content)['migration_kind'],'unknown')
            self.assertNotIn('advisor',migration.method_info(content)['available_actions'])
        self.assertEqual(migration.describe({'rsp':'/nonexistent.rsp'})['migration_kind'],'unknown')

    def test_secure_prompt_answer_is_ephemeral_and_validation_never_echoes_secret(self):
        oid='test-operation-prompt';event=threading.Event()
        session={'id':'test-prompt-id','prompt':'Enter source SYS password:','event':event,'value':None,'expires_at':time.time()+30}
        with interactive.LOCK:interactive.SESSIONS[oid]=session
        body={'prompt_id':session['id'],'value':'test-only-private-secret'}
        try:
            http=self.client.post('http://testserver/api/operations/'+oid+'/credential',json=body,headers=self.headers)
            self.assertEqual(http.status_code,403)
            result=self.post('/api/operations/'+oid+'/credential',body)
            self.assertEqual(result.status_code,200,result.text)
            self.assertNotIn(body['value'],result.text)
            self.assertTrue(event.is_set());self.assertEqual(session['value'],body['value'])
            again=self.post('/api/operations/'+oid+'/credential',body)
            self.assertEqual(again.status_code,422)
            malformed=self.post('/api/operations/'+oid+'/credential',{'prompt_id':[], 'value':body['value']})
            self.assertEqual(malformed.status_code,422);self.assertNotIn(body['value'],malformed.text)
            with self.module.db() as connection:
                self.assertNotIn(body['value'],str([tuple(row) for row in connection.execute('SELECT * FROM operations')]))
        finally:
            with interactive.LOCK:interactive.SESSIONS.pop(oid,None)

    def test_real_pty_prompt_and_secret_echo_are_redacted(self):
        binary=self.home/'bin/zdmcli';binary.parent.mkdir(exist_ok=True)
        binary.write_text('#!'+sys.executable+'\nimport sys\nprint("Enter source database SYS user password:",end=" ",flush=True)\nvalue=sys.stdin.readline().strip()\nprint("Deliberate test echo: "+value)\nprint(\'Operation scheduled with the job ID "123".\')\n')
        binary.chmod(0o755);oid='real-pty-test'
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result=pool.submit(interactive.run,oid,['migrate','database'],10)
                deadline=time.monotonic()+5;prompt=None
                while time.monotonic()<deadline:
                    prompt=interactive.pending(oid)
                    if prompt:break
                    time.sleep(.02)
                self.assertIsNotNone(prompt)
                interactive.answer(oid,prompt['id'],'one-time-test-secret')
                output=result.result(timeout=5)
            self.assertNotIn('one-time-test-secret',output)
            self.assertIn('[REDACTED]',output)
            self.assertIn('123',output)
            self.assertIsNone(interactive.pending(oid))
        finally:binary.unlink(missing_ok=True)

    def test_catalog_covers_command_reference_and_installed_extensions(self):
        schema=self.client.get('/api/migration-options').json()
        covered=set(schema['coverage']['core']+schema['coverage']['actions']+schema['coverage']['authentication'])
        covered.update(row['name'] for row in schema['options']);covered.update(schema['coverage']['aliases'])
        covered.update(['ignoreadvisor','skipadvisor'])
        expected='sourcedb sourcesid rsp sourcenode targetnode targethome eval tdekeystorepasswd tgttdekeystorepasswd tdemasterkey useractiondata imagetype backupuser restoreuser backuppasswd dvowner tdewallet tgttdewallet tdekeystorewallet tgttdekeystorewallet sourcesyswallet osswallet ossrestorewallet dvwallet backupwallet srcroot srccred srcuser srcsudouser srcsudopath srcauth tgtroot tgtcred tgtuser tgtsudouser tgtsudopath tgtauth schedule pauseafter stopafter listphases ignoremissingpatches ignore incrementalinterval advisor ignoreadvisor skipadvisor genfixup srcred summary jobtag okvkeystorepasswd tgtokvkeystorepasswd'.split()
        self.assertFalse(set(expected)-covered)

    def test_root_mode_preflight_can_use_explicit_transport_only_connection(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from backend import connections
        public=ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
        fingerprint=connections.public_fingerprint(public)
        source=self.post('/api/connections',{'name':'Root source','role':'source','kind':'ssh','host':'source.example.com',
                       'authentication_check':'zdm','database_port':None}).json()
        target=self.post('/api/connections',{'name':'SQL target','role':'target','kind':'database','host':'target.example.com'}).json()
        with patch.object(connections,'scan_host',return_value=[{'public_key':public,'fingerprint':fingerprint,'algorithm':'ssh-ed25519'}]):
            self.post('/api/connections/'+source['id']+'/host-key/scan',{})
        self.post('/api/connections/'+source['id']+'/host-key/trust',{'fingerprint':fingerprint,'confirmation':fingerprint})
        result=self.post('/api/profiles',{**self.profile,'name':'Root transport','source_server_id':source['id'],'target_server_id':target['id'],'source_auth_mode':'root'})
        self.assertEqual(result.status_code,200,result.text);pid=result.json()['id']
        def getent(args,**kwargs):
            self.assertEqual(args[0],'/usr/bin/getent','A transport-only check must not attempt SSH authentication.')
            return 0,'192.0.2.10 STREAM node\n'
        with patch.object(connections,'command',side_effect=getent),patch.object(connections.socket,'create_connection',return_value=MagicMock()):
            check=self.post('/api/profiles/'+pid+'/connections/check',{})
        self.assertEqual(check.status_code,200,check.text)
        self.assertTrue(check.json()['ok'],check.text)
        self.assertTrue(any(step['status']=='DEFERRED' for step in check.json()['results']['source']['steps']))
        preview=self.client.get('/api/profiles/'+pid+'/preview').json()['command']
        self.assertIn('-srcroot',preview);self.assertNotIn('-srcauth',preview)


if __name__=='__main__':unittest.main()
