#!/usr/bin/env python3
"""Strict nine-pair screen validation and bounded numeric logging."""
import argparse,json,math,statistics,re
from pathlib import Path
MODES=('original','candidate');FLAGS=['--js-flags=--no-wasm-revectorize']
def need(ok,msg):
 if not ok:raise ValueError(msg)
def schedule():
 orders=[['original','candidate'],['candidate','original'],['original','candidate'],['candidate','original'],['original','candidate'],['candidate','original'],['original','candidate'],['candidate','original'],['original','candidate']]
 return [{'process_set':i//3+1,'pair':i%3+1,'order':order} for i,order in enumerate(orders)]
def validate(r):
 need(r['status']=='complete' and r['normal_tiering'] and r['browser_flags']==FLAGS and r['private_temporary_directory_deleted'],'Completion/configuration')
 need(r['schedule']==schedule() and r['warmups_per_browser']==5,'Schedule/warmups');need(len(r['trials'])==18 and len(r['process_sets'])==3,'Bounded sample matrix');gate=r['gate_reference'];need(gate==json.loads((Path(__file__).parent/'gate_reference.json').read_text()),'Frozen gate evidence');need(gate['passed'] and gate['requested_browser_flags']==FLAGS,'Gate reference')
 need(gate['activation']['transformed_groups']==0 and not gate['activation']['flag_rejected'] and gate['activation']['launch_configuration_matches'],'OFF proof')
 for k in ['source_wasm_sha256','candidate_wasm_sha256','source_js_sha256','original_body_sha256','candidate_body_sha256']:need(r['transform'][k]==gate[k],'Artifact gate '+k)
 need(r['model_sha256']==gate['model_sha256'] and r['query_sha256']==gate['query_sha256'],'Model/query pin');need(r['transform']['all_other_functions_sections_identical'],'Single-body transform')
 def timing(x):need(type(x['elapsed_s']) in (int,float) and math.isfinite(x['elapsed_s']) and x['elapsed_s']>0 and x['wav_bytes']==478252,'Timing validity')
 expected=[(x['process_set'],x['pair'],m) for x in schedule() for m in x['order']];need([(x['process_set'],x['pair'],x['mode']) for x in r['trials']]==expected,'Actual balanced order')
 identities=[]
 for group in r['process_sets']:
  need(group['id']==r['process_sets'].index(group)+1,'Process set order');need(set(group['runtime'])==set(MODES),'Runtime matrix')
  for mode,v in group['runtime'].items():
   identities.append((v['browser_pid'],v['browser_created']));need(v['engine']==gate['engine'] and v['launch_flags']==FLAGS,'Engine/flags');init=v['initialization'];need(init['ready'] and init['timing_instrumentation']=='clock_only','No primary hook');need(init['runtime']==gate['runtime'] and init['dispatch']==gate['dispatch'],'Runtime/dispatch');need(init['wasm_sha256']==gate['source_wasm_sha256' if mode=='original' else 'candidate_wasm_sha256'] and init['js_sha256']==gate['source_js_sha256'],'Loaded module')
  need(len(group['warmups'])==10 and {(x['mode'],x['iteration']) for x in group['warmups']}=={(m,i) for m in MODES for i in range(1,6)},'Warmups');[timing(x) for x in group['warmups']]
  need(len(group['sentinels'])==2 and [x['position'] for x in group['sentinels']]==['before','after'],'Sentinels');[timing(x) for x in group['sentinels']]
  need(len(group['checks'])==8 and {(x['mode'],x['phase'],x['iteration']) for x in group['checks']}=={(m,p,i) for m in MODES for p in ['before','after'] for i in [1,2]},'Pre/post repeated check matrix')
  for x in group['checks']:
   need(x['finite'] and x['exact_bytes'] and x['wav_bytes']==478252 and x['raw_bytes']==956416,'Exact outputs')
   for k,v in gate['outputs'].items():need(x[k]==v,'Output hash')
  need(set(group['cleanup'])==set(MODES) and all(x['confirmed'] and x['remaining_processes']==0 and x['observed_processes']>0 for x in group['cleanup'].values()),'Process cleanup')
 need(len(set(identities))==6,'Fresh browser identities')
 for row in r['trials']:
  timing(row);block=next(x for x in schedule() if (x['process_set'],x['pair'])==(row['process_set'],row['pair']));need(row['pair_order']==block['order'],'Order receipt');need('before' in row and 'after' in row and 'resource_observations' in row,'Resource checkpoints');need(min(row['before']['host']['available_bytes'],row['after']['host']['available_bytes'])>=1024**3,'Low-memory stop expected')
  for point in ['before','after']:need(bool(row[point]['processes']),'Missing target process resources')
 # Strict nested allowlists prevent unrelated payloads in console records.
 def keys(value,allowed):need(isinstance(value,dict) and not(set(value)-set(allowed.split())),'Unexpected nested payload')
 keys(r['transform'],'source_wasm_sha256 candidate_wasm_sha256 original_body_sha256 candidate_body_sha256 absolute_function_index defined_function_index imported_function_count callback_table abi source_bytes candidate_bytes insertion_body_offset changed_length_fields noop_roundtrip_sha256 all_other_functions_sections_identical custom_sections source_receipt_sha256 source_js_sha256 archive_sha256 transform_source_sha256 session_id scope')
 need(r['transform']['abi']=={'params':['i32']*3,'results':[]} and r['transform']['callback_table']==gate['callback_table'] and r['transform']['absolute_function_index']==gate['absolute_function_index'] and r['transform']['custom_sections']==[],'Transform metadata')
 for field in r['transform']['changed_length_fields']:keys(field,'kind file_offset code_payload_offset before after')
 for name,digest in r['source_hashes'].items():need(Path(name).name==name and re.fullmatch('[a-f0-9]{64}',digest),'Source hash')
 from resources import validate_host,validate_snapshot,validate_processes,idle_cpu
 validate_host(r['initial_host'])
 keys(r['environment'],'os architecture cpu available_logical_cpus host_logical_cpus python core_commit onnxruntime_version onnxruntime_builder_commit uv rust emscripten affinity_logical_cpus cgroup_cpu_quota memory_gib')
 need(bool(r['environment']['cpu']) and bool(r['environment']['os']) and r['environment']['available_logical_cpus']>=1 and r['environment']['affinity_logical_cpus']>=1,'CPU environment identity')
 need(r['build_toolchain']=={'core':'9b539761f3e152b966e08c2de0784129fe8cf68d','rust':'1.96.0','emscripten':'4.0.8','runtime':'0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf','optimization':'z','graph_level':1},'Build toolchain')
 for group in r['process_sets']:
  keys(group,'id runtime warmups checks sentinels cleanup idle')
  idle=group['idle'];keys(idle,'interval_s modes');need(math.isfinite(idle['interval_s']) and idle['interval_s']>=1 and set(idle['modes'])==set(MODES),'Untimed idle interval')
  for m,x in idle['modes'].items():
   keys(x,'before after cpu_seconds_delta cpu_percent unmatched_processes');validate_processes(x['before']);validate_processes(x['after']);need({k:x[k] for k in ['cpu_seconds_delta','cpu_percent','unmatched_processes']}==idle_cpu(x['before'],x['after'],idle['interval_s']),'Idle CPU derivation')
  for v in group['runtime'].values():keys(v,'browser_pid browser_created engine launch_flags initialization');keys(v['initialization'],'ready runtime dispatch wasm_sha256 js_sha256 timing_instrumentation')
  for x in group['warmups']:keys(x,'mode iteration elapsed_s wav_bytes')
  for x in group['sentinels']:keys(x,'position elapsed_s wav_bytes')
  for x in group['checks']:keys(x,'mode phase iteration wav_bytes raw_bytes wav_sha256 raw_sha256 exact_bytes pcm_sha256 finite')
  for x in group['cleanup'].values():keys(x,'observed_processes remaining_processes confirmed')
 for row in r['trials']:
  keys(row,'process_set pair mode pair_order elapsed_s wav_bytes before after resource_observations')
  validate_snapshot(row['before']);validate_snapshot(row['after'])
  from resources import resource_observations
  need(row['resource_observations']==resource_observations(row['before'],row['after']),'Resource delta derivation')
 # This is a numeric/hash-only format. No arbitrary unvalidated payload gets logged.
 allowed={
  'schema status schedule warmups_per_browser normal_tiering browser_flags gate_reference source_hashes process_sets trials notes transform query_sha256 model_sha256 initial_host private_temporary_directory_deleted environment build_toolchain',
 }
 need(not(set(r)-set(next(iter(allowed)).split())),'Unexpected root payload')
 forbidden={'wav','raw','pcm','wav_base64','audio_outputs','stdout','stderr','tensor','samples'}
 def walk(x):
  if isinstance(x,dict):
   need(not(forbidden&set(x)),'Private payload field')
   for v in x.values():walk(v)
  elif isinstance(x,list):
   for v in x:walk(v)
  elif isinstance(x,str):need('data:audio/' not in x,'Embedded audio')
 walk(r)
 return True
def summarize(r):
 validate(r);pairs=[]
 for block in schedule():
  rows={x['mode']:x for x in r['trials'] if (x['process_set'],x['pair'])==(block['process_set'],block['pair'])};pairs.append({'process_set':block['process_set'],'pair':block['pair'],'candidate_over_original':rows['candidate']['elapsed_s']/rows['original']['elapsed_s']})
 sets=[]
 for group in r['process_sets']:
  ratios=[x['candidate_over_original'] for x in pairs if x['process_set']==group['id']];sets.append({'process_set':group['id'],'paired_ratios':ratios,'geometric_mean_ratio':math.exp(statistics.mean(math.log(x) for x in ratios)),'median_ratio':statistics.median(ratios),'baseline_sentinel_after_over_before':group['sentinels'][1]['elapsed_s']/group['sentinels'][0]['elapsed_s']})
 return {'passed':True,'scope':'Nine-pair screen only; three process-set clusters; no confirmed effect claim','primary_calls':18,'pairs':9,'process_set_clusters':3,'ab_pairs':5,'ba_pairs':4,'median_pair_ratio':statistics.median(x['candidate_over_original'] for x in pairs),'geometric_mean_pair_ratio':math.exp(statistics.mean(math.log(x['candidate_over_original']) for x in pairs)),'process_sets':sets,'fault_exposed_primary_calls':sum(x['resource_observations']['fault_exposed'] for x in r['trials'])}
def main():
 p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();r=json.loads(a.input.read_text());summary=summarize(r);a.output.write_text(json.dumps(summary,indent=2)+'\n');print('INPLACE_TIMING_SUMMARY '+json.dumps(summary,separators=(',',':')))
 # Print all validated records in bounded lines; artifact access is not required.
 print('INPLACE_TIMING_META '+json.dumps({k:v for k,v in r.items() if k not in ['trials','process_sets']},separators=(',',':')))
 for group in r['process_sets']:
  print('INPLACE_TIMING_SET '+json.dumps({k:v for k,v in group.items() if k not in ['warmups','checks']},separators=(',',':')))
  for x in group['warmups']:print('INPLACE_TIMING_WARMUP '+json.dumps({'process_set':group['id'],**x},separators=(',',':')))
  for x in group['checks']:print('INPLACE_TIMING_CHECK '+json.dumps({'process_set':group['id'],**x},separators=(',',':')))
 for x in r['trials']:print('INPLACE_TIMING_TRIAL '+json.dumps(x,separators=(',',':')))
if __name__=='__main__':main()
