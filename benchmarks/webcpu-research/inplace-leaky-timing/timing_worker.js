'use strict';
// Clock-only primary command. No callback replacement or alias-forwarding code.
importScripts('/core_dispatch_check.js');
let ready=false,stdout=[],pin,variant;
const digest=async b=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b))).map(x=>x.toString(16).padStart(2,'0')).join('');
const failure=e=>postMessage({error:e?.name||'Error'});
onmessage=async({data})=>{try{
 if(data.command==='init'){
  if(!self.crossOriginIsolated||typeof SharedArrayBuffer==='undefined')throw Error('Isolation required');
  variant=data.variant;if(!['original','candidate'].includes(variant))throw Error('Variant');pin=await(await fetch('/transform.json')).json();
  const wasm=await(await fetch('/'+variant+'/voicevox_benchmark.wasm')).arrayBuffer();
  const wasmSha=await digest(wasm);if(wasmSha!==(variant==='original'?pin.source_wasm_sha256:pin.candidate_wasm_sha256))throw Error('Wasm SHA');
  const moduleUrl='/'+variant+'/voicevox_benchmark.js',jsBytes=await(await fetch(moduleUrl)).arrayBuffer(),jsSha=await digest(jsBytes);if(jsSha!==pin.source_js_sha256)throw Error('JS SHA');
  globalThis.Module={noInitialRun:true,benchmarkPoolSize:2,wasmBinary:new Uint8Array(wasm),mainScriptUrlOrBlob:new URL(moduleUrl,self.location).href,
   locateFile:p=>new URL(p,new URL(moduleUrl,self.location)).href,print:(...v)=>stdout.push(v.join(' ')),printErr:()=>{},onAbort:failure,
   onRuntimeInitialized:async()=>{try{
    for(const f of ['sample.vvm','query.json'])Module.FS.writeFile('/'+f,new Uint8Array(await(await fetch('/'+f)).arrayBuffer()));
    if(Module._bench_init(2,1,1,2,0)!==0)throw Error('Init');for(const f of ['sample.vvm','query.json'])Module.FS.unlink('/'+f);
    const runtime={shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads:Module.PThread.runningWorkers.length,spin_off:!!Module._bench_spin_off(),fixed_length:Module._bench_fixed_length(),fixed_matches:Module._bench_fixed_matches(),xnn_threads:Module._bench_xnn_threads(),xnn_sessions:Module._bench_xnn_sessions(),configured_ort_global_threads:1};
    if(!runtime.shared_memory||runtime.pthreads!==1||!runtime.spin_off||runtime.fixed_length!==962||runtime.fixed_matches!==1||runtime.xnn_threads!==2||runtime.xnn_sessions!==1)throw Error('Runtime gate');
    const dispatch=verifyActualCoreDispatch(Module,stdout,'loadsplat');if(!dispatch.verified)throw Error('Dispatch gate');
    ready=true;postMessage({ready:true,runtime,dispatch,wasm_sha256:wasmSha,js_sha256:jsSha,timing_instrumentation:'clock_only'});
   }catch(e){failure(e);}}};importScripts(moduleUrl);
 }else if(data.command==='synthesize'){
  if(!ready)throw Error('Not ready');const start=performance.now();const code=Module._bench_synthesize(302);const elapsed_s=(performance.now()-start)/1000;
  if(code!==0)throw Error('Synthesis');postMessage({elapsed_s,wav_bytes:Module._bench_wav_len()});
 }else if(data.command==='check'){
  if(!ready)throw Error('Not ready');if(Module._bench_synthesize(302)!==0)throw Error('WAV check');const wav=Module.HEAPU8.slice(Module._bench_wav_ptr(),Module._bench_wav_ptr()+Module._bench_wav_len()).buffer;
  if(Module._bench_raw(302)!==0)throw Error('Raw check');const raw=Module.HEAPU8.slice(Module._bench_raw_ptr(),Module._bench_raw_ptr()+Module._bench_raw_len()).buffer;postMessage({wav,raw},[wav,raw]);
 }else throw Error('Unsupported command');
}catch(e){failure(e);}};
