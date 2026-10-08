const fs=require('node:fs');const os=require('node:os');
const create=require(process.env.PROBE_MODULE||'./kernel_probe_archive.js');
const run=require('./probe_common.js');
(async()=>{
 const M=await create();
 const result=await run(M,{validateOnly:process.argv.includes('--validate-only'),onResult:r=>console.log(JSON.stringify(r))});
 const metadata={node:process.version,v8:process.versions.v8,argv:process.execArgv,cpu:os.cpus()[0].model,logicalCpus:os.cpus().length,...result.validation,source:'XNNPACK fe98e0b93565382648129271381c14d6205255e3',scope:'direct archived-kernel calls, synthetic FP32; not whole CORE or browser result'};
 console.log(JSON.stringify({metadata}));
 if(process.env.PROBE_OUTPUT)fs.writeFileSync(process.env.PROBE_OUTPUT,JSON.stringify({metadata,results:result.results},null,2)+'\n');
})().catch(e=>{console.error(e);process.exitCode=1});
