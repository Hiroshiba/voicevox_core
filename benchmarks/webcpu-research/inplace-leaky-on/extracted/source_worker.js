'use strict';
// Hook-free primary worker. Alias instrumentation is injected into a separate untimed worker only.
importScripts('/core_dispatch_check.js');
let ready=false,stdout=[],variant,entry;
const digest=async b=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b))).map(x=>x.toString(16).padStart(2,'0')).join('');
const failure=e=>postMessage({error:e?.name||'Error'});
/*GATE_IMPORT*/
onmessage=async({data})=>{try{
 if(data.command==='init'){
  if(!self.crossOriginIsolated||typeof SharedArrayBuffer==='undefined')throw Error('Isolation required');
  variant=data.variant;const manifest=await(await fetch('/runtime_manifest.json')).json();entry=manifest[variant];if(!entry)throw Error('Variant');const cpu=variant==='cpu';
  const wasm=await(await fetch('/'+variant+'/voicevox_benchmark.wasm')).arrayBuffer(),wasmSha=await digest(wasm);if(wasmSha!==entry.wasm_sha256)throw Error('Wasm SHA');
  const moduleUrl='/'+variant+'/voicevox_benchmark.js',jsBytes=await(await fetch(moduleUrl)).arrayBuffer(),jsSha=await digest(jsBytes);if(jsSha!==entry.js_sha256)throw Error('JS SHA');
  globalThis.Module={noInitialRun:true,benchmarkPoolSize:2,wasmBinary:new Uint8Array(wasm),mainScriptUrlOrBlob:new URL(moduleUrl,self.location).href,
   locateFile:p=>new URL(p,new URL(moduleUrl,self.location)).href,print:(...v)=>stdout.push(v.join(' ')),printErr:()=>{},onAbort:failure,
   onRuntimeInitialized:async()=>{try{
    for(const f of ['sample.vvm','query.json'])Module.FS.writeFile('/'+f,new Uint8Array(await(await fetch('/'+f)).arrayBuffer()));
    if(Module._bench_init(2,cpu?0:1,cpu?0:1,cpu?0:2,0)!==0)throw Error('Init');for(const f of ['sample.vvm','query.json'])Module.FS.unlink('/'+f);
    const runtime={shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads:Module.PThread.runningWorkers.length,spin_off:!!Module._bench_spin_off(),fixed_length:Module._bench_fixed_length(),fixed_matches:Module._bench_fixed_matches(),xnn_threads:Module._bench_xnn_threads(),xnn_sessions:Module._bench_xnn_sessions(),configured_ort_global_threads:cpu?2:1};
    if(!runtime.shared_memory||runtime.pthreads!==1)throw Error('Shared/pthread gate');
    if(!cpu&&(!runtime.spin_off||runtime.fixed_length!==962||runtime.fixed_matches!==1||runtime.xnn_threads!==2||runtime.xnn_sessions!==1))throw Error('XNN runtime gate');
    if(cpu&&(runtime.xnn_threads!==0||runtime.xnn_sessions!==0||runtime.fixed_matches!==0||runtime.spin_off))throw Error('CPU runtime gate');
    const dispatch=cpu?null:verifyActualCoreDispatch(Module,stdout,'loadsplat');if(!cpu&&!dispatch.verified)throw Error('Dispatch gate');
    ready=true;postMessage({ready:true,runtime,dispatch,wasm_sha256:wasmSha,js_sha256:jsSha,timing_instrumentation:'clock_only'});
   }catch(e){failure(e);}}};importScripts(moduleUrl);
 }else if(data.command==='synthesize'){
  if(!ready)throw Error('Not ready');const start=performance.now(),code=Module._bench_synthesize(302),elapsed_s=(performance.now()-start)/1000;
  if(code!==0)throw Error('Synthesis');postMessage({elapsed_s,wav_bytes:Module._bench_wav_len()});
 }else if(data.command==='check'){
  if(!ready)throw Error('Not ready');const alias=[];
  function invoke(kind){
   /*GATE_BEFORE*/
   try{if((kind==='wav'?Module._bench_synthesize(302):Module._bench_raw(302))!==0)throw Error('Synthesis');}
   finally{/*GATE_AFTER*/}
  }
  invoke('wav');const wav=Module.HEAPU8.slice(Module._bench_wav_ptr(),Module._bench_wav_ptr()+Module._bench_wav_len()).buffer;
  invoke('raw');const raw=Module.HEAPU8.slice(Module._bench_raw_ptr(),Module._bench_raw_ptr()+Module._bench_raw_len()).buffer;
  postMessage({wav,raw,alias},[wav,raw]);
 }else throw Error('Unsupported command');
}catch(e){failure(e);}};
