"""Pure source/mock failure boundaries; no acquisition, process or payload."""
import ast
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools import failure_diagnostics as diag,scalar_export,program,build_driver
from launch import provisioning,bootstrap_fetch,source_only

class HTTPError(OSError):
    def __init__(self,code):
        super().__init__('https://private.example/token=credential-not-for-export')
        self.code=code

class FailureDiagnostics(unittest.TestCase):
    def test_closed_exception_codes_never_echo_arbitrary_text_or_args(self):
        for error in (RuntimeError('credential-not-for-export'),OSError(13,'/private/path'),HTTPError(403),ValueError('bootstrap_source_closure_pin:/private/path')):
            row=diag.diagnose(error,'provision','acquisition','runtime-archive')
            diag.validate(row)
            self.assertNotIn('/private',json.dumps(row));self.assertNotIn('credential-not-for-export',json.dumps(row))
        self.assertEqual(diag.diagnose(HTTPError(403),'provision')['http_status'],403)
        self.assertEqual(diag.diagnose(OSError(13,'/private'),'provision')['errno'],13)
        self.assertIsNone(diag.diagnose(HTTPError(True),'provision')['http_status'])
        self.assertEqual(diag.diagnose(ValueError('bootstrap_source_closure_pin:/private'),'provision')['check'],'bootstrap_source_closure_pin')

    def test_actual_provision_source_reaches_first_acquisition_and_extraction_with_mocks(self):
        inputs=json.loads((ROOT/'launch/bootstrap-inputs.json').read_text());helper=json.loads((ROOT/'launch/bootstrap-helper-lock.json').read_text())
        first=next(row for row in inputs['downloads'] if row['id']!='wheel-psutil')
        for failure_at in ('acquisition','extraction'):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'provisioned';root.mkdir();(root/'archives').mkdir();(root/'deps/python').mkdir(parents=True)
                (root/'bootstrap-owner.json').write_text(json.dumps({'schema':'panel-owned-bootstrap-root-v1','source':str(ROOT)}))
                (root/'bootstrap-psutil-receipt.json').write_text('{"complete":true}')
                ctx={'provision_root':str(root),'roots':{'SOURCE':str(ROOT),'PYTHON':sys.executable},'bootstrap_helper_lock':helper,'host_tools':{'make':{'path':'/usr/bin/make','bytes':1,'sha256':'a'*64}}}
                def record(path,deadline):
                    if Path(path)==Path('/usr/bin/make'):return {'bytes':1,'sha256':'a'*64}
                    data=Path(path).read_bytes();return {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                acquire=mock.Mock(side_effect=HTTPError(403) if failure_at=='acquisition' else None,return_value={'id':first['id']})
                extract=mock.Mock(side_effect=ValueError('bootstrap_member_path'))
                with mock.patch.object(provisioning,'require_execution_admission',return_value={}),mock.patch.object(provisioning,'_check'),mock.patch.object(provisioning,'_journal'),mock.patch.object(bootstrap_fetch,'file_record',side_effect=record),mock.patch.object(bootstrap_fetch,'acquire',acquire),mock.patch.object(bootstrap_fetch,'extract',extract):
                    with self.assertRaises((HTTPError,ValueError)) as caught:provisioning.run(ctx,time.monotonic()+120)
                self.assertEqual(acquire.call_count,1);self.assertEqual(extract.call_count,int(failure_at=='extraction'))
                d=caught.exception.panel_failure_diagnostic
                self.assertEqual(d['operation'],failure_at);self.assertEqual(d['operation_id'],first['id'])
                self.assertEqual(caught.exception.panel_known_operations,[])
                self.assertFalse((root/'sdk').exists())

    def test_inner_command_identity_survives_without_argv(self):
        def failed(context,deadline,journal,progress):
            journal.append({'id':'configure-offline','attempted':True,'acknowledged':False,'complete':False,'returncode':1,'argv':['SECRET_RAW_ARGV'],'cwd':'/private'})
            raise RuntimeError('bootstrap_command_failed:configure-offline')
        with mock.patch.object(provisioning,'require_execution_admission',return_value={}),mock.patch.object(provisioning,'_run',side_effect=failed):
            with self.assertRaises(RuntimeError) as caught:provisioning.run({},123)
        receipt=diag.phase_failure('provision',caught.exception,[])
        self.assertEqual(receipt['failure_diagnostic']['operation_id'],'configure-offline')
        self.assertEqual(receipt['failure_diagnostic']['check'],'bootstrap_command_failed')
        self.assertEqual(receipt['known_operations'][0]['acknowledged'],False)
        self.assertIsNone(receipt['operator_calls']);self.assertIsNone(receipt['browser_launches'])
        self.assertNotIn('SECRET_RAW_ARGV',json.dumps(receipt));self.assertNotIn('/private',json.dumps(receipt))

    def test_mock_child_failure_crosses_parent_export_and_log(self):
        error=HTTPError(403);error.panel_failure_diagnostic=diag.diagnose(error,'provision','acquisition',diag.POLICY['operation_ids'][0]);error.panel_known_operations=[]
        child=diag.phase_failure('provision',error,[{'id':'configure-offline','attempted':True,'acknowledged':False,'argv':['SECRET_RAW_ARGV']}])
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);journal=[];captured=io.StringIO()
            ctx={'roots':{'SOURCE':str(ROOT),'PYTHON':sys.executable},'phase_environment':{},'bootstrap_helper_lock':{},'runtime_helper_package_roots':{}}
            def supervisor(command,cwd,env,deadline,output_root,limits,notes):
                (Path(cwd)/'receipt.private.json').write_text(json.dumps(child))
                outer=build_driver.BuildGateRejected('command_failed_or_cleanup_unknown')
                notes.append({'attempted':True,'acknowledged':False,'first_failure':'BuildGateRejected','failure_diagnostic':diag.diagnose(outer,'provision','owned_command'),
                  'cleanup':{'confirmed':True,'remaining_processes':0,'returncode':1,'failure':'stopped','cleanup_failure':None,'timed_out':False}})
                raise outer
            with mock.patch.object(program,'require_execution_admission',return_value={}),mock.patch.object(source_only,'set_phase'),mock.patch.object(provisioning,'allowed_monitor_links',return_value={}),mock.patch.object(program,'phase_command',return_value=['mock-child']),mock.patch.object(program,'supervise_command',side_effect=supervisor),contextlib.redirect_stdout(captured):
                returned=program._phase('provision',ctx,time.monotonic()+120,work,journal)
            public=scalar_export.project_scalar_receipt(work,{'status':'failed','provisioning':returned,'phase_supervision':journal})
            self.assertTrue(public['projection_complete'],public['projection_issue_locations'])
            self.assertEqual(public['provisioning']['first_failure']['type'],'HTTPError')
            self.assertEqual(public['provisioning']['failure_diagnostic']['http_status'],403)
            self.assertEqual(public['provisioning']['outer_failure_diagnostic']['check'],'command_failed_or_cleanup_unknown')
            self.assertEqual(public['provisioning']['known_commands_count'],1)
            self.assertIsNone(public['provisioning']['operator_calls']);self.assertIsNone(public['qualification']);self.assertIsNone(public['timing'])
            for output in (captured.getvalue(),json.dumps(public)):
                for forbidden in ('SECRET_RAW_ARGV','credential-not-for-export','private.example','/private'):self.assertNotIn(forbidden,output)
            self.assertIn('PANEL_FAILURE_DIAGNOSTIC',captured.getvalue())
            self.assertFalse((work/'compile').exists());self.assertFalse((work/'timing').exists())

    def test_outer_gate_reason_survives_without_child_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);journal=[];captured=io.StringIO()
            ctx={'roots':{'SOURCE':str(ROOT),'PYTHON':sys.executable},'phase_environment':{},'bootstrap_helper_lock':{},'runtime_helper_package_roots':{}}
            def fail(*args):
                error=build_driver.BuildGateRejected('command_missing_observation')
                journal.append({'failure_diagnostic':diag.diagnose(error,'provision','owned_command'),'attempted':True,'acknowledged':False})
                raise error
            with mock.patch.object(program,'require_execution_admission',return_value={}),mock.patch.object(source_only,'set_phase'),mock.patch.object(provisioning,'allowed_monitor_links',return_value={}),mock.patch.object(program,'phase_command',return_value=['mock-child']),mock.patch.object(program,'supervise_command',side_effect=fail),contextlib.redirect_stdout(captured):
                with self.assertRaises(build_driver.BuildGateRejected):program._phase('provision',ctx,time.monotonic()+120,work,journal)
            self.assertIn('command_missing_observation',captured.getvalue())
            public=scalar_export.project_scalar_receipt(work,{'status':'failed','phase_supervision':journal})
            self.assertEqual(public['phase_supervision'][0]['failure_diagnostic']['check'],'command_missing_observation')
            self.assertNotIn('provisioning',public)
            self.assertIsNone(public['qualification']);self.assertIsNone(public['timing'])

    def test_bad_diagnostic_cannot_publish_unknown_tokens_or_record_tensors(self):
        d=diag.diagnose(RuntimeError('unknown'),'provision');d['operation_id']='credential-not-for-export'
        public=scalar_export.project_scalar_receipt(Path('/synthetic'),{'status':'failed','provisioning':{'complete':False,'failure_diagnostic':d,'known_commands':[{'id':'configure-offline','argv':['SECRET_RAW_ARGV'],'returncode':[123456789]}]}})
        self.assertFalse(public['projection_complete'])
        self.assertNotIn('credential-not-for-export',json.dumps(public));self.assertNotIn('123456789',json.dumps(public));self.assertNotIn('SECRET_RAW_ARGV',json.dumps(public))

    def test_diagnostic_boolean_and_integer_replacements_are_quarantined(self):
        for name in ('failure_diagnostic','outer_failure_diagnostic'):
            for malformed in (True,123):
                public=scalar_export.project_scalar_receipt(Path('/synthetic'),{'status':'failed','provisioning':{'complete':False,name:malformed}})
                self.assertFalse(public['projection_complete'])
                self.assertEqual(public['provisioning'][name]['projection_status'],'unexportable_scalar_field')

    def test_diagnostic_failure_cannot_skip_owned_cleanup_or_replace_first_error(self):
        from types import SimpleNamespace,ModuleType
        import tools as tool_package
        from browser import vendor as vendor_package
        psutil=ModuleType('psutil');psutil.swap_memory=lambda:SimpleNamespace(sin=0,sout=0)
        psutil.virtual_memory=lambda:SimpleNamespace(available=8*1024**3)
        guard=ModuleType('tools.process_guard');guard.assert_no_unresolved_owners=lambda:None
        owner=SimpleNamespace(receipt={},closed=0,released=0)
        owner.wait=lambda **kwargs:{'confirmed':True,'remaining_processes':0,'returncode':1,'failure':'stopped'}
        def close(**kwargs):owner.closed+=1;return {'confirmed':True,'remaining_processes':0,'returncode':1,'failure':'stopped'}
        def release():owner.released+=1
        owner.close=close;owner.release_streams=release;guard.OwnedProcess=lambda *args,**kwargs:owner
        strict=ModuleType('browser.vendor.strict_resources')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);journal=[];limits={'bootstrap_helper_lock':{},'runtime_helper_package_roots':{},'wall_seconds':60,'per_process_cpu_seconds':30,'stage':'provision'}
            with mock.patch.dict(sys.modules,{'psutil':psutil,'tools.process_guard':guard,'browser.vendor.strict_resources':strict}),mock.patch.object(tool_package,'process_guard',guard,create=True),mock.patch.object(vendor_package,'strict_resources',strict,create=True),mock.patch.object(build_driver,'require_execution_admission',return_value={}),mock.patch.object(build_driver,'verify_runtime_helpers'),mock.patch.object(build_driver,'verify_helper_import_routes'),mock.patch.object(diag,'diagnose',side_effect=MemoryError('private allocation detail')):
                with self.assertRaisesRegex(build_driver.BuildGateRejected,'command_failed_or_cleanup_unknown'):
                    build_driver.supervise_command(['mock-command'],root,{},time.monotonic()+120,root,limits,journal)
            self.assertEqual(owner.closed,1);self.assertEqual(owner.released,1)
            self.assertTrue(journal[0]['cleanup']['confirmed']);self.assertTrue(journal[0]['diagnostic_unavailable'])

    def test_initial_bootstrap_failure_preserves_only_whitelisted_class_string(self):
        base={'complete':False,'first_failure':'HTTPError','operator_calls':0,'browser_launches':0}
        public=scalar_export.project_scalar_receipt(Path('/synthetic'),{'status':'failed','initial_ownership_bootstrap':base})
        self.assertTrue(public['projection_complete'],public['projection_issue_locations'])
        self.assertEqual(public['initial_ownership_bootstrap']['first_failure'],'HTTPError')
        for malformed in ('credential-not-for-export',True,123,{'type':'HTTPError','check':'unclassified_exception'}):
            bad=dict(base,first_failure=malformed)
            public=scalar_export.project_scalar_receipt(Path('/synthetic'),{'status':'failed','initial_ownership_bootstrap':bad})
            self.assertFalse(public['projection_complete'])
            self.assertEqual(public['initial_ownership_bootstrap']['first_failure'],{'projection_status':'invalid_failure_diagnostic'})
            self.assertNotIn('credential-not-for-export',json.dumps(public))

    def test_recipe_assets_science_and_bounds_unchanged(self):
        old=ROOT.parent/'chrome-panel-order-actions-activation-v1'
        for name in ('launch/bootstrap-inputs.json','launch/build-recipe.json','launch/expected-assets.json','launch/provisioning-contract.json','tools/transition.py','tools/statistics.py','protocol/schedule.json','protocol/qualification-fixtures.json','browser/host/controller.js','browser/host/worker.js'):
            self.assertEqual((ROOT/name).read_bytes(),(old/name).read_bytes(),name)
        self.assertEqual(program.PHASES,(('provision',2700),('compile',600),('qualification',900),('transition',30),('timing',1800),('finalization',60)))
        tree=ast.parse((ROOT/'launch/phase_process.py').read_text())
        self.assertIn('phase_failure(a.stage, error, journal)',ast.unparse(tree))
