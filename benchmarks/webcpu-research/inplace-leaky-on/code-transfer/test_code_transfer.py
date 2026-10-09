"""Synthetic transfer fixtures are not numeric or performance evidence."""
import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import code_transfer as m

class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.out=self.root/'transfer'
  self.prov=copy.deepcopy(m.TEMPLATE['provenance']);self.proof=copy.deepcopy(m.TEMPLATE['source_proof']);self.entries={}
  for mode in m.KEYS:
   d=self.root/mode;d.mkdir();js=d/'voicevox_benchmark.js';js.write_text('// synthetic fixture\n');js.with_suffix('.wasm').write_bytes(b'\0asm\x01\0\0\0'+mode.encode())
   self.prov[mode]['wasm_sha256']=m.sha(js.with_suffix('.wasm').read_bytes());self.prov[mode]['js_sha256']=m.sha(js.read_bytes());self.entries[mode]={'binary':js}
  self.notice=self.root/'LICENSES.txt';self.notice.write_text('synthetic notice')
  self.prod=dict(repository='Hiroshiba/voicevox_core',run_id=123,commit='a'*40)
 def tearDown(self):self.temp.cleanup()
 def fake_discover(self,b,expected,size):
  mode=b[8:].decode();v=self.prov[mode]
  # Hash check is real in other tests; fake body hash only within synthetic parser stub.
  return [{'id':9,'payload':b'elements'}],None,{'index':v['callback_function_index'],'body':b'candidate' if mode=='candidate' else b'original'}, {'slot':v['callback_table_slot']},52,None
 def context(self):
  from contextlib import ExitStack
  s=ExitStack();s.enter_context(patch.object(m,'verify_manifest',return_value=(self.entries,self.prov,self.proof)));s.enter_context(patch.object(m,'discover',side_effect=self.fake_discover))
  original=m.sha
  s.enter_context(patch.object(m,'sha',side_effect=lambda b:m.CANDIDATE_BODY if b==b'candidate' else m.ORIGINAL_BODY if b==b'original' else original(b)))
  return s
 def export(self):return m.export(self.root/'not-read.json',self.out,self.notice,**self.prod)
 def test_roundtrip(self):
  with self.context():
   receipt=self.export();model=self.root/'model';model.write_bytes(b'synthetic-model')
   with patch.object(m,'MODEL_SHA',m.sha(model.read_bytes())):
    e,p,s,a=m.consume(self.out,model,expected_transfer_sha256=receipt['transfer_sha256'],expected_producer=self.prod)
   self.assertEqual(p,self.prov);self.assertEqual(s,self.proof);self.assertEqual(set(e),set(m.KEYS));self.assertFalse(a['source_and_archive_proofs_recomputed_here']);self.assertTrue(a['code_hashes_and_callbacks_recomputed_here']);self.assertTrue(a['extracted_file_set_and_hashes_verified']);self.assertFalse(a['downloaded_zip_digest_hard_verified'])
 def test_extra_file(self):
  with self.context():
   self.export();(self.out/'sample.vvm').write_bytes(b'private')
   with self.assertRaises(m.TransferError):m.verify_code(self.out,json.loads((self.out/'transfer.json').read_text()))
 def test_symlink(self):
  with self.context():
   self.export();p=self.out/'original/voicevox_benchmark.js';p.unlink();p.symlink_to(self.entries['original']['binary'])
   with self.assertRaises(m.TransferError):m.verify_code(self.out,json.loads((self.out/'transfer.json').read_text()))
 def test_hash_tampering(self):
  with self.context():
   self.export();(self.out/'original/voicevox_benchmark.js').write_text('changed')
   with self.assertRaises(m.TransferError):m.verify_code(self.out,json.loads((self.out/'transfer.json').read_text()))
 def test_record_rejections(self):
  with self.context():
   self.export();base=json.loads((self.out/'transfer.json').read_text())
   changes=[lambda x:x.update(raw_audio=[1]),lambda x:x['producer'].update(repository='VOICEVOX/voicevox_core'),lambda x:x['producer'].update(run_id=True),lambda x:x['provenance']['original'].update(secret='private'),lambda x:x['provenance']['original']['native_bundle'].update(all_ordered_native_members_verified=1437),lambda x:x['files'].update({'../private':{'sha256':'a'*64,'bytes':2}}),lambda x:x['files']['original/voicevox_benchmark.js'].update(bytes=10**9),lambda x:x['notices'].clear(),lambda x:x['source_proof']['candidate_scope']['counts'].update(raw_IR_identical=294)]
   for f in changes:
    r=copy.deepcopy(base);f(r)
    with self.assertRaises(m.TransferError):m.validate_record(r)
 def test_callback_tampering(self):
  with self.context():
   self.export();r=json.loads((self.out/'transfer.json').read_text())
   with patch.object(m,'discover',return_value=([{'id':9,'payload':b'elements'}],None,{'index':1,'body':b'original'},{'slot':1},52,None)):
    with self.assertRaises(m.TransferError):m.verify_code(self.out,r)
 def test_capture_binding(self):
  with self.context():
   self.export();c=self.root/'capture.json';r=dict(status='awaiting_manual_review',capture_only=True,trials=[],provenance=self.prov,source_proof=self.proof);c.write_text(json.dumps(r));self.assertFalse(m.verify_capture_binding(self.out,c)['timing_permitted'])
   r['trials']=[1];c.write_text(json.dumps(r))
   with self.assertRaises(m.TransferError):m.verify_capture_binding(self.out,c)
 def test_consumer_binding_rejects(self):
  with self.context():
   receipt=self.export();model=self.root/'model';model.write_bytes(b'synthetic-model')
   with self.assertRaises(m.TransferError):m.consume(self.out,model,expected_transfer_sha256='0'*64,expected_producer=self.prod)
   with self.assertRaises(m.TransferError):m.consume(self.out,model,expected_transfer_sha256=receipt['transfer_sha256'],expected_producer=dict(self.prod,run_id=999))
   with self.assertRaises(m.TransferError):m.consume(self.out,model,expected_transfer_sha256=receipt['transfer_sha256'],expected_producer=self.prod)
 def test_existing_destination_rejected(self):
  self.out.mkdir()
  with self.context():
   with self.assertRaises(m.TransferError):self.export()
if __name__=='__main__':unittest.main()
