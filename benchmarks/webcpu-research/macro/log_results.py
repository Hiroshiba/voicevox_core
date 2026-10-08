"""Validate privacy-safe synthetic receipts before writing any data to stdout."""
import pathlib,json,hashlib,subprocess,os,re,math
P=pathlib.Path(__file__).resolve().parent
FORBIDDEN={'wav_base64','audio_outputs','raw_samples','model_bytes','tensor_data','weights','audio_base64','raw_wave','wav_bytes'}
def safe(value):
 if isinstance(value,dict):
  for k,v in value.items():
   assert isinstance(k,str) and k.lower() not in FORBIDDEN,k
   safe(v)
 elif isinstance(value,list):
  for v in value:safe(v)
 elif isinstance(value,str):
  assert len(value)<=20000 and all(x not in value.lower() for x in ['data:audio/','wav_base64','"audio_outputs"','"tensor_data"'])
 elif isinstance(value,float):assert math.isfinite(value)
 else:assert value is None or isinstance(value,(int,bool))
pending=[]
def emit(label,value):
 safe(value);pending.append((label,value))
def record_ok(r):
 assert set(r)=={'condition','case','hashes','packedHash','selection','runs'}
 assert type(r['condition']) is int and r['condition'] in [0,1,2]
 assert len(r['case'])==5 and all(type(n) is int and n>0 for n in r['case'])
 assert len(r['hashes'])==3
 for h in [*r['hashes'],r['packedHash']]:assert isinstance(h,str) and re.fullmatch('[0-9a-f]{64}',h)
 keys={'m','c','k','d','seed','which','mr','nr','tile','range','ks','ks_scaled','kc','w_stride','cm_stride','cn_stride','alignment','kernel_mr4_linear_loadsplat','threads'}
 assert set(r['selection'])==keys
 for k,v in r['selection'].items():
  if k in ['tile','range']:assert len(v)==2 and all(type(x) is int and x>=0 for x in v)
  else:assert type(v) is int and v>=0
 assert len(r['runs'])==3
 for run in r['runs']:
  assert set(run)=={'input','callbacks','calls','bad','thread_ids','thread_callbacks','first_trace'}
  for k in ['input','callbacks','calls','bad']:assert type(run[k]) is int and run[k]>=0
  for k in ['thread_ids','thread_callbacks']:assert len(run[k])==2 and all(type(x) is int and x>=0 for x in run[k])
  assert len(run['first_trace'])<=32
  for tile in run['first_trace']:assert len(tile)==4 and all(type(x) is int and x>=0 for x in tile)
 safe(r)
# Validate every JSON file eligible for artifact collection before any stdout.
allowed=[P/'results'/name for name in ['dependency-provenance.json','BROWSER_GATE.json','browser-correctness.json','browser-condition0.json','browser-condition1.json','browser-condition2.json']]+[P/'manifests'/name for name in ['control-rebuild.json','diagnostic-build.json']]
for path in allowed:
 if path.exists():safe(json.loads(path.read_text()))
for name in ['dependency-provenance.json','BROWSER_GATE.json']:
 p=P/'results'/name;emit('MACRO_GATE',dict(file=name,data=json.loads(p.read_text()) if p.exists() else dict(missing=True)))
p=P/'manifests/control-rebuild.json'
if p.exists():emit('MACRO_CONTROL',json.loads(p.read_text())['results'])
for i in range(3):
 p=P/'results'/f'browser-condition{i}.json'
 if p.exists():
  for record in json.loads(p.read_text())['records']:record_ok(record);emit('MACRO_CASE',record)
p=P/'results/browser-correctness.json'
if p.exists():
 d=json.loads(p.read_text());emit('MACRO_BROWSER',{k:v for k,v in d.items() if k not in ['conditions','cases']})
if (P/'wasm').exists():
 for p in sorted((P/'wasm').glob('*.wasm')):
  text=subprocess.check_output([str(pathlib.Path(os.environ['EMSDK'])/'upstream/bin/wasm-dis'),str(p)],text=True)
  assert 'relaxed_' not in text and '.relaxed' not in text
  emit('MACRO_WASM',dict(file=p.name,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),relaxed_opcodes=0))
emit('MACRO_LIMITS',dict(untimed=True,synthetic_only=True,no_full_core=True,full_archive_asan=False,host_cacheline_verified=False,output_alignment_verified=64,no_false_sharing_claim=True))

# All complete receipts have been parsed and validated before the first data line.
for label,value in pending:print(label+' '+json.dumps(value,separators=(',',':')),flush=True)
