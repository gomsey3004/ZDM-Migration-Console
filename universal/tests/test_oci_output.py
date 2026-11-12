import json,subprocess,sys,unittest
from unittest.mock import patch
from backend import connections

class OciOutputTests(unittest.TestCase):
    def test_successful_json_excludes_stderr_warning(self):
        code,out=connections.command([sys.executable,'-c','import sys; print("Warning: OCI_API_KEY advisory",file=sys.stderr); print(\'{"state":"AVAILABLE"}\')'],json_output=True)
        self.assertEqual(code,0);self.assertEqual(json.loads(out),{'state':'AVAILABLE'});self.assertNotIn('Warning',out)
    def test_failure_preserves_redacted_diagnostic(self):
        code,out=connections.command([sys.executable,'-c','import sys; print("ServiceError: NotAuthorizedOrNotFound\\npassword=sample-secret",file=sys.stderr); sys.exit(1)'],json_output=True)
        self.assertEqual(code,1);self.assertIn('NotAuthorizedOrNotFound',out);self.assertNotIn('sample-secret',out)
    def test_non_json_empty_and_wrong_type_are_clear(self):
        for raw in ['', ' ', 'Warning: not JSON', '[]','null']:
            with self.subTest(raw=raw),self.assertRaises(ValueError) as exc:connections.oci_response(raw)
            self.assertIn('OCI CLI',str(exc.exception));self.assertNotIn('Expecting value',str(exc.exception))
    def test_successful_json_not_truncated_to_diagnostic_tail(self):
        code,out=connections.command([sys.executable,'-c','import json; print(json.dumps({"value":"x"*5000}))'],json_output=True)
        self.assertEqual(code,0);self.assertEqual(len(json.loads(out)['value']),5000)
    def test_default_ssh_diagnostics_still_include_stderr(self):
        code,out=connections.command([sys.executable,'-c','import sys; print("ssh failure",file=sys.stderr); sys.exit(1)'])
        self.assertEqual(code,1);self.assertIn('ssh failure',out)
    def test_oci_probes_request_json_and_separate_stderr(self):
        profile={'config_file':'fixture','profile_name':'PHX','region':'us-phoenix-1','tenancy':'fixture'}
        ocid='ocid1.autonomousdatabase.oc1.phx.exampledatabase'
        for check,output in [(lambda:connections.check_autonomous(profile,ocid),{'id':ocid,'state':'AVAILABLE'}),(lambda:connections.check_oci(profile),{'data':[{'name':'AD1'}]})]:
            with patch.object(connections,'command',return_value=(0,json.dumps(output))) as cmd:
                self.assertTrue(check()['ok']);self.assertTrue(cmd.call_args.kwargs['json_output']);args=cmd.call_args.args[0];self.assertEqual(args[args.index('--output')+1],'json')
    def test_api_failure_is_preserved_in_autonomous_report(self):
        profile={'config_file':'fixture','profile_name':'PHX','region':'us-phoenix-1'}
        with patch.object(connections,'command',return_value=(1,'ServiceError: NotAuthorizedOrNotFound')):
            result=connections.check_autonomous(profile,'ocid1.autonomousdatabase.oc1.phx.exampledatabase')
        self.assertFalse(result['ok']);self.assertIn('NotAuthorizedOrNotFound',result['steps'][0]['detail'])

if __name__=='__main__':unittest.main()
