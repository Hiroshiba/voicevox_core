"""Validate the full artifact allowlist before emitting synthetic-only receipts."""
import pathlib,json,math,os,shutil
P=pathlib.Path(__file__).resolve().parent
forbidden={'wav_base64','audio_outputs','raw_samples','model_bytes','tensor_data','weights','audio_base64','raw_wave','wav_bytes'}
def safe(x):
 if isinstance(x,dict):
  for k,v in x.items():assert isinstance(k,str) and k.lower() not in forbidden;safe(v)
 elif isinstance(x,list):
  for v in x:safe(v)
 elif isinstance(x,str):assert len(x)<20000 and all(s not in x.lower() for s in ['data:audio/','wav_base64','"tensor_data"'])
 elif isinstance(x,float):assert math.isfinite(x)
 else:assert x is None or isinstance(x,(int,bool))
paths=[P/'results'/n for n in ['screen.json','SCREEN_GATE.json','dependency-provenance.json','production-check0.json','production-check1.json','production-check2.json','set0-warmups.json','set1-warmups.json','set2-warmups.json']]+[P/'manifests'/n for n in ['build.json','control-rebuild.json']]
parsed={str(p.relative_to(P)):json.loads(p.read_text()) for p in paths if p.exists()}
for value in parsed.values():safe(value)
raw=P/'results/raw-calls.jsonl';raw_records=[json.loads(line) for line in raw.read_text().splitlines()] if raw.exists() else []
for r in raw_records:
 assert set(r)=={'set','round','returned'} and r['set'] in [0,1,2] and r['round'] in [0,1,2];safe(r)
report=parsed.get('results/screen.json')
if report and report.get('passed') and report.get('screen_requested'):
 from validate_screen import validate
 validate(report)
for name,data in parsed.items():
 if name=='results/screen.json':
  print('SCREEN_METADATA '+json.dumps({k:v for k,v in data.items() if k not in ['sets','diagnostic']}),flush=True)
  for d in data.get('diagnostic',[]):print('SCREEN_DIAGNOSTIC '+json.dumps(d),flush=True)
  for s in data.get('sets',[]):
   print('SCREEN_SET '+json.dumps({k:v for k,v in s.items() if k not in ['rounds','warmups']}),flush=True)
   print('SCREEN_WARMUPS '+json.dumps(dict(set=s['set'],records=s['warmups'])),flush=True)
   for r in s['rounds']:print('SCREEN_ROUND '+json.dumps(dict(set=s['set'],record=r)),flush=True)
 else:print('SCREEN_RECEIPT '+json.dumps(dict(file=name,data=data)),flush=True)
if not report:print('SCREEN_INCOMPLETE '+json.dumps(dict(raw_calls=raw_records)),flush=True)
if 'ARTIFACT_DIR' in os.environ:
 target=pathlib.Path(os.environ['ARTIFACT_DIR']);target.mkdir(parents=True,exist_ok=True)
 for p in paths:
  if p.exists():shutil.copyfile(p,target/p.name)
 if raw.exists():shutil.copyfile(raw,target/raw.name)
