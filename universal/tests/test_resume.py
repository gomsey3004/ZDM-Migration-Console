import json,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from backend import resume
import test_console as baseline

class ResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):baseline.ApiTests.setUpClass.__func__(cls)
    @classmethod
    def tearDownClass(cls):baseline.ApiTests.tearDownClass.__func__(cls)
    post=baseline.ApiTests.post
    def fixture(self,kind='logical',status='FAILED',job_type='MIGRATE'):
        p=self.stage/(uuid.uuid4().hex+' with spaces.rsp');p.write_text('# untouched\nMIGRATION_METHOD='+('ONLINE_LOGICAL' if kind=='logical' else 'ONLINE_PHYSICAL')+'\n')
        name='ZDM_DATAPUMP_IMPORT_TGT' if kind=='logical' else 'ZDM_CONFIGURE_DG_SRC'
        return {'jobs':[{'id':72,'type':job_type,'status':status,'phases':[{'name':name,'status':'FAILED'}]}],'raw':f'Job ID: 72\nScheduled job command: "zdmcli migrate database -rsp \'{p}\'"\n'},p
    def preview(self,options):return self.post('/api/jobs/72/resume/preview',{'resume_options':options})
    def action(self,options=None,revision='',**changes):return self.post('/api/jobs/72/actions',{'action':'resume','confirmation':'RESUME 72','request_key':uuid.uuid4().hex,'resume_options':options or {},'resume_revision':revision,**changes})
    def test_schema_filters_logical_and_physical_and_rsp_choices(self):
        for kind,fields,excluded in [('logical',{'pauseafter','rsp','ignore'},{'rerun','skip'}),('physical',{'pauseafter','rerun','skip'},{'rsp','ignore'})]:
            data,p=self.fixture(kind)
            with patch.object(self.module,'query_jobs',return_value=data):r=self.client.get('/api/jobs/72/resume-options')
            self.assertEqual(r.status_code,200,r.text);s=r.json();self.assertEqual(s['migration_kind'],kind);self.assertEqual(set(s['fields']),fields);self.assertFalse(set(s['fields'])&excluded)
            if kind=='logical':self.assertIn(str(p.resolve()),[f['path'] for f in s['responses']])
    def test_method_conflict_and_unknown_fail_closed_for_specific_options(self):
        data,p=self.fixture();p.write_text('MIGRATION_METHOD=ONLINE_PHYSICAL\n');s=resume.schema(data);self.assertEqual(s['migration_kind'],'unknown');self.assertTrue(s['method_conflict']);self.assertEqual(s['fields'],['pauseafter'])
        for opts in [resume.Options(ignore='IMPORT_ERRORS'),resume.Options(rerun='BACKUP')]:
            with self.assertRaises(ValueError):resume.build(data,opts)
        data['raw']='';data['jobs'][0]['phases']=[];self.assertEqual(resume.schema(data)['fields'],['pauseafter']);self.assertEqual(resume.build(data,resume.Options())[0],['resume','job','-jobid','72'])
    def test_logical_preview_and_submit_use_exact_reviewed_command(self):
        data,p=self.fixture();options={'rsp':str(p),'ignore':'IMPORT_ERRORS'}
        with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue',return_value={'id':'mock-resume'}) as queue:
            r=self.preview(options);self.assertEqual(r.status_code,200,r.text);queue.assert_not_called();self.assertIn('-ignore IMPORT_ERRORS',r.json()['command'])
            r2=self.action(options,r.json()['revision']);self.assertEqual(r2.status_code,202,r2.text)
            self.assertEqual(queue.call_args.args[2],['resume','job','-jobid','72','-rsp',str(p.resolve()),'-ignore','IMPORT_ERRORS']);self.assertEqual(queue.call_args.kwargs,{'job_id':72})
        self.assertIn('# untouched',p.read_text())
    def test_cross_method_options_rejected_even_if_client_forces_them(self):
        for kind,options in [('logical',{'rerun':'BACKUP'}),('logical',{'skip':'DG_CONFIG'}),('physical',{'ignore':'IMPORT_ERRORS'}),('physical',{'rsp':str(self.rsp)})]:
            data,p=self.fixture(kind)
            with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue') as queue:
                self.assertEqual(self.preview(options).status_code,422);self.assertEqual(self.action(options).status_code,422);queue.assert_not_called()
    def test_physical_options_and_restore_acknowledgement(self):
        data,p=self.fixture('physical')
        with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue',return_value={'id':'mock'}) as queue:
            self.assertEqual(self.preview({'rerun':'RESTORE'}).status_code,422)
            for rerun in ['BACKUP','RESTORE']:
                options={'rerun':rerun,'skip':'DG_CONFIG_STBY','restore_ack':rerun=='RESTORE'}
                r=self.preview(options);self.assertEqual(r.status_code,200,r.text);a=self.action(options,r.json()['revision']);self.assertEqual(a.status_code,202,a.text)
                self.assertEqual(queue.call_args.args[2],['resume','job','-jobid','72','-rerun',rerun,'-skip','DG_CONFIG_STBY'])
    def test_all_documented_physical_skip_choices(self):
        data,p=self.fixture('physical')
        for value in ['SWITCHOVER','UPGRADE','DG_CONFIG','DG_CONFIG_STBY']:self.assertIn(value,resume.build(data,resume.Options(skip=value))[0])
    def test_pause_common_rsp_exclusion_and_phase_injection(self):
        for kind in ['logical','physical']:
            data,p=self.fixture(kind);args,rev=resume.build(data,resume.Options(pauseafter='ZDM_FINALIZE_TGT'));self.assertEqual(args[-2:],['-pauseafter','ZDM_FINALIZE_TGT'])
        data,p=self.fixture()
        with patch.object(self.module,'query_jobs',return_value=data):
            self.assertEqual(self.preview({'rsp':str(p),'pauseafter':'ZDM_FINALIZE_TGT'}).status_code,422)
            for phase in ['-eval','ZDM_FINAL;id','ZDM_X\nBAD']:self.assertEqual(self.preview({'pauseafter':phase}).status_code,422)
    def test_updated_rsp_requires_logical_safe_configured_file(self):
        data,p=self.fixture();outside=self.root/'outside.rsp';outside.write_text('MIGRATION_METHOD=ONLINE_LOGICAL\n')
        template=self.stage/'templates'/'logical.rsp';template.parent.mkdir(exist_ok=True);template.write_text(p.read_text())
        for candidate in [outside,self.rsp,template]:
            with self.assertRaises(ValueError):resume.build(data,resume.Options(rsp=str(candidate)))
    def test_updated_rsp_or_original_method_changed_invalidates_review(self):
        data,p=self.fixture();options={'rsp':str(p)}
        with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue') as queue:
            r=self.preview(options);p.write_text(p.read_text()+'CUSTOM_OPTION=changed\n');self.assertEqual(self.action(options,r.json()['revision']).status_code,409);queue.assert_not_called()
    def test_missing_review_revision_and_option_changes_are_rejected(self):
        data,p=self.fixture()
        with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue') as queue:
            self.assertEqual(self.action({'ignore':'IMPORT_ERRORS'}).status_code,409)
            r=self.preview({'ignore':'IMPORT_ERRORS'});self.assertEqual(self.action({'ignore':'EXPORT_ERRORS'},r.json()['revision']).status_code,409);queue.assert_not_called()
    def test_failed_only_ignore_and_evaluation_jobs_cannot_resume(self):
        for status,typ in [('FAILED','EVAL'),('ABORTED','MIGRATE'),('SUCCEEDED','MIGRATE'),('EXECUTING','MIGRATE')]:
            data,p=self.fixture(status=status,job_type=typ);self.assertFalse(resume.schema(data)['can_resume'])
            with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue') as queue:self.assertIn(self.action().status_code,[409,422]);queue.assert_not_called()
        data,p=self.fixture(status='PAUSED')
        with self.assertRaises(ValueError):resume.build(data,resume.Options(ignore='IMPORT_ERRORS'))
    def test_plain_resume_backward_compatible_and_other_actions_reject_resume_options(self):
        data,p=self.fixture()
        with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue',return_value={'id':'mock'}) as queue:
            self.assertEqual(self.action().status_code,202);self.assertEqual(queue.call_args.args[2],['resume','job','-jobid','72']);queue.reset_mock()
            self.assertEqual(self.action({'ignore':'IMPORT_ERRORS'},action='abort',confirmation='ABORT 72').status_code,422);queue.assert_not_called()
    def test_confirmation_fresh_state_and_extra_keys_still_guard_resume(self):
        data,p=self.fixture()
        with patch.object(self.module,'query_jobs',return_value=data),patch.object(self.module,'enqueue') as queue:
            self.assertEqual(self.action(confirmation='wrong').status_code,422)
            for options in [{'migration_kind':'physical'},{'skip':'ALL'},{'rerun':'SHELL'},{'restore_ack':'yes'}]:self.assertEqual(self.preview(options).status_code,422)
            r=self.preview({});data['jobs'][0]['status']='EXECUTING';self.assertEqual(self.action(revision=r.json()['revision']).status_code,409);queue.assert_not_called()

if __name__=='__main__':unittest.main()
