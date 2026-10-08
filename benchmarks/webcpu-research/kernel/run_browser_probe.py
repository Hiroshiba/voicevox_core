#!/usr/bin/env python3
import argparse,functools,http.server,json,pathlib,threading,time,re,shlex,shutil,tempfile,os
from playwright.sync_api import sync_playwright
p=argparse.ArgumentParser()
p.add_argument('--revec',choices=['off','on'],required=True)
p.add_argument('--validate-only',action='store_true')
p.add_argument('--output',type=pathlib.Path,required=True)
p.add_argument('--browser',default='/usr/bin/chromium')
p.add_argument('--dispatch',action='store_true')
p.add_argument('--trace',action='store_true')
a=p.parse_args()
class Handler(http.server.SimpleHTTPRequestHandler):
 def end_headers(self):
  self.send_header('Cross-Origin-Opener-Policy','same-origin')
  self.send_header('Cross-Origin-Embedder-Policy','require-corp')
  super().end_headers()
 def log_message(self,*args):pass
root=pathlib.Path(__file__).parent
a.output.parent.mkdir(parents=True,exist_ok=True)
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(root)))
threading.Thread(target=server.serve_forever,daemon=True).start()
wrapper=None
result={'metadata':{'requestedRevectorization':a.revec,'traceEnabled':a.trace,'timeUnix':time.time()}}
trace_path=a.output.with_suffix('.v8-trace.txt')
try:
 with sync_playwright() as pw:
  flag='--wasm-revectorize' if a.revec=='on' else '--no-wasm-revectorize'
  executable=a.browser
  if a.trace:
   flag+=',--trace-wasm-revectorize'
   trace_path.write_text('')
   fd,path=tempfile.mkstemp(prefix='xnn-browser-',suffix='.sh');os.close(fd)
   wrapper=pathlib.Path(path)
   wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(shutil.which(a.browser) or a.browser)+' "$@" >>'+shlex.quote(str(trace_path.resolve()))+' 2>&1\n')
   wrapper.chmod(0o700);executable=str(wrapper)
  result['metadata']['requestedJsFlags']=flag
  browser=pw.chromium.launch(executable_path=executable,headless=True,args=['--js-flags='+flag])
  try:
   result['metadata']['browserVersion']=browser.version
   page=browser.new_page();page.goto(f'http://127.0.0.1:{server.server_port}/probe.html')
   if a.dispatch:
    result['metadata']['scope']='standalone actual browser worker dispatch, not CORE worker'
    result['dispatch']={}
    for mode in ['original','auto-roundtrip','loadsplat','splat']:
     record=page.evaluate('(script)=>runDispatch(script)',f'archives/{mode}/dispatch_browser.js')
     result['dispatch'][mode]=record
     d=record['dispatch']
     assert d['is_x86']==1 and d['relaxedSimd']==0 and d['checkedPointers']==12 and record['crossOriginIsolated']
     assert d['navigatorHardwareConcurrency']==record['hardwareConcurrency']
     expected=mode if mode in ['loadsplat','splat'] else ('loadsplat' if record['hardwareConcurrency']>4 else 'splat')
     assert d[expected+'Pointers']==12
   else:
    measurements=page.evaluate('(v)=>runProbe(v)',a.validate_only)
    result['metadata'].update(measurements.pop('metadata'));result.update(measurements)
    assert result['metadata']['crossOriginIsolated']
  finally:browser.close()
  if a.trace:
   trace=trace_path.read_text(errors='replace')
   nodes=[int(n) for n in re.findall(r'Decide(?:d)? to vectorize, ([1-9][0-9]*) revectorizable nodes',trace)]
   rejected=bool(re.search(r'unrecognized flag|unknown flag|bad option',trace,re.I))
   result['metadata'].update({'positiveTransformationNodes':nodes,'flagRejected':rejected,'activationVerified':not rejected and bool(nodes)==(a.revec=='on')})
   if not result['metadata']['activationVerified']:raise RuntimeError('V8 revectorization activation not established by trace')
  result['passed']=True
except BaseException as e:
 result['passed']=False;result['error']=str(e)
 raise
finally:
 a.output.write_text(json.dumps(result,indent=2)+'\n')
 print(json.dumps(result['metadata']))
 if wrapper:wrapper.unlink(missing_ok=True)
 server.shutdown();server.server_close()
