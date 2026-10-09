#!/usr/bin/env python3
"""Three-control confirmation validation, paired summaries and privacy-safe logging."""
import argparse,itertools,json,math,random,re,statistics
from pathlib import Path
from source_manifest import KEYS,FLAGS,QUERY_SHA,MODEL_SHA,LOADSPLAT_SHA,CPU_SHA,ORIGINAL_BODY,CANDIDATE_BODY,SOURCE_REFS,need
from source_resources import validate_host,validate_snapshot,validate_processes,idle_cpu,resource_observations
GOLD={'raw_sha256':'410a3a55cf81b3e35d003ce752aaaf613a5600cfeddf82c7c6970928a5a646d8','pcm_sha256':'100890180c1067bcc316674be9012130157beb18f794688920fbfccd1dbb51a0','wav_sha256':'a657c8dddfbebe6796739fd6090f4adeea3a1af05acc1e3db0da8b70b1b09576'}
XNN_RUNTIME={'shared_memory':True,'pthreads':1,'spin_off':True,'fixed_length':962,'fixed_matches':1,'xnn_threads':2,'xnn_sessions':1,'configured_ort_global_threads':1}
def schedule():
 orders=list(itertools.permutations(KEYS))*2+[KEYS,KEYS[1:]+KEYS[:1],KEYS[2:]+KEYS[:2]];random.Random(1729).shuffle(orders)
 return [{'process_set':i//5+1,'pair':i%5+1,'order':list(x)} for i,x in enumerate(orders)]
def keys(x,s):need(isinstance(x,dict) and not(set(x)-set(s.split())),'Unexpected result field')
def digest(x):need(isinstance(x,str) and re.fullmatch('[a-f0-9]{64}',x),'Invalid hash')
def positive(x):need(type(x['elapsed_s']) in (int,float) and math.isfinite(x['elapsed_s']) and x['elapsed_s']>0 and x['wav_bytes']==478252,'Invalid latency')
def alias(x,mode,p):
 keys(x,'variant table_slot function_index callbacks elements classes predicted_branch length_histogram restored metadata_errors scope');need(x['variant']==mode and x['table_slot']==p['callback_table_slot'] and x['function_index']==p['callback_function_index'],'Alias identity')
 need(x['restored'] and x['metadata_errors']==0 and x['callbacks']==69 and x['elements']==435901440,'Alias coverage')
 need(x['classes']=={'exact_inplace':{'calls':41,'elements':256615424},'disjoint':{'calls':28,'elements':179286016}} and x['length_histogram']=={'492544':1,'1970176':17,'7880704':51},'Alias distribution')
 expected={'simd':{'calls':69,'elements':435901440},'scalar':{'calls':0,'elements':0}} if mode=='candidate' else {'simd':{'calls':28,'elements':179286016},'scalar':{'calls':41,'elements':256615424}}
 need(x['predicted_branch']==expected,'Alias guard classification')
def metrics(m,exact=False):
 keys(m,'wav_format fp32_samples finite pcm_exact pcm_sha256 pcm_changed_samples pcm_max_abs_lsb pcm_rmse_lsb fp32_exact fp32_sha256 fp32_changed_samples fp32_max_abs fp32_rmse fp32_relative_rmse relative_rms_floor')
 need(m['finite'] is True and m['fp32_samples']==239104,'Numerical metric finiteness/shape');digest(m['pcm_sha256']);digest(m['fp32_sha256']);need(m['wav_format']=={'channels':1,'sample_bytes':2,'sample_rate':24000,'frames':239104},'WAV format pin')
 for k,v in m.items():
  if k in ['wav_format','pcm_sha256','fp32_sha256','finite','pcm_exact','fp32_exact']:continue
  need(type(v) in (int,float) and math.isfinite(v) and v>=0,'Numerical delta')
 if exact:need(m['pcm_exact'] and m['fp32_exact'] and m['pcm_changed_samples']==0 and m['fp32_changed_samples']==0,'Repeat metric exactness')
def validate(r):
 keys(r,'schema status schedule normal_tiering browser_flags warmups_per_browser provenance source_proof environment source_hashes model_sha256 query_sha256 browser_gate process_sets trials sampling_policy source_native_size_note distribution worker_source_sha256 activation initial_host primary_browsers_closed_before_quality cpu_quality private_temporary_directory_deleted')
 need(r['schema']=='inplace-leaky-source-confirmation-v1' and r['status']=='complete' and r['normal_tiering'] and r['browser_flags']==FLAGS and r['private_temporary_directory_deleted'],'Completion/configuration');need(r['model_sha256']==MODEL_SHA and r['query_sha256']==QUERY_SHA,'Inputs');need(r['schedule']==schedule() and r['warmups_per_browser']==5,'Frozen45-call design')
 a=r['activation'];keys(a,'product js_version transformed_groups revectorizable_nodes flag_rejected launch_configuration_matches trace_sha256');need(a['transformed_groups']==0 and a['revectorizable_nodes']==0 and not a['flag_rejected'] and a['launch_configuration_matches'],'Actual OFF gate');engine={k:a[k] for k in ['product','js_version']};digest(a['trace_sha256'])
 p=r['provenance'];need(set(p)==set(KEYS)|{'cpu'},'Four provenance records')
 for mode,v in p.items():
  keys(v,'receipt_sha256 archive_sha256 wasm_sha256 js_sha256 callback_body_sha256 callback_table_slot callback_function_index build_toolchain native_bundle')
  for name in ['receipt_sha256','archive_sha256','wasm_sha256','js_sha256']:digest(v[name])
  need(v['build_toolchain']=={'core':'9b539761f3e152b966e08c2de0784129fe8cf68d','rust':'1.96.0','emscripten':'4.0.8','optimization':'z','graph_level':1},'Toolchain')
  if mode=='cpu':need(v['archive_sha256']==CPU_SHA,'CPU archive');continue
  need(v['callback_body_sha256']==(CANDIDATE_BODY if mode=='candidate' else ORIGINAL_BODY),'Source callback pin');b=v['native_bundle'];keys(b,'verified runtime_sha256 dispatcher_sha256 invalidated_packages activation_member_sha256 activation_occurrence duplicate_activation_sha256 all_ordered_native_members_verified');need(b['verified'] and b['runtime_sha256']==v['archive_sha256'] and set(b['invalidated_packages'])=={'voicevox_core','voicevox_benchmark'},'Bundle proof')
  if mode=='original':need(v['archive_sha256']==LOADSPLAT_SHA,'Original forced archive')
  else:need(b['activation_occurrence']==1 and b['all_ordered_native_members_verified']==1438,'Indexed activation bundle')
 s=r['source_proof'];keys(s,'original_control candidate_scope rebuilt_dependency_count candidate_dependency_count all_element_segments_identical manifest_sha256 source_refs preparation_hashes');keys(s['original_control'],'passed function_definition_count all_function_definitions_exact_IR_equal_without_normalization complete_IR_equal_after_declared_normalization normalized_IR_sha256 all_defined_symbols_equal original_object_sha256 rebuilt_object_sha256');keys(s['candidate_scope'],'passed definition_count counts metadata_nodes_matched metadata_mapping_bijective metadata_edges_recursively_equal original_attribute_definitions_identical normalized_IR_sha256 defined_symbols_equal candidate_object_sha256');need(s['original_control']['passed'] and s['original_control']['function_definition_count']==372 and s['candidate_scope']['passed'] and s['candidate_scope']['definition_count']==372 and s['all_element_segments_identical'],'Source gate');need(s['candidate_scope']['counts']=={'raw_IR_identical':295,'metadata_ID_renumbering_only':73,'diagnostic_line_only':2,'intended_LeakyRelu_exact_alias_change':2},'Source scope')
 need(s['source_refs']==SOURCE_REFS,'Public source refs')
 need(set(s['preparation_hashes'])=={'preparer_sha256','builder_source_sha256','source_pins_sha256','source_proof_helper_sha256','callback_discovery_sha256'},'Preparation hash schema')
 for value in s['preparation_hashes'].values():digest(value)
 for obj,names in [(s['original_control'],['original_object_sha256','rebuilt_object_sha256']),(s['candidate_scope'],['candidate_object_sha256'])]:
  for name in names:digest(obj[name])
 need(s['original_control']['all_defined_symbols_equal'] and s['candidate_scope']['defined_symbols_equal'],'Symbol proof')
 keys(r['environment'],'os architecture cpu available_logical_cpus host_logical_cpus python core_commit onnxruntime_version onnxruntime_builder_commit uv rust emscripten affinity_logical_cpus cgroup_cpu_quota memory_gib');need(r['environment']['cpu'] and r['environment']['available_logical_cpus']>=1 and r['environment']['affinity_logical_cpus']>=1,'CPU environment');validate_host(r['initial_host'])
 for name,value in r['source_hashes'].items():need(Path(name).name==name,'Source basename');digest(value)
 for value in r['worker_source_sha256'].values():digest(value)
 if 'distribution' in r:keys(r['distribution'],'sha256 bytes');digest(r['distribution']['sha256']);need(r['distribution']['bytes']>0,'Distribution size')
 hcs=set();identities=[]
 def init(v,mode,gate=False):
  keys(v,'ready runtime dispatch wasm_sha256 js_sha256 timing_instrumentation engine browser_pid browser_created launch_flags');need(v['ready'] and v['engine']==engine and v['launch_flags']==FLAGS and v['wasm_sha256']==p[mode]['wasm_sha256'] and v['js_sha256']==p[mode]['js_sha256'],'Actual worker identity')
  need(v['timing_instrumentation']==('alias_gate_only' if gate else 'clock_only'),'Instrumentation isolation')
  if mode=='cpu':
   keys(v['runtime'],'shared_memory pthreads spin_off fixed_length fixed_matches xnn_threads xnn_sessions configured_ort_global_threads');need(v['dispatch'] is None and v['runtime']['shared_memory'] and v['runtime']['pthreads']==1 and not v['runtime']['spin_off'] and v['runtime']['fixed_matches']==0 and v['runtime']['xnn_threads']==0 and v['runtime']['xnn_sessions']==0 and v['runtime']['configured_ort_global_threads']==2,'CPU runtime');return
  need(v['runtime']==XNN_RUNTIME,'XNN runtime');d=v['dispatch'];keys(d,'schema_version source context expected_dispatch actual_dispatch worker_hardware_concurrency probe_hardware_concurrency is_x86 relaxed_simd mr nr splat_pointers loadsplat_pointers checked_pointers probe_return_code cross_origin_isolated shared_memory verified');need(d['verified'] and d['actual_dispatch']=='loadsplat' and d['expected_dispatch']=='loadsplat' and d['loadsplat_pointers']==12 and d['splat_pointers']==0 and d['checked_pointers']==12 and not d['relaxed_simd'] and d['is_x86'],'Actual dispatcher');hcs.add(d['worker_hardware_concurrency'])
 def clean(c):keys(c,'observed_processes remaining_processes confirmed');need(c['confirmed'] and c['observed_processes']>0 and c['remaining_processes']==0,'Cleanup')
 def output(x,with_alias=False):
  keys(x,'mode phase iteration wav_bytes raw_bytes wav_sha256 raw_sha256 exact_bytes alias pcm_sha256 finite');need(x['finite'] and x['exact_bytes'] and x['wav_bytes']==478252 and x['raw_bytes']==956416,'Output exactness')
  for k,value in GOLD.items():need(x[k]==value,'Output hash')
  if with_alias:
   need(len(x['alias'])==2,'Raw/WAV alias pair')
   for item in x['alias']:alias(item,x['mode'],p[x['mode']])
  else:need('alias' not in x,'Instrumented primary check')
 need(set(r['browser_gate'])==set(KEYS),'Browser gate coverage')
 for mode,g in r['browser_gate'].items():
  keys(g,'initialization checks cleanup');init(g['initialization'],mode,True);clean(g['cleanup']);need(len(g['checks'])==2 and [x['iteration'] for x in g['checks']]==[1,2],'Browser repeat gate')
  for x in g['checks']:need(x['mode']==mode and x['phase']=='gate','Gate labels');output(x,True)
 need(len(r['process_sets'])==3 and [g['id'] for g in r['process_sets']]==[1,2,3],'Fresh sets')
 for g in r['process_sets']:
  keys(g,'id runtime warmups checks sentinels cleanup idle');need(set(g['runtime'])==set(KEYS) and set(g['cleanup'])==set(KEYS),'Set conditions')
  for mode,v in g['runtime'].items():init(v,mode);identities.append((v['browser_pid'],v['browser_created']));clean(g['cleanup'][mode])
  need(len(g['warmups'])==15 and {(x['mode'],x['iteration']) for x in g['warmups']}=={(m,i) for m in KEYS for i in range(1,6)},'Warmups')
  for x in g['warmups']:keys(x,'mode iteration elapsed_s wav_bytes');positive(x)
  need(len(g['sentinels'])==2 and [x['position'] for x in g['sentinels']]==['before','after'],'Sentinels')
  for x in g['sentinels']:keys(x,'position elapsed_s wav_bytes');positive(x)
  need(len(g['checks'])==12 and {(x['mode'],x['phase'],x['iteration']) for x in g['checks']}=={(m,q,i) for m in KEYS for q in ['before','after'] for i in [1,2]},'Pre/post matrix')
  for x in g['checks']:output(x)
  idle=g['idle'];keys(idle,'interval_s modes');need(math.isfinite(idle['interval_s']) and idle['interval_s']>=1 and set(idle['modes'])==set(KEYS),'Idle observation')
  for x in idle['modes'].values():keys(x,'before after cpu_seconds_delta cpu_percent unmatched_processes');validate_processes(x['before']);validate_processes(x['after']);need({k:x[k] for k in ['cpu_seconds_delta','cpu_percent','unmatched_processes']}==idle_cpu(x['before'],x['after'],idle['interval_s']),'Idle deltas')
 need(len(set(identities))==9 and len(hcs)==1,'Process/hardware identity')
 expected=[(x['process_set'],x['pair'],mode) for x in schedule() for mode in x['order']];need(len(r['trials'])==45 and [(x['process_set'],x['pair'],x['mode']) for x in r['trials']]==expected,'All45 predeclared calls')
 for x in r['trials']:
  keys(x,'process_set pair mode pair_order elapsed_s wav_bytes before after resource_observations');positive(x);block=next(z for z in schedule() if (z['process_set'],z['pair'])==(x['process_set'],x['pair']));need(x['pair_order']==block['order'],'Order receipt');validate_snapshot(x['before']);validate_snapshot(x['after']);need(x['resource_observations']==resource_observations(x['before'],x['after']),'Resource derivation');need(min(x['before']['host']['available_bytes'],x['after']['host']['available_bytes'])>=1024**3,'Low-memory stop should be incomplete')
 need(r['primary_browsers_closed_before_quality'],'Quality timing separation');q=r['cpu_quality'];keys(q,'scope modes passed forced_loadsplat_vs_cpu');need(q['passed'] and set(q['modes'])=={'cpu','original'},'Same-run quality gate')
 for mode,v in q['modes'].items():
  keys(v,'initialization repeats cleanup');init(v['initialization'],mode);clean(v['cleanup']);need(len(v['repeats'])==2 and [x['index'] for x in v['repeats']]==[0,1],'Quality repeats')
  for x in v['repeats']:keys(x,'index wav_sha256 raw_sha256 self_metrics repeat_metrics');digest(x['wav_sha256']);digest(x['raw_sha256']);metrics(x['self_metrics'],True);metrics(x['repeat_metrics'],True);need(x['raw_sha256']==x['self_metrics']['fp32_sha256']==x['repeat_metrics']['fp32_sha256'],'Quality hash association')
 metrics(q['forced_loadsplat_vs_cpu']);need(q['forced_loadsplat_vs_cpu']['fp32_sha256']==GOLD['raw_sha256'] and q['forced_loadsplat_vs_cpu']['pcm_sha256']==GOLD['pcm_sha256'],'Forced quality output')
 return True

def summarize(r):
 validate(r);effects={}
 for variant in ['rebuilt','candidate']:
  pairs=[]
  for block in schedule():
   rows={x['mode']:x for x in r['trials'] if (x['process_set'],x['pair'])==(block['process_set'],block['pair'])};pairs.append({'process_set':block['process_set'],'pair':block['pair'],'ratio':rows[variant]['elapsed_s']/rows['original']['elapsed_s']})
  effects[variant]={'median_ratio':statistics.median(x['ratio'] for x in pairs),'geometric_mean_ratio':math.exp(statistics.mean(math.log(x['ratio']) for x in pairs)),'per_process_set':[{'id':i,'ratios':[x['ratio'] for x in pairs if x['process_set']==i],'geometric_mean_ratio':math.exp(statistics.mean(math.log(x['ratio']) for x in pairs if x['process_set']==i))} for i in [1,2,3]]}
 source_control=[]
 for block in schedule():
  rows={x['mode']:x for x in r['trials'] if (x['process_set'],x['pair'])==(block['process_set'],block['pair'])};source_control.append({'process_set':block['process_set'],'pair':block['pair'],'ratio':rows['candidate']['elapsed_s']/rows['rebuilt']['elapsed_s']})
 return {'candidate_vs_rebuilt':{'median_ratio':statistics.median(x['ratio'] for x in source_control),'pairs':source_control},'passed':True,'primary_calls':45,'observations_per_condition':15,'process_set_clusters':3,'effects_vs_forced_original':effects,'sentinel_ratios':[g['sentinels'][1]['elapsed_s']/g['sentinels'][0]['elapsed_s'] for g in r['process_sets']],'fault_exposed_calls':sum(x['resource_observations']['fault_exposed'] for x in r['trials']),'cpu_quality_finite':True,'source_outputs_exact_to_forced_baseline':True,'scope':'Three process-set clusters; retain all observations. Source result is distinct from the earlier binary guard.'}
def main():
 p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();r=json.loads(a.input.read_text());s=summarize(r);a.output.write_text(json.dumps(s,indent=2)+'\n');print('SOURCE_CONFIRM_SUMMARY '+json.dumps(s,separators=(',',':')))
 print('SOURCE_CONFIRM_META '+json.dumps({k:v for k,v in r.items() if k not in ['trials','process_sets','browser_gate','cpu_quality']},separators=(',',':')))
 for mode,value in r['browser_gate'].items():print('SOURCE_CONFIRM_GATE '+json.dumps({'mode':mode,**value},separators=(',',':')))
 for g in r['process_sets']:
  print('SOURCE_CONFIRM_SET '+json.dumps({k:v for k,v in g.items() if k not in ['warmups','checks']},separators=(',',':')))
  for x in g['warmups']:print('SOURCE_CONFIRM_WARMUP '+json.dumps({'process_set':g['id'],**x},separators=(',',':')))
  for x in g['checks']:print('SOURCE_CONFIRM_CHECK '+json.dumps({'process_set':g['id'],**x},separators=(',',':')))
 for x in r['trials']:print('SOURCE_CONFIRM_TRIAL '+json.dumps(x,separators=(',',':')))
 print('SOURCE_CONFIRM_CPU_QUALITY '+json.dumps(r['cpu_quality'],separators=(',',':')))
if __name__=='__main__':main()
