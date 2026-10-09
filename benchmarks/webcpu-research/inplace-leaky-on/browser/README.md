# Pristine XNN ON browser capture and manual review

The current entry point performs one capture-only Chrome phase. It runs the existing activation, dispatch, alias and exact-output gates for original/rebuilt/candidate, then captures each actual target function in a separate normal-tier ON browser. It stops with `status: awaiting_manual_review`, zero primary calls and `semantic_verified: false`. The future27-call schedule remains preserved but inactive. A non-capture runner invocation fails before artifact or browser access. Timing release will require a separately reviewed change after actual Chrome evidence is audited.

This sibling adapter leaves the published one-file distribution and reviewed extracted helpers unchanged. Preparation, browser execution and publication belong to the lead. No browser, build or inference was executed while preparing this source. Preserved Node dumps were read offline only to check complete target-code transport; they cannot approve Chrome lowering.

## Frozen artifact and runtime requirements

Input is `inplace-leaky-on-source-manifest-v1` from sibling `preparation/prepare_on_source.py`: ordered original/rebuilt/candidate entries, original pristine XNN archive SHA `407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd`, unchanged strict FP32 source/IR/body/toolchain pins, pristine auto dispatcher, ON revectorization, HC4/splat requirement, and all three complete receipts proving all1,438 ordered native archive members. Every binary carries research policy `inplace-source-pristine-auto-v1`.

The unchanged normal-tier ON flag is `--js-flags=--wasm-revectorize`. Separate code capture adds only `--print-wasm-code-function-index=<rediscovered index>`. The global print flag is excluded because V8's global-print OR index-match condition prints all functions. Partial and final capture stay bounded by64MiB. No forced tier, general parser relaxation, extra matrix, CPU quality companion, HTML or one-file edit is introduced.

1. Separate untimed OFF/ON activation traces cover each distinct JS/Wasm pair. OFF must report zero groups/nodes; ON must report positive groups/nodes. All diagnostic browser engine identities match. OFF remains a trace control only.
2. Separate ON alias workers check actual loaded hashes, real worker HC4, twelve splat pointers, fixed length962, ORT1/XNN2, spin-off and shared memory. Two WAV/raw checks per condition each observe69callbacks:41exact-in-place and28disjoint, restoring the table entry in `finally`.
3. The native capture workers perform the existing three normal-tier diagnostic exercises and two repeated output checks. There are no primary warmups, sentinels, process sets or primary trials in this phase. Native execution metadata binds actual module/JS/body hashes, function index/table slot, engine, flags, dispatch and outputs.

The first original ON alias-gate result is the sole raw FP32, PCM and WAV reference. All repeated outputs match it exactly. Historical forced-loadsplat/OFF output hashes are not gates. Format remains239104FP32samples, mono24000Hz PCM16, WAV478252bytes/raw956416bytes.

## Complete code evidence without semantic acceptance

The immutable shared semantic helper remains SHA `8f87d0b1a829edc3c381f8bf7ad50c5a1aa1cb5468a2b49d07f97ab3f05ae783`. Capture imports a separate frozen copy of the reviewed REX-annotation extraction stage. Its only additional interface is optional `include_encoding=True` in `parse_blocks`; default analyzer behavior is unchanged and the capture path never invokes that analyzer.

The structural lexer requires the exact target function index, complete V8 code blocks, valid printed encodings and contiguous coverage of every declared instruction byte. Unknown text inside the instruction stream stops capture and preserves its private dump. The public evidence contains every captured Liftoff/TurboFan block, each instruction's offset/size/exact `encoding_hex`, a code hash, normalized branch offsets and sanitized operands. Absolute printed addresses, arbitrary browser stderr, model/query data and audio buffers are not exported. Code bytes are retained to permit independent manual disassembly and CFG review. A structurally complete capture does not establish correct scalar/packed routing, comparison, select or fallback behavior.

After the entire capture receipt passes strict validation, `SOURCE_ON_NATIVE_CAPTURE_META` and numbered `SOURCE_ON_NATIVE_CAPTURE_CODE` chunks permit recovery of the complete code evidence from CI logs. Each chunk is at most8KiB; sequence, block, total count and canonical evidence hash protect reconstruction. The complete validated JSON is also retained. Every capture-only result retains its private targeted dumps. Oversized, interrupted or unsupported captures stop safely; cleanup and bounded failure receipts remain in place.

## Workflow interfaces

From the research root, run `bash browser/run_capture_only.sh VERIFIED_ON_MANIFEST OUTPUT_DIRECTORY` after the lead authorizes execution. It verifies `browser/SHA256SUMS`, invokes `source_confirmation.py --capture-only`, then strictly validates the result. Outputs are `OUTPUT_DIRECTORY/capture.json` and `capture-summary.json`.

Independent validation is `python browser/source_validate.py --capture-only OUTPUT_DIRECTORY/capture.json --output OUTPUT_DIRECTORY/capture-summary.json`. A successful capture summary explicitly states `awaiting_manual_review`, zero primary calls, false semantic verification and an inactive future timing schedule. Upload only artifacts that pass this command. Keep the complete sibling layout and only checksum-listed source files; do not upload private logs, models, build caches or audio.

Synthetic source checks use `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s browser -p 'test_*.py' -v`. Shared parser tests remain separate. The source-only fixtures cover transport, byte coverage, exact runtime/output bindings, zero primary calls, disabled non-capture CLI, redaction, bounded log reconstruction, interruption cleanup and cap rejection. Real preserved Node original/rebuilt/candidate traces also passed offline structural extraction without invoking semantic analysis.

## Inactive future screen

The preserved design is three fresh process sets × three matched rounds × three conditions =27primary calls and9paired observations per condition. It uses all six permutations plus three forward cyclic orders, `Random(1729)` shuffling, exact global positional balance and an explicitly reported4/5 relative pair-order imbalance. Startup order rotates by set. Existing code retains five warmups, repeated pre/post checks, idle/resource observations, original sentinels and all-row paired cluster/order/fault summaries. None of this executes in capture-only mode.

Prior source freezes and the narrow index-only repair remain under `browser/revisions/`. Source provenance may retain the historical loadsplat archive hash, while actual dispatch always requires zero loadsplat pointers. All13 reviewed extracted payloads remain unchanged.
