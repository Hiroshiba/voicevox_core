import copy,importlib.util,sys,types,unittest
from pathlib import Path
# Pure metadata tests do not claim a reviewed artifact exists.
old=sys.modules.get('release_gate');sys.modules['release_gate']=types.SimpleNamespace(load_contract=lambda:{})
spec=importlib.util.spec_from_file_location('artifact_binding_under_test',Path(__file__).with_name('check_artifact_binding.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
if old is None:sys.modules.pop('release_gate',None)
else:sys.modules['release_gate']=old
class Tests(unittest.TestCase):
 def test_identity_and_digest(self):
  binding={'producer':{'repository':'Hiroshiba/voicevox_core','run_id':1,'commit':'a'*40},'artifact_id':2,'artifact_zip_sha256':'b'*64}
  meta={'id':2,'name':'webcpu-pristine-on-immutable-code','expired':False,'digest':'sha256:'+'b'*64,'workflow_run':{'id':1,'head_sha':'a'*40,'repository_id':617625583,'head_repository_id':617625583}}
  self.assertTrue(m.validate(meta,binding))
  for target,key in [(meta,'id'),(meta,'name'),(meta,'expired'),(meta,'digest'),(meta['workflow_run'],'id'),(meta['workflow_run'],'head_sha'),(meta['workflow_run'],'repository_id'),(meta['workflow_run'],'head_repository_id')]:
   original=target[key];target[key]='changed'
   with self.assertRaises(ValueError):m.validate(meta,binding)
   target[key]=original
if __name__=='__main__':unittest.main()
