'use strict';
// Reviewed activation host source. Only the admitted owned Python driver serves it.
// This realm check is defense in depth, never a substitute for root CI admission.
function sourceAdmission() {
  if (self.crossOriginIsolated !== true || self.location.protocol !== 'http:' ||
      self.location.hostname !== '127.0.0.1' || !/^[0-9]{1,5}$/.test(self.location.port) ||
      Number(self.location.port) < 1 || Number(self.location.port) > 65535) throw new Error('activation_realm');
}
const HEAP_BYTES = 134217728;
const ARMS = ['R', 'A', 'B', 'Q', 'P'];
// Shared handshake words: state, completed sequence, dispatched sequence.
const QUIESCENT = 0, MUTATING = 1, INSPECTING = 2, TERMINAL = 3;
let mod = null, control = null, arm = null, stage = null;
let state = 'new', busy = false, sequence = 0, current = null;
let heap = null, backing = null, firstFailure = null;
let completedRun = null;
function gate(ok, check) { if (!ok) throw new Error(check); }
function uint(x) { return Number.isInteger(x) && x >= 0 && x <= 4294967295; }
function signedStatus(x) { return Number.isInteger(x) && x >= -2147483648 && x <= 2147483647; }
function meta(i) { const x = mod._screen_meta(i); gate(signedStatus(x), 'meta_scalar'); return x >>> 0; }
function fixtures() {
  const values = [];
  for (let i = 0; i < 12; ++i) {
    const x = mod._screen_fixture_meta(i); gate(signedStatus(x), 'fixture_meta_scalar'); values.push(x >>> 0);
  }
  return values;
}
function diagnostics() {
  const values = [];
  for (let i = 22; i < 127; ++i) values.push(meta(i));
  return values;
}
function regions() {
  return Array.from({length: 7}, (_, i) => ({region: i, ptr: mod._screen_ptr(i) >>> 0, bytes: mod._screen_bytes(i) >>> 0}));
}
function poolWorkers() {
  gate(mod.PThread && Array.isArray(mod.PThread.runningWorkers) && Array.isArray(mod.PThread.unusedWorkers), 'pool_api');
  return [...mod.PThread.runningWorkers, ...mod.PThread.unusedWorkers];
}
function fixedMemory() {
  gate(mod.HEAPU8 instanceof Uint8Array && mod.HEAPU8.buffer === heap && heap.byteLength === HEAP_BYTES, 'fixed_heap');
}
function guards() {
  gate(mod._screen_canaries() === 0, 'caller_canaries');
  mod.checkStackCookie();
}
function beginMutation() {
  gate(control && Atomics.compareExchange(control, 0, QUIESCENT, MUTATING) === QUIESCENT, 'quiescence_mutation');
  Atomics.store(control, 2, sequence);
}
function endMutation() {
  fixedMemory();
  Atomics.store(control, 1, sequence);
  gate(Atomics.compareExchange(control, 0, MUTATING, QUIESCENT) === MUTATING, 'quiescence_completion');
}
function calibrateClock() {
  // Immediately after this arm's init, before the next arm is initialized.
  const positive = [];
  let prior = performance.now(), reads = 1;
  while (positive.length < 1000 && reads < 1000000) {
    const now = performance.now(); ++reads;
    gate(Number.isFinite(now) && now >= prior, 'clock_monotone');
    if (now > prior) { positive.push(now - prior); prior = now; }
  }
  gate(positive.length === 1000, 'clock_resolution');
  const overhead = [];
  for (let i = 0; i < 1000; ++i) {
    const start = performance.now();
    const result = mod._screen_meta(0);
    const end = performance.now();
    gate(result === 0 && Number.isFinite(end - start) && end >= start, 'clock_probe');
    overhead.push(end - start);
  }
  overhead.sort((a, b) => a - b);
  const quantum = Math.min(...positive), p99 = overhead[989];
  return {quantum_ms: quantum, positive_samples: 1000, reads,
    empty_export_samples: 1000, empty_export_p99_ms: p99,
    p99_resolution_censored: p99 === 0, minimum_primary_ms: Math.max(200 * quantum, 100 * p99), subtracted: false};
}
// Exactly one synchronous operator call. No guard, scan, allocation, callback,
// resource sample, IPC or controller operation belongs inside these brackets.
function timedRun() {
  const start = performance.now();
  const native_result = mod._screen_run();
  const end = performance.now();
  return {native_result, elapsed_ms: end - start};
}
async function handle(data) {
  sourceAdmission();
  gate(data && Number.isSafeInteger(data.sequence) && data.sequence === sequence + 1, 'command_sequence');
  sequence = data.sequence;
  if (data.command === 'init') {
    gate(state === 'new' && ARMS.includes(data.arm) && ['qualification', 'timing'].includes(data.stage), 'init_state');
    gate(self.crossOriginIsolated === true && data.control instanceof SharedArrayBuffer && data.control.byteLength === 16, 'shared_isolation');
    control = new Int32Array(data.control); arm = data.arm; stage = data.stage;
    beginMutation();
    const prefix = '/' + arm + '/';
    importScripts(prefix + 'screen.js');
    gate(typeof createScreenModule === 'function', 'module_factory');
    mod = await createScreenModule({
      mainScriptUrlOrBlob: new URL(prefix + 'screen.js', self.location.href).href,
      locateFile: name => { gate(name === 'screen.wasm', 'asset_name'); return prefix + name; },
      print() { throw new Error('unexpected_stdout'); },
      printErr() { throw new Error('unexpected_stderr'); },
      onAbort() { throw new Error('module_abort'); }
    });
    for (const name of ['init', 'prepare', 'prepare_fixture', 'run', 'after', 'canaries', 'ptr', 'bytes', 'meta', 'fixture_meta', 'alias_probe_ptr', 'delete', 'shutdown']) {
      gate(typeof mod['_screen_' + name] === 'function', 'native_export');
    }
    gate(typeof mod.checkStackCookie === 'function', 'stack_export');
    heap = mod.HEAPU8.buffer;
    gate(heap instanceof SharedArrayBuffer && heap.byteLength === HEAP_BYTES, 'fixed_shared_heap');
    const ws = poolWorkers(); gate(ws.length === 1, 'backing_worker_count'); backing = ws[0];
    gate(mod._screen_init() === 0, 'native_init');
    gate(meta(118) === 1 && meta(119) === 0 && meta(120) === 0 && meta(121) === 1 && meta(122) === 1, 'native_pool_init');
    fixedMemory();
    const calibration = stage === 'timing' ? calibrateClock() : null;
    gate(Number.isInteger(navigator.hardwareConcurrency) && navigator.hardwareConcurrency > 0, 'hardware_concurrency');
    state = 'idle'; endMutation();
    // Heap reference is shared once, never copied or transferred. It stays in-page.
    return {ready: true, arm, stage, sequence, shared_heap: heap,
      alias_probe_ptr: mod._screen_alias_probe_ptr() >>> 0,
      heap_bytes: HEAP_BYTES, backing_workers: 1, pool_participants: 2,
      hardware_concurrency: navigator.hardwareConcurrency, calibration};
  }
  gate(mod !== null && state !== 'closed' && state !== 'failed', 'module_state');
  fixedMemory(); beginMutation();
  let value;
  if (data.command === 'prepare') {
    gate(state === 'idle' && data.call.arm === arm && data.call.stage === stage, 'prepare_state');
    current = data.call;
    completedRun = null;
    const result = current.fixture_id === 0 ? mod._screen_prepare(current.dilation, +current.coverage) :
      mod._screen_prepare_fixture(current.dilation, +current.coverage, current.fixture_id);
    gate(result === 0, 'native_prepare'); guards();
    state = 'prepared';
    value = {prepared: true, native_result: result, regions: regions(), meta: Array.from({length: 22}, (_, i) => meta(i)),
      fixture_meta: fixtures(), pre_canaries: true, pre_stack: true};
  } else if (data.command === 'arm') {
    gate(state === 'prepared', 'arm_state'); guards(); state = 'armed';
    value = {armed: true, pre_canaries: true, pre_stack: true};
  } else if (data.command === 'run') {
    gate(state === 'armed', 'run_state'); state = 'running';
    // A native throw produces an unknown outcome; it is never retried.
    const result = current.retained ? timedRun() : {native_result: mod._screen_run(), elapsed_ms: null};
    // Preserve all known results before fixedMemory/endMutation can fail.
    completedRun = {completed: true, ...result, heap_before_bytes: HEAP_BYTES, heap_after_bytes: null};
    completedRun.heap_after_bytes = mod.HEAPU8.byteLength;
    state = 'ran';
    value = completedRun;
    // No numerical checks here: return the duration before later validation.
  } else if (data.command === 'verify') {
    gate(state === 'ran', 'verify_state');
    const result = mod._screen_after(); gate(result === 0, 'native_after'); guards();
    state = 'verified';
    value = {verified: true, native_result: result, post_canaries: true, post_stack: true,
      participant_ids: current.coverage ? [meta(18), meta(19)] : [], callbacks: current.coverage ? meta(20) : 0,
      coverage: meta(21), actual_selector: true, live_descriptor: true, fixture_meta: fixtures()};
  } else if (data.command === 'delete') {
    gate(state === 'verified', 'delete_state');
    const result = mod._screen_delete(); gate(result === 0, 'native_delete'); guards();
    state = 'idle'; current = null; value = {deleted: true, native_result: result};
  } else if (data.command === 'shutdown') {
    gate(state === 'idle', 'shutdown_state');
    const before = poolWorkers(); gate(before.length === 1 && before[0] === backing, 'backing_identity');
    gate(mod._screen_shutdown() === 0, 'native_shutdown');
    gate(meta(123) === 1 && meta(124) === 1 && meta(125) === 1 && meta(126) === 0, 'pool_join');
    gate(mod.PThread.runningWorkers.length === 0 && Object.keys(mod.PThread.pthreads).length === 0 &&
      mod.PThread.unusedWorkers.length === 1 && mod.PThread.unusedWorkers[0] === backing && backing.pthread_ptr === 0, 'pool_empty');
    mod.PThread.terminateAllThreads();
    state = 'closed'; value = {shutdown: true, pool_destroy_returned: true, library_deinitialized: true,
      backing_termination_requested: true, process_cleanup_required: true};
  } else throw new Error('unknown_command');
  endMutation(); return {...value, sequence};
}
self.onmessage = async event => {
  const data = event.data;
  if (busy) { self.postMessage({sequence: data && data.sequence, ok: false, error: 'concurrent_worker_command'}); return; }
  busy = true;
  try { self.postMessage({sequence: data && data.sequence, ok: true, value: await handle(data)}); }
  catch (error) {
    state = 'failed';
    if (control) Atomics.store(control, 0, TERMINAL);
    // No arbitrary message, stack, pointers or tensor can escape the worker.
    const allowed = new Set(['activation_realm', 'native_prepare', 'native_after', 'caller_canaries', 'pool_join', 'pool_empty', 'native_delete']);
    firstFailure = firstFailure || (allowed.has(error.message) ? error.message : 'worker_gate');
    let boundedDiagnostics = null;
    try { if (mod) boundedDiagnostics = diagnostics(); } catch (_) { /* Known run scalars still survive. */ }
    self.postMessage({sequence: data && data.sequence, ok: false, error: firstFailure,
      diagnostics: boundedDiagnostics, completed_run: completedRun,
      outcome_unknown: current !== null && completedRun === null});
  } finally { busy = false; }
};
