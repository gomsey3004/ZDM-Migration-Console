import unittest
import os
from unittest.mock import patch
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
import test_console as baseline


class HTTPSProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)

    def proxy_client(self,host):
        wrapped=ProxyHeadersMiddleware(self.module.app,trusted_hosts=['127.0.0.1'])
        client=TestClient(wrapped,client=(host,12345))
        client.auth=('admin','test-only-password')
        return client

    def test_local_proxy_preserves_client_ip_https_and_origin(self):
        client=self.proxy_client('127.0.0.1')
        headers={**self.headers,'X-Forwarded-For':'testclient','X-Forwarded-Proto':'https','Origin':'https://testserver'}
        response=client.post('/api/responses',headers=headers,json={'name':'https-proxy','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n'})
        self.assertEqual(response.status_code,200,response.text)
        # An invalid private key should reach credential validation over HTTPS,
        # while the same public-client request over HTTP is rejected first.
        response=client.post('/api/keys',headers=headers,json={'name':'test','kind':'ssh','private_key':'invalid-test-key'})
        self.assertEqual(response.status_code,422,response.text)
        response=client.post('/api/keys',headers={**headers,'X-Forwarded-Proto':'http','Origin':'http://testserver'},json={'name':'test','kind':'ssh','private_key':'invalid-test-key'})
        self.assertEqual(response.status_code,403,response.text)
        response=client.post('/api/responses',headers={**headers,'Origin':'https://evil.example'},json={'name':'bad-origin','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n'})
        self.assertEqual(response.status_code,403,response.text)

    def test_public_access_keeps_authentication_and_origin_checks(self):
        client=self.proxy_client('127.0.0.1')
        forwarded={'X-Forwarded-For':'198.51.100.44','X-Forwarded-Proto':'https'}
        with patch.dict(os.environ,{'ZDM_GUI_ALLOWED_IPS':'*'}):
            self.assertEqual(client.get('/api/health',headers=forwarded).status_code,200)
            client.auth=None
            self.assertEqual(client.get('/api/health',headers=forwarded).status_code,401)
            client.auth=('admin','wrong-password')
            self.assertEqual(client.get('/api/health',headers=forwarded).status_code,401)
            client.auth=('admin','test-only-password')
            response=client.post('/api/responses',headers={**self.headers,**forwarded,'Origin':'https://evil.example'},json={'name':'bad-public-origin','content':'MIGRATION_METHOD=ONLINE_PHYSICAL\n'})
            self.assertEqual(response.status_code,403)

    def test_untrusted_peer_cannot_spoof_forwarded_identity(self):
        client=self.proxy_client('198.51.100.12')
        response=client.get('/api/health',headers={'X-Forwarded-For':'testclient','X-Forwarded-Proto':'https'})
        self.assertEqual(response.status_code,403,response.text)


if __name__=='__main__':unittest.main()
