#!/usr/bin/env python3
import argparse,json,re
from pathlib import Path
from browser_gate import check_alias
from prepare_inplace_leaky import require
HEX=re.compile('[a-f0-9]{64}')
def validate(r):
 def keys(x,allowed):require(isinstance(x,dict) and not(set(x)-set(allowed.split())), 'Unexpected report field')
 keys(r,'schema scope normal_tiering requested_browser_flags session_id variants passed transform activation private_temporary_directory_deleted')
 keys(r['activation'],'product js_version transformed_groups revectorizable_nodes flag_rejected launch_configuration_matches trace_sha256')
 keys(r['transform'],'source_wasm_sha256 candidate_wasm_sha256 original_body_sha256 candidate_body_sha256 absolute_function_index defined_function_index imported_function_count callback_table abi source_bytes candidate_bytes insertion_body_offset changed_length_fields noop_roundtrip_sha256 all_other_functions_sections_identical custom_sections source_receipt_sha256 source_js_sha256 archive_sha256 transform_source_sha256 session_id scope')
 require(r['transform'].get('abi')=={'params':['i32']*3,'results':[]},'ABI proof')
 keys(r['transform'].get('callback_table'),'table slot');require(r['transform']['callback_table']['table']==0 and isinstance(r['transform']['callback_table']['slot'],int) and r['transform']['callback_table']['slot']>=0,'Table proof')
 require(r['transform'].get('custom_sections')==[],'Custom sections require review')
 for field in r['transform'].get('changed_length_fields',[]):keys(field,'kind file_offset code_payload_offset before after')
 for variant,value in r['variants'].items():
  keys(value,'initialization checks');keys(value['initialization'],'ready runtime dispatch wasm_sha256 callback_identity_checked')
  keys(value['initialization']['dispatch'],'schema_version source context expected_dispatch actual_dispatch worker_hardware_concurrency probe_hardware_concurrency is_x86 relaxed_simd mr nr splat_pointers loadsplat_pointers checked_pointers probe_return_code cross_origin_isolated shared_memory verified')
  for check in value['checks']:
   keys(check,'index wav_bytes raw_bytes wav_sha256 pcm_sha256 raw_sha256 exact_bytes_to_first alias')
   for alias in check['alias']:keys(alias,'variant table_slot function_index callbacks elements classes predicted_branch length_histogram restored metadata_errors scope')
 require(r.get('passed') is True and r.get('normal_tiering') is True and r.get('private_temporary_directory_deleted') is True,'Completion/privacy gate')
 require(r['requested_browser_flags']==['--js-flags=--no-wasm-revectorize'],'Normal-tier OFF flags')
 a=r['activation'];require(a['transformed_groups']==0 and a['revectorizable_nodes']==0 and not a['flag_rejected'] and a['launch_configuration_matches'],'OFF activation')
 t=r['transform'];require(t['all_other_functions_sections_identical'] and t['candidate_bytes']-t['source_bytes']==6 and t['original_body_sha256']=='30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6','Transformation');require(t['noop_roundtrip_sha256']==t['source_wasm_sha256'],'Roundtrip')
 require(set(r['variants'])=={'original','candidate'},'Variants');hashes={k:set() for k in ['raw_sha256','pcm_sha256','wav_sha256']}
 for variant,v in r['variants'].items():
  init=v['initialization'];require(init['ready'] and init['callback_identity_checked'],'Init identity');require(init['wasm_sha256']==t['source_wasm_sha256' if variant=='original' else 'candidate_wasm_sha256'],'Wasm identity')
  runtime=init['runtime'];require(runtime=={'shared_memory':True,'pthreads':1,'spin_off':True,'fixed_length':962,'fixed_matches':1,'xnn_threads':2,'xnn_sessions':1,'configured_ort_global_threads':1},'Runtime')
  dispatch=init['dispatch'];require(dispatch['verified'] and dispatch['actual_dispatch']=='loadsplat' and dispatch['loadsplat_pointers']==12 and dispatch['splat_pointers']==0 and dispatch['checked_pointers']==12 and dispatch['relaxed_simd'] is False,'Dispatch')
  require(len(v['checks'])==3,'Repeats')
  for index,c in enumerate(v['checks']):
   require(c['index']==index and c['exact_bytes_to_first'] and c['wav_bytes']==478252 and c['raw_bytes']==956416,'Outputs')
   for key in hashes:require(HEX.fullmatch(c[key]),'Hash shape');hashes[key].add(c[key])
   require(len(c['alias'])==2,'Alias calls')
   for x in c['alias']:check_alias(x,variant,t)
 require(all(len(v)==1 for v in hashes.values()),'Output equality')
 # Disallow binary output encodings/measurements and arbitrary log fields recursively.
 forbidden={'wav','raw','pcm','wav_base64','audio_outputs','stdout','stderr','elapsed_s','rtf','timing'}
 def walk(value):
  if isinstance(value,dict):
   require(not(forbidden & set(value)),'Forbidden report field')
   for v in value.values():walk(v)
  elif isinstance(value,list):
   for v in value:walk(v)
  elif isinstance(value,str):require('data:audio/' not in value,'Embedded audio')
 walk(r)
 return {'passed':True,'normal_tiering':True,'revectorization_off':True,'variants':2,'repeats_per_variant':3,'raw_and_wav_calls_per_variant':6,'callbacks_each':69,'inplace_callbacks_each':41,'candidate_simd_callbacks_each':69,'module_growth_bytes':6,'output_hashes':{k:next(iter(v)) for k,v in hashes.items()},'source_wasm_sha256':t['source_wasm_sha256'],'candidate_wasm_sha256':t['candidate_wasm_sha256'],'private_temporary_directory_deleted':True}
def main():
 p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('--validated-output',type=Path,required=True);a=p.parse_args();r=json.loads(a.input.read_text());summary=validate(r);a.validated_output.parent.mkdir(parents=True,exist_ok=True);a.validated_output.write_text(json.dumps(summary,indent=2)+'\n');print('INPLACE_LEAKY_GATE '+json.dumps(summary,separators=(',',':')))
 # Emit only after complete validation; bounded records retain independent evidence if artifact download is unavailable.
 print('INPLACE_LEAKY_META '+json.dumps({k:v for k,v in r.items() if k!='variants'},separators=(',',':')))
 for variant,entry in r['variants'].items():
  print('INPLACE_LEAKY_INIT '+json.dumps({'variant':variant,**entry['initialization']},separators=(',',':')))
  for check in entry['checks']:print('INPLACE_LEAKY_CHECK '+json.dumps({'variant':variant,**check},separators=(',',':')))
if __name__=='__main__':main()
