"""Closed scalar export tests on synthetic receipts only; no runtime actions."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools import scalar_export as export
from test_stage_gates import expected, synthetic_receipt, browser_receipt


class ScalarExportTests(unittest.TestCase):
    def document(self,work):
        exp=expected()
        exp['owned_temp_roots']={s:str(work/s/'tmp') for s in ('qualification','timing')}
        q=synthetic_receipt('qualification',exp)
        t=synthetic_receipt('timing',exp,q['fixture_identities'])
        return {'status':'completed_pending_independent_terminal_audit','source_release':'a'*40,
            'first_failure':None,'phases':[], 'qualification':q,'timing':t,
            'provisioning':{'complete':True,'operator_calls':0,'browser_launches':0,
                'roots':{'private':'/secret/private-sdk'},'command_argv':['DO-NOT-EXPORT']},
            'build':{'complete':True,'compile_count':1,'asset_manifest':exp['asset_manifest'],
                'commands':[{'argv':['DO-NOT-EXPORT']}], 'operator_calls':0,'browser_launches':0}}, exp

    def test_complete_numerical_and_resource_records_survive_without_raw_identity_or_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            value=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertTrue(value['projection_complete'],value['projection_issue_locations'])
            for s,n in (('qualification',330),('timing',600)):
                self.assertEqual(len(value[s]['calls']),n)
                self.assertEqual(value[s]['calls'],final[s]['calls'])
                self.assertEqual(value[s]['resources'],final[s]['resources'])
                self.assertTrue(all(x['launch_policy_validated_before_omission'] for x in value[s]['public_launch_policy_checks']))
            raw=json.dumps(value)
            for forbidden in ('actual_launch_argv','--user-data-dir=','synthetic-job','synthetic-boot','/secret/','DO-NOT-EXPORT',str(work)):
                self.assertNotIn(forbidden,raw)
            self.assertIn('--js-flags=--no-wasm-revectorize',raw)
            self.assertFalse(value['performance_claim_authorized'])
            self.assertFalse(value['advancement_authorized'])

    def test_valid_partial_failure_keeps_failed_attempt_without_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['status']='failed';final.pop('timing');q=final['qualification'];q['status']='incomplete'
            q['calls']=q['calls'][:2];q['calls'][1]['checks']['readonly']=False
            q['calls'][1]['worker_failure']={'private_argv':['/private/browser','--profile=/private/path']}
            q['first_failure']={'check':'readonly_difference','known_calls':2,'no_retry':True}
            q['resources']=None;q['root_receipts'][0]['shutdown']=None
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertTrue(result['projection_complete'],result['projection_issue_locations'])
            self.assertEqual(len(result['qualification']['calls']),2)
            self.assertFalse(result['qualification']['calls'][1]['checks']['readonly'])
            self.assertTrue(result['qualification']['calls'][1]['worker_failure']['present'])
            self.assertIsNone(result['timing'])
            self.assertNotIn('/private/',json.dumps(result))

    def test_unlisted_tensor_field_is_quarantined_not_filtered_into_a_success(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['qualification']['calls'][0]['evidence']['raw_output']=[123456789]*3
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertFalse(result['projection_complete'])
            self.assertEqual(len(result['qualification']['calls']),330)
            self.assertEqual(result['qualification']['calls'][0]['projection_status'],'unexportable_scalar_field')
            self.assertNotIn('raw_output',json.dumps(result));self.assertNotIn('123456789',json.dumps(result))

    def test_numeric_array_in_known_scalar_field_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['status']='failed'
            final['qualification']['calls'][0]['evidence']['output']['words']=[123456789,987654321]
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertFalse(result['projection_complete'])
            self.assertEqual(len(result['qualification']['calls']),330)
            self.assertEqual(result['qualification']['calls'][0]['projection_status'],'unexportable_scalar_field')
            self.assertNotIn('123456789',json.dumps(result));self.assertNotIn('987654321',json.dumps(result))

    def test_context_misplaced_record_tensor_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['status']='failed'
            final['qualification']['calls'][0]['evidence']['output']['samples']=[{'words':123456789},{'words':987654321}]
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertFalse(result['projection_complete'])
            self.assertEqual(result['qualification']['calls'][0]['projection_status'],'unexportable_scalar_field')
            self.assertNotIn('123456789',json.dumps(result));self.assertNotIn('987654321',json.dumps(result))

    def test_context_numeric_regions_rejects_record_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['qualification']['calls'][0]['evidence']['readonly']['regions']=[{'region':1,'bytes':123456789,'minimum_alignment':64}]
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertFalse(result['projection_complete'])
            self.assertNotIn('123456789',json.dumps(result))

    def test_dynamic_map_rejects_globally_known_container_name(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['qualification']['calls'][0]['resource_windows'][0]['before']['processes']['samples']=[{'words':123456789}]
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertFalse(result['projection_complete'])
            self.assertEqual(result['qualification']['calls'][0]['projection_status'],'unexportable_scalar_field')
            self.assertNotIn('123456789',json.dumps(result))

    def test_acquisition_and_owned_extrema_are_public_without_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            policy=json.loads((ROOT/'launch/browser-acquisition-policy.json').read_text())
            acquired=browser_receipt(policy)
            final['provisioning']['browser_provisioning_receipt']=acquired
            note={'attempted':True,'acknowledged':True,'returncode':0,'stage':'compile','command_id':'link_Q',
                  'samples':[{'rss_bytes':42,'artifact_bytes':81,'host_available_bytes':999,'tree_cpu_seconds':1.5,'generations':2,'target_swap_growth_bytes':0,'host_swap_in_delta_bytes':0,'host_swap_out_delta_bytes':0}],
                  'min_available_disk_bytes':4096,'argv':['DO-NOT-EXPORT'],'cleanup':{'confirmed':True,'remaining_processes':0}}
            final['phase_supervision']=[note]
            final['build']['commands']=[note]
            final['provisioning']['outer_supervision']=note
            final['provisioning']['bootstrap']={'command_receipts':[{'id':'sdk-link','kind':'bootstrap','complete':True,'ownership':'inherits_outer_production_phase_no_new_session','argv':['DO-NOT-EXPORT']}]}
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertTrue(result['projection_complete'],result['projection_issue_locations'])
            evidence=result['provisioning']['browser_acquisition']
            self.assertEqual(evidence['installation'],acquired['installation'])
            self.assertEqual(evidence['archive'],acquired['archive'])
            self.assertEqual(evidence['private_receipt_canonical_sha256'],export._hash(acquired))
            self.assertNotIn('etag',evidence['head']);self.assertIn('etag_sha256',evidence['head'])
            self.assertEqual(result['build']['commands'][0]['sample_extrema']['rss_bytes'],{'minimum':42,'maximum':42})
            self.assertEqual(result['phase_supervision'][0]['min_available_disk_bytes'],4096)
            self.assertNotIn('DO-NOT-EXPORT',json.dumps(result))

    def test_atomic_two_file_roster_hashes_budget_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['export_deadline_monotonic']=time.monotonic()+60
            report=export.export_scalar_artifact(work,final,source_bindings=exp['bindings'])
            out=work/'public-scalars'
            self.assertEqual({p.name for p in out.iterdir()},{'receipt.json','manifest.json'})
            self.assertFalse((work/'.public-scalars.pending').exists())
            manifest=json.loads((out/'manifest.json').read_text());payload=(out/'receipt.json').read_bytes()
            self.assertEqual(manifest['files']['receipt.json'],{'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()})
            self.assertEqual(report['bytes'],sum(p.stat().st_size for p in out.iterdir()))
            self.assertLessEqual(report['bytes'],32*1024**2)
            with self.assertRaises(export.ProjectionError):export.export_scalar_artifact(work,final)

    def test_expired_or_nonfinite_deadline_publishes_nothing(self):
        for deadline in (time.monotonic()-1,float('nan')):
            with tempfile.TemporaryDirectory() as directory:
                work=Path(directory);final={'status':'failed','export_deadline_monotonic':deadline}
                with self.assertRaises(export.ProjectionError):export.export_scalar_artifact(work,final)
                self.assertFalse((work/'public-scalars').exists())

    def test_size_and_interrupted_write_leave_no_public_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)
            with mock.patch.object(export,'LIMIT',128):
                with self.assertRaises(export.ProjectionError):export.export_scalar_artifact(work,{'status':'failed'})
            self.assertFalse((work/'public-scalars').exists())
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)
            with mock.patch.object(Path,'rename',side_effect=OSError('synthetic rename failure')):
                with self.assertRaises(OSError):export.export_scalar_artifact(work,{'status':'failed'})
            self.assertFalse((work/'public-scalars').exists())
            self.assertTrue((work/'.public-scalars.pending/receipt.json').is_file())

    def test_changed_private_argv_digest_is_not_export_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);final,exp=self.document(work)
            final['qualification']['flag_probe']['launch_argv_sha256']='0'*64
            result=export.project_scalar_receipt(work,final,exp['bindings'])
            self.assertFalse(result['projection_complete'])
            self.assertFalse(result['qualification']['public_launch_policy_checks'][0]['launch_policy_validated_before_omission'])

    def test_early_failure_keeps_cleanup_and_hashes_private_failure_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            value={'status':'failed','first_failure':{'type':'ValueError','phase':'provision','check':'program_gate_failure'},
                   'attempted_phases':[{'attempted':True,'acknowledged':False,'returncode':1,
                       'first_failure':{'argv':['/private/compiler'],'message':'/private/log'},
                       'cleanup':{'confirmed':True,'remaining_processes':0,'returncode':1,
                                  'failure':None,'cleanup_failure':None,'timed_out':False}}]}
            result=export.project_scalar_receipt(Path(directory),value)
            self.assertTrue(result['projection_complete'],result['projection_issue_locations'])
            self.assertTrue(result['attempted_phase_outcomes'][0]['cleanup']['confirmed'])
            self.assertTrue(result['attempted_phase_outcomes'][0]['first_failure_present'])
            self.assertNotIn('/private/',json.dumps(result))
            self.assertIsNone(result['qualification']);self.assertIsNone(result['timing'])
