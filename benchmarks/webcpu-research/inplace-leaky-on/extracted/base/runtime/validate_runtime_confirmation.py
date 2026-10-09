"""Strict structural validation independent of browser availability."""
import math,itertools,random
MODES=("original","auto_roundtrip","loadsplat")
ARCHIVES={"original":"407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd","auto_roundtrip":"da6c857b4bfc3f651f96b95a3fcf225fbf28e2abbb0b2b277d5bcb586020ebd2","loadsplat":"0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf"}

def balanced_schedule(sets=3,pairs=5,seed=1729):
 if sets!=3 or pairs!=5:raise ValueError("Predeclared runtime confirmation requires 3 sets x 5 rounds")
 orders=list(itertools.permutations(MODES))*2+[MODES,MODES[1:]+MODES[:1],MODES[2:]+MODES[:2]]
 random.Random(seed).shuffle(orders)
 return [{"process_set":i//pairs+1,"pair":i%pairs+1,"order":list(order)} for i,order in enumerate(orders)]


def validate(result,sets,pairs,warmups):
 def positive(row):
  for key in ['elapsed_s','audio_s']:
   if type(row[key]) not in (int,float) or not math.isfinite(row[key]) or row[key]<=0:raise ValueError('Invalid timing '+key)
 schedule=balanced_schedule(sets,pairs,result['seed'])
 if result['schedule']!=schedule:raise ValueError('Schedule differs from predeclared matrix')
 actual_orders={}
 for row in result['trials']:
  actual_orders.setdefault((row['process_set'],row['pair']),[]).append(row['mode'])
 for block in schedule:
  if actual_orders.get((block['process_set'],block['pair']))!=block['order']:raise ValueError('Trial order differs from schedule')
 if len(result['process_sets'])!=sets or [x['id'] for x in result['process_sets']]!=list(range(1,sets+1)):raise ValueError('Process sets incomplete')
 seen=set()
 for row in result['trials']:
  positive(row);key=(row['process_set'],row['pair'],row['mode'])
  if key in seen:raise ValueError('Duplicate trial')
  seen.add(key)
 expected={(s,p,m) for s in range(1,sets+1) for p in range(1,pairs+1) for m in MODES}
 if seen!=expected:raise ValueError('Incomplete or unexpected trial matrix')
 all_hc={v['dispatch']['worker_hardware_concurrency'] for group in result['process_sets'] for v in group['runtime'].values()}
 if len(all_hc)!=1:raise ValueError('Worker concurrency differs across sets')
 identities=[(v['browser_pid'],v['browser_created']) for group in result['process_sets'] for v in group['runtime'].values()]
 if len(set(identities))!=sets*len(MODES):raise ValueError('Browser process identity reused')
 for group in result['process_sets']:
  if len(group['sentinels'])!=2 or {x['position'] for x in group['sentinels']}!={'before','after'}:raise ValueError('Missing sentinels')
  for row in group['sentinels']+group['warmups']:positive(row)
  keys=[(x['mode'],x['iteration']) for x in group['warmups']]
  if len(keys)!=len(MODES)*warmups or set(keys)!={(m,i) for m in MODES for i in range(1,warmups+1)}:raise ValueError('Warmup matrix incomplete')
  if set(group['runtime'])!=set(MODES):raise ValueError('Runtime conditions incomplete')
  if len({v['dispatch']['worker_hardware_concurrency'] for v in group['runtime'].values()})!=1:raise ValueError('Worker concurrency differs')
  for mode in MODES:
   proof=group['runtime'][mode]['dispatch']
   expected=('loadsplat' if proof['worker_hardware_concurrency']>4 else 'splat') if mode!='loadsplat' else 'loadsplat'
   if not proof['verified'] or proof['actual_dispatch']!=expected or proof['checked_pointers']!=12:raise ValueError('Actual dispatcher not verified')
   for check in ['repeat','versus_initial_control','after','after_repeat']:
    metrics=group['checks'][mode][check]
    if not (metrics['pcm_exact'] and metrics['fp32_exact'] and metrics['finite']):raise ValueError('Output gate failed')
 if len(result['cpu_companion'])!=sets*len(MODES):raise ValueError('Missing CPU companion')
 for row in result['cpu_companion']:positive(row)
 if {(x['process_set'],x['mode']) for x in result['cpu_companion']}!={(s,m) for s in range(1,sets+1) for m in MODES}:raise ValueError('Duplicate or incorrect CPU companion matrix')


def resource_observations(before,after):
 old={(x['pid'],x['created']):x for x in before['processes']}
 major=0;minor=0;swap_growth=0;unknown=len(set(old)-{(x['pid'],x['created']) for x in after['processes']})
 for x in after['processes']:
  previous=old.get((x['pid'],x['created']))
  if previous is None or x.get('major_faults') is None or previous.get('major_faults') is None:unknown+=1;continue
  major+=max(0,x['major_faults']-previous['major_faults'])
  if x.get('minor_faults') is not None and previous.get('minor_faults') is not None:minor+=max(0,x['minor_faults']-previous['minor_faults'])
  if x.get('swap_bytes') is not None and previous.get('swap_bytes') is not None:swap_growth+=max(0,x['swap_bytes']-previous['swap_bytes'])
 return {'target_major_fault_delta':major,'target_minor_fault_delta':minor,'target_swap_growth_bytes':swap_growth,'host_swap_in_delta':max(0,after['host']['swap_in']-before['host']['swap_in']),'host_swap_out_delta':max(0,after['host']['swap_out']-before['host']['swap_out']),'unmatched_or_unknown_processes':unknown,'fault_exposed':major>0}

def pressure_during_call(before,after):
 # Faults and host swap counters are context, not a reason to remove observations.
 if min(before['host']['available_bytes'],after['host']['available_bytes']) < 1024**3:return 'Available memory below1GiB'
 return None
