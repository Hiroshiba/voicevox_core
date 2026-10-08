import copy,unittest
from validate_gate import validate
HASH='a'*64
def fixture():
 r={'passed':True,'normal_tiering':True,'private_temporary_directory_deleted':True,'requested_browser_flags':['--js-flags=--no-wasm-revectorize'],'activation':{'transformed_groups':0,'revectorizable_nodes':0,'flag_rejected':False,'launch_configuration_matches':True},'transform':{'all_other_functions_sections_identical':True,'candidate_bytes':106,'source_bytes':100,'original_body_sha256':'30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6','source_wasm_sha256':HASH,'candidate_wasm_sha256':'b'*64,'noop_roundtrip_sha256':HASH},'variants':{}}
 r['transform'].update({'scope':'Experimental guard-only Wasm transform. No browser execution or timing.','abi':{'params':['i32']*3,'results':[]},'callback_table':{'table':0,'slot':17},'absolute_function_index':1,'custom_sections':[]})
 for variant in ['original','candidate']:
  init={'ready':True,'callback_identity_checked':True,'wasm_sha256':r['transform']['source_wasm_sha256' if variant=='original' else 'candidate_wasm_sha256'],'runtime':{'shared_memory':True,'pthreads':1,'spin_off':True,'fixed_length':962,'fixed_matches':1,'xnn_threads':2,'xnn_sessions':1,'configured_ort_global_threads':1},'dispatch':{'verified':True,'actual_dispatch':'loadsplat','loadsplat_pointers':12,'splat_pointers':0,'checked_pointers':12,'relaxed_simd':False}}
  alias={'variant':variant,'table_slot':17,'function_index':1,'restored':True,'metadata_errors':0,'callbacks':69,'elements':435901440,'classes':{'exact_inplace':{'calls':41,'elements':256615424},'disjoint':{'calls':28,'elements':179286016}},'length_histogram':{'492544':1,'1970176':17,'7880704':51},'predicted_branch':{'simd':{'calls':69,'elements':435901440},'scalar':{'calls':0,'elements':0}} if variant=='candidate' else {'simd':{'calls':28,'elements':179286016},'scalar':{'calls':41,'elements':256615424}}}
  checks=[{'index':i,'exact_bytes_to_first':True,'wav_bytes':478252,'raw_bytes':956416,'raw_sha256':HASH,'pcm_sha256':HASH,'wav_sha256':HASH,'alias':[copy.deepcopy(alias),copy.deepcopy(alias)]} for i in range(3)];r['variants'][variant]={'initialization':init,'checks':checks}
 return r
class Test(unittest.TestCase):
 def test_success(self):self.assertTrue(validate(fixture())['passed'])
 def test_failed(self):
  r=fixture();r['passed']=False
  with self.assertRaises(ValueError):validate(r)
 def test_tiering(self):
  r=fixture();r['requested_browser_flags'].append('--no-liftoff')
  with self.assertRaises(ValueError):validate(r)
 def test_hash(self):
  r=fixture();r['variants']['candidate']['checks'][2]['raw_sha256']='c'*64
  with self.assertRaises(ValueError):validate(r)
 def test_restore(self):
  r=fixture();r['variants']['candidate']['checks'][0]['alias'][0]['restored']=False
  with self.assertRaises(ValueError):validate(r)
 def test_unexpected(self):
  r=fixture();r['tensor']='encoded-private-output'
  with self.assertRaises(ValueError):validate(r)
 def test_identity(self):
  r=fixture();r['variants']['candidate']['checks'][0]['alias'][0]['function_index']=99
  with self.assertRaises(ValueError):validate(r)
 def test_nested(self):
  r=fixture();r['transform']['callback_table']['tensor']='private'
  with self.assertRaises(ValueError):validate(r)
 def test_wrong_coverage(self):
  r=fixture();r['variants']['candidate']['checks'][0]['alias'][0]['callbacks']=68
  with self.assertRaises(ValueError):validate(r)
if __name__=='__main__':unittest.main()
