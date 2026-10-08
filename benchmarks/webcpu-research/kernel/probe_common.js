const median=a=>[...a].sort((a,b)=>a-b)[Math.floor(a.length/2)];
async function runKernelProbe(M,{validateOnly=false,onResult=()=>{}}={}) {
 let maxError=0;let checks=0;
 for(const ig of [0,1])for(const mr of [1,2,3,4])for(const nc of [1,7,8,9,31,32])for(const kc of [1,3,4,7,32,80])for(const ks of ig?[1,3,7]:[1])for(const bounded of [0,1])for(const zero of ig?[0,1]:[0]){
  M._setup(ig,mr,nc,kc,ks);M._configure_validation(bounded,zero);const err=M._validate();
  if(err!==0)throw new Error(`validation ${[ig,mr,nc,kc,ks,bounded,zero]} err=${err}`);checks++;maxError=Math.max(maxError,err);
 }

 const validation={validationCases:checks,maxAbsoluteError:maxError};
 if(validateOnly)return {validation,results:[]};
 const shapes=[];
 for(const c of [32,64,128,256])for(const ks of [3,7,11])shapes.push({name:`residual_c${c}_k${ks}`,ig:1,mr:4,nc:c,kc:c,ks});
 shapes.push({name:'conv_pre',ig:1,mr:4,nc:512,kc:80,ks:7});
 for(const c of [32,64,128,256,512])shapes.push({name:`gemm_c${c}`,ig:0,mr:4,nc:c,kc:c,ks:1});
 const results=[];
 for(const shape of shapes){
  const {ig,mr,nc,kc,ks}=shape;M._setup(ig,mr,nc,kc,ks);
  if(M._validate()!==0)throw new Error(`shape validation failed: ${shape.name}`);
  // Warm both paths before calibration so Liftoff/tiering is less dominant.
  for(let j=0;j<30;j++){M._bench(0,100);M._bench(1,100);}
  const t=performance.now();M._bench(0,100);const per=(performance.now()-t)/100;
  const reps=Math.max(10,Math.min(100000,Math.ceil(30/per)));
  const samples={splat:[],loadsplat:[]};
  for(let round=0;round<10;round++){
   for(const mode of round%2?[1,0]:[0,1]){
    const t=performance.now();const checksum=M._bench(mode,reps);const ms=performance.now()-t;
    samples[mode?'loadsplat':'splat'].push(ms/reps);
    if(!Number.isFinite(checksum))throw new Error('bad checksum');
   }
  }
  const medSplat=median(samples.splat),medLoadsplat=median(samples.loadsplat);
  const result={...shape,reps,samples,splatMedianUs:1000*medSplat,loadsplatMedianUs:1000*medLoadsplat,loadsplatSpeedup:medSplat/medLoadsplat};results.push(result);onResult(result);
 }

 return {validation,results};
}
if(typeof module!=="undefined")module.exports=runKernelProbe;
