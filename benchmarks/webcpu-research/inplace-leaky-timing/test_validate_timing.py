import copy,json,unittest
from pathlib import Path
from validate_timing import validate,summarize,schedule
from resources import resource_observations,pressure_during_call,idle_cpu
GATE=json.loads((Path(__file__).parent/'gate_reference.json').read_text())
def fixture():
 g=copy.deepcopy(GATE);r={'schema':'inplace-leaky-timing-v1','status':'complete','schedule':schedule(),'warmups_per_browser':5,'normal_tiering':True,'browser_flags':['--js-flags=--no-wasm-revectorize'],'gate_reference':g,'source_hashes':{},'process_sets':[],'trials':[],'notes':[],'query_sha256':g['query_sha256'],'model_sha256':g['model_sha256'],'initial_host':{'available_bytes':2**31,'swap_in':0,'swap_out':0},'environment':{'cpu':'fixture','os':'fixture','available_logical_cpus':4,'affinity_logical_cpus':4},'build_toolchain':{'core':'9b539761f3e152b966e08c2de0784129fe8cf68d','rust':'1.96.0','emscripten':'4.0.8','runtime':'0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf','optimization':'z','graph_level':1},'private_temporary_directory_deleted':True}
 r['transform']={k:g[k] for k in ['source_wasm_sha256','candidate_wasm_sha256','source_js_sha256','original_body_sha256','candidate_body_sha256','callback_table','absolute_function_index']};r['transform'].update(all_other_functions_sections_identical=True,abi={'params':['i32']*3,'results':[]},custom_sections=[],changed_length_fields=[])
 for i in range(1,4):
  group={'id':i,'runtime':{},'warmups':[],'sentinels':[{'position':p,'elapsed_s':2,'wav_bytes':478252} for p in ['before','after']],'checks':[],'cleanup':{}}
  for j,m in enumerate(['original','candidate']):
   group['runtime'][m]={'browser_pid':100*i+j,'browser_created':1234+i,'engine':g['engine'],'launch_flags':r['browser_flags'],'initialization':{'ready':True,'timing_instrumentation':'clock_only','runtime':g['runtime'],'dispatch':g['dispatch'],'wasm_sha256':g['source_wasm_sha256' if m=='original' else 'candidate_wasm_sha256'],'js_sha256':g['source_js_sha256']}}
   group['warmups'] += [{'mode':m,'iteration':n,'elapsed_s':2,'wav_bytes':478252} for n in range(1,6)]
   group['checks'] += [{'mode':m,'phase':p,'iteration':n,'finite':True,'exact_bytes':True,'wav_bytes':478252,'raw_bytes':956416,**g['outputs']} for p in ['before','after'] for n in [1,2]]
   group['cleanup'][m]={'confirmed':True,'observed_processes':5,'remaining_processes':0}
  procs=[{'pid':9,'created':1,'cpu':{'user':0,'system':0},'major_faults':0,'minor_faults':0,'swap_bytes':0}];group['idle']={'interval_s':1,'modes':{m:{'before':procs,'after':procs,**idle_cpu(procs,procs,1)} for m in ['original','candidate']}}
  r['process_sets'].append(group)
 for block in schedule():
  for m in block['order']:
   point={'host':{'available_bytes':2**31,'swap_in':0,'swap_out':0},'processes':[{'pid':9,'created':1,'cpu':{'user':0,'system':0},'major_faults':0,'minor_faults':0,'swap_bytes':0}]}
   r['trials'].append({'process_set':block['process_set'],'pair':block['pair'],'mode':m,'pair_order':block['order'],'elapsed_s':2 if m=='original' else 1.9,'wav_bytes':478252,'before':copy.deepcopy(point),'after':copy.deepcopy(point),'resource_observations':resource_observations(point,point)})
 return r
class Test(unittest.TestCase):
 def test_success(self):self.assertTrue(validate(fixture()));self.assertAlmostEqual(summarize(fixture())['median_pair_ratio'],.95)
 def test_schedule(self):self.assertEqual(sum(x['order'][0]=='original' for x in schedule()),5)
 def test_missing(self):
  r=fixture();r['trials'].pop()
  with self.assertRaises(ValueError):validate(r)
 def test_order(self):
  r=fixture();r['trials'][0],r['trials'][1]=r['trials'][1],r['trials'][0]
  with self.assertRaises(ValueError):validate(r)
 def test_stale(self):
  r=fixture();r['process_sets'][0]['runtime']['candidate']['initialization']['wasm_sha256']='0'*64
  with self.assertRaises(ValueError):validate(r)
 def test_bad_output(self):
  r=fixture();r['process_sets'][1]['checks'][0]['raw_sha256']='0'*64
  with self.assertRaises(ValueError):validate(r)
 def test_cleanup(self):
  r=fixture();r['process_sets'][0]['cleanup']['original']['confirmed']=False
  with self.assertRaises(ValueError):validate(r)
 def test_fault_retained(self):
  r=fixture();row=r['trials'][0];row['after']['processes'][0]['major_faults']=3;row['resource_observations']=resource_observations(row['before'],row['after']);self.assertTrue(validate(r));self.assertIsNone(pressure_during_call(row['before'],row['after']))
 def test_lowmemory(self):
  r=fixture();r['trials'][0]['after']['host']['available_bytes']=1
  with self.assertRaises(ValueError):validate(r)
 def test_private(self):
  r=fixture();r['process_sets'][0]['checks'][0]['raw']=[1,2]
  with self.assertRaises(ValueError):validate(r)
 def test_resource_payload(self):
  r=fixture();r['trials'][0]['after']['host']['encoded_data']=[1,2]
  with self.assertRaises(ValueError):validate(r)
 def test_idle_delta(self):
  r=fixture();r['process_sets'][0]['idle']['modes']['original']['cpu_percent']=99
  with self.assertRaises(ValueError):validate(r)
 def test_hook_free_worker(self):
  text=(Path(__file__).parent/'timing_worker.js').read_text()
  for bad in ['installLeakyAliasCheck','leakyForwarder','wasmTable','ProcessCpuSampler','core_alias_check']:self.assertNotIn(bad,text)
  self.assertIn('wasmBinary:new Uint8Array(wasm)',text)
if __name__=='__main__':unittest.main()
