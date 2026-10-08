"""Strict structural validation independent of browser availability."""
import math

def validate(result,sets,pairs,warmups):
 def positive(row):
  for key in ['elapsed_s','audio_s']:
   if not isinstance(row[key],(int,float)) or not math.isfinite(row[key]) or row[key]<=0:raise ValueError('Invalid timing '+key)
 if len(result['process_sets'])!=sets or [x['id'] for x in result['process_sets']]!=list(range(1,sets+1)):raise ValueError('Process sets incomplete')
 seen=set()
 for row in result['trials']:
  positive(row);key=(row['process_set'],row['pair'],row['mode'])
  if key in seen:raise ValueError('Duplicate trial')
  seen.add(key)
 expected={(s,p,m) for s in range(1,sets+1) for p in range(1,pairs+1) for m in ['original','combined']}
 if seen!=expected:raise ValueError('Incomplete or unexpected trial matrix')
 for group in result['process_sets']:
  if len(group['sentinels'])!=2 or {x['position'] for x in group['sentinels']}!={'before','after'}:raise ValueError('Missing sentinels')
  for row in group['sentinels']+group['warmups']:positive(row)
  keys=[(x['mode'],x['iteration']) for x in group['warmups']]
  if len(keys)!=2*warmups or set(keys)!={(m,i) for m in ['original','combined'] for i in range(1,warmups+1)}:raise ValueError('Warmup matrix incomplete')
  for mode in ['original','combined']:
   for check in ['repeat','versus_initial_control','after']:
    metrics=group['checks'][mode][check]
    if not (metrics['pcm_exact'] and metrics['fp32_exact'] and metrics['finite']):raise ValueError('Output gate failed')
 if len(result['cpu_companion'])!=sets*2:raise ValueError('Missing CPU companion')
 for row in result['cpu_companion']:positive(row)
 if len({(x['process_set'],x['mode']) for x in result['cpu_companion']})!=sets*2:raise ValueError('Duplicate CPU companion')


def resource_observations(before,after):
 old={(x['pid'],x['created']):x for x in before['processes']}
 major=0;swap_growth=0;unknown=len(set(old)-{(x['pid'],x['created']) for x in after['processes']})
 for x in after['processes']:
  previous=old.get((x['pid'],x['created']))
  if previous is None or x.get('major_faults') is None or previous.get('major_faults') is None:unknown+=1;continue
  major+=max(0,x['major_faults']-previous['major_faults'])
  if x.get('swap_bytes') is not None and previous.get('swap_bytes') is not None:swap_growth+=max(0,x['swap_bytes']-previous['swap_bytes'])
 return {'target_major_fault_delta':major,'target_swap_growth_bytes':swap_growth,'host_swap_in_delta':max(0,after['host']['swap_in']-before['host']['swap_in']),'host_swap_out_delta':max(0,after['host']['swap_out']-before['host']['swap_out']),'unmatched_or_unknown_processes':unknown,'fault_exposed':major>0}

def pressure_during_call(before,after):
 # Faults and host swap counters are context, not a reason to remove observations.
 if after['host']['available_bytes'] < 1024**3:return 'Available memory below1GiB'
 return None
