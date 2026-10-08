let moduleInstance,condition,kind,lines=[];
const digest=async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
onmessage=async event=>{const {id,action,...args}=event.data;try{let result;
 if(action==='init'){
  condition=args.condition;kind=args.kind;importScripts('wasm/'+kind+condition+'.js');
  moduleInstance=await MacroProbe({locateFile:path=>new URL('wasm/'+path,location.href).href,mainScriptUrlOrBlob:new URL('wasm/'+kind+condition+'.js',location.href).href,print:s=>lines.push(s),printErr:s=>{throw Error(s)}});
  if(kind==='production'&&moduleInstance._initialize())throw Error('initialize');
  result={condition,kind,shared:moduleInstance.HEAPU8.buffer instanceof SharedArrayBuffer,isolated:crossOriginIsolated,heapBytes:moduleInstance.HEAPU8.buffer.byteLength,hardwareConcurrency:navigator.hardwareConcurrency};
 }else if(action==='check'){
  const p=moduleInstance;result=[];
  for(let q=0;q<3;q++){let hashes=[];for(let input of [0,1]){if(p._cycle(q,input))throw Error('cycle');if(!p._selection(q,condition))throw Error('selection');hashes.push(await digest(p.HEAPU8.slice(p._output(q),p._output(q)+p._output_bytes())));}
   result.push({dilation:2*q+1,hashes,packedHash:await digest(p.HEAPU8.slice(p._packed(q),p._packed(q)+p._packed_bytes(q))),selection:true,heapBytes:p.HEAPU8.buffer.byteLength});}
 }else if(action==='warm'||action==='sample'){
  const p=moduleInstance;result=[];const shapes=action==='warm'?[0,1,2]:[args.q];const count=action==='warm'?3:1;
  for(const q of shapes)for(let w=0;w<count;w++){
   const start=performance.now();const status=p._cycle(q,0);const end=performance.now();
   if(status)throw Error('cycle status '+status);
   result.push({condition,dilation:2*q+1,iteration:w,startEpochMs:performance.timeOrigin+start,endEpochMs:performance.timeOrigin+end,latencyMs:end-start,heapBytes:p.HEAPU8.buffer.byteLength});
  }
 }else if(action==='diagnostic'){
  const p=moduleInstance;result=[];
  for(const d of [1,3,5]){lines=[];if(p._setup(61568,128,11,d,17))throw Error('diagnostic setup');let hashes=[];for(const input of [0,0,1]){if(p._run_case(input))throw Error('diagnostic run');hashes.push(await digest(p.HEAPU8.slice(p._output(),p._output()+p._output_bytes())));}
   const packedHash=await digest(p.HEAPU8.slice(p._packed(),p._packed()+p._packed_bytes()));p._cleanup();
   result.push({condition,dilation:d,hashes,packedHash,selection:JSON.parse(lines.find(s=>s.startsWith('SELECT ')).slice(7)),runs:lines.filter(s=>s.startsWith('RUN ')).map(s=>JSON.parse(s.slice(4)))});
  }
 }else throw Error('unknown action');
 postMessage({id,result});
 }catch(error){postMessage({id,error:String(error),stack:error.stack});}};
