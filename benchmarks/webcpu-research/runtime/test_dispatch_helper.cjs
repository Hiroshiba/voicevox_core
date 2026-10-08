// Unit mocks only. These tests are NOT browser/CORE measurement evidence.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(__dirname+'/core_dispatch_check.js','utf8');
let count=0;
function fixture(hc=4,family='splat',change={}){
 class FakeDedicatedWorkerGlobalScope{}
 const self=new FakeDedicatedWorkerGlobalScope();self.navigator={hardwareConcurrency:hc};self.crossOriginIsolated=true;
 const scope={self,DedicatedWorkerGlobalScope:FakeDedicatedWorkerGlobalScope,SharedArrayBuffer};vm.createContext(scope);vm.runInContext(source,scope);
 const stdout=['an earlier ordinary log'];const record={is_x86:1,navigatorHardwareConcurrency:hc,mr:4,nr:8,loadsplatPointers:family==='loadsplat'?12:0,splatPointers:family==='splat'?12:0,checkedPointers:12,relaxedSimd:0,...change};
 const module={HEAPU8:{buffer:new SharedArrayBuffer(16)},_probe_xnn_f32_dispatch:()=>{stdout.push('XNN_DISPATCH '+JSON.stringify(record));return family==='loadsplat'?1:0;}};
 return {scope,module,stdout,run:expected=>scope.verifyActualCoreDispatch(module,stdout,expected)};
}
for(const [hc,family,expected] of [[4,'splat','auto'],[8,'loadsplat','auto'],[4,'loadsplat','loadsplat'],[8,'splat','splat']]){const f=fixture(hc,family);const result=f.run(expected);assert.equal(result.actual_dispatch,family);assert.equal(result.worker_hardware_concurrency,hc);assert.equal(f.stdout[0],'an earlier ordinary log');count++;}
for(const change of [{navigatorHardwareConcurrency:8},{relaxedSimd:1},{mr:5},{loadsplatPointers:1},{extra:'forbidden'}]){assert.throws(()=>fixture(4,'splat',change).run('auto'));count++;}
assert.throws(()=>fixture(4,'splat').run('loadsplat'));count++;
assert.throws(()=>fixture(4,'splat').run(null));count++;
{const f=fixture();f.scope.self.crossOriginIsolated=false;assert.throws(()=>f.run('auto'));count++;}
{const f=fixture();f.module.HEAPU8.buffer=new ArrayBuffer(16);assert.throws(()=>f.run('auto'));count++;}
{const f=fixture();f.scope.self={navigator:{hardwareConcurrency:4},crossOriginIsolated:true};assert.throws(()=>f.run('auto'));count++;}
{const f=fixture(4,'splat');const r=f.scope.captureActualCoreDispatch(f.module,f.stdout,'loadsplat');assert.equal(r.verified,false);assert.equal(r.numeric_records[0].splatPointers,12);assert.equal(r.worker_hardware_concurrency,4);count++;}
{const f=fixture();f.module._probe_xnn_f32_dispatch=()=>{throw new Error('PRIVATE PAYLOAD');};const r=f.scope.captureActualCoreDispatch(f.module,f.stdout,'auto');assert.equal(r.error_message,'Unexpected dispatcher exception');assert.ok(!JSON.stringify(r).includes('PRIVATE PAYLOAD'));count++;}
console.log(JSON.stringify({unit_tests:true,mocked_environment:true,actual_browser_execution:false,passed:count}));
