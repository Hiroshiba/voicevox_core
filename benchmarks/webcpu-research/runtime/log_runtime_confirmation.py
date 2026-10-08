#!/usr/bin/env python3
"""Allowlisted numeric logs and CPU usage traces; full evidence also remains in JSON."""
import argparse,json,math,statistics
from pathlib import Path
from validate_runtime_confirmation import MODES

def number(value):
 if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Invalid number')
 return value

def summarize(result):
 if result['schema']!='voicevox-browser-runtime-confirmation-v1' or result['revectorization'] not in ('off','on') or result['status'] not in ('initializing','failed','complete'):raise ValueError('Unknown result schema')
 rows=[];paired={}
 for row in result['trials']:
  if row['mode'] not in MODES or row['process_set'] not in (1,2,3) or row['pair'] not in range(1,6):raise ValueError('Unexpected trial')
  safe={k:row[k] for k in ('process_set','pair','mode')}
  for key in ('elapsed_s','audio_s'):safe[key]=number(row[key])
  safe['resource_observations']={k:number(v) for k,v in row.get('resource_observations',{}).items() if k in ('target_major_fault_delta','target_minor_fault_delta','target_swap_growth_bytes','host_swap_in_delta','host_swap_out_delta','unmatched_or_unknown_processes')}
  rows.append(safe);paired.setdefault((row['process_set'],row['pair']),{})[row['mode']]=safe['elapsed_s']
 dispatch=[]
 for group in result.get('process_sets',[]):
  if group['id'] not in (1,2,3):raise ValueError('Unexpected process set')
  for mode,value in group.get('runtime',{}).items():
   if mode not in MODES:raise ValueError('Unexpected runtime mode')
   proof=value.get('dispatch')
   if not proof:continue
   row={'process_set':group['id'],'mode':mode,'verified':proof.get('verified') is True}
   if proof.get('actual_dispatch') in ('splat','loadsplat'):row['actual_dispatch']=proof['actual_dispatch']
   for key in ('worker_hardware_concurrency','checked_pointers','splat_pointers','loadsplat_pointers'):
    if proof.get(key) is not None:row[key]=number(proof[key])
   dispatch.append(row)
 companions=[]
 for row in result.get('cpu_companion',[]):
  if row['mode'] not in MODES or row['process_set'] not in (1,2,3):raise ValueError('Unexpected companion')
  safe={'mode':row['mode'],'process_set':row['process_set'],'trace_samples':len(row.get('cpu_trace',[]))}
  for key in ('elapsed_s','audio_s','cpu_time_s','cpu_window_s','cpu_avg_cores','cpu_percent','cpu_samples','cpu_processes'):
   if key in row:safe[key]=number(row[key])
  safe['cpu_incomplete']=row.get('cpu_incomplete') is not False;companions.append(safe)
 effects={}
 for mode in MODES[1:]:
  ratios=[x['original']/x[mode] for x in paired.values() if 'original' in x and mode in x and x[mode]>0]
  clusters=[]
  for process_set in (1,2,3):
   values=[x['original']/x[mode] for (s,p),x in paired.items() if s==process_set and 'original' in x and mode in x and x[mode]>0]
   if values:clusters.append({'process_set':process_set,'pairs':len(values),'geomean_speedup':statistics.geometric_mean(values)})
  effects[mode]={'paired_observations':len(ratios),'geomean_speedup':statistics.geometric_mean(ratios) if ratios else None,'process_set_effects':clusters}
 return {'schema':'runtime-confirmation-numeric-v1','status':result['status'],'revectorization':result['revectorization'],'primary_unsampled':True,'actual_core_dispatch':dispatch,'trials':rows,'paired_effects':effects,'cpu_companion_count':len(result.get('cpu_companion',[])),'cpu_companion':companions,'cpu_coverage_complete':len(companions)==9 and all(not c['cpu_incomplete'] and c['trace_samples']>0 for c in companions),'full_cpu_traces_retained_in_result_json':True,'private_temporary_directory_deleted':result.get('private_temporary_directory_deleted') is True}

def main():
 p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);a=p.parse_args();result=json.loads(a.input.read_text());safe=summarize(result)
 print('RUNTIME_CONFIRMATION_NUMERIC_BEGIN');print(json.dumps(safe,separators=(',',':'),allow_nan=False));print('RUNTIME_CONFIRMATION_NUMERIC_END')
 meta={k:result.get(k) for k in ['schema','status','revectorization','seed','schedule','manifest_sha256','harness_sha256','validator_sha256','research_harness_sha256','environment','diagnostics','query_sha256','initial_host','notes','error_type','private_temporary_directory_deleted']}
 print('RUNTIME_CONFIRMATION_METADATA '+json.dumps(meta,allow_nan=False))
 for mode,proof in result.get('provenance',{}).items():
  if mode not in MODES:raise ValueError('Unknown provenance mode')
  print('RUNTIME_CONFIRMATION_BUILD '+json.dumps({'mode':mode,'proof':proof},allow_nan=False))
 for group in result.get('process_sets',[]):
  print('RUNTIME_CONFIRMATION_PROCESS_SET '+json.dumps({k:group.get(k) for k in ['id','warmups','checks','runtime','sentinels']},allow_nan=False))
 for row in result.get('trials',[]):
  print('RUNTIME_CONFIRMATION_TRIAL '+json.dumps({k:row.get(k) for k in ['process_set','pair','mode','pair_order','elapsed_s','audio_s','before','after','resource_observations']},allow_nan=False))
 for row in result.get('cpu_companion',[]):
  print('RUNTIME_CONFIRMATION_CPU_TRACE '+json.dumps({k:row.get(k) for k in ['process_set','mode','elapsed_s','audio_s','cpu_time_s','cpu_window_s','cpu_avg_cores','cpu_percent','cpu_samples','cpu_processes','cpu_incomplete','cpu_trace','before','after','resource_observations']},allow_nan=False))
if __name__=='__main__':main()
