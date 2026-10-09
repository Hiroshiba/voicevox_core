#!/usr/bin/env python3
import hashlib,importlib.util,json,pathlib,tempfile,unittest
from unittest.mock import patch
HERE=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('notice_gate',HERE/'verify_notice_inventory.py');g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
def write(p,data):p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
def js(p,v):write(p,(json.dumps(v)+'\n').encode())
class GateTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.cache=self.root/'cache';self.cache.mkdir()
  self.notices=self.root/'LICENSES.txt';write(self.notices,b'Original preserved notices\n')
  self.inv={'schema':'voicevox-code-notice-inventory-v1','notice_materials_complete':True,'combined_notices_sha256':g.sha(self.notices),'core_commit':'a'*40,'required_source_refs':{'source_commit':'b'*40},'required_build_identity':{'core':'a'*40,'rust':'1.96.0'},'eigen_source_inventory':{'files':{}},'eigen_source_url':'https://github.com/eigen-mirror/eigen/archive/pin.zip'}
  self.ip=self.root/'inventory.json';js(self.ip,self.inv)
  self.source=self.cache/'inplace-leaky-on-source-v1'/('core-source-'+'c'*16)/('voicevox_core-'+'a'*40);self.source.mkdir(parents=True)
  rows=[]
  for key in ['original','rebuilt','candidate']:
   binary=self.cache/key/'voicevox_benchmark.js';write(binary,b'// code only\n');write(binary.with_suffix('.wasm'),b'\0asm\1\0\0\0')
   write(binary.parent/'cargo-link.log',b'rustc ordinary flags\n');bundle={'verified':True,'all_ordered_native_members_verified':1438,'runtime_sha256':'d'*64};js(binary.parent/'native-bundle-verification.json',bundle)
   identity=dict(self.inv['required_build_identity'],runtime='d'*64)
   receipt={'identity':identity,'files':{p.name:g.sha(p) for p in binary.parent.iterdir() if p.is_file()}};js(binary.parent/'complete.json',receipt)
   rows.append({'key':key,'binary':str(binary),'build_identity':identity,'complete_receipt_sha256':g.sha(binary.parent/'complete.json')})
  self.man={'schema':'inplace-leaky-on-source-manifest-v1','verified':True,'builder_source_sha256':'c'*64,'source_refs':self.inv['required_source_refs'],'variants':rows};self.mp=self.root/'manifest.json';js(self.mp,self.man)
 def tearDown(self):self.tmp.cleanup()
 def run_gate(self):
  with patch.object(g,'verify_dependencies',return_value={'test_fixture':True}),patch.object(g,'verify_eigen_source',return_value={'test_fixture':True}):return g.verify(self.mp,self.cache,self.ip,self.notices)
 def mutate_man(self):js(self.mp,self.man)
 def refresh_receipt(self,key=0):
  r=self.man['variants'][key];p=pathlib.Path(r['binary']).parent;c=g.read_json(p/'complete.json');c['files']={n:g.sha(p/n) for n in c['files']};js(p/'complete.json',c);r['complete_receipt_sha256']=g.sha(p/'complete.json');self.mutate_man()
 def test_synthetic_valid_identity_path(self):self.assertTrue(self.run_gate()['verified'])
 def test_notice_mutation(self):write(self.notices,b'changed');self.assertRaisesRegex(g.GateError,'notices_hash',self.run_gate)
 def test_incomplete_materials(self):self.inv['notice_materials_complete']=False;js(self.ip,self.inv);self.assertRaisesRegex(g.GateError,'incomplete',self.run_gate)
 def test_core_path_missing(self):self.man['builder_source_sha256']='e'*64;self.mutate_man();self.assertRaisesRegex(g.GateError,'source_missing',self.run_gate)
 def test_native_pin_mismatch(self):self.man['source_refs']={'source_commit':'f'*40};self.mutate_man();self.assertRaisesRegex(g.GateError,'native_source_pin',self.run_gate)
 def test_identity_mismatch(self):self.man['variants'][0]['build_identity']['rust']='1.95.0';self.mutate_man();self.assertRaisesRegex(g.GateError,'build_identity',self.run_gate)
 def test_binary_outside_cache(self):self.man['variants'][0]['binary']=str(self.root/'outside.js');self.mutate_man();self.assertRaisesRegex(g.GateError,'outside_cache',self.run_gate)
 def test_changed_code(self):write(pathlib.Path(self.man['variants'][0]['binary']),b'changed');self.assertRaisesRegex(g.GateError,'build_file_digest',self.run_gate)
 def test_invalid_wasm(self):write(pathlib.Path(self.man['variants'][0]['binary']).with_suffix('.wasm'),b'invalid!');self.refresh_receipt();self.assertRaisesRegex(g.GateError,'wasm_magic',self.run_gate)
 def test_native_member_count(self):p=pathlib.Path(self.man['variants'][0]['binary']).parent/'native-bundle-verification.json';b=g.read_json(p);b['all_ordered_native_members_verified']=1437;js(p,b);self.refresh_receipt();self.assertRaisesRegex(g.GateError,'native_members',self.run_gate)
 def test_embedding_flag(self):p=pathlib.Path(self.man['variants'][0]['binary']).parent/'cargo-link.log';write(p,b'--embed-file sample.vvm');self.refresh_receipt();self.assertRaisesRegex(g.GateError,'embedded_data',self.run_gate)
 def test_eigen_unchanged(self):d=self.root/'eigen';write(d/'a.h',b'original');i={'eigen_source_inventory':{'files':{'a.h':g.sha(d/'a.h')}}};self.assertEqual(g.verify_eigen_source(d,i,check_network=False)['files'],1)
 def test_eigen_changed(self):d=self.root/'eigen';write(d/'a.h',b'original');i={'eigen_source_inventory':{'files':{'a.h':'a'*64}}};self.assertRaisesRegex(g.GateError,'eigen_source_changed',g.verify_eigen_source,d,i,check_network=False)
 def test_eigen_added(self):d=self.root/'eigen';write(d/'extra.h',b'extra');i={'eigen_source_inventory':{'files':{}}};self.assertRaisesRegex(g.GateError,'eigen_source_changed',g.verify_eigen_source,d,i,check_network=False)
if __name__=='__main__':unittest.main()
