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
   write(binary.parent/'cargo-link.log',b'''Running `rustc --crate-name voicevox_benchmark --target wasm32-unknown-emscripten --cfg 'feature="browser"' --cfg 'feature="threaded"'`\n''');bundle={'verified':True,'all_ordered_native_members_verified':1438,'runtime_sha256':'d'*64};js(binary.parent/'native-bundle-verification.json',bundle)
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
 def test_receipt_bound_candidate_features(self):
  p=pathlib.Path(self.man['variants'][2]['binary']).parent/'cargo-link.log';write(p,p.read_bytes().replace(b'threaded',b'native'));self.refresh_receipt(2);self.assertRaisesRegex(g.GateError,'benchmark_features_mismatch',self.run_gate)
 def test_embedding_flag(self):p=pathlib.Path(self.man['variants'][0]['binary']).parent/'cargo-link.log';write(p,b'--embed-file sample.vvm');self.refresh_receipt();self.assertRaisesRegex(g.GateError,'embedded_data',self.run_gate)
 def test_eigen_unchanged(self):d=self.root/'eigen';write(d/'a.h',b'original');i={'eigen_source_inventory':{'files':{'a.h':g.sha(d/'a.h')}}};self.assertEqual(g.verify_eigen_source(d,i,check_network=False)['files'],1)
 def test_eigen_changed(self):d=self.root/'eigen';write(d/'a.h',b'original');i={'eigen_source_inventory':{'files':{'a.h':'a'*64}}};self.assertRaisesRegex(g.GateError,'eigen_source_changed',g.verify_eigen_source,d,i,check_network=False)
 def test_eigen_added(self):d=self.root/'eigen';write(d/'extra.h',b'extra');i={'eigen_source_inventory':{'files':{}}};self.assertRaisesRegex(g.GateError,'eigen_source_changed',g.verify_eigen_source,d,i,check_network=False)

class DependencyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.tmp.name);self.cache=self.root/'cache';self.source=self.root/'source'
  write(self.source/'Cargo.toml',b'[workspace]\nmembers=["wrapper"]\n');write(self.source/'.cargo/config.toml',b'[alias]\n')
  self.wrapper=self.source/'wrapper/Cargo.toml';write(self.wrapper,b'[package]\nname="voicevox_benchmark"\nversion="0.0.0"\n')
  self.registry='index.crates.io-fixture';self.gitrev='a'*40;self.gitsource='git+https://example.invalid/dependency?rev='+self.gitrev+'#'+self.gitrev
  self.gitroot=self.cache/'cargo/git/checkouts/gitcrate-fixture/aaaaaaa';write(self.gitroot/'Cargo.toml',b'[package]\nname="gitcrate"\nversion="1.0.0"\nlicense="MIT"\n');write(self.gitroot/'.revision',self.gitrev.encode())
  self.crate=self.cache/'cargo/registry/src'/self.registry/'crate-1.0.0/Cargo.toml';write(self.crate,b'[package]\nname="crate"\nversion="1.0.0"\nlicense="MIT"\n')
  self.archive=self.cache/'cargo/registry/cache'/self.registry/'crate-1.0.0.crate';write(self.archive,b'pinned crate archive')
  self.stdcrate=self.cache/'cargo/registry/src'/self.registry/'stdcrate-2.0.0/Cargo.toml';write(self.stdcrate,b'[package]\nname="stdcrate"\nversion="2.0.0"\nlicense="MIT"\n')
  self.stdarchive=self.cache/'cargo/registry/cache'/self.registry/'stdcrate-2.0.0.crate';write(self.stdarchive,b'pinned std crate archive')
  lock='''[[package]]
name="voicevox_benchmark"
version="0.0.0"
[[package]]
name="crate"
version="1.0.0"
source="%s"
checksum="%s"
[[package]]
name="gitcrate"
version="1.0.0"
source="%s"
'''%(g.REGISTRY,g.sha(self.archive),self.gitsource);write(self.source/'Cargo.lock',lock.encode())
  self.library=self.cache/'rustup/toolchains/1.96.0-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library';write(self.library/'Cargo.lock',('[[package]]\nname="stdcrate"\nversion="2.0.0"\nsource="%s"\nchecksum="%s"\n'%(g.REGISTRY,g.sha(self.stdarchive))).encode());write(self.library/'LICENSE',b'std notice')
  self.target=self.cache/'cargo-target/browser-mt-xnnpack/wasm32-unknown-emscripten/c-api';self.host=self.cache/'cargo-target/browser-mt-xnnpack/c-api'
  dep=(str(self.crate.parent)+'/src/lib.rs '+str(self.stdcrate.parent)+'/src/lib.rs '+str(self.gitroot)+'/src/lib.rs\n').encode()
  write(self.target/'deps/wrapper.d',dep);write(self.host/'deps/helper.d',dep);write(self.host/'build/helper-123/build_script.d',dep)
  self.native=self.target/'build/open_jtalk-sys-123/out/build/_deps/openjtalk-src';write(self.native/'.revision',b'b'*40);write(self.native/'COPYING',b'native notice')
  def row(name,version,source,manifest,license,checksum=None,relative=None):
   d={'name':name,'version':version,'source':source,'declared_license':license,'cargo_manifest_sha256':g.sha(manifest),'package_checksum_sha256':checksum}
   if relative is not None:d['manifest_relpath']=relative
   return d
  self.rows=[row('voicevox_benchmark','0.0.0',None,self.wrapper,None,relative='wrapper/Cargo.toml'),row('crate','1.0.0',g.REGISTRY,self.crate,'MIT',g.sha(self.archive)),row('gitcrate','1.0.0',self.gitsource,self.gitroot/'Cargo.toml','MIT',relative='Cargo.toml')]
  self.inv={'cargo_lock_sha256':g.sha(self.source/'Cargo.lock'),'workspace_cargo_manifest_sha256':g.sha(self.source/'Cargo.toml'),'workspace_cargo_config_sha256':g.sha(self.source/'.cargo/config.toml'),'workspace_manifests':{'wrapper/Cargo.toml':g.sha(self.wrapper)},'workspace_configs':{'.cargo/config.toml':g.sha(self.source/'.cargo/config.toml')},'cargo_selection':dict(g.SELECTION),'cargo_packages':self.rows,'frozen_cargo_graph':{'schema':'cargo-notice-selection-v1','selected_packages':3,'package_identities':[list(g.package_key(p)) for p in self.rows]},'rust_library_cargo_lock_sha256':g.sha(self.library/'Cargo.lock'),'rust_source_notices':[{'source_path':'LICENSE','sha256':g.sha(self.library/'LICENSE')}],'build_std_packages':[row('stdcrate','2.0.0',g.REGISTRY,self.stdcrate,'MIT',g.sha(self.stdarchive))],'git_workspace_manifests':{self.gitsource:{'Cargo.toml':g.sha(self.gitroot/'Cargo.toml')}},'open_jtalk_commit':'b'*40,'open_jtalk_notices':[{'source_path':'COPYING','sha256':g.sha(self.native/'COPYING')}]}
  self.meta={'packages':[{'name':'voicevox_benchmark','version':'0.0.0','source':None,'license':None,'manifest_path':str(self.wrapper)}]}
 def tearDown(self):self.tmp.cleanup()
 def run_gate(self):
  def run(args,**kwargs):
   self.assertEqual(args[0],'git');directory=pathlib.Path(args[2]);return type('Result',(),{'returncode':0,'stdout':(directory/'.revision').read_text()+'\n'})()
  def command(args,env,*,cwd=None):
   self.assertEqual(cwd,self.source);self.assertIn('--no-deps',args);self.assertIn('--offline',args);self.assertIn('--locked',args);self.assertNotIn('tree',args);self.assertEqual(env['CARGO_HOME'],str(self.cache/'cargo'));self.assertEqual(env['RUSTUP_HOME'],str(self.cache/'rustup'));return json.dumps(self.meta)
  with patch.object(g,'command',side_effect=command),patch.object(g.subprocess,'run',side_effect=run):return g.verify_dependencies(self.cache,self.source,self.inv)
 def fails(self,code):self.assertRaisesRegex(g.GateError,code,self.run_gate)
 def test_valid_sparse_source_inventory(self):r=self.run_gate();self.assertEqual(r['cargo_packages'],3);self.assertEqual(r['registry_archives_verified'],2);self.assertEqual(r['observed_git_packages'],1)
 def test_lock_changed(self):write(self.source/'Cargo.lock',b'changed');self.fails('core_cargo_lock')
 def test_workspace_root_changed(self):write(self.source/'Cargo.toml',b'changed');self.fails('workspace_manifest')
 def test_workspace_member_changed(self):write(self.wrapper,b'changed');self.fails('workspace_member_manifest')
 def test_workspace_member_added(self):self.meta['packages'].append(dict(self.meta['packages'][0],name='other',manifest_path=str(self.source/'other/Cargo.toml')));self.fails('workspace_member_set')
 def test_workspace_config_changed(self):write(self.source/'.cargo/config.toml',b'changed');self.fails('workspace_config')
 def test_workspace_config_added(self):write(self.source/'wrapper/.cargo/config.toml',b'new');self.fails('workspace_config')
 def test_cargo_home_config_added(self):write(self.cache/'cargo/config.toml',b'[source.crates-io]\nreplace-with="other"');self.fails('unrecorded_cargo_config')
 def test_ancestor_config_added(self):write(self.root/'.cargo/config.toml',b'[source.crates-io]\nreplace-with="other"');self.fails('unrecorded_cargo_config')
 def test_selection_extra_feature(self):self.inv['cargo_selection']=dict(g.SELECTION,features=['browser','native','threaded']);self.fails('cargo_selection')
 def test_frozen_graph_missing_package(self):self.inv['frozen_cargo_graph']['package_identities'].pop();self.fails('frozen_graph_package_set')
 def test_inventory_missing_package(self):self.rows.pop();self.fails('frozen_graph_schema_or_count')
 def test_inventory_duplicate_package(self):self.rows.append(dict(self.rows[-1]));self.fails('duplicate_cargo_package')
 def test_frozen_graph_duplicate_package(self):self.inv['frozen_cargo_graph']['package_identities'].append(list(self.inv['frozen_cargo_graph']['package_identities'][0]));self.fails('frozen_graph_package_set')
 def test_registry_source_missing(self):self.crate.unlink();self.fails('registry_crate_source_missing')
 def test_registry_source_ambiguous(self):write(self.cache/'cargo/registry/src/index.crates.io-second/crate-1.0.0/Cargo.toml',self.crate.read_bytes());self.fails('registry_crate_source_missing_or_ambiguous')
 def test_registry_archive_missing(self):self.archive.unlink();self.fails('registry_archive_missing')
 def test_registry_archive_changed(self):write(self.archive,b'changed');self.fails('registry_archive_checksum')
 def test_registry_archive_ambiguous(self):write(self.cache/'cargo/registry/cache/index.crates.io-second/crate-1.0.0.crate',self.archive.read_bytes());self.fails('registry_archive_missing_or_ambiguous')
 def test_registry_manifest_changed(self):write(self.crate,self.crate.read_bytes()+b'edition="2021"\n');self.fails('cargo_manifest')
 def test_registry_license_changed(self):write(self.crate,self.crate.read_bytes().replace(b'MIT',b'BSD'));self.fails('cargo_license_metadata')
 def test_registry_identity_changed(self):write(self.crate,self.crate.read_bytes().replace(b'1.0.0',b'1.0.1'));self.fails('cargo_package_identity')
 def test_inventory_source_changed(self):self.rows[1]['source']='registry+https://example.invalid';self.fails('frozen_graph_package_set')
 def test_git_revision_changed(self):write(self.gitroot/'.revision',b'c'*40);self.fails('git_source_missing')
 def test_git_manifest_changed(self):write(self.gitroot/'Cargo.toml',b'changed');self.fails('git_workspace_manifest')
 def test_std_lock_changed(self):write(self.library/'Cargo.lock',b'changed');self.fails('rust_source_lock')
 def test_std_notice_changed(self):write(self.library/'LICENSE',b'changed');self.fails('rust_source_notice')
 def test_std_archive_changed(self):write(self.stdarchive,b'changed');self.fails('registry_archive_checksum')
 def test_std_manifest_changed(self):write(self.stdcrate,self.stdcrate.read_bytes()+b'edition="2021"\n');self.fails('cargo_manifest')
 def test_std_inventory_unlocked(self):self.inv['build_std_packages'][0]['version']='3.0.0';self.fails('build_std_package_not_locked')
 def test_uncovered_target_crate(self):write(self.target/'deps/extra.d',b'/old/cache/cargo/registry/src/index.crates.io-fixture/uncovered-1.0.0/src/lib.rs\n');self.fails('uncovered_compiled_crate')
 def test_uncovered_host_build_script(self):write(self.host/'build/extra-123/build.d',b'/old/cache/cargo/registry/src/index.crates.io-fixture/uncovered-1.0.0/build.rs\n');self.fails('uncovered_compiled_crate')
 def test_replaced_registry_identity(self):write(self.target/'deps/extra.d',b'/old/cache/cargo/registry/src/private-registry/crate-1.0.0/src/lib.rs\n');self.fails('uncovered_compiled_crate')
 def test_uncovered_git_crate(self):write(self.target/'deps/extra.d',b'/old/cache/cargo/git/checkouts/uncovered-123/defabcd/src/lib.rs\n');self.fails('uncovered_compiled_git_crate')
 def test_missing_target_depfiles(self):(self.target/'deps/wrapper.d').unlink();self.fails('target_dependency_files_missing')
 def test_missing_host_depfiles(self):(self.host/'deps/helper.d').unlink();self.fails('host_dependency_files_missing')
 def test_missing_host_build_script_depfiles(self):(self.host/'build/helper-123/build_script.d').unlink();self.fails('host_build_script_dependency_files_missing')
 def test_wrong_host_toolchain(self):
  toolchain=self.library.parents[4];toolchain.rename(toolchain.with_name('1.96.0-aarch64-unknown-linux-gnu'));self.fails('rust_host_toolchain_mismatch')
 def test_empty_registry_evidence(self):write(self.target/'deps/wrapper.d',b'unrelated evidence');self.fails('target_registry_evidence_empty')
 def test_native_revision_changed(self):write(self.native/'.revision',b'c'*40);self.fails('open_jtalk_native_commit')
 def test_native_notice_changed(self):write(self.native/'COPYING',b'changed');self.fails('open_jtalk_notice')

 def test_valid_cwd_source(self):
  with patch.object(g.pathlib.Path,'cwd',return_value=self.source):self.assertTrue(self.run_gate()['frozen_selection_verified'])
 def test_valid_cwd_source_descendant(self):
  with patch.object(g.pathlib.Path,'cwd',return_value=self.wrapper.parent):self.assertTrue(self.run_gate()['frozen_selection_verified'])
 def test_nested_uncovered_git_package(self):
  write(self.gitroot/'backend/Cargo.toml',b'[package]\nname="uncovered"\nversion="1.0.0"\n')
  write(self.target/'deps/extra.d',(str(self.gitroot/'backend/src/lib.rs')+'\n').encode());self.fails('uncovered_compiled_git_crate')
 def test_registry_outside_verified_cache(self):
  write(self.target/'deps/extra.d',b'/outside/cache/cargo/registry/src/index.crates.io-fixture/crate-1.0.0/src/lib.rs\n');self.fails('compiled_registry_source_path_mismatch')
 def test_git_outside_verified_cache(self):
  write(self.target/'deps/extra.d',b'/outside/cache/cargo/git/checkouts/gitcrate-fixture/aaaaaaa/src/lib.rs\n');self.fails('uncovered_compiled_git_crate')

class BuildSelectionTests(unittest.TestCase):
 valid='''Running `rustc --crate-name voicevox_benchmark --target wasm32-unknown-emscripten --cfg 'feature="browser"' --cfg 'feature="threaded"'`\n'''
 def test_exact_features(self):self.assertEqual(g.verify_build_selection(self.valid)['features'],['browser','threaded'])
 def test_missing_command(self):self.assertRaisesRegex(g.GateError,'command_missing',g.verify_build_selection,'Fresh voicevox_benchmark v0.0.0')
 def test_duplicate_command(self):self.assertRaisesRegex(g.GateError,'command_missing_or_ambiguous',g.verify_build_selection,self.valid*2)
 def test_wrong_target(self):self.assertRaisesRegex(g.GateError,'target_mismatch',g.verify_build_selection,self.valid.replace('wasm32-unknown-emscripten','x86_64-unknown-linux-gnu'))
 def test_missing_feature(self):self.assertRaisesRegex(g.GateError,'features_mismatch',g.verify_build_selection,self.valid.replace(' --cfg \'feature="threaded"\'',''))
 def test_extra_feature(self):self.assertRaisesRegex(g.GateError,'features_mismatch',g.verify_build_selection,self.valid.replace('`\n',' --cfg \'feature="native"\'`\n'))

 def test_inline_features_and_target(self):
  inline=self.valid.replace('--cfg ', '--cfg=').replace('--target ', '--target=').replace('--crate-name ', '--crate-name=')
  self.assertEqual(g.verify_build_selection(inline)['features'],['browser','threaded'])
 def test_extra_inline_feature(self):self.assertRaisesRegex(g.GateError,'features_mismatch',g.verify_build_selection,self.valid.replace('`\n', ' --cfg=\'feature="native"\'`\n'))
 def test_extra_inline_target(self):self.assertRaisesRegex(g.GateError,'target_mismatch',g.verify_build_selection,self.valid.replace('`\n', ' --target=x86_64-unknown-linux-gnu`\n'))
 def test_extra_custom_cfg(self):self.assertRaisesRegex(g.GateError,'features_mismatch',g.verify_build_selection,self.valid.replace('`\n', ' --cfg unexpected`\n'))
 def test_response_file(self):self.assertRaisesRegex(g.GateError,'response_file',g.verify_build_selection,self.valid.replace('`\n', ' @unverified.rsp`\n'))
 def test_ambiguous_mixed_command_spelling(self):self.assertRaisesRegex(g.GateError,'command_missing_or_ambiguous',g.verify_build_selection,self.valid+self.valid.replace('--crate-name ', '--crate-name='))

if __name__=='__main__':unittest.main()
