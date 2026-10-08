"""Schema/privacy unit tests only; fixtures are not measured benchmark results."""
import copy,unittest
import log_numeric_summary as log
class Tests(unittest.TestCase):
 def proof(self):
  return {'original_dispatch':{'schema_version':1,'source':'actual_CORE_module','context':'dedicated_worker','expected_dispatch':'auto','actual_dispatch':'splat','worker_hardware_concurrency':4,'probe_hardware_concurrency':4,'is_x86':True,'relaxed_simd':False,'mr':4,'nr':8,'splat_pointers':12,'loadsplat_pointers':0,'checked_pointers':12,'probe_return_code':0,'cross_origin_isolated':True,'shared_memory':True,'verified':True}}
 def test_whitelist(self):self.assertEqual(log.core_dispatch(self.proof())['kind'],'actual_core_worker_dispatch')
 def test_private_keys(self):
  for key in ['wav','raw','audio','weights','tensor','access_token','api_key']:
   with self.assertRaises(ValueError):log.privacy_guard({key:[0.1]})
 def test_large_arrays(self):
  with self.assertRaises(ValueError):log.privacy_guard({'samples':[1]*129})
 def test_unknown_dispatch_field(self):
  p=self.proof();p['original_dispatch']['raw_samples']=[.5]
  with self.assertRaises(ValueError):log.core_dispatch(p)
 def test_no_node_mislabel(self):
  p=self.proof();p['original_dispatch']['context']='node'
  with self.assertRaises(ValueError):log.core_dispatch(p)
 def test_invalid_numbers(self):
  for value in [True,float('nan'),float('inf'),-1]:
   with self.assertRaises(ValueError):log.number(value)
 def test_failed_evidence(self):
  p={'original_dispatch':{'schema_version':1,'source':'actual_CORE_module','context':'dedicated_worker','expected_dispatch':'loadsplat','worker_hardware_concurrency':4,'numeric_records':[{'splatPointers':12}],'verified':False,'error_type':'Error','error_message':'Actual CORE dispatcher does not match manifest/heuristic'}}
  self.assertFalse(log.core_dispatch(p)['records'][0]['verified'])
  p['original_dispatch']['error_message']='PRIVATE PAYLOAD'
  with self.assertRaises(ValueError):log.core_dispatch(p)
 def test_same_host(self):
  p=self.proof();q=copy.deepcopy(p['original_dispatch']);q.update(worker_hardware_concurrency=8,probe_hardware_concurrency=8,actual_dispatch='loadsplat',loadsplat_pointers=12,splat_pointers=0,probe_return_code=1);p['auto_roundtrip_dispatch']=q
  with self.assertRaises(ValueError):log.core_dispatch(p)
if __name__=='__main__':unittest.main()
