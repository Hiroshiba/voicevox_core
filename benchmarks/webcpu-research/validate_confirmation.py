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
