"""Emit allowlisted reproducibility fields, never generated audio or model arrays."""
import argparse,hashlib,json,statistics
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args()
for path in sorted(a.directory.rglob('*.json')):
 data=json.loads(path.read_text())
 if isinstance(data,dict) and data.get('schema')=='voicevox-browser-paired-confirmation-v1':
  print('CONFIRMATION_META '+json.dumps({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),**{k:data.get(k) for k in ['schema','status','revectorization','manifest_sha256','harness_sha256','research_harness_sha256','query_sha256','environment','initial_host','provenance','diagnostics','notes','error']}}))
  for group in data['process_sets']:
   print('CONFIRMATION_PROCESS_SET '+json.dumps(group))
  for row in data['trials']:
   print('CONFIRMATION_TRIAL '+json.dumps(row))
  for row in data['cpu_companion']:
   summary={k:v for k,v in row.items() if not isinstance(v,list)}
   print('CONFIRMATION_CPU_COMPANION '+json.dumps(summary))
  pairs={}
  for row in data['trials']:pairs.setdefault((row['process_set'],row['pair']),{})[row['mode']]=row['elapsed_s']
  effects=[{'process_set':key[0],'pair':key[1],'reduction_percent':100*(1-v['combined']/v['original'])} for key,v in pairs.items() if set(v)=={'original','combined'}]
  print('CONFIRMATION_PAIRED_EFFECTS '+json.dumps({'pairs':effects,'median_reduction_percent':statistics.median(x['reduction_percent'] for x in effects) if effects else None}))
 elif path.parent.name=='kernel':
  # These files are created by the synthetic kernel-only harness; keys are known below.
  allowed={k:data[k] for k in ['requestedRevectorization','traceEnabled','requestedJsFlags','browserVersion','hardwareConcurrency','crossOriginIsolated','context','scope','validationCases','maxAbsoluteError','positiveTransformationNodes','flagRejected','activationVerified','results','dispatch','variants','timings','benchmark'] if k in data}
  if isinstance(data.get('metadata'),dict):allowed['metadata']={k:data['metadata'][k] for k in ['requestedRevectorization','traceEnabled','requestedJsFlags','browserVersion','hardwareConcurrency','crossOriginIsolated','context','scope','validationCases','maxAbsoluteError','positiveTransformationNodes','flagRejected','activationVerified'] if k in data['metadata']}
  print('KERNEL_NUMERIC_RESULT '+json.dumps({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'data':allowed}))
