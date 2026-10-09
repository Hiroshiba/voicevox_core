'use strict';
// Executes only a 41-byte synthetic module with a fake pthread transport.
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'jit_binding.js'), 'utf8');
const wasm = Uint8Array.from(Buffer.from('0061736d0100000001060160017d017d03020100070501016600000a0c010a002000430000003f940b', 'hex'));
function scope() {
  class Worker { postMessage() {} }
  const W = {};
  for (const k of Object.getOwnPropertyNames(WebAssembly)) Object.defineProperty(W, k, Object.getOwnPropertyDescriptor(WebAssembly, k));
  const context = vm.createContext({WebAssembly: W, Worker, Uint8Array, ArrayBuffer, WeakSet, Proxy, Reflect, Object, Error});
  vm.runInContext(source, context);
  return context;
}
(async () => {
  for (const kind of ['instantiate', 'compile', 'constructor']) {
    const c = scope(), audit = c.installJitModuleBinding(wasm, 'a'.repeat(64), 'https://local.invalid/core.js');
    let module, instance;
    if (kind === 'instantiate') ({module, instance} = await c.WebAssembly.instantiate(wasm));
    if (kind === 'compile') { module = await c.WebAssembly.compile(wasm); instance = await c.WebAssembly.instantiate(module); }
    if (kind === 'constructor') { module = new c.WebAssembly.Module(wasm); instance = new c.WebAssembly.Instance(module); }
    assert.equal(instance.exports.f(3), 1.5);
    for (let i = 0; i < 2; ++i) new c.Worker('https://local.invalid/core.js').postMessage({cmd: 'load', wasmModule: module});
    const receipt = audit.snapshot();
    assert.equal(receipt.compilation_calls, 1); assert.equal(receipt.instantiation_calls, 1);
    assert.equal(receipt.known_module_transfers, 2); assert.equal(receipt.module_transfer_verified, true);
  }
  for (const attack of ['wrong_bytes', 'unknown_module', 'second_compile', 'foreign_worker', 'foreign_transfer', 'streaming']) {
    const c = scope(), audit = c.installJitModuleBinding(wasm, 'a'.repeat(64), 'https://local.invalid/core.js');
    await assert.rejects(async () => {
      if (attack === 'wrong_bytes') await c.WebAssembly.instantiate(wasm.slice(0, -1));
      if (attack === 'unknown_module') await c.WebAssembly.instantiate(new WebAssembly.Module(wasm));
      if (attack === 'second_compile') { await c.WebAssembly.compile(wasm); await c.WebAssembly.compile(wasm); }
      if (attack === 'foreign_worker') new c.Worker('https://other.invalid/private.js');
      if (attack === 'foreign_transfer') new c.Worker('https://local.invalid/core.js').postMessage({cmd: 'load', wasmModule: new WebAssembly.Module(wasm)});
      if (attack === 'streaming') await c.WebAssembly.compileStreaming(Promise.resolve(null));
    });
    assert.ok(audit.snapshot().violations > 0); assert.equal(audit.snapshot().module_transfer_verified, false);
  }
  // The expected bytes are copied, so mutating the caller's original buffer
  // cannot mutate both sides of the comparison and evade the input identity check.
  const bytes = wasm.slice(), c = scope(), audit = c.installJitModuleBinding(bytes, 'a'.repeat(64), 'https://local.invalid/core.js');
  bytes[bytes.length - 2] ^= 1;
  await assert.rejects(() => c.WebAssembly.compile(bytes));
  assert.equal(audit.snapshot().violations, 1);
  console.log('10 synthetic module audit cases passed');
})().catch(error => { console.error(error.name); process.exit(1); });
