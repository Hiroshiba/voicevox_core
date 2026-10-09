"""Pure tests. Synthetic timing fixtures are not measurements or evidence."""
import copy,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'browser'))
import timing_validate as tv
from release_gate import load_contract,release_gate
from test_browser import fixture

def timing_fixture():
    r=fixture();r.pop('native_gate');r['schema']='inplace-leaky-on-reviewed-timing-v1'
    r['release_gate']={'synthetic':True};r['private_native_dump_retained']=False
    return r

class ReleaseTests(unittest.TestCase):

    def test_diagnostic_is_closed_and_boolean_only(self):
        from release_gate import release_diagnostic
        c=load_contract();p=copy.deepcopy(c['identities'])
        p['original']['wasm_sha256']='PRIVATE_SENTINEL'
        p['private_audio']={'samples':[1,2,3]}
        d=release_diagnostic(p,{'product':'PRIVATE_SENTINEL'}, {'mode':'PRIVATE_SENTINEL'})
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(d));self.assertNotIn('samples',json.dumps(d))
        self.assertFalse(d['conditions_match']);self.assertFalse(d['identity_fields']['original']['wasm_sha256'])
        self.assertFalse(d['engine_equal']);self.assertFalse(d['reference_equal'])
        with self.assertRaises(ValueError):release_gate(p,c['engine'],c['reference'])

    def test_early_identity_diagnostic_precedes_browser(self):
        s=(ROOT/'timing_confirmation.py').read_text()
        self.assertLess(s.index('identity_diagnostic ='),s.index("stage('activation_gates')"))
        self.assertIn("all(all(fields.values()) for fields in identity_diagnostic['identity_fields'].values())",s)

    def test_frozen_release(self):
        c=load_contract();self.assertEqual(c['primary_calls'],27)
        g=release_gate(c['identities'],c['engine'],c['reference'])
        self.assertFalse(g['uninstrumented_code_identity_claimed'])
        for mode in c['conditions']:
            for field in c['identities'][mode]:
                p=copy.deepcopy(c['identities']);p[mode][field]='changed'
                with self.assertRaises(ValueError): release_gate(p,c['engine'],c['reference'])
        with self.assertRaises(ValueError):release_gate(c['identities'],{},c['reference'])
        with self.assertRaises(ValueError):release_gate(c['identities'],c['engine'],{})
    def test_complete_synthetic_schedule(self):
        with patch.object(tv,'release_gate',return_value={'synthetic':True}):
            r=timing_fixture();self.assertTrue(tv.validate(r));s=tv.summarize(r)
            self.assertEqual(s['primary_calls'],27);self.assertEqual(s['observations_per_condition'],9)
            self.assertEqual(len(s['paired_effects']['candidate_vs_original']['pairs']),9)
    def test_mutations_fail(self):
        mutations=[lambda r:r['trials'].pop(),lambda r:r['process_sets'].pop(),lambda r:r.update(gates_complete_before_timing=False),lambda r:r.update(release_gate={}),lambda r:r.update(private_native_dump_retained=True),lambda r:r['browser_flags'].append('--perf-prof'),lambda r:r['process_sets'][0]['runtime']['original']['launch_flags'].append('--trace-baseline'),lambda r:r['trials'][0].update(elapsed_s=float('nan'))]
        for f in mutations:
            r=timing_fixture();f(r)
            with patch.object(tv,'release_gate',return_value={'synthetic':True}):
                with self.assertRaises((ValueError,RuntimeError,AssertionError,KeyError)):tv.validate(r)
    def test_primary_source_no_profiler_or_native_capture(self):
        s=(ROOT/'timing_confirmation.py').read_text()
        self.assertNotIn('run_native_gates',s);self.assertNotIn('--perf-prof',s)
        self.assertNotIn('--trace-baseline',s);self.assertNotIn('no-liftoff',s)
        self.assertIn('release_gate(provenance, expected_engine',s)

    def test_quality_boundaries(self):
        from timing_quality import evaluate_quality
        for ratio,valid in [(0.9,True),(1.1,True),(0.899999,False),(1.100001,False)]:
            r=timing_fixture();r['process_sets'][0]['sentinels'][1]['elapsed_s']=ratio
            self.assertEqual(evaluate_quality(r)['passed'],valid)
        for memory,valid in [(1073741824,True),(1073741823,False)]:
            r=timing_fixture();r['trials'][0]['before']['host']['available_bytes']=memory
            self.assertEqual(evaluate_quality(r)['passed'],valid)
    def test_resource_quality_failures(self):
        from timing_quality import evaluate_quality
        changes=[lambda x:x['after']['processes'][0].update(pid=999),lambda x:x['after']['processes'][0].update(created=2),lambda x:x['after']['processes'][0].update(major_faults=None),lambda x:x['after']['processes'][0]['cpu'].update(user=0),lambda x:x['after']['processes'][0].update(swap_bytes=1),lambda x:x['after']['host'].update(swap_in=1),lambda x:x['after']['host'].update(swap_out=1),lambda x:x['after']['host'].update(swap_used=1),lambda x:x['before']['host'].update(swap_in=1),lambda x:x['before']['processes'][0].update(minor_faults=1)]
        for change in changes:
            r=timing_fixture();change(r['trials'][0]);q=evaluate_quality(r)
            self.assertFalse(q['passed']);self.assertFalse(q['performance_claim_allowed'])
            self.assertEqual(len(r['trials']),27)
    def test_faults_retained_common_round_sensitivity(self):
        from source_resources import resource_observations
        r=timing_fixture();seen=False
        for row in r['trials']:
            if row['process_set']==1 and row['mode']=='candidate':
                if seen:row['before']['processes'][0]['major_faults']=1
                row['after']['processes'][0]['major_faults']=1;seen=True
                row['resource_observations']=resource_observations(row['before'],row['after'])
        with patch.object(tv,'release_gate',return_value={'synthetic':True}):s=tv.summarize(r)
        self.assertTrue(s['quality']['passed']);self.assertEqual(s['primary_calls'],27)
        self.assertEqual(s['fault_sensitivity_remaining_rounds'],8)
        for x in s['paired_effects'].values():
            self.assertEqual(len(x['pairs']),9)
            self.assertEqual(x['fault_sensitivity'][0]['observations'],8)
    def test_inconclusive_summary_no_gain_claim(self):
        r=timing_fixture();r['process_sets'][0]['sentinels'][1]['elapsed_s']=10
        with patch.object(tv,'release_gate',return_value={'synthetic':True}):s=tv.summarize(r)
        self.assertTrue(s['structurally_valid']);self.assertFalse(s['passed'])
        self.assertEqual(s['status'],'inconclusive_invalid_quality');self.assertFalse(s['performance_claim_allowed'])
        self.assertEqual(s['primary_calls'],27)

    def test_between_call_host_activity(self):
        from timing_quality import evaluate_quality
        for counter in ['swap_in','swap_out','swap_used']:
            r=timing_fixture()
            for row in r['trials'][1:]:
                for side in ['before','after']:row[side]['host'][counter]=1
            self.assertFalse(evaluate_quality(r)['passed'])
        r=timing_fixture();r['initial_host']['swap_in']=1
        self.assertFalse(evaluate_quality(r)['passed'])
        r=timing_fixture()
        for side in ['before','after']:r['trials'][0][side]['host']['swap_out']=1
        self.assertFalse(evaluate_quality(r)['passed'])

if __name__=='__main__':unittest.main()
