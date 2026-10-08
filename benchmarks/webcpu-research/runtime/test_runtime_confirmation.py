"""Synthetic tests only: no browser, model, timer, or inference is executed."""
import collections,copy,itertools,unittest
from validate_runtime_confirmation import MODES,balanced_schedule,validate,pressure_during_call,resource_observations
from log_runtime_confirmation import summarize

def fixture():
 schedule=balanced_schedule();exact={'pcm_exact':True,'fp32_exact':True,'finite':True}
 result={'schema':'voicevox-browser-runtime-confirmation-v1','revectorization':'off','status':'complete','seed':1729,'schedule':schedule,'process_sets':[],'trials':[],'cpu_companion':[]}
 for s in (1,2,3):
  runtime={m:{'browser_pid':s*10+i,'browser_created':100+s,'dispatch':{'verified':True,'worker_hardware_concurrency':4,'actual_dispatch':'loadsplat' if m=='loadsplat' else 'splat','checked_pointers':12}} for i,m in enumerate(MODES)}
  result['process_sets'].append({'id':s,'runtime':runtime,'sentinels':[{'position':p,'elapsed_s':1,'audio_s':10} for p in ('before','after')],'warmups':[{'mode':m,'iteration':i,'elapsed_s':1,'audio_s':10} for m in MODES for i in range(1,6)],'checks':{m:{k:exact.copy() for k in ('repeat','versus_initial_control','after','after_repeat')} for m in MODES}})
  result['cpu_companion'] += [{'process_set':s,'mode':m,'elapsed_s':1,'audio_s':10,'cpu_trace':[{'cpu_percent':100}]} for m in MODES]
 for block in schedule:
  for m in block['order']:result['trials'].append({'process_set':block['process_set'],'pair':block['pair'],'mode':m,'pair_order':block['order'],'elapsed_s':1,'audio_s':10})
 return result

class Tests(unittest.TestCase):
 def test_schedule_balance(self):
  for seed in range(20):
   schedule=balanced_schedule(seed=seed);orders=[x['order'] for x in schedule]
   for m in MODES:self.assertEqual(collections.Counter(o.index(m) for o in orders),{0:5,1:5,2:5})
   for x,y in itertools.combinations(MODES,2):self.assertIn(sum(o.index(x)<o.index(y) for o in orders),(7,8))
 def test_valid(self):validate(fixture(),3,5,5)
 def test_reject_bad_matrix(self):
  x=fixture();x['trials'].pop()
  with self.assertRaises(ValueError):validate(x,3,5,5)
 def test_reject_bad_order(self):
  x=fixture();x['trials'][0],x['trials'][1]=x['trials'][1],x['trials'][0]
  with self.assertRaises(ValueError):validate(x,3,5,5)
 def test_reject_failed_repeat(self):
  x=fixture();x['process_sets'][0]['checks']['loadsplat']['after_repeat']['fp32_exact']=False
  with self.assertRaises(ValueError):validate(x,3,5,5)
 def test_reject_wrong_dispatch(self):
  x=fixture();x['process_sets'][0]['runtime']['loadsplat']['dispatch']['actual_dispatch']='splat'
  with self.assertRaises(ValueError):validate(x,3,5,5)
 def test_reject_nonfresh(self):
  x=fixture();x['process_sets'][1]['runtime']=copy.deepcopy(x['process_sets'][0]['runtime'])
  with self.assertRaises(ValueError):validate(x,3,5,5)
 def test_pressure_policy(self):
  before={'host':{'available_bytes':2*1024**3,'swap_in':1,'swap_out':2},'processes':[{'pid':1,'created':1,'major_faults':0,'swap_bytes':0}]};after=copy.deepcopy(before);after['processes'][0].update(major_faults=100,swap_bytes=999);after['host']['swap_in']=10000
  self.assertIsNone(pressure_during_call(before,after));self.assertEqual(resource_observations(before,after)['target_major_fault_delta'],100)
  after['host']['available_bytes']=1024**3-1;self.assertIsNotNone(pressure_during_call(before,after))
 def test_log_allowlist(self):
  x=fixture();x['raw_audio']=[.1]*1000;x['cpu_companion'][0]['private']='secret'
  safe=summarize(x);self.assertNotIn('raw_audio',str(safe));self.assertNotIn('secret',str(safe));self.assertEqual(safe['paired_effects']['loadsplat']['paired_observations'],15);self.assertIn('cpu_trace',x['cpu_companion'][0])
 def test_no_different_design(self):
  with self.assertRaises(ValueError):balanced_schedule(sets=1,pairs=15)
if __name__=='__main__':unittest.main()
