# Exact-inplace LeakyRelu browser correctness gate

Source-only experimental guard transformation; no timing or production source patch.

The preparer verifies cached CORE asset receipts, original loadsplat archive hash, strict build identity and native-bundle proof. It discovers exactly one original callback by its immutable 344-byte instruction-body SHA256, verifies its three-i32 ABI and table membership, and applies the independently reviewed six-byte equality exception. It never assumes local function/table indices. All other functions and sections stay byte-identical. Unsupported encodings, missing/duplicate pins, changed ABI, debug offsets or LEB-boundary growth fail closed.

The browser runner first runs the existing pinned OFF diagnostic in its own browser. It then closes that process and runs original/candidate correctness sequentially in fresh normal-tier browser processes with only `--js-flags=--no-wasm-revectorize`. There is no forced-tier or native-dump flag in correctness runs. All three raw FP32, PCM and WAV repeats are compared as bytes in memory, across and within variants. Each raw/WAV call checks all 69 callback aliases and restores the forwarding hook in `finally`. The actual compiled table entry and fetched Wasm SHA are verified. The original generated JS is copied unchanged. No browser API is spoofed.

The diagnostic graph remains global ORT1 / XNN2 / spinning OFF, fixed length 962, all 12 loadsplat pointers, with the same original model. The gate expects 41 exact-inplace callbacks to change from scalar to SIMD eligibility and 28 disjoint callbacks to remain unchanged. Native CFG plus actual pointers establish guard/path eligibility; this does not claim hardware branch tracing.

Private generated binaries, model and diagnostic audio live only in temporary directories. Correctness waveforms never reach disk. A narrow result validator rejects unknown fields, wrong flags, missing restoration, partial coverage and nonidentical hashes before the workflow uploads two JSON reports. Only the validated numeric/hash summary is printed. No cache is saved by this job and it has its own noncanceling concurrency group.

## Local source checks

- `python test_prepare_inplace_leaky.py`: 9 tests, including shifted indices/table slots and negative/LEB-boundary cases
- `python test_validate_gate.py`: 9 completion/privacy/correctness rejection tests
- `node test_core_alias_check.cjs`: actual typed-table forwarding, classification, success/error restoration
- Python compilation and Node syntax checks pass
- Portable transform reproduced the local reviewed candidate SHA exactly

No local Chromium attempt was made. The browser runner and workflow are source-reviewed preparation, not an executed browser result. CI publication is lead-owned.

Reviewer integration fixes: the actual prepared transform schema is exercised against the validator; numerical hashes and alias metadata are checkpointed before comparison assertions. Table/ABI/alias identity and nested field allowlists are enforced. No unconditional workflow deletion of the private directory is performed.
