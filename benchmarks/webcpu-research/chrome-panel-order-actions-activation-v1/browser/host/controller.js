'use strict';
// Reviewed activation host source. Only the admitted owned Python driver serves it.
// This realm check is defense in depth, never a substitute for root CI admission.
function sourceAdmission() {
  if (self.crossOriginIsolated !== true || self.location.protocol !== 'http:' ||
      self.location.hostname !== '127.0.0.1' || !/^[0-9]{1,5}$/.test(self.location.port) ||
      Number(self.location.port) < 1 || Number(self.location.port) > 65535) throw new Error('activation_realm');
}
const ARMS = ['R', 'A', 'B', 'Q', 'P'];
const HEAP_BYTES = 134217728, WORDS = 7880704, OUTPUT_BYTES = 31522816;
const READONLY_BYTES = 35675216, PACKED_BYTES = 721408;
const REGION_BYTES = [31522880, 720896, 512, OUTPUT_BYTES, PACKED_BYTES, 2708992, 528];
const ALIGNMENT = [64, 64, 64, 64, 16, 8, 16];
const READONLY = [0, 1, 2, 4, 5, 6];
const QUIESCENT = 0, INSPECTING = 2;
const ARM_ORDERS = ['RAPBQ', 'Q B P A R', 'ABRQP', 'PQRBA', 'BQAPR', 'RPAQB', 'QPBRA', 'ARBPQ', 'PRQAB', 'BAQRP'].map(s => s.replaceAll(' ', '').split(''));
const DILATION_ORDERS = [[1,3,5],[5,3,1],[3,5,1],[1,5,3],[5,1,3],[3,1,5],[1,3,5],[5,3,1],[3,5,1],[1,5,3]];
const workers = new Map();
let activeRequest = false, rootState = 'new', stage = null, root = null, current = null;
let cursor = 0, schedule = null, phases = null, identities = null;
let reference = null, readonlyScratch = null, packedReference = null, referenceKey = null;
let referenceSeeded = false, firstFailure = null;
const receipts = [], initReceipts = [], cleanupReceipts = [];
function gate(ok, check, detail = {}) {
  if (ok) return;
  if (firstFailure === null) {
    firstFailure = {check, call_index: current ? current.call.call_index : null};
    for (const k of ['region', 'other_region', 'index', 'expected', 'actual']) {
      const v = detail[k];
      if (typeof v === 'boolean' || Number.isSafeInteger(v) && v >= -2147483648 && v <= 4294967295) firstFailure[k] = v;
    }
  }
  throw new Error(check);
}
function equalObject(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a); return keys.length === Object.keys(b).length && keys.every(k => Object.hasOwn(b, k) && equalObject(a[k], b[k]));
}
function expectedSchedule() {
  const calls = [];
  let stageIndex = 0;
  function append(s, r, d, a, coverage, retained, fixture_id, phase, pass_index, dilation_position, arm_position, reference) {
    calls.push({stage: s, root: r, dilation: d, arm: a, coverage, retained, fixture_id, call_index: calls.length, stage_call_index: stageIndex++,
      phase, pass_index, dilation_position, arm_position, reference, reference_words: reference ? WORDS : 0,
      comparison_words: reference ? 0 : WORDS, operator_runs: 1});
  }
  for (const [di, d] of [1,3,5].entries()) for (let f = 1; f <= 11; ++f) for (const [ai, a] of ARMS.entries()) {
    for (let repeat = 0; repeat < 2; ++repeat) append('qualification', 0, d, a, repeat === 0, false, f,
      'qualification', repeat, di, ai, a === 'R' && repeat === 0);
  }
  stageIndex = 0;
  for (let r = 1; r <= 10; ++r) for (const [di, d] of DILATION_ORDERS[r - 1].entries()) {
    for (let pass = 0; pass < 4; ++pass) for (const [ai, a] of (pass === 0 ? ARMS : ARM_ORDERS[r - 1]).entries()) {
      append('timing', r, d, a, pass === 0, pass === 3, 0, pass === 0 ? 'coverage_warmup' : pass === 3 ? 'retained' : 'warmup',
        pass, di, ai, pass === 0 && a === 'R');
    }
  }
  return calls;
}
function validatePhases(value, complete) {
  gate(value && typeof value === 'object' && !Array.isArray(value), 'phase_document');
  gate(Object.keys(value).every(k => ['1','3','5'].includes(k)), 'phase_keys');
  if (complete) gate(Object.keys(value).length === 3, 'phase_carry_missing');
  for (const rows of Object.values(value)) {
    gate(Array.isArray(rows) && rows.length === 7, 'phase_regions');
    rows.forEach((v, i) => gate(equalObject(Object.keys(v).sort(), ['mod4096','mod64','region']) && v.region === i &&
      Number.isInteger(v.mod4096) && v.mod4096 >= 0 && v.mod4096 < 4096 && v.mod64 === v.mod4096 % 64, 'phase_scalar'));
  }
}
function startWorker(a) {
  gate(!workers.has(a), 'duplicate_worker');
  const entry = {worker: new Worker('/worker.js'), control: new Int32Array(new SharedArrayBuffer(16)), sequence: 0, pending: null, heap: null};
  workers.set(a, entry); // Track before initialization, including failed construction.
  entry.worker.onmessage = event => {
    const v = event.data, p = entry.pending;
    if (!p || v.sequence !== p.sequence) { rootState = 'failed'; if (p) p.reject(new Error('worker_sequence')); return; }
    clearTimeout(p.timer); entry.pending = null;
    if (v.ok === true) p.resolve(v.value);
    else {
      if (firstFailure === null) firstFailure = {check: 'worker_gate', call_index: current ? current.call.call_index : null};
      if (current) {
        if (v.completed_run && v.completed_run.completed === true) {
          current.duration_ms = v.completed_run.elapsed_ms;
          current.evidence.run_ack = v.completed_run;
          const run = current.commands.find(x => x.command === 'run');
          if (run && !run.acknowledged) Object.assign(run, {acknowledged: true, receipt_ms: performance.now(), native_result: v.completed_run.native_result});
        }
        current.worker_failure = {error: v.error, diagnostics: v.diagnostics, outcome_unknown: v.outcome_unknown === true};
      }
      p.reject(new Error('worker_gate'));
    }
  };
  entry.worker.onerror = () => { if (entry.pending) { clearTimeout(entry.pending.timer); entry.pending.reject(new Error('worker_error')); entry.pending = null; } rootState = 'failed'; };
  return entry;
}
function call(a, command, payload = {}) {
  const entry = workers.get(a); gate(entry && entry.pending === null, 'worker_busy');
  const sequence = ++entry.sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { rootState = 'failed'; entry.pending = null; reject(new Error('unknown_command_outcome')); }, command === 'init' ? 60000 : 30000);
    entry.pending = {sequence, resolve, reject, timer};
    entry.worker.postMessage({command, sequence, ...payload});
  });
}
async function inspect(a, fn) {
  const e = workers.get(a);
  gate(e && e.pending === null && e.heap instanceof SharedArrayBuffer && e.heap.byteLength === HEAP_BYTES, 'inspection_heap');
  gate(Atomics.load(e.control, 1) === e.sequence && Atomics.load(e.control, 2) === e.sequence, 'completion_handshake');
  gate(Atomics.compareExchange(e.control, 0, QUIESCENT, INSPECTING) === QUIESCENT, 'inspection_lease');
  try { return await fn(e.heap); }
  finally { gate(Atomics.load(e.control, 1) === e.sequence && Atomics.compareExchange(e.control, 0, INSPECTING, QUIESCENT) === INSPECTING, 'inspection_release'); }
}
async function proveDistinctBacking(arm, ptr) {
  gate(Number.isInteger(ptr) && ptr > 0 && ptr % 4 === 0 && ptr + 4 <= HEAP_BYTES, 'alias_probe_bounds');
  const entries = [...workers.entries()], leases = [];
  try {
    for (const [a, e] of entries) {
      gate(e.pending === null && Atomics.load(e.control, 1) === e.sequence && Atomics.load(e.control, 2) === e.sequence, 'alias_probe_completion');
      gate(Atomics.compareExchange(e.control, 0, QUIESCENT, INSPECTING) === QUIESCENT, 'alias_probe_lease'); leases.push(e);
    }
    const own = new Uint32Array(workers.get(arm).heap, ptr, 1), original = Atomics.load(own, 0);
    // Read every PRIOR heap at the SAME new probe offset. Their own reserved
    // probe offsets may differ. Only the new module's reserved word is written.
    const prior = entries.filter(([a]) => a !== arm).map(([a, e]) => {
      const view = new Uint32Array(e.heap, ptr, 1); return {view, before: Atomics.load(view, 0)};
    });
    let distinct = true;
    try {
      Atomics.store(own, 0, (~original) >>> 0);
      for (const p of prior) if (Atomics.load(p.view, 0) !== p.before) distinct = false;
    } finally { Atomics.store(own, 0, original); }
    gate(Atomics.load(own, 0) === original, 'alias_probe_restore');
    gate(distinct, 'shared_backing_alias');
  } finally {
    for (const e of leases) gate(Atomics.compareExchange(e.control, 0, INSPECTING, QUIESCENT) === INSPECTING, 'alias_probe_release');
  }
  return true;
}
function byteView(heap, region) { return new Uint8Array(heap, region.ptr, region.bytes); }
function compareBytes(expected, actual, check, region) {
  gate(expected.byteLength === actual.byteLength, check);
  for (let i = 0; i < expected.length; ++i) if (expected[i] !== actual[i]) gate(false, check, {region, index: i, expected: expected[i], actual: actual[i]});
}
async function digestView(view) {
  // view aliases the ONE nonshared scratch buffer. Await before reuse. Digest
  // result is 32 bytes; no application-owned full-sized hash copy is allocated.
  gate(view.buffer === readonlyScratch.buffer, 'hash_workspace');
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', view));
  return Array.from(bytes, x => x.toString(16).padStart(2, '0')).join('');
}
function geometry(heap, prepared, c) {
  const rs = prepared.regions, m = prepared.meta;
  gate(Array.isArray(rs) && rs.length === 7 && Array.isArray(m) && m.length === 22, 'geometry_schema');
  for (let i = 0; i < 7; ++i) {
    const r = rs[i];
    gate(r.region === i && Number.isInteger(r.ptr) && r.ptr > 0 && r.ptr % ALIGNMENT[i] === 0 && r.bytes === REGION_BYTES[i] && r.ptr + r.bytes <= HEAP_BYTES, 'region_geometry', {region: i});
    // Region INDEX order is fixed. Actual allocator addresses need not increase.
  }
  for (let i = 0; i < 7; ++i) for (let j = i + 1; j < 7; ++j) gate(rs[i].ptr + rs[i].bytes <= rs[j].ptr || rs[j].ptr + rs[j].bytes <= rs[i].ptr, 'region_overlap', {region: i, other_region: j});
  const expected = [c.dilation,61568,128,4,8,11,512,176,['R','A'].includes(c.arm)?4:32,128,2,61568,128];
  for (let i = 0; i < expected.length; ++i) gate(m[i] === expected[i], 'live_descriptor', {index: i, expected: expected[i], actual: m[i]});
  gate(m[21] === +c.coverage, 'coverage_mode');
  const phase = rs.map(r => ({region: r.region, mod64: r.ptr % 64, mod4096: r.ptr % 4096}));
  if (!Object.hasOwn(phases, c.dilation)) {
    gate(stage === 'qualification' && c.arm === 'R' && c.coverage && c.fixture_id === 1, 'phase_seed'); phases[c.dilation] = phase;
  } else gate(equalObject(phases[c.dilation], phase), 'phase_mismatch');
  const table = new Uint32Array(heap, rs[5].ptr, 61568 * 11);
  for (let tile = 0; tile < 61568; tile += 4) for (let ks = 0; ks < 11; ++ks) for (let row = 0; row < 4; ++row) {
    const index = tile * 11 + ks * 4 + row, x = tile + row + ks * c.dilation - 5 * c.dilation, ptr = table[index];
    if (x < 0 || x >= 61568) gate(ptr === rs[6].ptr, 'indirection_sentinel', {index});
    else {
      gate(ptr !== rs[6].ptr, 'nonpadding_not_zero_sentinel', {index});
      gate(((ptr + m[13]) >>> 0) === ((rs[0].ptr + x * 512) >>> 0), 'indirection_offset', {index});
    }
  }
  const zero = byteView(heap, rs[6]);
  for (let i = 0; i < zero.length; ++i) gate(zero[i] === 0, 'zero_bytes', {index: i});
  return {regions: rs.map(r => ({region: r.region, bytes: r.bytes, minimum_alignment: ALIGNMENT[r.region]})), phases: phase,
    descriptor: m.slice(0, 13),
    indirection_words: 677248, actual_zero_sentinel: true, modulo_uint32_offset: true, actual_selector: true, live_descriptor: true};
}
function fixtureExpectation(out, meta, c) {
  gate(meta.length === 12 && meta[0] === 1 && meta[1] === c.fixture_id && meta[11] === 11, 'fixture_metadata');
  if (c.fixture_id === 0) return {designated: 0, checked: 0, passed: true};
  gate(meta[2] === 512 && meta[3] === 30752, 'witness_count');
  const rows = meta.slice(4, 8);
  const offsets = {1:[8,21,34,47],3:[48,17,22,15],5:[28,29,30,31]};
  gate(equalObject(rows, offsets[c.dilation].map(x => 30752 + x)), 'witness_rows');
  const endpoints = {6:[0,0],7:[0,127],8:[10,0],9:[10,127]};
  gate(equalObject(meta.slice(8,10), endpoints[c.fixture_id] || [4294967295,4294967295]), 'witness_endpoint');
  const expectedType = c.fixture_id === 1 ? 1 : c.fixture_id === 2 ? 3 : c.fixture_id === 3 ? 4 : 2;
  gate(meta[10] === expectedType, 'witness_expectation_type');
  // These are declared fixture-specific class expectations, separate from the
  // generic timing fixture's all-finite poison/store gate.
  if ([1,3].includes(c.fixture_id)) for (let i = 0; i < WORDS; ++i) gate((out[i] & 0x7f800000) !== 0x7f800000, 'fixture_full_finite', {index: i});
  if (c.fixture_id === 2) for (let i = 0; i < WORDS; ++i) gate((out[i] & 0x7fffffff) === 0, 'fixture_full_zero', {index: i});
  let checked = 0;
  function checkAt(row, channel) {
    const index = row * 128 + channel, bits = out[index], exp = bits & 0x7f800000, fraction = bits & 0x007fffff;
    if (meta[10] === 2) gate(exp === 0x7f800000 && fraction !== 0, 'witness_nan', {index});
    else if (meta[10] === 3) gate(bits === (channel % 2 === 0 ? 0x80000000 : 0), 'witness_signed_zero', {index});
    else gate(exp !== 0x7f800000, 'witness_finite', {index});
    ++checked;
  }
  if (c.fixture_id === 5) {
    for (let r = 0; r < 4; ++r) for (let ch = 0; ch < 64; ++ch) { checkAt(r, ch); checkAt(61564 + r, 64 + ch); }
  } else for (const row of rows) for (let ch = 0; ch < 128; ++ch) checkAt(row, ch);
  gate(checked === 512, 'witness_checked');
  return {designated: 512, checked, passed: true, fixture_id: c.fixture_id, expectation_code: meta[10], full_class_words: c.fixture_id <= 3 ? WORDS : 0};
}
async function prepare(c) {
  gate(rootState === 'ready' && current === null && equalObject(c, schedule[cursor]), 'schedule_call');
  current = {call: c, commands: [], checks: {}, evidence: {}, duration_ms: null}; receipts.push(current);
  const begin = performance.now(); current.commands.push({command: 'prepare', attempted: true, acknowledged: false, dispatch_ms: begin});
  const p = await call(c.arm, 'prepare', {call: c});
  current.prepared = p; // Internal only: raw pointers never returned by publicReceipt.
  const key = c.dilation + ':' + c.fixture_id;
  await inspect(c.arm, async heap => {
    current.evidence.geometry = geometry(heap, p, c);
    if (referenceKey !== key) {
      gate(c.arm === 'R' && c.coverage, 'fixture_seed_order'); referenceKey = key; referenceSeeded = false;
    }
    let offset = 0; const hashes = [];
    for (const i of READONLY) {
      const view = byteView(heap, p.regions[i]); readonlyScratch.set(view, offset);
      if (i < 3) hashes.push(await digestView(readonlyScratch.subarray(offset, offset + view.length)));
      offset += view.length;
    }
    gate(offset === READONLY_BYTES, 'snapshot_extent');
    const packed = byteView(heap, p.regions[4]);
    if (!referenceSeeded) packedReference.set(packed);
    else compareBytes(packedReference, packed, 'packed_bytes', 4);
    if (Object.hasOwn(identities, key)) gate(equalObject(identities[key].fixture_sha256, hashes), 'fixture_identity');
    else { gate(c.arm === 'R' && c.coverage, 'fixture_identity_seed'); identities[key] = {fixture_sha256: hashes}; }
    current.evidence.fixture_sha256 = hashes;
    // The first special reference uses complete stores + coverage as its write
    // coverage proof; its NaN poison is not an absence oracle. Subsequent special
    // output poison differs from EVERY required uint32 word, including NaNs.
    if (c.fixture_id !== 0 && referenceSeeded) {
      const out = new Uint32Array(heap, p.regions[3].ptr, WORDS);
      for (let i = 0; i < WORDS; ++i) out[i] = ~reference[i];
    }
  });
  const armed = await call(c.arm, 'arm');
  gate(armed.armed && armed.pre_canaries && armed.pre_stack, 'pre_guards');
  Object.assign(current.commands[0], {acknowledged: true, receipt_ms: performance.now()});
  current.checks.prepare = true; rootState = 'prepared';
  return {prepared: true, call_index: c.call_index, heap_bytes: HEAP_BYTES, phases: phases[c.dilation]};
}
async function run() {
  gate(rootState === 'prepared' && current !== null, 'run_state');
  const cmd = {command: 'run', attempted: true, acknowledged: false, dispatch_ms: performance.now()}; current.commands.push(cmd); rootState = 'running';
  const result = await call(current.call.arm, 'run');
  // Preserve the acknowledgement and duration before applying any quality gate.
  Object.assign(cmd, {acknowledged: true, receipt_ms: performance.now(), native_result: result.native_result});
  current.duration_ms = result.elapsed_ms; current.evidence.run_ack = result;
  rootState = 'ran';
  return {call_index: current.call.call_index, ...result};
}
async function verify() {
  gate(rootState === 'ran' && current !== null, 'verify_state');
  const c = current.call, p = current.prepared;
  const cmd = {command: 'verify', attempted: true, acknowledged: false, dispatch_ms: performance.now()}; current.commands.push(cmd);
  gate(current.evidence.run_ack.completed && current.evidence.run_ack.native_result === 0 && current.evidence.run_ack.heap_before_bytes === HEAP_BYTES && current.evidence.run_ack.heap_after_bytes === HEAP_BYTES, 'native_run');
  const post = await call(c.arm, 'verify');
  await inspect(c.arm, async heap => {
    let offset = 0;
    for (const i of READONLY) {
      const view = byteView(heap, p.regions[i]); compareBytes(readonlyScratch.subarray(offset, offset + view.length), view, 'readonly_bytes', i); offset += view.length;
    }
    // Readonly scratch's complete comparison obligation is now discharged.
    // Hashes below reuse that workspace serially, leaving both references live.
    const packed = byteView(heap, p.regions[4]); compareBytes(packedReference, packed, 'packed_bytes_after', 4);
    readonlyScratch.set(packed, 0); const packedHash = await digestView(readonlyScratch.subarray(0, PACKED_BYTES));
    const out = new Uint32Array(heap, p.regions[3].ptr, WORDS);
    if (c.fixture_id === 0) for (let i = 0; i < WORDS; ++i) gate((out[i] & 0x7f800000) !== 0x7f800000, 'finite_timing', {index: i});
    const seeded = !referenceSeeded;
    if (seeded) { gate(c.arm === 'R' && c.coverage, 'output_seed_order'); reference.set(out); referenceSeeded = true; }
    else for (let i = 0; i < WORDS; ++i) if (out[i] !== reference[i]) gate(false, 'output_uint32', {index: i, expected: reference[i], actual: out[i]});
    const witnesses = fixtureExpectation(out, post.fixture_meta, c);
    readonlyScratch.set(new Uint8Array(heap, p.regions[3].ptr, OUTPUT_BYTES), 0);
    const outputHash = await digestView(readonlyScratch.subarray(0, OUTPUT_BYTES));
    const identity = identities[referenceKey];
    if (Object.hasOwn(identity, 'output_sha256')) gate(identity.output_sha256 === outputHash && identity.packed_sha256 === packedHash, 'cross_root_identity');
    else { gate(seeded, 'identity_first_reference'); identity.output_sha256 = outputHash; identity.packed_sha256 = packedHash; }
    current.evidence.output = {words: WORDS, reference_capture: seeded, compared_words: seeded ? 0 : WORDS,
      finite_required: c.fixture_id === 0, exact_uint32: !seeded, output_sha256: outputHash};
    current.evidence.readonly = {regions: 6, bytes: READONLY_BYTES, differences: 0};
    current.evidence.packed = {bytes: PACKED_BYTES, differences: 0, packed_sha256: packedHash};
    current.evidence.witnesses = witnesses;
    if (c.coverage) {
      const ids = post.participant_ids;
      gate(ids.length === 2 && ids.every(x => Number.isInteger(x) && x > 0) && ids[0] !== ids[1], 'actual_participants');
      const sorted = ids.slice().sort((a,b) => a-b), entry = workers.get(c.arm);
      if (entry.participantIds) gate(equalObject(entry.participantIds, sorted), 'persistent_participants');
      else entry.participantIds = sorted;
      const labels = [];
      for (let i = 0; i < 2; ++i) {
        new Uint32Array(readonlyScratch.buffer, 0, 4).set([root, ARMS.indexOf(c.arm), stage === 'timing' ? 1 : 0, sorted[i]]);
        labels.push({label: i, identity_sha256: await digestView(readonlyScratch.subarray(0, 16))});
      }
      current.evidence.coverage = {enabled: true, participants: labels, callbacks: post.callbacks, exact_disjoint: true};
    } else current.evidence.coverage = {enabled: false, participants: [], callbacks: 0, exact_disjoint: false};
  });
  if (c.coverage) {
    const ids = post.participant_ids;
    gate(ids.length === 2 && ids.every(x => Number.isInteger(x) && x > 0) && ids[0] !== ids[1], 'actual_participants');
    gate(post.callbacks === (['R','A'].includes(c.arm) ? 15392 : 1924), 'callback_count');
  }
  gate(post.verified && post.post_canaries && post.post_stack && post.actual_selector && post.live_descriptor && post.coverage === +c.coverage, 'post_proof');
  current.evidence.canaries = {caller_regions: 4, prefix_bytes: 256, suffix_bytes: 256, before_bytes: 2048, after_bytes: 2048, compared_bytes: 4096, differences: 0, hidden_allocator_padding_covered: false};
  current.evidence.stack = {before: true, after: true};
  current.checks = {prepare: true, phase: true, readonly: true, canaries: true, stack: true, output: true, packed: true, indirection: true, selector: true, coverage: true, fixture_expectations: true};
  Object.assign(cmd, {acknowledged: true, receipt_ms: performance.now()}); rootState = 'verified';
  return publicReceipt(current);
}
function publicReceipt(record) {
  return {call: record.call, commands: record.commands, checks: record.checks, evidence: record.evidence, duration_ms: record.duration_ms,
    worker_failure: record.worker_failure || null};
}
async function handle(data) {
  sourceAdmission();
  gate(data && typeof data.command === 'string', 'command');
  if (data.command === 'init') {
    gate(rootState === 'new' && workers.size === 0, 'root_init');
    gate(equalObject(data.schedule, expectedSchedule()), 'exact_930_schedule');
    gate(['qualification', 'timing'].includes(data.stage) && Number.isInteger(data.root), 'root_identity');
    stage = data.stage; root = data.root;
    gate(stage === 'qualification' ? root === 0 : root >= 1 && root <= 10, 'root_number');
    schedule = data.schedule.filter(c => c.stage === stage && c.root === root);
    gate(schedule.length === (stage === 'qualification' ? 330 : 60), 'root_call_count');
    phases = structuredClone(data.phase_records); validatePhases(phases, stage === 'timing');
    gate(stage !== 'qualification' || Object.keys(phases).length === 0, 'qualification_phase_seed');
    identities = structuredClone(data.fixture_identities);
    gate(identities && typeof identities === 'object' && !Array.isArray(identities), 'fixture_identity_document');
    reference = new Uint32Array(WORDS); readonlyScratch = new Uint8Array(READONLY_BYTES); packedReference = new Uint8Array(PACKED_BYTES);
    rootState = 'initializing';
    return {controller_ready: true, stage, root, large_application_buffer_bytes: 739008080};
  }
  if (data.command === 'init_arm') {
    gate(rootState === 'initializing' && data.arm === ARMS[workers.size], 'serial_initialization_order');
    const a = data.arm, entry = startWorker(a);
    const result = await call(a, 'init', {arm: a, stage, control: entry.control.buffer});
    entry.heap = result.shared_heap;
    gate(result.ready && entry.heap instanceof SharedArrayBuffer && entry.heap.byteLength === HEAP_BYTES, 'worker_init');
    for (const [other, existing] of workers) if (other !== a) gate(existing.heap !== entry.heap, 'distinct_shared_memories');
    await proveDistinctBacking(a, result.alias_probe_ptr);
    gate(stage === 'timing' ? result.calibration !== null : result.calibration === null, 'calibration_stage');
    const {shared_heap, alias_probe_ptr, ...scalars} = result;
    scalars.distinct_backing_verified = true; initReceipts.push(scalars);
    if (workers.size === 5) rootState = 'ready';
    return scalars;
  }
  if (data.command === 'prepare') return prepare(data.call);
  if (data.command === 'run') return run();
  if (data.command === 'verify') return verify();
  if (data.command === 'delete') {
    gate(rootState === 'verified' && current !== null, 'delete_state');
    const cmd = {command: 'delete', attempted: true, acknowledged: false, dispatch_ms: performance.now()}; current.commands.push(cmd);
    const result = await call(current.call.arm, 'delete'); gate(result.deleted && result.native_result === 0, 'delete_ack');
    Object.assign(cmd, {acknowledged: true, receipt_ms: performance.now(), native_result: 0});
    const receipt = publicReceipt(current); current = null; ++cursor; rootState = 'ready'; return receipt;
  }
  if (data.command === 'shutdown') {
    gate(rootState === 'ready' && current === null && cursor === schedule.length, 'root_complete');
    for (const a of ARMS) { const value = await call(a, 'shutdown'); gate(value.shutdown && value.pool_destroy_returned, 'pool_cleanup'); cleanupReceipts.push({arm: a, ...value}); }
    for (const entry of workers.values()) { gate(entry.pending === null, 'pending_cleanup'); entry.worker.terminate(); }
    reference.fill(0); readonlyScratch.fill(0); packedReference.fill(0); rootState = 'closed';
    return {shutdown: true, calls: receipts.length, phase_records: phases, fixture_identities: identities,
      modules: cleanupReceipts, process_cleanup_required: true, buffers_erased: true};
  }
  throw new Error('unknown_command');
}
async function request(data) {
  sourceAdmission();
  gate(!activeRequest && rootState !== 'failed', 'controller_serial'); activeRequest = true;
  try { return {ok: true, value: await handle(data)}; }
  catch (_) {
    rootState = 'failed';
    return {ok: false, error: 'terminal_no_retry', first_failure: firstFailure || {check:'controller_gate',call_index:current ? current.call.call_index : null},
      current: current ? publicReceipt(current) : null, completed_calls: cursor};
  } finally { activeRequest = false; }
}
