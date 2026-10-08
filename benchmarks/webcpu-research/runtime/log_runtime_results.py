"""Numeric-only evidence logs for freshly generated runtime experiments."""
import argparse,hashlib,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
for f in sorted(a.directory.glob('*.json')):
 d=json.loads(f.read_text());sha=hashlib.sha256(f.read_bytes()).hexdigest()
 if 'trials' in d and 'environment' in d:
  env=d['environment'];safe={k:v for k,v in env.items() if k in ['cpu','architecture','os','cgroup_cpu_quota','affinity_logical_cpus','host_logical_cpus','memory_gib','python','rust','browser','available_logical_cpus','cpu_metric','cpu_sample_interval_ms','cpu_padding_seconds','audio_query_sha256','core_variants','wasm_bytes','v8_activation_diagnostics'] or k.endswith(('_engine','_runtime','_dispatch','_v8_flags','_hardware_concurrency','_pthreads'))}
  checks={k:v for k,v in d.get('output_checks',{}).items() if k!='spectrograms'}
  print('RUNTIME_PROVENANCE '+json.dumps({'file':f.name,'sha256':sha,'environment':safe,'output_checks':checks,'research_screen':d.get('research_screen'),'notes':d.get('notes')}))
  for t in d['trials']:print('RUNTIME_TRIAL '+json.dumps({k:v for k,v in t.items() if k!='cpu_trace'}))
  for r in d.get('log',[]):
   if r.get('event')=='warmup_excluded':print('RUNTIME_WARMUP '+json.dumps(r))
 elif f.name.endswith('.dispatch.json'):print('RUNTIME_ACTUAL_DISPATCH '+json.dumps({'file':f.name,'sha256':sha,'data':d}))
 elif 'quality' in f.name or 'profile' in f.name:print('RUNTIME_AUXILIARY '+json.dumps({'file':f.name,'sha256':sha,'data':d}))
