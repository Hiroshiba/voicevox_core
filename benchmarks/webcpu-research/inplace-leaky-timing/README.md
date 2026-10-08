# Bounded exact-inplace LeakyRelu browser timing screen

Source preparation only until separately published and executed by the lead.

## Frozen design

- Three fresh process sets, each with a separate original forced-loadsplat browser and candidate browser
- Three paired trials per set, 18 primary calls total
- Predetermined order: AB/BA/AB; BA/AB/BA; AB/BA/AB (five AB and four BA pairs)
- Five recorded warmups per browser
- Two full raw-FP32/PCM/WAV byte checks before and after each browser's primary trials
- Original-baseline sentinels before and after each process set
- Normal tiering, explicit revectorization OFF, exact browser/V8/JS/Wasm/model/query pins from successful gate run 37778610494
- Worker fetches and hashes the exact selected Wasm and supplies those bytes through wasmBinary; JavaScript is independently hashed before import
- No alias helper, callback forwarding/table replacement, native dump, profiler or high-frequency CPU sampler in the primary worker
- CPU model, OS, affinity, available CPUs and build-toolchain context are recorded
- A separate one-second idle process-tree CPU delta after both browsers are ready checks for unused-pool spinning, without a primary sampler
- Low-frequency host/process resource snapshots immediately before/after calls. Every fault/swap observation is retained, without exclusions. Only execution failure or available memory below 1 GiB stops the screen
- Each latency row is checkpointed before post-call resource collection and assertions. Post-call resources include checkpoint IO; measured synthesis latency excludes it
- Browser root/descendant process identities are recorded and termination is checked before another process set starts. Private files are retained if descendant cleanup is unconfirmed

## Interpretation

These are nine pairs in three process-set clusters. The report provides paired ratios, per-set ratios, sentinel drift and all observations. This screen alone is not a confirmed effect estimate or a new release decision. No per-trial HTML or CPU companion is added.

## Provenance and privacy

The successful browser gate's numeric/hash-only reference is committed as gate_reference.json. Existing gate source validates the original cached build and constructs the exact six-byte candidate; this screen additionally requires exact successful-CI Wasm and JavaScript hashes. If a rebuild changes those bytes, it fails closed rather than silently treating a different artifact as the gated one.

Waveform comparisons occur only in memory. Private temporary assets are not uploaded. Only fully validated JSON reports are uploaded; detailed numeric records are also printed because artifact downloads may be unavailable. The workflow uses a new sibling source path and separate noncanceling concurrency group, with no cache save or unrelated workflow trigger.

## Local checks

Thirteen tests cover schedule balance, missing/reordered calls, stale modules, numerical mismatches, incomplete cleanup, retained major faults, low-memory stop, privacy rejection and hook-free worker source. Python/JavaScript syntax and YAML parsing are checked. No local browser execution is attempted.
