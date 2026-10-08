"""Explicit --screen is required for the bounded timing operation."""
import argparse,pathlib,json,http.server,functools,threading,time,os,shlex,re,statistics,math,platform
import psutil
from playwright.sync_api import sync_playwright
P=pathlib.Path(__file__).resolve().parent
parser=argparse.ArgumentParser();parser.add_argument('--screen',action='store_true');args=parser.parse_args()
def safe_numbers(x):
 if isinstance(x,float) and not math.isfinite(x):return {'nonfinite':str(x)}
 if isinstance(x,list):return [safe_numbers(v) for v in x]
 if isinstance(x,dict):return {k:safe_numbers(v) for k,v in x.items()}
 return x
expected=json.loads((P/'expected.json').read_text());(P/'results').mkdir(exist_ok=True)
class Handler(http.server.SimpleHTTPRequestHandler):
 def end_headers(self):
  self.send_header('Cross-Origin-Opener-Policy','same-origin');self.send_header('Cross-Origin-Embedder-Policy','require-corp');super().end_headers()
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(P)));threading.Thread(target=server.serve_forever,daemon=True).start()
def resource():
 procs=[]
 for process in [psutil.Process(),*psutil.Process().children(recursive=True)]:
  try:
   c=process.cpu_times();stat=pathlib.Path(f'/proc/{process.pid}/stat').read_text();fields=stat[stat.rfind(')')+2:].split()
   procs.append(dict(pid=process.pid,creationTime=process.create_time(),name=process.name(),cpuSeconds=c.user+c.system,rssBytes=process.memory_info().rss,threads=process.num_threads(),minorFaults=int(fields[7]),majorFaults=int(fields[9])))
  except (psutil.NoSuchProcess,psutil.AccessDenied,OSError):pass
 cpu=pathlib.Path('/sys/fs/cgroup/cpu.stat');pressure={name:pathlib.Path('/proc/pressure',name).read_text() for name in ['cpu','memory','io'] if pathlib.Path('/proc/pressure',name).exists()}
 return dict(epoch=time.time(),processes=procs,totalRssBytes=sum(p['rssBytes'] for p in procs),totalThreads=sum(p['threads'] for p in procs),cgroupCpuStat=cpu.read_text() if cpu.exists() else None,loadAverage=list(os.getloadavg()),hostAvailableBytes=psutil.virtual_memory().available,hostSwapUsedBytes=psutil.swap_memory().used,pressure=pressure)
def exposure(before,after):
 key=lambda p:(p['pid'],p['creationTime'])
 b={key(p):p for p in before['processes']};a={key(p):p for p in after['processes']}
 parse=lambda text:{line.split()[0]:int(line.split()[1]) for line in (text or '').splitlines()}
 bc,ac=parse(before['cgroupCpuStat']),parse(after['cgroupCpuStat'])
 return dict(scope='Round-level snapshots only, not per-call cleanliness',newProcesses=list(a.keys()-b.keys()),exitedProcesses=list(b.keys()-a.keys()),majorFaultDelta=sum(max(0,a[k]['majorFaults']-b[k]['majorFaults']) for k in a.keys()&b.keys()),minorFaultDelta=sum(max(0,a[k]['minorFaults']-b[k]['minorFaults']) for k in a.keys()&b.keys()),throttledUsecDelta=ac.get('throttled_usec',0)-bc.get('throttled_usec',0),hostSwapUsedChange=after['hostSwapUsedBytes']-before['hostSwapUsedBytes'])
def idle(page,observations):
 consecutive=0
 for attempt in range(10):
  before=resource();time.sleep(1);after=resource();b={(p['pid'],p['creationTime']):p['cpuSeconds'] for p in before['processes']};a={(p['pid'],p['creationTime']):p['cpuSeconds'] for p in after['processes']};delta=sum(max(0,a[k]-b[k]) for k in a.keys()&b.keys());new=list(a.keys()-b.keys());exited=list(b.keys()-a.keys())
  observations.append(dict(before=before,after=after,cpuSeconds=delta,newProcesses=new,exitedProcesses=exited));consecutive=consecutive+1 if delta<=0.10 and not new and not exited else 0
  if consecutive==2:return observations
 raise RuntimeError('Idle readiness failed after10 retained windows')
def rpc(page,c,action,**kw):return page.evaluate('v=>rpc(v.condition,v.action,v.args)',dict(condition=c,action=action,args=kw))
def check_metadata(m):assert m['shared'] and m['isolated'] and m['heapBytes']==1073741824

def check_hashes(rows):
 assert [r['dilation'] for r in rows]==[1,3,5]
 for r in rows:
  assert r['hashes']==expected[str(r['dilation'])]['output'] and r['packedHash']==expected[str(r['dilation'])]['packed'];assert r['selection'] and r['heapBytes']==1073741824

def open_browser(pw,label):
 trace=P/'results'/f'{label}-browser-stderr.log';wrapper=P/'results'/f'{label}-launch.sh';executable=os.environ.get('CHROMIUM',pw.chromium.executable_path)
 wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(executable)+' "$@" 2>>'+shlex.quote(str(trace))+'\n');wrapper.chmod(0o700)
 browser=pw.chromium.launch(executable_path=str(wrapper),headless=True,args=['--js-flags=--no-wasm-revectorize','--no-sandbox']);assert browser.version.startswith('153.'),browser.version
 page=browser.new_page();page.goto(f'http://127.0.0.1:{server.server_port}/index.html');version=browser.new_browser_cdp_session().send('Browser.getVersion')
 return browser,page,trace,version

def close_browser(browser,trace):
 browser.close();text=trace.read_text();assert not re.search(r'unrecognized flag|unknown flag|bad option',text,re.I)

schedule=json.loads((P/'schedule.json').read_text())
report=dict(scope='Warm standalone reshape+setup+run; no full CORE claim',screen_requested=args.screen,schedule=schedule,hardware=dict(platform=platform.platform(),cpuModel=next((l.split(':',1)[1].strip() for l in pathlib.Path('/proc/cpuinfo').read_text().splitlines() if l.startswith('model name')),None),affinity=list(os.sched_getaffinity(0)),logicalCPUs=os.cpu_count()),diagnostic=[],sets=[])
try:
 with sync_playwright() as pw:
  browser,page,trace,version=open_browser(pw,'diagnostic');report['diagnosticEngine']=version
  try:
   for c in range(3):
    metadata=rpc(page,c,'init',kind='diagnostic');check_metadata(metadata)
    if c:assert metadata['hardwareConcurrency']==report['diagnostic'][0]['metadata']['hardwareConcurrency']
    rows=rpc(page,c,'diagnostic');assert len(rows)==3
    for row in rows:
     e=expected[str(row['dilation'])];assert row['hashes']==[e['output'][0],e['output'][0],e['output'][1]] and row['packedHash']==e['packed'];selection=row['selection'];assert selection['which']==c and selection['kernel_mr4_linear_loadsplat']==1 and selection['threads']==2
     assert len(row['runs'])==3
     for run in row['runs']:
      assert run['bad']==0 and run['callbacks']==(15392 if c==0 else 1924) and run['calls']==(61568 if c==2 else 15392)
      assert len(set(run['thread_ids']))==2 and all(run['thread_callbacks'])
    report['diagnostic'].append(dict(metadata=metadata,records=rows));page.evaluate('stopAll()')
  finally:close_browser(browser,trace)
  for s in range(3 if args.screen else 1):
   browser,page,trace,version=open_browser(pw,f'set{s}');assert version==report['diagnosticEngine'];trial=dict(set=s,engine=version,metadata=[],pre=[],post=[],warmups=[],rounds=[]);report['sets'].append(trial)
   try:
    for c in range(3):
     m=rpc(page,c,'init',kind='production');check_metadata(m);assert m['hardwareConcurrency']==report['diagnostic'][0]['metadata']['hardwareConcurrency'];trial['metadata'].append(m)
     hashes=rpc(page,c,'check');check_hashes(hashes);trial['pre'].append(hashes)
    if args.screen:
     for c in [(s+i)%3 for i in range(3)]:trial['warmups'].extend(rpc(page,c,'warm'))
     (P/'results'/f'set{s}-warmups.json').write_text(json.dumps(safe_numbers(trial['warmups']),indent=2))
     trial['idleReadiness']=[];idle(page,trial['idleReadiness'])
     for r,items in enumerate(schedule[s]):
      rr=dict(round=r,before=resource(),calls=[]);trial['rounds'].append(rr)
      for item in items:
       sample=rpc(page,item['condition'],'sample',q=item['q'])
       with (P/'results/raw-calls.jsonl').open('a') as stream:stream.write(json.dumps(safe_numbers(dict(set=s,round=r,returned=sample)))+'\n')
       rr['calls'].extend(safe_numbers(sample));assert len(sample)==1;record=sample[0]
       assert record['condition']==item['condition'] and record['dilation']==item['dilation'] and record['heapBytes']==1073741824 and math.isfinite(record['latencyMs']) and record['latencyMs']>0
      rr['after']=resource();rr['resourceExposure']=exposure(rr['before'],rr['after'])
    for c in range(3):
     hashes=rpc(page,c,'check');check_hashes(hashes);trial['post'].append(hashes)
   finally:close_browser(browser,trace)
 if args.screen:
  assert len(report['sets'])==3 and sum(len(s['warmups']) for s in report['sets'])==81 and sum(len(r['calls']) for s in report['sets'] for r in s['rounds'])==81
  ratios=[]
  for s in report['sets']:
   for d in [1,3,5]:
    triplets=[{x['condition']:x['latencyMs'] for x in r['calls'] if x['dilation']==d} for r in s['rounds']]
    assert all(set(t)=={0,1,2} for t in triplets)
    ratios.append(dict(set=s['set'],dilation=d,B_over_A=math.exp(statistics.mean(math.log(t[1]/t[0]) for t in triplets)),C_over_B=math.exp(statistics.mean(math.log(t[2]/t[1]) for t in triplets)),C_over_A=math.exp(statistics.mean(math.log(t[2]/t[0]) for t in triplets))))
  report['paired_geomean_ratios_per_set_and_dilation']=ratios
 report['passed']=True
except BaseException as e:report['passed']=False;report['error']=str(e);raise
finally:
 (P/'results/screen.json').write_text(json.dumps(safe_numbers(report),indent=2));server.shutdown()
