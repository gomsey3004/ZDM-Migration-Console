import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch

from backend import host_records,connections
import test_console as baseline

ORIGINAL='# Existing system entries\n127.0.0.1 localhost\n10.1.1.62 zdm.example.com zdm\n'
ENTRIES=[{'ip':'172.17.100.219','names':['source1.example.com','source1']},
         {'ip':'10.8.0.12','names':['target1.example.com','target1']}]


class HostsFileTests(unittest.TestCase):
    def test_managed_block_preserves_unmanaged_lines_and_removes_only_owned_block(self):
        rendered=host_records.render(ORIGINAL,ENTRIES)
        self.assertTrue(rendered.startswith(ORIGINAL))
        self.assertEqual(host_records.snapshot(rendered)['entries'],ENTRIES)
        self.assertEqual(host_records.render(rendered,[]),ORIGINAL)
        suffix='# Administrator suffix\n10.9.9.9 other-host\n'
        self.assertEqual(host_records.render(rendered+suffix,ENTRIES),rendered+suffix)

    def test_reject_invalid_ips_names_duplicates_and_system_overrides(self):
        invalid=[{'ip':'not-an-ip','names':['node1']},{'ip':'127.0.0.1','names':['node1']},
                 {'ip':'0.0.0.0','names':['node1']},{'ip':'10.0.0.1','names':['node1\nmalicious']},
                 {'ip':'10.0.0.1','names':['localhost']},{'ip':'10.0.0.1','names':['zdm']},
                 {'ip':'10.0.0.1','names':['node1','node1']},{'ip':'10.0.0.1','names':['-bad']}]
        for row in invalid:
            with self.assertRaises(ValueError,msg=str(row)):host_records.render(ORIGINAL,[row])
        with self.assertRaises(ValueError):host_records.render(ORIGINAL,[ENTRIES[0],ENTRIES[0]])
        with self.assertRaises(ValueError):host_records.sections(ORIGINAL+host_records.BEGIN+'\n')

    def test_atomic_apply_backup_conflict_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts=Path(tmp)/'hosts';hosts.write_text(ORIGINAL);hosts.chmod(0o644)
            state=Path(tmp)/'backups'
            with patch.object(host_records,'HOSTS',hosts),patch.object(host_records,'STATE',state):
                revision=hashlib.sha256(hosts.read_bytes()).hexdigest()
                result=host_records.apply(ENTRIES,revision)
                self.assertTrue(result['ok']);self.assertTrue(result['changed'])
                self.assertEqual(Path(result['backup']).read_text(),ORIGINAL)
                self.assertEqual(Path(result['backup']).stat().st_mode&0o777,0o600)
                self.assertEqual(hosts.stat().st_mode&0o777,0o644)
                self.assertTrue(host_records.apply(ENTRIES,revision)['conflict'])
                self.assertTrue(host_records.apply([],result['revision'])['ok'])
                self.assertEqual(hosts.read_text(),ORIGINAL)

    def test_helper_accepts_no_arbitrary_paths_or_commands(self):
        for body in ({'op':'shell','command':'id'}, {'op':'apply','entries':[],'revision':'0'*64,'path':'/etc/passwd'}):
            with patch.object(host_records,'apply') as apply:
                with self.assertRaises(ValueError):host_records.dispatch(body)
                apply.assert_not_called()


class NameResolutionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline.ApiTests.setUpClass.__func__(cls)
        cls.hosts=cls.root/'hosts';cls.hosts.write_text(ORIGINAL)
        cls.patches.append(patch.object(host_records,'HOSTS',cls.hosts));cls.patches[-1].start()
        cls.patches.append(patch.object(host_records,'SOCKET',cls.root/'helper.sock'));cls.patches[-1].start()
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post

    def test_read_hosts_has_separate_protected_and_managed_entries(self):
        result=self.client.get('/api/name-resolution')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['entries'],[])
        self.assertEqual(result.json()['protected_entries'][0]['names'],['localhost'])
        self.assertFalse(result.json()['helper_available'])

    def test_system_and_dns_checks_are_distinct_and_bounded(self):
        def command(args,timeout):
            self.assertEqual(timeout,5)
            if args[-1]=='missing-node':return 2,''
            return (2,'') if '-s' in args else (0,'10.1.1.62 STREAM zdm\n10.1.1.62 DGRAM zdm\n')
        with patch.object(connections,'command',side_effect=command):
            result=self.post('/api/name-resolution/check',{'names':['zdm','missing-node']})
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(result.json()['ok'])
        good,bad=result.json()['results']
        self.assertTrue(good['ok']);self.assertEqual(good['source'],'/etc/hosts')
        self.assertEqual(good['dns_addresses'],[])
        self.assertFalse(bad['ok'])
        self.assertEqual(self.post('/api/name-resolution/check',{'names':['node;id']}).status_code,422)

    def test_save_validates_payload_and_requires_helper(self):
        snap=self.client.get('/api/name-resolution').json()
        body={'revision':snap['revision'],'entries':ENTRIES}
        result=self.client.put('/api/name-resolution',json=body,headers=self.headers)
        self.assertEqual(result.status_code,503,result.text)
        result=self.client.put('/api/name-resolution',json={**body,'entries':[{'ip':'10.0.0.1','names':['localhost']}]},headers=self.headers)
        self.assertEqual(result.status_code,422)
        self.assertEqual(self.hosts.read_text(),ORIGINAL)

    def test_user_install_is_read_only_even_when_a_helper_exists(self):
        from fastapi.testclient import TestClient
        client=TestClient(self.module.app,client=('127.0.0.1',12345))
        with self.module.db() as db:
            username,previous=db.execute('select username,must_change_password from console_users where role=\'admin\' limit 1').fetchone()
            db.execute('update console_users set must_change_password=0 where username=?',(username,))
        client.auth=(username,'test-only-password')
        host_records.SOCKET.touch()
        try:
            with patch.dict(os.environ,{'ZDM_GUI_HOSTS_READ_ONLY':'1','ZDM_GUI_ALLOWED_IPS':'127.0.0.1/32'}):
                snapshot=client.get('/api/name-resolution').json()
                self.assertTrue(snapshot['read_only'])
                self.assertFalse(snapshot['helper_available'])
                response=client.put('/api/name-resolution',headers=self.headers,
                                    json={'revision':snapshot['revision'],'entries':ENTRIES})
                self.assertEqual(response.status_code,403,response.text)
                self.assertEqual(self.hosts.read_text(),ORIGINAL)
        finally:
            host_records.SOCKET.unlink(missing_ok=True)
            client.close()
            with self.module.db() as db:
                db.execute('update console_users set must_change_password=? where username=?',(previous,username))

    def test_save_uses_fixed_socket_protocol_and_reports_conflicts(self):
        snap=self.client.get('/api/name-resolution').json()
        body={'revision':snap['revision'],'entries':ENTRIES}
        connection=MagicMock();connection.__enter__.return_value=connection
        connection.recv.return_value=b'{"ok":false,"conflict":true,"message":"Reload saved entries."}\n'
        with patch.object(self.module.socket,'socket',return_value=connection):
            result=self.client.put('/api/name-resolution',json=body,headers=self.headers)
        self.assertEqual(result.status_code,409,result.text)
        connection.connect.assert_called_once_with(str(host_records.SOCKET))
        payload=json.loads(connection.sendall.call_args.args[0])
        self.assertEqual(set(payload),{'op','revision','entries'})
        self.assertEqual(payload['op'],'apply')

    def test_successful_save_returns_updated_entries_and_backup(self):
        snap=self.client.get('/api/name-resolution').json()
        connection=MagicMock();connection.__enter__.return_value=connection
        def reply(size):
            payload=json.loads(connection.sendall.call_args.args[0])
            return json.dumps(host_records.dispatch(payload)).encode()+b'\n'
        connection.recv.side_effect=reply
        try:
            with patch.object(host_records,'STATE',self.root/'backups'),patch.object(self.module.socket,'socket',return_value=connection):
                result=self.client.put('/api/name-resolution',json={'revision':snap['revision'],'entries':ENTRIES},headers=self.headers)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['entries'],ENTRIES)
            self.assertEqual(Path(result.json()['backup']).read_text(),ORIGINAL)
        finally:self.hosts.write_text(ORIGINAL)

    def test_hosts_changes_invalidate_profile_signature(self):
        before=self.module.profile_signature(self.profile)
        try:
            self.hosts.write_text(host_records.render(ORIGINAL,ENTRIES))
            self.assertNotEqual(self.module.profile_signature(self.profile),before)
        finally:self.hosts.write_text(ORIGINAL)


if __name__=='__main__':unittest.main()
