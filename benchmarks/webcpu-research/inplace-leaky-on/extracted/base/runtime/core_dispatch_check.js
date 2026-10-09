// Read-only proof from the actual initialized CORE instance in its dedicated Worker.
// This helper never supplies, replaces, or overrides self/navigator.
function verifyActualCoreDispatch(module, stdout, expected = 'auto') {
  if (!['auto', 'splat', 'loadsplat'].includes(expected)) throw new Error('Invalid expected dispatcher');
  if (typeof DedicatedWorkerGlobalScope === 'undefined' || !(self instanceof DedicatedWorkerGlobalScope)) {
    throw new Error('Dispatch proof must run inside the actual dedicated CORE Worker');
  }
  const hc = self.navigator.hardwareConcurrency;
  if (!Number.isInteger(hc) || hc < 1) throw new Error('Worker hardwareConcurrency unavailable');
  if (self.crossOriginIsolated !== true || typeof SharedArrayBuffer === 'undefined' ||
      !(module.HEAPU8.buffer instanceof SharedArrayBuffer)) throw new Error('Shared isolated CORE required');
  if (!Array.isArray(stdout) || typeof module._probe_xnn_f32_dispatch !== 'function') {
    throw new Error('Actual CORE dispatcher export/stdout capture missing');
  }
  const begin = stdout.length;
  const code = module._probe_xnn_f32_dispatch();
  const prefix = 'XNN_DISPATCH ';
  const lines = stdout.slice(begin).filter(line => typeof line === 'string' && line.startsWith(prefix));
  if (lines.length !== 1) throw new Error('Expected exactly one actual CORE dispatch record');
  const record = JSON.parse(lines[0].slice(prefix.length));
  const keys = ['is_x86', 'navigatorHardwareConcurrency', 'mr', 'nr', 'loadsplatPointers', 'splatPointers', 'checkedPointers', 'relaxedSimd'];
  if (Object.keys(record).sort().join(',') !== keys.sort().join(',') ||
      keys.some(key => !Number.isInteger(record[key]))) throw new Error('Unexpected dispatcher record schema');
  if (record.is_x86 !== 1 || record.relaxedSimd !== 0 || record.mr !== 4 || record.nr !== 8 ||
      record.checkedPointers !== 12 || record.navigatorHardwareConcurrency !== hc) {
    throw new Error('Actual CORE architecture/precision/tile/context mismatch');
  }
  const actual = code === 0 ? 'splat' : code === 1 ? 'loadsplat' : null;
  if (actual === null || record[actual + 'Pointers'] !== 12 ||
      record.loadsplatPointers + record.splatPointers !== 12) throw new Error('Inconsistent actual CORE pointer family');
  const required = expected === 'auto' ? (hc > 4 ? 'loadsplat' : 'splat') : expected;
  if (actual !== required) throw new Error('Actual CORE dispatcher does not match manifest/heuristic');
  return {
    schema_version: 1, source: 'actual_CORE_module', context: 'dedicated_worker',
    expected_dispatch: expected, actual_dispatch: actual,
    worker_hardware_concurrency: hc, probe_hardware_concurrency: record.navigatorHardwareConcurrency,
    is_x86: true, relaxed_simd: false, mr: 4, nr: 8,
    splat_pointers: record.splatPointers, loadsplat_pointers: record.loadsplatPointers,
    checked_pointers: 12, probe_return_code: code,
    cross_origin_isolated: true, shared_memory: true, verified: true
  };
}

// Return bounded failed evidence rather than discarding it. No arbitrary stdout
// or error payloads survive this wrapper; the caller checkpoints then asserts.
function captureActualCoreDispatch(module, stdout, expected = 'auto') {
  const start = Array.isArray(stdout) ? stdout.length : 0;
  try { return verifyActualCoreDispatch(module, stdout, expected); }
  catch (error) {
    let hc = null;
    let context = 'not_dedicated_worker';
    try {
      if (typeof DedicatedWorkerGlobalScope !== 'undefined' && self instanceof DedicatedWorkerGlobalScope) context = 'dedicated_worker';
      if (Number.isInteger(self.navigator.hardwareConcurrency)) hc = self.navigator.hardwareConcurrency;
    } catch (_) {}
    const records = [];
    const prefix = 'XNN_DISPATCH ';
    for (const line of (Array.isArray(stdout) ? stdout.slice(start) : [])) {
      if (records.length === 2) break;
      if (typeof line !== 'string' || !line.startsWith(prefix) || line.length > 1024) continue;
      try {
        const value = JSON.parse(line.slice(prefix.length));
        const numeric = {};
        for (const key of ['is_x86','navigatorHardwareConcurrency','mr','nr','loadsplatPointers','splatPointers','checkedPointers','relaxedSimd']) {
          if (Number.isInteger(value[key]) && Math.abs(value[key]) <= 2147483647) numeric[key] = value[key];
        }
        records.push(numeric);
      } catch (_) {}
    }
    const allowed = new Set([
      'Invalid expected dispatcher','Dispatch proof must run inside the actual dedicated CORE Worker',
      'Worker hardwareConcurrency unavailable','Shared isolated CORE required',
      'Actual CORE dispatcher export/stdout capture missing','Expected exactly one actual CORE dispatch record',
      'Unexpected dispatcher record schema','Actual CORE architecture/precision/tile/context mismatch',
      'Inconsistent actual CORE pointer family','Actual CORE dispatcher does not match manifest/heuristic'
    ]);
    return {
      schema_version: 1, source: 'actual_CORE_module', context,
      expected_dispatch: ['auto','splat','loadsplat'].includes(expected) ? expected : 'invalid',
      worker_hardware_concurrency: hc, numeric_records: records, verified: false,
      error_type: ['Error','TypeError','RangeError','SyntaxError','RuntimeError'].includes(error?.name) ? error.name : 'Error',
      error_message: allowed.has(error?.message) ? error.message : 'Unexpected dispatcher exception'
    };
  }
}
