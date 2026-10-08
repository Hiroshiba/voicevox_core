#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]
# ///
"""Predeclared nine-pair browser screen. No primary sampler or callback hooks."""
import argparse,hashlib,importlib.util,json,math,os,shutil,struct,sys,tempfile,time
from pathlib import Path
import psutil
from resources import resource_observations,pressure_during_call,idle_cpu
from validate_timing import schedule,validate
ROOT=Path(__file__).parent
MODES=('original','candidate');FLAGS=['--js-flags=--no-wasm-revectorize'];WARMUPS=5
sha=lambda b:hashlib.sha256(b).hexdigest()
def require(ok,msg):
 if not ok:raise ValueError(msg)
def host():
 m=psutil.virtual_memory();s=psutil.swap_memory();psi={}
 try:
  for line in Path('/proc/pressure/memory').read_text().splitlines():
   name,*fields=line.split();psi[name]={k:float(v) for k,v in (f.split('=') for f in fields)}
 except (OSError,ValueError):pass
 return {'memory_psi':psi,'available_bytes':m.available,'total_bytes':m.total,'swap_used':s.used,'swap_in':s.sin,'swap_out':s.sout,'loadavg':list(os.getloadavg())}
def usage(pid):
 out=[]
 for q in [psutil.Process(pid),*psutil.Process(pid).children(recursive=True)]:
  try:
   stat=Path(f'/proc/{q.pid}/stat').read_text().rsplit(')',1)[1].split();status=Path(f'/proc/{q.pid}/status').read_text();swap=next((int(x.split()[1])*1024 for x in status.splitlines() if x.startswith('VmSwap:')),None)
   out.append({'pid':q.pid,'created':q.create_time(),'rss':q.memory_info().rss,'cpu':q.cpu_times()._asdict(),'major_faults':int(stat[9]),'minor_faults':int(stat[7]),'swap_bytes':swap})
  except (psutil.NoSuchProcess,psutil.AccessDenied,FileNotFoundError,PermissionError):pass
 return out

def main():
 p=argparse.ArgumentParser();p.add_argument('--selection',type=Path,required=True);p.add_argument('--gate-dir',type=Path,required=True);p.add_argument('--harness',type=Path,required=True);p.add_argument('--dispatch-helper',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();sys.path.insert(0,str(a.gate_dir));from prepare_inplace_leaky import prepare;from browser_gate import load,pcm,HARNESS_SHA,MODEL_SHA
 require(sha(a.harness.read_bytes())==HARNESS_SHA,'Harness pin');require(sha(a.dispatch_helper.read_bytes())=='abe7453d3feeaf6bc7f489452055a8ba0d02d22c7c158481112c51e0ada10c2b','Dispatch helper pin');h=load(a.harness);selected=json.loads(a.selection.read_text());gate=json.loads((ROOT/'gate_reference.json').read_text());require(gate['passed'],'Gate reference');binary=Path(selected['binary']);model=Path(selected['model']);require(sha(model.read_bytes())==MODEL_SHA,'Model pin')
 result={'schema':'inplace-leaky-timing-v1','status':'initializing','schedule':schedule(),'warmups_per_browser':WARMUPS,'normal_tiering':True,'browser_flags':FLAGS,'gate_reference':gate,'environment':h.environment_info(),'source_hashes':{p.name:sha(p.read_bytes()) for p in ROOT.iterdir() if p.is_file()},'process_sets':[],'trials':[],'notes':['Nine pairs in three process-set clusters; screening only, not a confirmed effect.','No callback instrumentation, profiler or high-frequency sampler in primary calls.','All fault/swap observations retained; stop only on execution failure or available memory below1GiB.','Post-call resources include checkpoint IO; synthesis latency does not.','Five recorded warmups per browser are not proof of tiering convergence.']};work=None;cleanup_safe=True;reference=None
 def save():a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
 def memory():
  snapshot=host()
  if snapshot['available_bytes']<1024**3:result['low_memory_stop']=snapshot;save();raise RuntimeError('Available memory below1GiB')
 try:
  from playwright.sync_api import sync_playwright
  work=Path(tempfile.mkdtemp(prefix='inplace-timing-private-'));proof=prepare(binary,Path(selected['archive']),work/'patched')
  for k in ['source_wasm_sha256','candidate_wasm_sha256','source_js_sha256','original_body_sha256','candidate_body_sha256']:require(proof[k]==gate[k],'Must use exact successful browser-gate artifact: '+k)
  result['build_toolchain']={k:proof['build_identity'][k] for k in ['core','rust','emscripten','runtime','optimization','graph_level']};result['transform']={k:v for k,v in proof.items() if k not in ['source_binary','native_bundle_proof','build_identity']};query=json.dumps(h.prepared_query(10),separators=(',',':'),ensure_ascii=False).encode();result['query_sha256']=sha(query);require(result['query_sha256']==gate['query_sha256'],'Query pin');result['model_sha256']=sha(model.read_bytes());(work/'query.json').write_bytes(query);shutil.copyfile(model,work/'sample.vvm');(work/'transform.json').write_text(json.dumps(proof));(work/'index.html').write_text(h.BROWSER_PAGE);(work/'worker.js').write_bytes((ROOT/'timing_worker.js').read_bytes());shutil.copyfile(a.dispatch_helper,work/'core_dispatch_check.js')
  for mode in MODES:
   folder=work/mode;folder.mkdir();shutil.copyfile(binary,folder/'voicevox_benchmark.js');shutil.copyfile(binary.with_suffix('.wasm') if mode=='original' else work/'patched/candidate.wasm',folder/'voicevox_benchmark.wasm')
  result['status']='running';result['initial_host']=host();save()
  with h.serve_assets(work) as url,sync_playwright() as pw:
   for set_id in range(1,4):
    pages={};browsers={};pids={};group={'id':set_id,'runtime':{},'warmups':[],'checks':[],'sentinels':[],'cleanup':{}};result['process_sets'].append(group);save()
    def call(mode):
     response=pages[mode].evaluate('d=>request(d)',{'command':'synthesize'});return {'elapsed_s':response['elapsed_s'],'wav_bytes':response['wav_bytes']}
    def check(mode,phase,iteration):
     nonlocal reference
     memory();value=pages[mode].evaluate('d=>request(d)',{'command':'check'});wav=bytes(value['wav']);raw=bytes(value['raw']);observation={'mode':mode,'phase':phase,'iteration':iteration,'wav_bytes':len(wav),'raw_bytes':len(raw),'wav_sha256':sha(wav),'raw_sha256':sha(raw),'exact_bytes':False};group['checks'].append(observation);save();pcmbytes=pcm(wav);observation['pcm_sha256']=sha(pcmbytes);observation['finite']=len(raw)%4==0 and all(math.isfinite(x[0]) for x in struct.iter_unpack('<f',raw))
     if reference is None:reference=(wav,raw,pcmbytes)
     observation['exact_bytes']=(wav,raw,pcmbytes)==reference;save();require(observation['finite'] and observation['exact_bytes'],'Output equality')
     for k,v in gate['outputs'].items():require(observation[k]==v,'Gate output hash differs')
    try:
     order=MODES if set_id%2 else MODES[::-1]
     for mode in order:
      memory();browser=pw.chromium.launch(headless=True,args=FLAGS);browsers[mode]=browser;pid=h.chromium_process_id(browser);pids[mode]=pid;engine=h.browser_engine_info(browser);require(engine==gate['engine'],'Engine must match successful OFF gate');page=browser.new_page();pages[mode]=page;page.goto(url);init=page.evaluate('d=>request(d)',{'command':'init','variant':mode});group['runtime'][mode]={'browser_pid':pid,'browser_created':psutil.Process(pid).create_time(),'engine':engine,'launch_flags':FLAGS,'initialization':init};save();require(init['ready'] and init['timing_instrumentation']=='clock_only','Ready');require(init['runtime']==gate['runtime'],'Runtime differs');require(init['dispatch']==gate['dispatch'],'Dispatch/concurrency differs');require(init['wasm_sha256']==gate['source_wasm_sha256' if mode=='original' else 'candidate_wasm_sha256'] and init['js_sha256']==gate['source_js_sha256'],'Loaded artifact differs')
      for iteration in range(1,WARMUPS+1):memory();group['warmups'].append({'mode':mode,'iteration':iteration,**call(mode)});save()
      check(mode,'before',1);check(mode,'before',2)
     # Untimed one-second idle observation, after both browsers/pools are ready.
     idle_start=time.monotonic();idle_before={m:usage(pids[m]) for m in MODES};time.sleep(1);idle_after={m:usage(pids[m]) for m in MODES};idle_interval=time.monotonic()-idle_start
     group['idle']={'interval_s':idle_interval,'modes':{m:{'before':idle_before[m],'after':idle_after[m],**idle_cpu(idle_before[m],idle_after[m],idle_interval)} for m in MODES}};save()
     memory();group['sentinels'].append({'position':'before',**call('original')});save()
     for block in [x for x in schedule() if x['process_set']==set_id]:
      for mode in block['order']:
       memory();before={'host':host(),'processes':usage(pids[mode])};observation=call(mode);row={'process_set':set_id,'pair':block['pair'],'mode':mode,'pair_order':block['order'],**observation,'before':before};result['trials'].append(row);save();after={'host':host(),'processes':usage(pids[mode])};row.update(after=after,resource_observations=resource_observations(before,after));save();require(math.isfinite(row['elapsed_s']) and row['elapsed_s']>0 and row['wav_bytes']==478252,'Primary timing invalid');stop=pressure_during_call(before,after)
       if stop:raise RuntimeError(stop)
     memory();group['sentinels'].append({'position':'after',**call('original')});save()
     for mode in MODES:check(mode,'after',1);check(mode,'after',2)
    finally:
     for mode,browser in browsers.items():
      targets=[]
      try:
       root=psutil.Process(pids[mode]);targets=[root,*root.children(recursive=True)]
      except (KeyError,psutil.NoSuchProcess):pass
      try:browser.close()
      except Exception:cleanup_safe=False
      _,pending=psutil.wait_procs(targets,timeout=10);alive=[]
      for q in pending:
       try:
        if q.is_running() and q.status()!=psutil.STATUS_ZOMBIE:alive.append(q)
       except psutil.NoSuchProcess:pass
      confirmed=bool(targets) and not alive
      group['cleanup'][mode]={'observed_processes':len(targets),'remaining_processes':len(alive),'confirmed':confirmed};cleanup_safe=cleanup_safe and confirmed;save()
     require(cleanup_safe,'Browser descendants remain; private files retained')
  reference=None;result['status']='complete';save()
 except BaseException as e:result['status']='failed';result['error_type']=type(e).__name__;save();raise
 finally:
  reference=None
  if work is not None and cleanup_safe:shutil.rmtree(work)
  result['private_temporary_directory_deleted']=work is None or not work.exists();save()
 try:validate(result)
 except BaseException as e:result['status']='failed';result['error_type']=type(e).__name__;save();raise
if __name__=='__main__':main()
