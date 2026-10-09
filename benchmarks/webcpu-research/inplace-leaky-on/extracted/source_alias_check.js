'use strict';
// Untimed browser/Node diagnostic. Call within try/finally and restore before return.
function leakyForwarder(callback){
 const sec=(id,p)=>[id,p.length,...p];
 const bytes=new Uint8Array([0,97,115,109,1,0,0,0,...sec(1,[1,96,3,127,127,127,0]),...sec(2,[1,3,101,110,118,3,116,97,112,0,0]),...sec(3,[1,0]),...sec(7,[1,1,102,0,1]),...sec(10,[1,10,0,32,0,32,1,32,2,16,0,11])]);
 return new WebAssembly.Instance(new WebAssembly.Module(bytes),{env:{tap:callback}}).exports.f;
}
function installLeakyAliasCheck(module,table,pin,variant){
 if(!['original','rebuilt','candidate'].includes(variant)||pin.callback_body_sha256!==(variant==='candidate'?'5b2c6e2ddf5d836b4bde745de8fe76b845014669a9718d574fbed5dd5df27c3b':'30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6')||pin.callback_table.table!==0)throw Error('Invalid alias proof input');
 const slot=pin.callback_table.slot,index=pin.absolute_function_index;
 if(!Number.isInteger(slot)||slot<0||slot>=table.length||!Number.isInteger(index))throw Error('Invalid callback table mapping');
 const original=table.get(slot);
 if(typeof original!=='function'||original.name!==String(index))throw Error('Actual callback identity differs from module mapping');
 const result={variant,table_slot:slot,function_index:index,callbacks:0,elements:0,classes:{},predicted_branch:{simd:{calls:0,elements:0},scalar:{calls:0,elements:0}},length_histogram:{},restored:false,metadata_errors:0,scope:'Observed actual pointers and verified Wasm guard; eligibility evidence, not hardware branch tracing.'};
 const hook=leakyForwarder((context,beginPtr,endPtr)=>{
  try{
   const h=new Uint32Array(module.HEAPU8.buffer),u=[context,beginPtr,endPtr].map(x=>x>>>0);
   if(u.some(x=>x%4)||u[0]+20>module.HEAPU8.byteLength||u[1]+4>module.HEAPU8.byteLength||u[2]+4>module.HEAPU8.byteLength)throw Error('Invalid metadata address');
   const begin=h[u[1]>>>2],end=h[u[2]>>>2],ib=h[(u[0]+8)>>>2],ob=h[(u[0]+12)>>>2],n=(end-begin)|0;
   if(n<0||n>0x1fffffff)throw Error('Invalid range');const input=ib+begin*4,output=ob+begin*4,bytes=n*4;
   if(input+bytes>module.HEAPU8.byteLength||output+bytes>module.HEAPU8.byteLength)throw Error('Invalid bounds');
   const cls=input===output?'exact_inplace':input+bytes<=output||output+bytes<=input?'disjoint':output>input?'partial_overlap_after':'partial_overlap_before';
   const simd=n>=4&&((variant==='candidate'&&ib===ob)||((ob-ib)>>>0)>=16);
   result.callbacks++;result.elements+=n;const a=result.classes[cls]??={calls:0,elements:0};a.calls++;a.elements+=n;
   const branch=result.predicted_branch[simd?'simd':'scalar'];branch.calls++;branch.elements+=n;result.length_histogram[n]=(result.length_histogram[n]||0)+1;
  }catch(e){result.metadata_errors++;throw Error('Alias metadata validation failed');}
  return original(context,beginPtr,endPtr);
 });
 table.set(slot,hook);
 return {result,restore(){table.set(slot,original);result.restored=table.get(slot)===original;if(!result.restored)throw Error('Callback restore failed');}};
}
if(typeof module!=='undefined')module.exports={leakyForwarder,installLeakyAliasCheck};
