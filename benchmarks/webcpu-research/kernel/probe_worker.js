importScripts('kernel_probe_archive.js','probe_common.js');
self.onmessage=async ({data})=>{
 try {
  const M=await KernelProbe();
  const result=await runKernelProbe(M,{validateOnly:data.validateOnly,onResult:r=>self.postMessage({progress:r})});
  self.postMessage({done:true,metadata:{userAgent:self.navigator.userAgent,hardwareConcurrency:self.navigator.hardwareConcurrency,crossOriginIsolated:self.crossOriginIsolated,context:'dedicated Worker',scope:'synthetic direct archived XNNPACK minmax kernels, not whole CORE',...result.validation},results:result.results});
 } catch(e) {self.postMessage({error:e.stack||String(e)});}
};
