import json
import uuid
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import auth,adapter
import test_console as baseline

class UserRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline.ApiTests.setUpClass.__func__(cls)
        cls.client.base_url='https://testserver'
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post
    def new_user(self,role='viewer',activate=True):
        name='user-'+uuid.uuid4().hex[:10];temporary='temporary-test-password';password='changed-test-password'
        r=self.post('/api/users',{'username':name,'role':role,'password':temporary,'confirm_password':temporary})
        self.assertEqual(r.status_code,200,r.text)
        client=TestClient(self.module.app,base_url='https://testserver');client.auth=(name,temporary)
        if activate:
            r=client.post('/api/account/password',headers=self.headers,json={'current_password':temporary,'new_password':password,'confirm_password':password})
            self.assertEqual(r.status_code,200,r.text);client.auth=(name,password)
        return client,name,password if activate else temporary
    def test_existing_admin_preserved_and_hashes_never_returned(self):
        with self.module.db() as conn:
            before=dict(conn.execute('SELECT * FROM console_account WHERE id=1').fetchone())
            auth.initialize(conn,'ignored-bootstrap','different-password',self.module.now())
            after=dict(conn.execute('SELECT * FROM console_account WHERE id=1').fetchone())
        self.assertEqual(before,after)
        account=self.client.get('/api/account').json()
        self.assertEqual(account['role'],'admin');self.assertTrue(account['can_admin'])
        users=self.client.get('/api/users').json()
        self.assertNotIn('digest',json.dumps(users));self.assertNotIn('salt',json.dumps(users))
        self.assertNotIn('test-only-password',json.dumps(users))
    def test_first_login_must_change_own_password(self):
        client,name,temporary=self.new_user(activate=False)
        self.assertTrue(client.get('/api/account').json()['must_change_password'])
        self.assertEqual(client.get('/api/jobs').status_code,403)
        self.assertEqual(client.post('/api/submissions',headers=self.headers,json={}).status_code,403)
        r=client.post('/api/account/password',headers=self.headers,json={'current_password':temporary,'new_password':'new-private-test-password','confirm_password':'new-private-test-password'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(client.get('/api/account').status_code,401)
        client.auth=(name,'new-private-test-password')
        self.assertFalse(client.get('/api/account').json()['must_change_password'])
        self.assertEqual(client.get('/api/jobs').status_code,200)
    def test_viewer_mutation_routes_blocked_before_side_effects(self):
        viewer,_,_=self.new_user()
        self.assertEqual(viewer.get('/api/jobs').status_code,200)
        self.assertEqual(viewer.get('/api/profiles').status_code,200)
        self.assertFalse(viewer.get('/api/account').json()['can_manage'])
        with patch.object(adapter,'run',side_effect=AssertionError('viewer executed a command')):
            for path in ['/api/submissions','/api/jobs/31/actions','/api/profiles','/api/responses','/api/keys','/api/wallets','/api/connections','/api/oci-profiles','/api/name-resolution/check','/api/ai/chat','/api/inventory/keys/example/delete','/api/inventory/keys/example/restore','/api/operations/example/credential']:
                with self.subTest(path=path):self.assertEqual(viewer.post(path,headers=self.headers,json={}).status_code,403)
        self.assertEqual(viewer.get('/api/users').status_code,403)
        self.assertEqual(viewer.put('/api/name-resolution',headers=self.headers,json={}).status_code,403)
    def test_manager_can_configure_but_not_administer_users_or_ai(self):
        manager,_,_=self.new_user('manager')
        account=manager.get('/api/account').json();self.assertTrue(account['can_manage']);self.assertFalse(account['can_admin'])
        r=manager.post('/api/responses',headers=self.headers,json={'name':'manager-fixture','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n'})
        self.assertEqual(r.status_code,200,r.text)
        for method,path in [('GET','/api/users'),('POST','/api/users'),('PUT','/api/users/admin'),('POST','/api/users/admin/password'),('PUT','/api/ai/settings'),('POST','/api/ai/test')]:
            with self.subTest(path=path):self.assertEqual(manager.request(method,path,headers=self.headers,json={}).status_code,403)
        self.assertEqual(manager.get('/api/ai/settings').status_code,200)
    def test_disabled_and_demoted_users_lose_access_immediately(self):
        client,name,_=self.new_user('manager')
        self.assertEqual(self.client.put('/api/users/'+name,headers=self.headers,json={'role':'manager','enabled':False,'confirmation':'UPDATE '+name}).status_code,200)
        self.assertEqual(client.get('/api/account').status_code,401)
        self.assertEqual(self.client.put('/api/users/'+name,headers=self.headers,json={'role':'viewer','enabled':True,'confirmation':'UPDATE '+name}).status_code,200)
        self.assertEqual(client.get('/api/account').json()['role'],'viewer')
        self.assertEqual(client.post('/api/responses',headers=self.headers,json={}).status_code,403)
    def test_last_admin_and_confirmation_protection(self):
        for role,enabled in [('viewer',True),('admin',False)]:
            r=self.client.put('/api/users/admin',headers=self.headers,json={'role':role,'enabled':enabled,'confirmation':'UPDATE admin'})
            self.assertEqual(r.status_code,409,r.text)
        self.assertEqual(self.client.put('/api/users/admin',headers=self.headers,json={'role':'admin','enabled':True,'confirmation':'wrong'}).status_code,422)
        self.assertEqual(self.post('/api/users/admin/password',{'password':'temporary-test-password','confirm_password':'temporary-test-password','confirmation':'RESET admin'}).status_code,422)
    def test_admin_reset_invalidates_old_credentials_and_records_no_password(self):
        client,name,password=self.new_user('manager');temporary='new-temporary-test-password'
        r=self.post('/api/users/'+name+'/password',{'password':temporary,'confirm_password':temporary,'confirmation':'RESET '+name})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(client.get('/api/account').status_code,401)
        client.auth=(name,temporary)
        self.assertTrue(client.get('/api/account').json()['must_change_password'])
        events=self.client.get('/api/users').json()['events']
        self.assertTrue(any(e['event']=='password_reset' and e['subject']==name for e in events))
        self.assertNotIn(temporary,json.dumps(events));self.assertNotIn(password,json.dumps(events))
    def test_username_validation_and_sensitive_error_redaction(self):
        _,name,_=self.new_user()
        r=self.post('/api/users',{'username':name.upper(),'role':'viewer','password':'temporary-test-password','confirm_password':'temporary-test-password'})
        self.assertEqual(r.status_code,409)
        r=self.post('/api/users',{'username':'valid-user','role':'root','password':{'secret':'never-echo-this'},'confirm_password':'never-echo-this'})
        self.assertEqual(r.status_code,422);self.assertNotIn('never-echo-this',r.text)
    def test_route_policy_covers_all_current_mutations(self):
        for route in self.module.app.routes:
            if not getattr(route,'path','').startswith('/api/'):continue
            for method in getattr(route,'methods',set())-{'GET','HEAD','OPTIONS'}:
                if route.path=='/api/account/password':continue
                with self.subTest(path=route.path,method=method):
                    self.assertFalse(auth.permitted('viewer',method,route.path))
                    if route.path.startswith('/api/users') or route.path in {'/api/ai/settings','/api/ai/test'}:
                        self.assertFalse(auth.permitted('manager',method,route.path))

if __name__=='__main__':unittest.main()
