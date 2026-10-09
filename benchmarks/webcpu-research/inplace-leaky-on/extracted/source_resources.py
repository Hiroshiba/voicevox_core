"""Pre/post-call observations only. All fault/swap observations retained."""
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


def idle_cpu(before,after,interval_s):
 old={(x['pid'],x['created']):x for x in before};seconds=0;unknown=len(set(old)-{(x['pid'],x['created']) for x in after})
 for x in after:
  prior=old.get((x['pid'],x['created']))
  if prior is None:unknown+=1;continue
  seconds+=max(0,(x['cpu']['user']+x['cpu']['system'])-(prior['cpu']['user']+prior['cpu']['system']))
 return {'cpu_seconds_delta':seconds,'cpu_percent':100*seconds/interval_s,'unmatched_processes':unknown}


def validate_host(x):
 import math
 allowed={'memory_psi','available_bytes','total_bytes','swap_used','swap_in','swap_out','loadavg'}
 if not isinstance(x,dict) or set(x)-allowed:raise ValueError('Unexpected host resource field')
 for k,v in x.items():
  if k=='memory_psi':
   if not isinstance(v,dict) or set(v)-{'some','full'}:raise ValueError('Unexpected PSI field')
   for row in v.values():
    if set(row)-{'avg10','avg60','avg300','total'} or any(type(z) not in (int,float) or not math.isfinite(z) or z<0 for z in row.values()):raise ValueError('Invalid PSI')
  elif k=='loadavg':
   if not isinstance(v,list) or len(v)!=3 or any(type(z) not in (int,float) or not math.isfinite(z) or z<0 for z in v):raise ValueError('Invalid loadavg')
  elif type(v) not in (int,float) or not math.isfinite(v) or v<0:raise ValueError('Invalid host metric')
 for required in ['available_bytes','swap_in','swap_out']:
  if required not in x:raise ValueError('Missing host metric')


def validate_processes(xs):
 import math
 if not isinstance(xs,list) or not xs:raise ValueError('No process resources')
 for x in xs:
  if set(x)-{'pid','created','rss','cpu','major_faults','minor_faults','swap_bytes'}:raise ValueError('Unexpected process resource field')
  for k,v in x.items():
   if k=='cpu':
    if not isinstance(v,dict) or set(v)-{'user','system','children_user','children_system','iowait'} or not {'user','system'}<=set(v):raise ValueError('Unexpected CPU fields')
    if any(type(z) not in (int,float) or not math.isfinite(z) or z<0 for z in v.values()):raise ValueError('Invalid CPU counter')
   elif v is not None and (type(v) not in (int,float) or not math.isfinite(v) or v<0):raise ValueError('Invalid process metric')
  if not {'pid','created','cpu'}<=set(x):raise ValueError('Missing process identity/counters')


def validate_snapshot(x):
 if set(x)!={'host','processes'}:raise ValueError('Unexpected resource snapshot field')
 validate_host(x['host']);validate_processes(x['processes'])
