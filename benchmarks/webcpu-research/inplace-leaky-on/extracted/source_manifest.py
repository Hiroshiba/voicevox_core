"""Read-only verification and bounded public provenance for source confirmation."""
import hashlib,json
from pathlib import Path
import callback_discovery
from callback_discovery import discover
KEYS=('original','rebuilt','candidate')
HARNESS_SHA='d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe'
MODEL_SHA='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9'
QUERY_SHA='3d3b07b94d5a1159994888d72b4e9f3362acec551f2bf5275eb71d8fa6802d63'
LOADSPLAT_SHA='0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf'
CPU_SHA='0d62a53ddb7924704abb640daa55153e15f0cc1eca63d5f2a191f23654f7b57a'
ORIGINAL_BODY='30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6'
CANDIDATE_BODY='5b2c6e2ddf5d836b4bde745de8fe76b845014669a9718d574fbed5dd5df27c3b'
FLAGS=['--js-flags=--no-wasm-revectorize']
PINS=json.loads((Path(callback_discovery.__file__).parent/'source_pins.json').read_text())
SOURCE_REFS={k:PINS[k] for k in ('source_commit','source_tar_sha256','archive_builder_commit','generic_harness_builder_label','lineage','emsdk_version','emsdk_commit','llvm_commit','pristine_runtime_archive_sha256','loadsplat_runtime_archive_sha256','source_patch_sha256','release_patch_sha256','harness_sha256','runtime_preparer_sha256')}
def need(ok,message):
 if not ok:raise ValueError(message)
def sha(data):return hashlib.sha256(data).hexdigest()
def read_receipt(binary):
 binary=Path(binary);path=binary.parent/'complete.json';r=json.loads(path.read_text());need(binary.name in r['files'] and binary.with_suffix('.wasm').name in r['files'],'Missing JS/Wasm receipt')
 for name,digest in r['files'].items():need(Path(name).name==name and sha((binary.parent/name).read_bytes())==digest,'Artifact receipt mismatch')
 return r,sha(path.read_bytes())
def verify_manifest(path,cpu_override=None):
 man=json.loads(Path(path).read_text());need(man['schema']=='inplace-leaky-source-manifest-v1' and man.get('verified') is True,'Source manifest verification')
 need([x['key'] for x in man['variants']]==list(KEYS),'Three control order');need(man['model_sha256']==MODEL_SHA and man['fixture_query_sha256']==QUERY_SHA and man['research_harness_sha256']==HARNESS_SHA,'Input pins')
 need(man['source_refs']==SOURCE_REFS,'Source provenance pins')
 p=man['source_proofs'];need(p['original_control']['passed'] and p['original_control']['function_definition_count']==372 and p['original_control']['all_function_definitions_exact_IR_equal_without_normalization'],'Source control proof')
 need(p['candidate_scope']['passed'] and p['candidate_scope']['definition_count']==372 and p['candidate_scope']['counts']=={'raw_IR_identical':295,'metadata_ID_renumbering_only':73,'diagnostic_line_only':2,'intended_LeakyRelu_exact_alias_change':2},'Candidate scope proof')
 need(p['original_control']['normalized_IR_sha256']=='1ddac01abf505708e17ad0f22cafcbc178e428cf1c170b9bb89ffff4110f3fcc' and p['candidate_scope']['normalized_IR_sha256']=='e3679458624f93875f2eb22c555f73a7b93634f67c1bbdca40c94bd4224abca7','Complete IR pins')
 need(p['all_element_segments_identical_across_three_modules'] and p['untouched_and_rebuilt_callback_bytes_identical'],'Table/source parity')
 entries={};provenance={}
 for e in man['variants']:
  k=e['key'];binary=Path(e['binary']);model=Path(e['model']);r,receipt_sha=read_receipt(binary);identity=r['identity'];need(identity==e['build_identity'] and receipt_sha==e['complete_receipt_sha256'],'Build identity');need(identity['runtime']==e['archive_sha256'] and identity['kind']=='browser-mt-xnnpack' and identity['optimization']=='z' and identity['graph_level']==1,'Runtime build policy')
  need(sha(model.read_bytes())==MODEL_SHA and e['model_sha256']==MODEL_SHA,'Model hash');need(e['threads']==2 and e['ort_threads']==1 and e['xnn_threads']==2 and e['spin_off'] and e['fixed_shape'] and e['fixed_length']==962 and e['dispatch_expected']=='loadsplat' and e['revectorize'] is False,'Source runtime configuration')
  if k=='original':need(identity['runtime']==LOADSPLAT_SHA,'Forced loadsplat baseline')
  bundle=json.loads((binary.parent/'native-bundle-verification.json').read_text());need(bundle['verified'] and bundle['runtime_sha256']==identity['runtime'] and set(bundle['invalidated_packages'])=={'voicevox_core','voicevox_benchmark'},'Native bundle')
  if k!='original':need(bundle['activation_occurrence']==1 and bundle['all_ordered_native_members_verified']==1438 and bundle['activation_member_sha256']==e['activation_member_sha256'],'Indexed activation bundle')
  wasm=binary.with_suffix('.wasm').read_bytes();expected=CANDIDATE_BODY if k=='candidate' else ORIGINAL_BODY;sections,_,target,slot,imports,_=discover(wasm,expected,918 if k=='candidate' else 344)
  need(target['index']==e['callback_function_index'] and slot['slot']==e['callback_table_slot'] and e['callback_body_sha256']==expected and imports==52,'Actual callback mapping')
  item={'wasm_sha256':sha(wasm),'js_sha256':sha(binary.read_bytes()),'callback_body_sha256':expected,'callback_table':slot,'absolute_function_index':target['index']};entries[k]={'binary':binary,'model':model,'worker':item}
  provenance[k]={'receipt_sha256':receipt_sha,'archive_sha256':identity['runtime'],'wasm_sha256':item['wasm_sha256'],'js_sha256':item['js_sha256'],'callback_body_sha256':expected,'callback_table_slot':slot['slot'],'callback_function_index':target['index'],'build_toolchain':{x:identity[x] for x in ['core','rust','emscripten','optimization','graph_level']},'native_bundle':{x:bundle[x] for x in ['verified','runtime_sha256','dispatcher_sha256','invalidated_packages']+(['activation_member_sha256','activation_occurrence','duplicate_activation_sha256','all_ordered_native_members_verified'] if k!='original' else[])}}
 cpu=Path(cpu_override or man.get('cpu_reference',{}).get('binary',''));need(cpu.is_file(),'Original CPU reference artifact required before confirmation');r,receipt_sha=read_receipt(cpu);identity=r['identity'];need(identity['runtime']==CPU_SHA and identity['kind']=='browser-mt' and not identity['xnnpack'] and identity['optimization']=='z' and identity['graph_level']==1,'Original CPU artifact pin')
 entries['cpu']={'binary':cpu,'model':entries['original']['model'],'worker':{'wasm_sha256':sha(cpu.with_suffix('.wasm').read_bytes()),'js_sha256':sha(cpu.read_bytes())}}
 provenance['cpu']={'receipt_sha256':receipt_sha,'archive_sha256':CPU_SHA,**entries['cpu']['worker'],'build_toolchain':{x:identity[x] for x in ['core','rust','emscripten','optimization','graph_level']}}
 summary={'source_refs':man['source_refs'],'preparation_hashes':{k:man[k] for k in ['preparer_sha256','builder_source_sha256','source_pins_sha256','source_proof_helper_sha256','callback_discovery_sha256']},'original_control':{k:v for k,v in p['original_control'].items() if k!='normalizations'},'candidate_scope':{k:v for k,v in p['candidate_scope'].items() if k not in ['normalizations','diagnostic_line_changes']},'rebuilt_dependency_count':p['rebuilt_dependency_count'],'candidate_dependency_count':p['candidate_dependency_count'],'all_element_segments_identical':True,'manifest_sha256':sha(Path(path).read_bytes())}
 return entries,provenance,summary
