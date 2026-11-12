import json
import uuid
import unittest
from pathlib import Path
import test_console as baseline

class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post
    def fixture(self,kind,**extra):
        iid=uuid.uuid4().hex;name='Item '+iid[:6];stamp=self.module.now()
        if kind=='keys':
            path=self.base/(iid+'.key');path.write_text('disposable key fixture');path.chmod(0o600)
            row={'id':iid,'name':name,'kind':'ssh','path':str(path),'fingerprint':'fixture','public_key':'public fixture','created':stamp}
        elif kind=='wallets':
            path=self.base/iid;path.mkdir();(path/'cwallet.sso').write_text('opaque disposable wallet');(path/'cwallet.sso').chmod(0o600)
            row={'id':iid,'name':name,'purpose':'WALLET_SOURCEGGADMIN','username':'GGADMIN','path':str(path),'created':stamp,'last_check':'{}'}
        else:
            body={'name':name,'kind':'ssh','ssh_key_id':'','host':'db.example.com',**extra}
            if kind=='oci-profiles':
                config=self.base/(iid+'.config');config.write_text('nonsecret config fixture');body.update(config_file=str(config));body.setdefault('key_id','')
                row={'id':iid,'body':json.dumps(body),'last_check':'{}','created':stamp}
            else:row={'id':iid,'body':json.dumps(body),'trusted_key':'verified fixture','scan':None,'last_check':'{}','updated':stamp}
        table=self.module.inventory.TABLES[kind]
        with self.module.db() as conn:
            conn.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        return {'id':iid,'name':name,**({'path':str(path)} if kind in {'wallets','keys'} else {})}
    def delete(self,kind,item,confirmation=None):
        return self.post('/api/inventory/'+kind+'/'+item['id']+'/delete',{'confirmation':confirmation or 'DELETE '+item['name']})
    def restore(self,kind,item):return self.post('/api/inventory/'+kind+'/'+item['id']+'/restore',{'confirmation':'RESTORE '+item['name']})
    def add_profile(self,body):
        pid=uuid.uuid4().hex
        with self.module.db() as conn:conn.execute('INSERT INTO profiles VALUES (?,?,?)',(pid,json.dumps({'name':'Dependent profile',**body}),self.module.now()))
        self.addCleanup(self.remove_profile,pid)
    def remove_profile(self,pid):
        with self.module.db() as conn:conn.execute('DELETE FROM profiles WHERE id=?',(pid,))
    def test_delete_restore_all_types_keeps_original_files(self):
        for kind in ['keys','wallets','connections','oci-profiles']:
            with self.subTest(kind=kind):
                item=self.fixture(kind)
                original=Path(item['path'])/ 'cwallet.sso' if kind=='wallets' else Path(item['path']) if kind=='keys' else None
                content=original.read_bytes() if original else None
                self.assertEqual(self.delete(kind,item).status_code,200)
                if original:self.assertEqual(original.read_bytes(),content)
                archived=self.client.get('/api/inventory/deleted').json()
                found=next(r for r in archived if r['id']==item['id'])
                self.assertEqual(set(found),{'kind','id','name','deleted_at'})
                table=self.module.inventory.TABLES[kind]
                with self.module.db() as conn:self.assertIsNone(conn.execute('SELECT id FROM '+table+' WHERE id=?',(item['id'],)).fetchone())
                self.assertEqual(self.restore(kind,item).status_code,200)
                with self.module.db() as conn:
                    row=conn.execute('SELECT * FROM '+table+' WHERE id=?',(item['id'],)).fetchone()
                    self.assertIsNotNone(row)
                    if kind in {'connections','oci-profiles'}:self.assertIsNone(row['last_check'])
    def test_confirmation_and_unknown_kind(self):
        item=self.fixture('keys')
        self.assertEqual(self.delete('keys',item,'DELETE wrong').status_code,422)
        self.assertEqual(self.delete('untrusted-table',item).status_code,404)
        self.assertEqual(self.delete('keys',item).status_code,200)
        self.assertEqual(self.delete('keys',item).status_code,404)
        self.assertEqual(self.post('/api/inventory/keys/'+item['id']+'/restore',{'confirmation':'wrong'}).status_code,422)
        self.assertEqual(self.restore('keys',item).status_code,200)
    def test_profiles_block_registered_ids_and_manual_paths(self):
        for kind,field in [('wallets','wallet_ids'),('connections','source_server_id'),('oci-profiles','oci_profile_id'),('keys','source_key')]:
            item=self.fixture(kind)
            value=[item['id']] if field=='wallet_ids' else item['path'] if kind=='keys' else item['id']
            self.add_profile({field:value})
            r=self.delete(kind,item)
            self.assertEqual(r.status_code,409,r.text);self.assertIn('Dependent profile',r.text)
    def test_key_dependencies_and_restore_order(self):
        key=self.fixture('keys');server=self.fixture('connections',ssh_key_id=key['id'])
        self.assertEqual(self.delete('keys',key).status_code,409)
        self.assertEqual(self.delete('connections',server).status_code,200)
        oci=self.fixture('oci-profiles',key_id=key['id'])
        self.assertEqual(self.delete('keys',key).status_code,409)
        self.assertEqual(self.delete('oci-profiles',oci).status_code,200)
        self.assertEqual(self.delete('keys',key).status_code,200)
        self.assertEqual(self.restore('connections',server).status_code,409)
        self.assertEqual(self.restore('oci-profiles',oci).status_code,409)
        self.assertEqual(self.restore('keys',key).status_code,200)
        self.assertEqual(self.restore('connections',server).status_code,200)
        self.assertEqual(self.restore('oci-profiles',oci).status_code,200)
    def test_pending_commands_block_delete_and_restore(self):
        item=self.fixture('keys');archived=self.fixture('keys')
        self.assertEqual(self.delete('keys',archived).status_code,200)
        oid=uuid.uuid4().hex;stamp=self.module.now()
        with self.module.db() as conn:conn.execute('INSERT INTO operations(id,request_key,kind,state,command,created,updated) VALUES (?,?,?,?,?,?,?)',(oid,oid,'test','RUNNING','fixture',stamp,stamp))
        try:
            self.assertEqual(self.delete('keys',item).status_code,409)
            self.assertEqual(self.restore('keys',archived).status_code,409)
        finally:
            with self.module.db() as conn:conn.execute('DELETE FROM operations WHERE id=?',(oid,))
    def test_missing_file_blocks_restore(self):
        item=self.fixture('keys');self.assertEqual(self.delete('keys',item).status_code,200)
        Path(item['path']).unlink()
        self.assertEqual(self.restore('keys',item).status_code,409)

if __name__=='__main__':unittest.main()
