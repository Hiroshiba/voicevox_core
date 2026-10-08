'use strict';
const assert=require('assert'),fs=require('fs'),{installLeakyAliasCheck}=require('./core_alias_check.js');
// Source-only fixture module with function name0, exact callback signature, no arithmetic.
const bytes=new Uint8Array([0,97,115,109,1,0,0,0,1,7,1,96,3,127,127,127,0,3,2,1,0,7,7,1,3,114,117,110,0,0,10,4,1,2,0,11]);
const fn=new WebAssembly.Instance(new WebAssembly.Module(bytes)).exports.run,table=new WebAssembly.Table({initial:4,element:'anyfunc'});table.set(3,fn);
const memory=new ArrayBuffer(4096),h=new Uint32Array(memory),M={HEAPU8:new Uint8Array(memory)},pin={all_other_functions_sections_identical:true,callback_table:{table:0,slot:3},absolute_function_index:0};
for(const variant of ['original','candidate']){
 h[2]=512;h[3]=512;h[8]=0;h[9]=8;
 const hook=installLeakyAliasCheck(M,table,pin,variant);
 try{table.get(3)(0,32,36);assert.equal(hook.result.predicted_branch[variant==='candidate'?'simd':'scalar'].calls,1);h[3]=1024;table.get(3)(0,32,36);assert.equal(hook.result.classes.disjoint.calls,1);}finally{hook.restore();}
 assert.equal(table.get(3),fn);assert(hook.result.restored);
}
const hook=installLeakyAliasCheck(M,table,pin,'candidate');
try{assert.throws(()=>table.get(3)(4092,32,36));}finally{hook.restore();}
assert.equal(table.get(3),fn);assert.equal(hook.result.metadata_errors,1);assert(hook.result.restored);
assert.throws(()=>installLeakyAliasCheck(M,table,{...pin,absolute_function_index:9},'candidate'));
console.log(JSON.stringify({passed:true,normal_and_error_finally_restore:true,original_candidate_classification:true,actual_function_identity_checked:true}));
