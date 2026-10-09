'use strict';
// Diagnostic-only module identity audit; never loaded by a primary timing worker.
function installJitModuleBinding(expectedBytes, expectedSha, expectedWorkerUrl) {
  const expected = new Uint8Array(expectedBytes).slice(), known = new WeakSet();
  const original = {Module: WebAssembly.Module, Instance: WebAssembly.Instance,
    compile: WebAssembly.compile, instantiate: WebAssembly.instantiate, Worker: globalThis.Worker};
  let compilations = 0, instances = 0, modules = 0, workers = 0, transfers = 0, violations = 0;
  const fail = () => { ++violations; throw new Error('Native module identity audit'); };
  const bytes = input => {
    const view = input instanceof ArrayBuffer ? new Uint8Array(input) :
      ArrayBuffer.isView(input) ? new Uint8Array(input.buffer, input.byteOffset, input.byteLength) : null;
    if (!view || view.byteLength !== expected.byteLength) fail();
    for (let i = 0; i < expected.byteLength; ++i) if (view[i] !== expected[i]) fail();
    ++compilations;
    if (compilations !== 1) fail();
  };
  const record = module => { if (!(module instanceof original.Module) || known.has(module)) fail(); known.add(module); ++modules; };
  WebAssembly.Module = new Proxy(original.Module, {construct(target, args) {
    bytes(args[0]); const module = Reflect.construct(target, args); record(module); return module;
  }});
  WebAssembly.compile = async function(input, ...rest) {
    bytes(input); const module = await original.compile.call(WebAssembly, input, ...rest); record(module); return module;
  };
  WebAssembly.instantiate = async function(input, ...rest) {
    ++instances;
    if (input instanceof original.Module) {
      if (!known.has(input)) fail();
      return original.instantiate.call(WebAssembly, input, ...rest);
    }
    bytes(input); const result = await original.instantiate.call(WebAssembly, input, ...rest);
    record(result.module); return result;
  };
  WebAssembly.Instance = new Proxy(original.Instance, {construct(target, args) {
    if (!known.has(args[0])) fail(); ++instances; return Reflect.construct(target, args);
  }});
  // The pinned loader supplies wasmBinary, so neither streaming API is needed.
  WebAssembly.compileStreaming = async () => fail();
  WebAssembly.instantiateStreaming = async () => fail();
  globalThis.Worker = new Proxy(original.Worker, {construct(target, args) {
    if (String(args[0]) !== expectedWorkerUrl) fail();
    const worker = Reflect.construct(target, args); ++workers;
    const post = worker.postMessage.bind(worker);
    worker.postMessage = function(message, ...rest) {
      if (message && message.cmd === 'load') {
        if (!known.has(message.wasmModule)) fail(); ++transfers;
      }
      return post(message, ...rest);
    };
    return worker;
  }});
  return Object.freeze({snapshot: () => ({schema: 'inplace-jit-module-binding-v1',
    wasm_sha256: expectedSha, byte_length: expected.byteLength, compilation_calls: compilations,
    instantiation_calls: instances, tracked_modules: modules, workers_created: workers,
    known_module_transfers: transfers, violations,
    all_compilation_bytes_exact: violations === 0, all_worker_urls_exact: violations === 0,
    module_transfer_verified: modules === 1 && workers === 2 && transfers === 2 && violations === 0})});
}
