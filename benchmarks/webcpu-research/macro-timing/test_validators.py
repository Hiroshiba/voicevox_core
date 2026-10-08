"""Mock-only validator sensitivity checks. No operators or clocks execute."""
import copy,json,pathlib,unittest
from validate_screen import validate,expected
P=pathlib.Path(__file__).resolve().parent
schedule=json.loads((P/'schedule.json').read_text())
def fixture():
 engine={'jsVersion':'MOCK_ONLY'};report=dict(passed=True,screen_requested=True,schedule=copy.deepcopy(schedule),diagnosticEngine=engine,diagnostic=[],sets=[],paired_geomean_ratios_per_set_and_dilation=[{} for _ in range(9)])
 for c in range(3):
  records=[]
  for d in [1,3,5]:
   e=expected[str(d)];records.append(dict(condition=c,dilation=d,hashes=[e['output'][0],e['output'][0],e['output'][1]],packedHash=e['packed'],selection=dict(which=c,threads=2,kernel_mr4_linear_loadsplat=1),runs=[dict(bad=0,callbacks=15392 if c==0 else 1924,calls=61568 if c==2 else 15392,thread_ids=[1,2],thread_callbacks=[1,1]) for _ in range(3)]))
  report['diagnostic'].append(dict(metadata=dict(condition=c,hardwareConcurrency=2),records=records))
 for s in range(3):
  rows=[dict(dilation=d,selection=True,hashes=expected[str(d)]['output'],packedHash=expected[str(d)]['packed']) for d in [1,3,5]]
  trial=dict(set=s,engine=engine,metadata=[dict(condition=c,heapBytes=1073741824,shared=True,isolated=True,hardwareConcurrency=2) for c in range(3)],pre=[copy.deepcopy(rows) for _ in range(3)],post=[copy.deepcopy(rows) for _ in range(3)],warmups=[dict(condition=c,dilation=d,iteration=i,latencyMs=1.0,heapBytes=1073741824) for c in range(3) for d in [1,3,5] for i in range(3)],idleReadiness=[dict(cpuSeconds=0,newProcesses=[],exitedProcesses=[]) for _ in range(2)],rounds=[])
  t=1
  for r,items in enumerate(schedule[s]):
   calls=[]
   for item in items:calls.append(dict(condition=item['condition'],dilation=item['dilation'],startEpochMs=t,endEpochMs=t+1,latencyMs=1,heapBytes=1073741824));t+=2
   resource=dict(processes=[],hostAvailableBytes=1,hostSwapUsedBytes=0,pressure={},cgroupCpuStat='')
   trial['rounds'].append(dict(round=r,calls=calls,before=resource,after=resource,resourceExposure={}))
  report['sets'].append(trial)
 return report
class Tests(unittest.TestCase):
 def test_valid_mock(self):self.assertTrue(validate(fixture())['passed'])
 def reject(self,mutate):
  r=fixture();mutate(r)
  with self.assertRaises((AssertionError,KeyError,TypeError)):validate(r)
 def test_missing_call(self):self.reject(lambda r:r['sets'][0]['rounds'][0]['calls'].pop())
 def test_missing_warmup(self):self.reject(lambda r:r['sets'][0]['warmups'].pop())
 def test_wrong_post_hash(self):self.reject(lambda r:r['sets'][0]['post'][0][0].update(hashes=['0'*64]*2))
 def test_overlapping_calls(self):self.reject(lambda r:r['sets'][0]['rounds'][0]['calls'][1].update(startEpochMs=1))
 def test_engine_change(self):self.reject(lambda r:r['sets'][0].update(engine={'jsVersion':'OTHER'}))
 def test_idle_pid_churn(self):self.reject(lambda r:r['sets'][0]['idleReadiness'][-1].update(newProcesses=[[1,1]]))
 def test_nan_latency(self):self.reject(lambda r:r['sets'][0]['rounds'][0]['calls'][0].update(latencyMs=float('nan')))
 def test_missing_resources(self):self.reject(lambda r:r['sets'][0]['rounds'][0].pop('before'))
 def test_private_payload(self):
  ns={'__file__':str(P/'log_results.py')};exec((P/'log_results.py').read_text().split('paths=')[0],ns)
  with self.assertRaises(AssertionError):ns['safe']({'tensor_data':[1,2]})
if __name__=='__main__':unittest.main()
