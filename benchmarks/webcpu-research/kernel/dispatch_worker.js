self.onmessage=async({data})=>{
 try {
  const script=new URL(data.script,self.location.href).href;
  importScripts(script);
  const lines=[];
  const M=await DispatchProbe({print:line=>lines.push(line),locateFile:path=>new URL(path,script).href});
  const status=M._probe_xnn_f32_dispatch();
  const prefix='XNN_DISPATCH ';
  const records=lines.filter(x=>x.startsWith(prefix)).map(x=>JSON.parse(x.slice(prefix.length)));
  if(records.length!==1)throw new Error('Expected exactly one dispatch record');
  self.postMessage({done:true,status,dispatch:records[0],context:'dedicated Worker',hardwareConcurrency:self.navigator.hardwareConcurrency,crossOriginIsolated:self.crossOriginIsolated});
 } catch(e) {self.postMessage({error:e.stack||String(e)});}
};
