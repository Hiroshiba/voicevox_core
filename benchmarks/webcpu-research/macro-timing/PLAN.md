# Bounded macro timing screen, pending execution clearance

Primary scope: persistent real-XNN FP32 operators, actual pthreadpool_create(2), M61568/C128/N128/K11, dilation1/3/5, strict original MR4 loadsplat arithmetic. A original callbacks; B M8 whole-N grouping; C M8 NC32-first. No parameter sweep, model/precision change or full CORE claim.

## Phase boundary
Pinned ORT1.23.2 xnnpack/nn/conv.cc Compute lines110–113,121–122,142 executes reshape, setup, run each inference. PrePack20–60 transposes weights and creates the operator once. Primary interval therefore encloses one C export calling xnn_reshape_convolution2d_nhwc_f32, xnn_setup_convolution2d_nhwc_f32, xnn_run_operator. No internal per-phase clocks. Includes JS/Wasm entry/return and status return, with two outer performance.now calls. Excludes fixture allocation, weight packing/create, initial indirection allocation, output hashing and ORT tensor-allocation/wrapper work. Persistent same-shape workspace is zero. This is a warm standalone operator screen, not the full ORT Compute or CORE duration.

## Bounded matrix
3 fresh Chromium processes ×3 rounds ×3 conditions ×3 dilations =81 primary calls. Within each process instantiate persistent separate A/B/C dedicated workers and heap-local pthread2 pools; never share pointers between modules. Each worker owns three operators. Established1GiB initial/2GiB maximum heap per module,3GiB initially reserved shared Wasm memory; record actual RSS and no memory growth. No overlapping calls or background ORT pool.

Each operator gets3 explicitly recorded warmup calls (81 warmups total), with all raw warmup durations retained and excluded from primary. Pre/post exactness runs each operator with input0 and changed-address input1, compares exact SHA256 against frozen diagnostic fixture hashes and across conditions. No tolerance. Restore input0 through the next timed setup. Pre/post runs are untimed and excluded; no condition-specific retries.

Balanced frozen schedule: in set s, round r, visit dilations in cyclic order rotated by s+r. For dilation label q=0/1/2, condition order starts at s+r+q; sets0/2 step forward and set1 steps backward. All six permutations plus three forward cyclic rows appear per geometry. Across nine triplets, each condition occupies every position three times and pairwise ahead/behind counts are5:4. schedule.json freezes all81calls; validation asserts both invariants. Every condition has each ordinal position equally across the9triplets/process; shape order also balances. Record complete schedule before execution, all81 raw latencies, and pair B/A, C/B, C/A within the same triplet. Fresh processes are the independent replicate unit. With3sets this is a preliminary directional screen, not statistical significance or an E2E win. Report each dilation separately and all set-level effects; no pooled unlabeled geomean or automatic expansion.

## Production versus diagnostic separation
Reuse exact same generated production operator-run.c.o and convolution-nhwc.c.o for sibling diagnostic and primary links, plus identical frozen loadsplat archive, compiler/link precision flags. Only harness object differs. Diagnostic probe contains callback forwarding, task counters, guards and snapshots. Primary harness never changes callback/ukernel pointers and contains no counters, traces, guard scans or snapshots in cycle. Check LLVM body of cycle has only reshape/setup/run and return/control flow; seal object/source/Wasm hashes. Untimed sibling dispatch and count proof plus primary pre/post hashes must pass before accepting any timing.

## Idle/resource readiness
Pinned pthreadpool4e80ca source defaults110 spin iterations then infinite emscripten_futex_wait; verified actual released pthreads.c.o IR has both110 bound and futex waits. Persistent inactive modules receive no RPC or timers. After warmups, sample process-tree CPU over1second idle windows, require two consecutive windows <=0.10CPU-seconds, at most10windows; retain every observation, fail closed without timing if unmet. This tests lack of material idle spinning; it does not prove CPU pinning. Record start/end CPU,RSS,thread counts, PIDcreation, minor/major faults, host available memory/swap/PSI and cgroup throttle counters per round and process; account for newly created/exited PIDs during idle checks. Round snapshots cannot establish per-call cleanliness. Also record CPU model/affinity, engine/version/flags, hardwareConcurrency, isolation/shared memory, heap sizes. No sampler during primary calls. Never silently drop slow/outlier trials or resource failures; preserve receipts and withhold advancement on a contaminated/failed set.

## Gates
- Source/ABI provenance, prior browser correctness and actual sibling dispatch remain mandatory.
- Primary-module untimed Node correctness is allowed before freeze; no timings before reviewer/lead explicit release.
- After release, one bounded CI screen only on authorized fork/research branch, new sibling macro-timing/** + dedicated exact workflow so old correctness workflow is not triggered.
- All pre/post hashes and complete81calls required. All warmups/raws preserved.
- C consistently worse than B rejects traversal; any B benefit is scheduling, not evidence of cache blocking.
- A surviving preliminary result requires separate reviewed confirmation before full CORE integration. No automatic matrix expansion.
