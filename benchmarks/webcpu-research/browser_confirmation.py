#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]
# ///
"""Fresh-process paired CORE browser confirmation; no high-frequency sampling in primary trials."""
import argparse,contextlib,hashlib,importlib.util,json,os,random,shutil,sys,tempfile,time
from pathlib import Path
from dataclasses import asdict
from validate_confirmation import validate,pressure_during_call
import psutil
from playwright.sync_api import sync_playwright
p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--revec',choices=['off','on'],default='off');p.add_argument('--process-sets',type=int,default=3);p.add_argument('--pairs',type=int,default=5);p.add_argument('--warmups',type=int,default=5);a=p.parse_args();assert a.process_sets >= 1 and a.pairs >= 1 and a.warmups >= 1
root=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('bench',root/'voicevox_webcpu_research.py');b=importlib.util.module_from_spec(spec);sys.modules[spec.name]=b;spec.loader.exec_module(b)
man=json.loads(a.manifest.read_text());entries=[e for e in man['variants'] if e['key'] in ['original','combined']];assert [e['key'] for e in entries]==['original','combined']
os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(a.cache.resolve()/'playwright')
modes=[b.Mode(e['key'],e['label'],e['threads'],'FP32 model-only confirmation',experimental=i>0,revectorize=a.revec=='on') for i,e in enumerate(entries)]
result={'schema':'voicevox-browser-paired-confirmation-v1','status':'initializing','revectorization':a.revec,'manifest_sha256':b.sha256_file(a.manifest),'harness_sha256':b.sha256_file(Path(__file__)),'research_harness_sha256':b.sha256_file(root/'voicevox_webcpu_research.py'),'environment':b.environment_info(),'provenance':{},'diagnostics':{},'process_sets':[],'trials':[],'cpu_companion':[],'notes':['Primary latency is unsampled; separate CPU companion retains process-tree time series.','Five warmups are recorded, not a proof of tiering convergence.','Fifteen paired observations in three fresh browser-process sets; analyze paired effects and process-set clusters.','Memory pressure guard compares each measured call before/after; setup swap activity is recorded but is not attributed to a timed call.', 'Raw FP32 and PCM remain private and are removed after checks.']}
def save():b.atomic_write(a.output,json.dumps(result,indent=2).encode())
def host():
 m=psutil.virtual_memory();s=psutil.swap_memory();return {'available_bytes':m.available,'total_bytes':m.total,'swap_used':s.used,'swap_in':s.sin,'swap_out':s.sout,'loadavg':list(os.getloadavg())}
def usage(pid):
 out=[]
 for q in [psutil.Process(pid),*psutil.Process(pid).children(recursive=True)]:
  try:
   stat=Path(f'/proc/{q.pid}/stat').read_text().rsplit(')',1)[1].split() if sys.platform.startswith('linux') else None
   out.append({'pid':q.pid,'created':q.create_time(),'rss':q.memory_info().rss,'cpu':q.cpu_times()._asdict(),'major_faults':int(stat[9]) if stat else None,'minor_faults':int(stat[7]) if stat else None})
  except (psutil.NoSuchProcess,psutil.AccessDenied,FileNotFoundError,PermissionError):pass
 return out
initial_host=host();result['initial_host']=initial_host;save()
try:
 with tempfile.TemporaryDirectory(prefix='voicevox-confirm-private-') as td:
  work=Path(td);(work/'index.html').write_text(b.BROWSER_PAGE);(work/'worker.js').write_text(b.BROWSER_WORKER);query=json.dumps(b.prepared_query(10),separators=(',',':'),ensure_ascii=False).encode();(work/'query.json').write_bytes(query);result['query_sha256']=hashlib.sha256(query).hexdigest()
  for e in entries:
   binary=Path(e['binary']);model=Path(e['model']);receipt=json.loads((binary.parent/'complete.json').read_text());assert receipt['identity']==e['build_identity'] and binary.name in receipt['files'];assert b.sha256_file(model)==e['model_sha256']
   for name,sha in receipt['files'].items():assert b.sha256_file(binary.parent/name)==sha
   folder=work/e['key'];folder.mkdir()
   for f in binary.parent.glob('voicevox_benchmark.*'):
    if f.suffix in ['.js','.wasm']:shutil.copyfile(f,folder/f.name)
   shutil.copyfile(model,folder/'sample.vvm');result['provenance'][e['key']]={'receipt':receipt,'receipt_sha256':b.sha256_file(binary.parent/'complete.json'),'model_sha256':e['model_sha256']}
  with b.serve_assets(work) as url,sync_playwright() as pw:
   for e,m in zip(entries,modes):
    diagnostic=b.verify_research_revectorization(e,m,{'headless':True},url,work,a.output,302,require_on=a.revec=='on');result['diagnostics'][m.key]=diagnostic;save();assert diagnostic['on_verified' if m.revectorize else 'off_verified']
   reference=None
   for replica in range(1,a.process_sets+1):
    browsers={};runners={};pids={};group={'id':replica,'warmups':[],'checks':{},'runtime':{},'sentinels':[]};result['process_sets'].append(group);save()
    try:
     for e,m in zip(entries,modes):
      browser=pw.chromium.launch(headless=True,args=['--js-flags='+('--wasm-revectorize' if m.revectorize else '--no-wasm-revectorize')]);browsers[m.key]=browser;pids[m.key]=b.chromium_process_id(browser);engine=b.browser_engine_info(browser);checked=result['diagnostics'][m.key]['on' if m.revectorize else 'off'];assert (engine['product'],engine['js_version'])==(checked['product'],checked['js_version'])
      r=b.BrowserRunner(browser,url,'/'+m.key+'/voicevox_benchmark.js',m.threads,True,302,work/(m.key+'.wav'),model_url='/'+m.key+'/sample.vvm');runners[m.key]=r
      group['runtime'][m.key]={'engine':engine,'browser_pid':pids[m.key],'browser_created':psutil.Process(pids[m.key]).create_time(),'browser':browser.version,'launch_args':['--js-flags='+('--wasm-revectorize' if m.revectorize else '--no-wasm-revectorize')],'hardware_concurrency':r.page.evaluate('navigator.hardwareConcurrency'),'threads_requested':m.threads,'pthreads_running':r.thread_state(),'fixed_matches':r.fixed_matches,'xnn_sessions':r.xnn_sessions,'shared_memory':r.shared_memory}
      for iteration in range(a.warmups):
       elapsed,duration=r.synthesize(save=True);group['warmups'].append({'mode':m.key,'iteration':iteration+1,'elapsed_s':elapsed,'audio_s':duration});save()
      wav,raw=r.wav.read_bytes(),r.raw_wave();r.synthesize(save=True);repeat=b.waveform_comparison(wav,raw,r.wav.read_bytes(),r.raw_wave());group['checks'][m.key]={'repeat':repeat};save();assert repeat['pcm_exact'] and repeat['fp32_exact']
      if reference is None:reference=(wav,raw)
      diff=b.waveform_comparison(reference[0],reference[1],wav,raw);group['checks'][m.key]['versus_initial_control']=diff;group['runtime'][m.key]['pthreads_after_warmup']=r.thread_state();save();assert diff['pcm_exact'] and diff['fp32_exact']
     assert len(set(pids.values()))==len(entries), 'Conditions must use separate Chromium processes'
     elapsed,duration=runners['original'].synthesize();group['sentinels'].append({'position':'before','elapsed_s':elapsed,'audio_s':duration});save()
     for pair in range(1,a.pairs+1):
      order=['original','combined'] if (replica+pair)%2 else ['combined','original']
      for key in order:
       before={'host':host(),'processes':usage(pids[key])};elapsed,duration=runners[key].synthesize();after={'host':host(),'processes':usage(pids[key])}
       row={'process_set':replica,'pair':pair,'mode':key,'pair_order':order,'elapsed_s':elapsed,'audio_s':duration,'before':before,'after':after};result['trials'].append(row);save();print('PRIMARY_TRIAL '+json.dumps(row),flush=True)
       pressure=pressure_during_call(before,after)
       if pressure:raise RuntimeError(pressure+'; saved trial retained')
     elapsed,duration=runners['original'].synthesize();group['sentinels'].append({'position':'after','elapsed_s':elapsed,'audio_s':duration});save()
     # Separate instrumented companion, excluded from primary latency comparison.
     for key in (['original','combined'] if replica%2 else ['combined','original']):
      elapsed,duration,cpu=b.ProcessCpuSampler(pids[key],key).measure(runners[key].synthesize);result['cpu_companion'].append({'process_set':replica,'mode':key,'elapsed_s':elapsed,'audio_s':duration,**asdict(cpu)});save()
     for key,r in runners.items():
      r.synthesize(save=True);diff=b.waveform_comparison(reference[0],reference[1],r.wav.read_bytes(),r.raw_wave());group['checks'][key]['after']=diff;save();assert diff['pcm_exact'] and diff['fp32_exact']
    finally:
     for r in runners.values():
      with contextlib.suppress(Exception):r.close()
     for browser in browsers.values():
      with contextlib.suppress(Exception):browser.close()
   validate(result,a.process_sets,a.pairs,a.warmups);result['status']='complete';save()
except BaseException as exc:result['status']='failed';result['error']=repr(exc);save();raise
