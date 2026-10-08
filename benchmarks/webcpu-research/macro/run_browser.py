import pathlib,json,http.server,functools,threading,sys,os,shlex,re
from playwright.sync_api import sync_playwright
P=pathlib.Path(__file__).parent
class H(http.server.SimpleHTTPRequestHandler):
 def end_headers(self):
  self.send_header('Cross-Origin-Opener-Policy','same-origin');self.send_header('Cross-Origin-Embedder-Policy','require-corp');super().end_headers()
 def log_message(self,*a):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(H,directory=str(P)));threading.Thread(target=server.serve_forever,daemon=True).start()
widths=[1,2,3,4,5,31,32,33,63,64,65,61568]
cases=[[m,128,11,d,seed] for seed in [17,731] for d in [1,3,5] for m in widths]+[[65,c,k,1,17] for c,k in [(128,7),(64,11),(256,11)]]
if '--smoke' in sys.argv:cases=[[33,128,11,1,17]]
report={'untimed':True,'cases':cases,'conditions':{}}
try:
 with sync_playwright() as pw:
  executable=os.environ.get('CHROMIUM',pw.chromium.executable_path)
  trace=P/'results/browser-stderr.log';wrapper=P/'results/launch-browser.sh'
  wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(executable)+' "$@" 2>>'+shlex.quote(str(trace))+'\n');wrapper.chmod(0o700)
  browser=pw.chromium.launch(executable_path=str(wrapper),headless=True,args=['--js-flags=--no-wasm-revectorize,--trace-wasm-revectorize','--no-sandbox'])
  report['browser']=browser.version;report['js_flags']='--no-wasm-revectorize';report['affinity']=list(os.sched_getaffinity(0));page=browser.new_page();page.on('console',lambda m:print(m.text,flush=True));page.goto(f'http://127.0.0.1:{server.server_port}/index.html')
  for condition in range(3):report['conditions'][condition]=page.evaluate('args=>run(args)',dict(condition=condition,cases=cases))
  browser.close()
  trace_text=trace.read_text();assert not re.search(r'unrecognized flag|unknown flag|bad option',trace_text,re.I)
  assert not re.search(r'Decide(?:d)? to vectorize, [1-9][0-9]* revectorizable nodes',trace_text)
  report['revectorization_flag_accepted']=True;report['positive_revectorization_nodes']=0
 for j,case in enumerate(cases):
  results=[report['conditions'][i][j] for i in range(3)]
  assert all(r['shared'] and r['crossOriginIsolated'] for r in results)
  assert all(r['hashes']==results[0]['hashes'] and r['packedHash']==results[0]['packedHash'] for r in results)
  assert all(r['hashes'][0]==r['hashes'][1] and r['hashes'][0]!=r['hashes'][2] for r in results)
 for condition in range(3):
  rows=[]
  for r in report['conditions'][condition]:
   lines=r['lines']; rows.append(dict(condition=condition,case=r['case'],hashes=r['hashes'],packedHash=r['packedHash'],selection=json.loads(next(s[7:] for s in lines if s.startswith('SELECT '))),runs=[json.loads(s[4:]) for s in lines if s.startswith('RUN ')]))
  (P/'results'/f'browser-condition{condition}.json').write_text(json.dumps(dict(browser=report['browser'],records=rows),indent=2))
 report['passed']=True
except BaseException as e:
 report['passed']=False;report['error']=str(e);raise
finally:
 (P/'results'/('browser-smoke.json' if '--smoke' in sys.argv else 'browser-correctness.json')).write_text(json.dumps(report,indent=2));server.shutdown()
