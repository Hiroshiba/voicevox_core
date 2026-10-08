import copy,unittest
from validate_confirmation import validate
class Tests(unittest.TestCase):
 def setUp(self):
  exact={'pcm_exact':True,'fp32_exact':True,'finite':True};t={'elapsed_s':1.,'audio_s':10.}
  self.x={'process_sets':[{'id':1,'sentinels':[dict(t,position=p) for p in ['before','after']],'warmups':[dict(t,mode=m,iteration=1) for m in ['original','combined']],'checks':{m:{k:exact for k in ['repeat','versus_initial_control','after']} for m in ['original','combined']}}],'trials':[dict(t,mode=m,process_set=1,pair=1) for m in ['original','combined']],'cpu_companion':[dict(t,mode=m,process_set=1) for m in ['original','combined']]}
 def test_valid(self):validate(self.x,1,1,1)
 def test_missing(self):self.x['trials'].pop();self.assertRaises(ValueError,validate,self.x,1,1,1)
 def test_nan(self):self.x['trials'][0]['elapsed_s']=float('nan');self.assertRaises(ValueError,validate,self.x,1,1,1)
 def test_duplicate(self):self.x['trials'].append(self.x['trials'][0]);self.assertRaises(ValueError,validate,self.x,1,1,1)
 def test_warmup(self):self.x['process_sets'][0]['warmups'].pop();self.assertRaises(ValueError,validate,self.x,1,1,1)
if __name__=='__main__':unittest.main()
