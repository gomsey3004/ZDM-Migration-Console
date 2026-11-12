import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from backend import wallets, responses
import test_console as baseline


class WalletResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline.ApiTests.setUpClass.__func__(cls)
        cls.client.base_url='https://testserver'
        folder=cls.stage/'zdm26.1/template/26.1.0'
        folder.mkdir(parents=True)
        for kind in ('logical','physical'):
            (folder/f'zdm_{kind}_template.rsp').write_text('MIGRATION_METHOD=\nDATA_TRANSFER_MEDIUM=\nCUSTOM_PARAMETER=\n')

    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post

    def new_wallet(self,purpose='WALLET_SOURCEGGADMIN',username='GGADMIN'):
        def create(path,*args):
            path.mkdir(parents=True,mode=0o700)
            file=path/'cwallet.sso';file.write_bytes(b'opaque wallet fixture');file.chmod(0o600)
        with patch.object(wallets,'create',side_effect=create):
            result=self.post('/api/wallets',{'name':'Test credential','purpose':purpose,'username':username,
                'password':'private-test-password','confirm_password':'private-test-password'})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_wallet_secret_not_returned_or_stored_in_database(self):
        wallet=self.new_wallet()
        self.assertNotIn('private-test-password',json.dumps(wallet))
        with self.module.db() as conn:
            self.assertNotIn('private-test-password',str([tuple(r) for r in conn.execute('SELECT * FROM credential_wallets')]))
        self.assertEqual((Path(wallet['path'])/'cwallet.sso').stat().st_mode&0o777,0o600)

    def test_public_http_and_invalid_passwords_rejected_without_echo(self):
        body={'name':'test','purpose':'WALLET_TARGETADMIN','username':'ADMIN','password':'never-echo-secret','confirm_password':'never-echo-secret'}
        result=self.client.post('http://testserver/api/wallets',json=body,headers=self.headers)
        self.assertEqual(result.status_code,403)
        result=self.post('/api/wallets',{**body,'confirm_password':'different'})
        self.assertEqual(result.status_code,422)
        self.assertNotIn('never-echo-secret',result.text)
        for password in ('','x\nline','x\x00'):
            with self.assertRaises(ValueError):wallets.validate_input('WALLET_TARGETADMIN','ADMIN',password)

    def test_profile_wallet_assignment_versions_rsp_and_deselection(self):
        wallet=self.new_wallet()
        source=self.stage/'logical.rsp'
        source.write_text('# Original\nMIGRATION_METHOD=ONLINE_LOGICAL\nSOURCEDATABASE_GGADMINUSERNAME=OLD\n')
        body={**self.profile,'rsp':str(source),'wallet_ids':[wallet['id']]}
        result=self.post('/api/profiles',body)
        self.assertEqual(result.status_code,200,result.text)
        saved={'id':result.json()['id'],**self.module.get_profile(result.json()['id'])};content=Path(saved['rsp']).read_text()
        self.assertNotEqual(saved['rsp'],str(source))
        self.assertIn('WALLET_SOURCEGGADMIN='+str(Path(wallet['path']).resolve()),content)
        self.assertIn('SOURCEDATABASE_GGADMINUSERNAME=GGADMIN',content)
        self.assertNotIn('WALLET_',source.read_text())
        self.assertEqual(self.post('/api/profiles',{**body,'wallet_ids':[wallet['id'],wallet['id']]}).status_code,422)
        result=self.client.put('/api/profiles/'+saved['id'],json={**body,'rsp':saved['rsp'],'wallet_ids':[]},headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn('WALLET_SOURCEGGADMIN=',Path(self.module.get_profile(saved['id'])['rsp']).read_text())
        self.assertEqual(self.post('/api/profiles',{**body,'rsp':str(self.rsp)}).status_code,422)

    def test_physical_wallet_uses_directory_cli_argument(self):
        wallet=self.new_wallet('SOURCE_SYS_PHYSICAL','SYS')
        result=self.post('/api/profiles',{**self.profile,'wallet_ids':[wallet['id']]})
        self.assertEqual(result.status_code,200,result.text)
        preview=self.client.get('/api/profiles/'+result.json()['id']+'/preview').json()['command']
        self.assertIn('-sourcesyswallet '+str(Path(wallet['path']).resolve()),preview)
        self.assertNotIn('cwallet.sso',preview)

    def test_additional_physical_wallets_populate_cli_and_okv_response(self):
        wallet=self.new_wallet('PHYSICAL_OSS_BACKUP','OBJECTSTOREUSER')
        result=self.post('/api/profiles',{**self.profile,'wallet_ids':[wallet['id']]})
        self.assertEqual(result.status_code,200,result.text)
        preview=self.client.get('/api/profiles/'+result.json()['id']+'/preview').json()['command']
        self.assertIn('-osswallet',preview);self.assertIn('-backupuser OBJECTSTOREUSER',preview)
        okv=self.new_wallet('PHYSICAL_SOURCE_OKV','OKVUSER')
        result=self.post('/api/profiles',{**self.profile,'wallet_ids':[okv['id']]})
        self.assertEqual(result.status_code,200,result.text)
        pid=result.json()['id'];saved=self.module.get_profile(pid)
        self.assertIn('WALLET_OKVKEYSTORE=',Path(saved['rsp']).read_text())
        updated=self.client.put('/api/profiles/'+pid,json={**saved,'wallet_ids':[]},headers=self.headers)
        self.assertEqual(updated.status_code,200,updated.text)
        self.assertNotIn('WALLET_OKVKEYSTORE=',Path(self.module.get_profile(pid)['rsp']).read_text())

    def test_wallet_pty_keeps_secret_out_of_arguments_and_logs(self):
        path=self.base/'pty-wallet'
        child=MagicMock();child.exitstatus=0
        with patch.object(wallets.connections,'command',return_value=(0,'')),patch.object(wallets.pexpect,'spawn',return_value=child) as spawn,patch.object(wallets,'verify',return_value={'ok':True}):
            wallets.create(path,'WALLET_TARGETADMIN','ADMIN','test-secret')
        self.assertNotIn('test-secret',str(spawn.call_args))
        self.assertFalse(spawn.call_args.kwargs['echo'])
        self.assertIsNone(child.logfile)
        self.assertEqual(child.sendline.call_count,2)
        bad=self.base/'failed-wallet'
        with patch.object(wallets.connections,'command',return_value=(1,'secret-failure-output')):
            with self.assertRaises(ValueError) as error:wallets.create(bad,'WALLET_TARGETADMIN','ADMIN','test-secret')
        self.assertNotIn('secret-failure-output',str(error.exception))
        self.assertFalse(bad.exists())

    def test_separate_schema_and_conditional_required_parameters(self):
        logical=self.client.get('/api/response-schema/logical').json()
        physical=self.client.get('/api/response-schema/physical').json()
        self.assertNotIn('SOURCEDATABASE_ADMINUSERNAME',[f['name'] for f in physical['fields']])
        self.assertIn('SOURCEDATABASE_ADMINUSERNAME',logical['core'])
        self.assertIn('BACKUP_PATH',responses.required_fields('physical',{'DATA_TRANSFER_MEDIUM':'NFS'}))
        self.assertIn('ZDM_SRC_DB_RESTORE_SERVICE_NAME',responses.required_fields('physical',{'DATA_TRANSFER_MEDIUM':'DIRECT'}))
        self.assertIn('GOLDENGATEHUB_URL',responses.required_fields('logical',{'MIGRATION_METHOD':'ONLINE_LOGICAL'}))

    def test_generate_preview_optional_custom_and_secret_validation(self):
        values={'MIGRATION_METHOD':'ONLINE_PHYSICAL','DATA_TRANSFER_MEDIUM':'NFS','TGT_DB_UNIQUE_NAME':'TARGET','BACKUP_PATH':'/backup','CUSTOM_PARAMETER':'value'}
        body={'name':'generated','kind':'physical','values':values}
        preview=self.post('/api/responses/build',{**body,'preview':True})
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertNotIn('path',preview.json())
        result=self.post('/api/responses/build',body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn('CUSTOM_PARAMETER=value',Path(result.json()['path']).read_text())
        for changes in ({'BACKUP_PATH':''},{'CUSTOM_PARAMETER':'value\nINJECTED=1'},{'DB_PASSWORD':'secret'}):
            self.assertEqual(self.post('/api/responses/build',{**body,'values':{**values,**changes}}).status_code,422)
        self.assertEqual(self.post('/api/responses',{'name':'imported','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n'}).status_code,200)

    def test_secret_named_boolean_and_wallet_path_parameters(self):
        content='DATAPUMPSETTINGS_CREATEAUTHTOKEN=true\nZDM_TGT_DBLINK_PASSWORD_WALLET=/u01/app/wallets/link\nSRC_DB_PASSWORDFILE_LOC=/u01/app/dbs/orapwPROD\n'
        self.assertEqual(self.post('/api/responses',{'name':'paths','content':content}).status_code,200)
        for content in ('DATAPUMPSETTINGS_CREATEAUTHTOKEN=actual-secret','ZDM_TGT_DBLINK_PASSWORD_WALLET=actual-secret','PASSWORD=secret\nPASSWORD='):
            result=self.post('/api/responses',{'name':'invalid','content':content})
            self.assertEqual(result.status_code,422,result.text)


if __name__=='__main__':unittest.main()
