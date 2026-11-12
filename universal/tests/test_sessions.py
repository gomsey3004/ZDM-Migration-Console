import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend import auth,adapter
import test_console as baseline

class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    def browser(self):return TestClient(self.module.app,base_url='https://testserver',headers={'X-ZDM-Session':'browser'})
    def login(self,client):
        r=client.post('/api/auth/login',headers=self.headers,json={'username':'admin','password':'test-only-password'})
        self.assertEqual(r.status_code,200,r.text);return r
    def test_public_signin_shell_does_not_expose_api_data(self):
        c=self.browser()
        self.assertEqual(c.get('/').status_code,200)
        r=c.get('/api/jobs');self.assertEqual(r.status_code,401);self.assertNotIn('WWW-Authenticate',r.headers)
        # Even a browser with previously cached valid Basic credentials needs a session.
        c.auth=('admin','test-only-password')
        self.assertEqual(c.get('/api/account').status_code,401)
    def test_logout_revokes_token_and_clears_cookie(self):
        c=self.browser();r=self.login(c)
        cookie=r.headers['set-cookie'].lower()
        for flag in ['httponly','secure','samesite=strict','max-age=28800']:self.assertIn(flag,cookie)
        token=c.cookies.get(auth.COOKIE)
        self.assertEqual(c.get('/api/account').status_code,200)
        with self.module.db() as conn:
            rows=conn.execute('SELECT token_hash FROM console_sessions').fetchall()
            self.assertNotIn(token,str([tuple(row) for row in rows]))
        with patch.object(adapter,'run',side_effect=AssertionError('logout changed a migration')):
            r=c.post('/api/auth/logout',headers=self.headers,json={});self.assertEqual(r.status_code,200,r.text)
        self.assertIn('max-age=0',r.headers['set-cookie'].lower())
        self.assertEqual(c.get('/api/account').status_code,401)
        c.cookies.set(auth.COOKIE,token)
        self.assertEqual(c.get('/api/account').status_code,401)
    def test_login_logout_csrf_and_invalid_credentials(self):
        c=self.browser()
        self.assertEqual(c.post('/api/auth/login',json={'username':'admin','password':'test-only-password'}).status_code,403)
        self.assertEqual(c.post('/api/auth/login',headers={**self.headers,'Origin':'https://evil.example'},json={'username':'admin','password':'test-only-password'}).status_code,403)
        r=c.post('/api/auth/login',headers=self.headers,json={'username':'admin','password':'wrong'})
        self.assertEqual(r.status_code,401);self.assertNotIn('wrong',r.text)
        self.login(c)
        self.assertEqual(c.post('/api/auth/logout',headers={**self.headers,'Origin':'https://evil.example'},json={}).status_code,403)
        self.assertEqual(c.get('/api/account').status_code,200)
    def test_expired_sessions_cannot_be_reused(self):
        c=self.browser();self.login(c)
        with self.module.db() as conn:conn.execute('UPDATE console_sessions SET expires=?',(time.time()-1,))
        self.assertEqual(c.get('/api/account').status_code,401)
    def test_password_change_rotates_current_session_and_revokes_other_session(self):
        first=self.browser();second=self.browser();self.login(first);self.login(second)
        old=first.cookies.get(auth.COOKIE);new='new-session-test-password'
        try:
            r=first.post('/api/account/password',headers=self.headers,json={'current_password':'test-only-password','new_password':new,'confirm_password':new})
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotEqual(first.cookies.get(auth.COOKIE),old)
            self.assertEqual(first.get('/api/account').status_code,200)
            self.assertEqual(second.get('/api/account').status_code,401)
        finally:
            r=first.post('/api/account/password',headers=self.headers,json={'current_password':new,'new_password':'test-only-password','confirm_password':'test-only-password'})
            self.assertEqual(r.status_code,200,r.text)
    def test_logout_is_idempotent_and_can_switch_accounts_by_new_login(self):
        c=self.browser();self.login(c)
        self.assertEqual(c.post('/api/auth/logout',headers=self.headers,json={}).status_code,200)
        self.assertEqual(c.post('/api/auth/logout',headers=self.headers,json={}).status_code,200)
        self.login(c);self.assertEqual(c.get('/api/account').json()['username'],'admin')
    def test_browser_navigation_cannot_use_cached_basic_credentials(self):
        c=TestClient(self.module.app,base_url='https://testserver',headers={'User-Agent':'Mozilla/5.0'})
        c.auth=('admin','test-only-password')
        self.assertEqual(c.get('/').status_code,200)
        self.assertEqual(c.get('/api/jobs').status_code,401)
        self.assertEqual(self.client.get('/api/jobs').status_code,200)
    def test_cookie_roles_and_disable_are_rechecked_each_request(self):
        username='cookie-role-user';salt='12'*32;stamp=self.module.now()
        with self.module.db() as conn:
            conn.execute('INSERT INTO console_users VALUES (?,?,?,?,?,?,?,?)',(username,salt,auth.digest('test-only-password',salt),'manager',1,0,stamp,stamp))
        browser=self.browser()
        self.assertEqual(browser.post('/api/auth/login',headers=self.headers,json={'username':username,'password':'test-only-password'}).status_code,200)
        with self.module.db() as conn:conn.execute("UPDATE console_users SET role='viewer' WHERE username=?",(username,))
        self.assertEqual(browser.get('/api/account').json()['role'],'viewer')
        self.assertEqual(browser.post('/api/profiles',headers=self.headers,json={}).status_code,403)
        admin=TestClient(self.module.app,base_url='https://testserver');admin.auth=('admin','test-only-password')
        self.assertEqual(admin.put('/api/users/'+username.upper(),headers=self.headers,json={'role':'viewer','enabled':False,'confirmation':'UPDATE '+username}).status_code,200)
        self.assertEqual(browser.get('/api/account').status_code,401)
        self.assertEqual(admin.put('/api/users/'+username,headers=self.headers,json={'role':'viewer','enabled':True,'confirmation':'UPDATE '+username}).status_code,200)
        self.assertEqual(browser.get('/api/account').status_code,401)

    def test_login_validation_does_not_echo_password_and_requires_https(self):
        c=self.browser();secret='must-not-echo-test-secret'
        r=c.post('/api/auth/login',headers=self.headers,json={'username':'admin','password':{'value':secret}})
        self.assertEqual(r.status_code,422);self.assertNotIn(secret,r.text)
        c.base_url='http://testserver'
        self.assertEqual(c.post('/api/auth/login',headers=self.headers,json={'username':'admin','password':'test-only-password'}).status_code,403)

if __name__=='__main__':unittest.main()
