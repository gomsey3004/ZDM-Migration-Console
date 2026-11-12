import json
import stat
import time
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519,rsa
from backend import connections
import test_console as baseline

class ConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline.ApiTests.setUpClass.__func__(cls)
        cls.client.base_url='https://testserver'
        cls.ssh_private=ed25519.Ed25519PrivateKey.generate()
        cls.ssh_text=cls.ssh_private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.OpenSSH,serialization.NoEncryption()).decode()
        cls.rsa_private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.rsa_text=cls.rsa_private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post

    def new_key(self,kind='ssh'):
        response=self.post('/api/keys',{'name':'Test '+kind,'kind':kind,'private_key':self.ssh_text if kind=='ssh' else self.rsa_text})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def new_server(self,key=None,kind='ssh',port=22):
        body={'name':'Test endpoint','role':'both','kind':kind,'host':'server.example.com','ssh_port':port,'database_port':1521}
        if kind=='ssh':body['ssh_key_id']=(key or self.new_key())['id']
        response=self.post('/api/connections',body)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def trust(self,server):
        public=self.ssh_private.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
        key={'public_key':public,'fingerprint':connections.public_fingerprint(public),'algorithm':'ssh-ed25519'}
        with patch.object(connections,'scan_host',return_value=[key]):
            self.assertEqual(self.post('/api/connections/'+server['id']+'/host-key/scan',{}).status_code,200)
        response=self.post('/api/connections/'+server['id']+'/host-key/trust',{'fingerprint':key['fingerprint'],'confirmation':key['fingerprint']})
        self.assertEqual(response.status_code,200,response.text)
        return self.module.server_record(server['id'])

    def test_private_key_validation_storage_and_no_secret_echo(self):
        key=self.new_key()
        self.assertNotIn('private_key',key)
        self.assertNotIn('BEGIN OPENSSH PRIVATE KEY',json.dumps(key))
        self.assertTrue(key['fingerprint'].startswith('SHA256:'))
        self.assertEqual(stat.S_IMODE(Path(key['path']).stat().st_mode),0o600)
        encrypted=self.rsa_private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.BestAvailableEncryption(b'test-passphrase')).decode()
        for value in ['invalid-secret-key-text',encrypted]:
            result=self.post('/api/keys',{'name':'Invalid','kind':'ssh','private_key':value})
            self.assertEqual(result.status_code,422)
            self.assertNotIn(value,result.text)
        malformed=self.post('/api/keys',{'name':'bad','kind':'wrong','private_key':'never-echo-this-key'})
        self.assertEqual(malformed.status_code,422)
        self.assertNotIn('never-echo-this-key',malformed.text)

    def test_key_upload_rejects_unencrypted_public_http(self):
        response=self.client.post('http://testserver/api/keys',json={'name':'not-imported','kind':'ssh','private_key':self.ssh_text},headers=self.headers)
        self.assertEqual(response.status_code,403)
        self.assertNotIn(self.ssh_text,response.text)

    def test_legacy_1024_bit_rsa_ssh_import_and_registration(self):
        private=rsa.generate_private_key(public_exponent=65537,key_size=1024)
        for format in (serialization.PrivateFormat.OpenSSH,serialization.PrivateFormat.TraditionalOpenSSL):
            content=private.private_bytes(serialization.Encoding.PEM,format,serialization.NoEncryption()).decode()
            result=self.post('/api/keys',{'name':'Legacy RSA SSH','kind':'ssh','private_key':content})
            self.assertEqual(result.status_code,200,result.text)
            saved=result.json()
            self.assertNotIn(content,result.text)
            self.assertEqual(stat.S_IMODE(Path(saved['path']).stat().st_mode),0o600)
            normalized=serialization.load_pem_private_key(Path(saved['path']).read_bytes(),password=None)
            self.assertEqual(normalized.key_size,1024)
            registered=self.post('/api/keys',{'name':'Existing legacy RSA','kind':'ssh','existing_path':saved['path']})
            self.assertEqual(registered.status_code,200,registered.text)
            self.assertEqual(registered.json()['fingerprint'],saved['fingerprint'])
            oci=self.post('/api/keys',{'name':'Too small for OCI','kind':'oci','private_key':content})
            self.assertEqual(oci.status_code,422,oci.text)
            self.assertNotIn(content,oci.text)

    def test_register_existing_and_reject_wrong_key_type(self):
        key=self.new_key()
        result=self.post('/api/keys',{'name':'Existing','kind':'ssh','existing_path':key['path']})
        self.assertEqual(result.status_code,200,result.text)
        result=self.post('/api/keys',{'name':'Wrong','kind':'oci','private_key':self.ssh_text})
        self.assertEqual(result.status_code,422)
        self.assertEqual(self.post('/api/keys',{'name':'Outside','kind':'ssh','existing_path':'/etc/shadow'}).status_code,422)

    def test_oci_config_fingerprint_and_signed_read_only_check(self):
        key=self.new_key('oci')
        body={'name':'Phoenix','profile_name':'PHX','key_id':key['id'],'user':'ocid1.user.oc1..aaaa',
              'tenancy':'ocid1.tenancy.oc1..aaaa','region':'us-phoenix-1','fingerprint':key['fingerprint']}
        self.assertEqual(self.post('/api/oci-profiles',{**body,'fingerprint':'wrong'}).status_code,422)
        self.assertEqual(self.post('/api/oci-profiles',{**body,'region':'us-phoenix-1\nkey_file=/etc/passwd'}).status_code,422)
        result=self.post('/api/oci-profiles',body)
        self.assertEqual(result.status_code,200,result.text)
        profile=result.json()
        config=Path(profile['config_file'])
        self.assertEqual(stat.S_IMODE(config.stat().st_mode),0o600)
        self.assertIn('key_file='+key['path'],config.read_text())
        self.assertNotIn('PRIVATE KEY',config.read_text())
        with patch.object(connections,'command',return_value=(0,json.dumps({'data':[{'name':'PHX-AD-1'}]}))) as cmd:
            checked=self.post('/api/oci-profiles/'+profile['id']+'/check',{})
            self.assertTrue(checked.json()['ok'],checked.text)
            args=cmd.call_args.args[0]
            self.assertIn(profile['config_file'],args)
            self.assertEqual(args[-5:],['iam','availability-domain','list','--compartment-id',body['tenancy']])
            self.assertIn('api_key',args)

    def test_host_key_requires_independent_confirmation(self):
        server=self.new_server()
        public=self.ssh_private.public_key().public_bytes(serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
        fingerprint=connections.public_fingerprint(public)
        with patch.object(connections,'scan_host',return_value=[{'public_key':public,'fingerprint':fingerprint,'algorithm':'ssh-ed25519'}]):
            self.post('/api/connections/'+server['id']+'/host-key/scan',{})
        response=self.post('/api/connections/'+server['id']+'/host-key/trust',{'fingerprint':fingerprint,'confirmation':'not-verified'})
        self.assertEqual(response.status_code,422)
        self.assertIsNone(self.module.server_record(server['id'])['trusted_key'])
        trusted=self.trust(server)
        self.assertEqual(trusted['trusted_key'],public)
        self.assertEqual(stat.S_IMODE(connections.known_hosts_path(server['id']).stat().st_mode),0o600)

    def test_ssh_checks_fail_closed_until_host_is_pinned(self):
        key=self.new_key()
        server=self.new_server(key)
        def command(args,**kwargs):
            if 'getent' in args[0]:return 0,'192.0.2.10 STREAM server.example.com\n'
            self.fail('SSH authentication must not run before host-key trust.')
        with patch.object(connections,'command',side_effect=command),patch.object(connections.socket,'create_connection',return_value=MagicMock()):
            result=connections.check_server(server,key['path'])
        self.assertFalse(result['ok'])
        self.assertTrue(any(s['name']=='SSH host identity' and s['status']=='FAIL' for s in result['steps']))

    def test_ssh_success_and_host_key_mismatch(self):
        key=self.new_key()
        server=self.trust(self.new_server(key,port=2222))
        def command(args,**kwargs):
            if 'getent' in args[0]:return 0,'192.0.2.10 STREAM server.example.com\n'
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('IdentitiesOnly=yes',args)
            return 0,'ZDM_CONNECTION_OK\nopc\nserver\n'
        with patch.object(connections,'command',side_effect=command),patch.object(connections.socket,'create_connection',return_value=MagicMock()):
            result=connections.check_server(server,key['path'])
        self.assertTrue(result['ok'],result)
        self.assertTrue(any(s['name']=='Passwordless sudo' for s in result['steps']))
        def mismatch(args,**kwargs):
            return (0,'192.0.2.10 STREAM server.example.com\n') if 'getent' in args[0] else (255,'REMOTE HOST IDENTIFICATION HAS CHANGED!')
        with patch.object(connections,'command',side_effect=mismatch),patch.object(connections.socket,'create_connection',return_value=MagicMock()):
            self.assertFalse(connections.check_server(server,key['path'])['ok'])

    def test_database_endpoint_does_not_attempt_ssh(self):
        server=self.new_server(kind='database')
        with patch.object(connections,'command',return_value=(0,'192.0.2.10 STREAM database\n')) as cmd,patch.object(connections.socket,'create_connection',return_value=MagicMock()):
            result=connections.check_server(server)
        self.assertTrue(result['ok'])
        self.assertTrue(all('getent' in call.args[0][0] for call in cmd.call_args_list))
        self.assertIn('not a database login',result['scope'])

    def test_preflight_required_fresh_and_bound_to_keys_and_settings(self):
        key=self.new_key()
        source=self.trust(self.new_server(key,port=2222))
        target=self.new_server(kind='database')
        profile={**self.profile,'name':'Managed','source_server_id':source['id'],'target_server_id':target['id']}
        result=self.post('/api/profiles',profile)
        self.assertEqual(result.status_code,200,result.text)
        pid=result.json()['id']
        body={'profile_id':pid,'evaluate':False,'confirmation':'START Managed','request_key':'managed-migration-request'}
        self.assertEqual(self.post('/api/submissions',body).status_code,409)
        preview=self.client.get('/api/profiles/'+pid+'/preview').json()['command']
        self.assertIn('ssh_port=2222',preview)
        self.assertNotIn('-targetnode',preview)
        passed={'ok':True,'steps':[{'name':'Test fixture','status':'PASS','detail':'passed','duration_ms':1}],'scope':'test'}
        with patch.object(connections,'check_server',return_value={**passed,'ok':False}):
            failed=self.post('/api/profiles/'+pid+'/connections/check',{}).json()
        self.assertFalse(failed['ok'])
        self.assertEqual(self.post('/api/submissions',{**body,'preflight_id':failed['id']}).status_code,409)
        with patch.object(connections,'check_server',return_value=passed):
            check=self.post('/api/profiles/'+pid+'/connections/check',{}).json()
        self.assertTrue(check['ok'])
        body['preflight_id']=check['id']
        with self.module.db() as conn:
            conn.execute('UPDATE preflights SET created=? WHERE id=?',(time.time()-901,check['id']))
        self.assertEqual(self.post('/api/submissions',body).status_code,409)
        with patch.object(connections,'check_server',return_value=passed):
            check=self.post('/api/profiles/'+pid+'/connections/check',{}).json()
        body['preflight_id']=check['id']
        self.rsp.write_text('MIGRATION_METHOD=OFFLINE_PHYSICAL\n')
        self.assertEqual(self.post('/api/submissions',body).status_code,409)
        with patch.object(connections,'check_server',return_value=passed):
            check=self.post('/api/profiles/'+pid+'/connections/check',{}).json()
        body['preflight_id']=check['id']
        accepted=self.post('/api/submissions',body)
        self.assertEqual(accepted.status_code,202,accepted.text)
        for _ in range(100):
            rows=self.client.get('/api/operations').json()
            row=next(r for r in rows if r['id']==accepted.json()['id'])
            if row['state'] not in {'QUEUED','RUNNING'}:break
            time.sleep(.02)
        self.assertEqual(row['state'],'SUBMITTED',row)
        # A credential-file change invalidates the same check even at the same path.
        Path(key['path']).write_text(self.ssh_text+'\n')
        changed={**body,'request_key':'managed-new-request-after-key-change'}
        self.assertEqual(self.post('/api/submissions',changed).status_code,409)

if __name__=='__main__':unittest.main()
