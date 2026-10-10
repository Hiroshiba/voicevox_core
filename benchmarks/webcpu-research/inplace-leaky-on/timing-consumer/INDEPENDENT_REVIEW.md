# Independent review: fresh normal-tier ON capture 38025093941

Review date: 2026-10-10 UTC. Independently authored from the fresh transferred bytes; no earlier capture or another reviewer’s conclusions were used to derive the findings. No producer rebuild, primary timing run, GitHub write, or model/audio/tensor export was performed by this review.

## Decision and bounded conclusion

PASS for the bounded local callback/alias-path semantic review of the captured instruction prefixes and their exact transferred Wasm bodies. The fresh evidence supports scalar arithmetic for original/rebuilt exact-in-place inputs and XMM SIMD128 arithmetic for candidate exact-in-place inputs of at least four elements. The candidate retains the ordinary fallback path. Both recorded compilation tiers implement multiply plus ordered compare/select; none of the 18 instruction prefixes contains YMM/ZMM arithmetic or FMA. This does not establish a callback-specific SIMD256 revectorization benefit.

No arithmetic, alias-guard, loop-bound, or tail blocker was found within this scope. The original/rebuilt callback bodies are byte-identical, as are their native non-call bytes and internal branches. Their external runtime callee identities are not authenticated by this artifact, so this review does not claim whole-machine-code functional identity or normalize away the external calls to manufacture it.

The capture remains a profiled diagnostic with zero primary calls. This review does not itself authorize or validate primary timing, alter capture.json’s semantic_verified fields, establish a final executed tier, claim per-thread tier coverage, or establish profiled/unprofiled native byte identity. Those unavailable claims are explicitly excluded, rather than silently assumed.

## Immutable artifact and module identity

Producer: Hiroshiba/voicevox_core, run 38025093941, commit 269527e07303d8c3ab8bf4eabce158e9b72092c5. Run metadata records completed/success. Run URL: https://github.com/Hiroshiba/voicevox_core/actions/runs/38025093941

- capture/capture.json: `1835280c55bdd8d0388b0c26361d454d18d92b9df0b61e35623115b333055b30`
- capture/capture-summary.json: `c79ed615b870c7d824774942f177040426e125d048e942d299ca0999c8a4bdd2`
- code/transfer.json: `5bac0aa2d18d56a6f62d458424f532f0ecb28babf9bc69ddc9939d8beb85f982`
- on-immutable-code-38025093941.zip: `667cc99dcf19ce1b2fa0049d60c98fb175cae660baa398ff71103cc58a099e76`
- on-jit-capture-38025093941.zip: `bbce107f8a0a87768ec7be2a050af4a5004756a6598b68746dd4cf4167c7e856`
- artifact-metadata.json: `3137ac6b588992b35781c4a9acf7478c41adfd0760db3583a9aa1771c05556d6`
- run-metadata.json: `3836de9b1901e553ec36459eb7d534c8e0015fa1dae61e341940428744c43d11`

Both original ZIP byte digests were independently recomputed and matched the API artifact digests: immutable code artifact 11660531133 and capture artifact 11660271449. The extracted code directory is a closed eight-file set: exactly six JS/Wasm files, transfer.json, and LICENSES.txt. Every size/hash below was independently recomputed from those files. The capture provenance and source_proof objects exactly equal the transfer record. No approximate rebuild was substituted.

| File | Bytes | SHA-256 |
|---|---:|---|
| LICENSES.txt | 3142129 | `7d6a46636017ab02c58ee49042318b061656e75a2097238e6b0e3e0459584599` |
| candidate/voicevox_benchmark.js | 91796 | `e78f8974071755a0a4dbe05626b43e7fc80f6bdd8c9cd6336e3d60002a1f9eaa` |
| candidate/voicevox_benchmark.wasm | 13124820 | `2d45afc211b0db41c93bcaffbcad21a7d8d6960fa743eed2e8520279b36ee2ff` |
| original/voicevox_benchmark.js | 91796 | `48b0db6eec9f1dbe8368a6e940446c6122775b61a01faf995842b61953247985` |
| original/voicevox_benchmark.wasm | 13123137 | `ce115aa7ab547badd83a2b4e443e05c123b4e41dbc5b3f341407df198f1297c7` |
| rebuilt/voicevox_benchmark.js | 91796 | `e78f8974071755a0a4dbe05626b43e7fc80f6bdd8c9cd6336e3d60002a1f9eaa` |
| rebuilt/voicevox_benchmark.wasm | 13123678 | `5d1dd443c03fd5b26cd3e678cd61aea69dbe451eeece1b2f6b7ab56d87b9f8e6` |

An independent Wasm section parser counted 52 imported functions, resolved function 8287, verified its `(i32,i32,i32)->()` type, and found the unique table-0 entry at slot 10977. It also verified that the callback body hash is unique within each complete module and that all three element-section payloads are byte-identical.

| Variant | Callback bytes | Callback SHA-256 |
|---|---:|---|
| original | 344 | `30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6` |
| rebuilt | 344 | `30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6` |
| candidate | 918 | `5b2c6e2ddf5d836b4bde745de8fe76b845014669a9718d574fbed5dd5df27c3b` |

Common element-section SHA-256: `fb1c50da502d8ffb4c5069243fd4bef4fb269508f5b9e180bbc200f4f93f1518`.

The pinned producer workflow exports verified code before the capture/inference step, then revalidates capture and binds it back to the unchanged exported code before upload. Its source/archive verification of 1,438 ordered native members is retained as a producer attestation; this consumer did not receive and recompute the build archives or complete LLVM proof.

## Browser configuration, dispatch, and module audit

The capture reports HeadlessChrome/153.0.8010.12, V8 15.3.76.4, x86-64 AMD EPYC 7763. Its normal configuration uses --wasm-revectorize. The separate native diagnostic additionally uses --print-wasm-code-function-index=8287, --perf-prof, and --trace-baseline. There is no forced optimized-only/baseline-only tier flag in the recorded configuration. Profiling changes cannot be assumed inert: the diagnostic instruction bytes are not asserted identical to future uninstrumented timing code.

All three actual-CORE dispatch receipts say expected auto, actual splat, 12 checked splat pointers, zero loadsplat pointers, relaxed_simd false, worker/probe hardwareConcurrency 4, shared memory and cross-origin isolation true. The source original archive is the pristine XNN archive, not the forced-loadsplat archive. No CT/affine comparison or forced-loadsplat condition is included in this review. The source patch and callback bytes delimit the exact-in-place LeakyRelu change; broader archive scope remains the producer’s proof.

The audited initialization and final drain receipts match for each variant: one full-byte-checked module compilation, one main instantiation, one tracked Module, two created workers, two transfers of that known Module, and zero violations. jit_binding.js checks the complete expected byte array and the exact worker URL, rejects extra/unknown compilation and streaming compilation, and tracks the Module sent in worker load messages. This is stronger than matching a truncated Wasm module identifier. It is not an audit of each worker’s subsequently executed instruction pointer or tier.

The ON/OFF activation traces report ON 16 transformed groups / 695 revectorizable nodes and OFF zero for every module. These are module-level activation receipts, not proof that function 8287 became SIMD256.

## Allocation-to-instruction-prefix and record integrity

I reviewed the pinned jit_transport extraction implementation as well as the resulting safe evidence. It sequentially parses complete JIT_CODE_LOAD records and never resynchronizes after an invalid/truncated record. The target symbol binds function 8287 and Liftoff/TurboFan tier; a private address+tier+allocation-size join to the V8 WasmCode disassembly header supplies the true instruction length. Only allocation[:instruction_bytes] is decoded. Metadata and allocation-padding suffix bytes are separately counted and hashed. This avoids treating the complete JIT allocation as executable instructions.

The original private JIT logs, native addresses, and metadata suffix bytes were deleted by the producer and were not requested or recovered. Thus the reviewer independently recomputed the retained instruction bytes, their hashes, decoding, and branch targets, but relies on the pinned producer extraction/receipts for the original address join and the full-allocation/suffix hashes. These distinctions are not hidden by a generic “verified” label.

For all 18 retained records, the independent audit reconstructs the prefix byte-for-byte from contiguous instruction encodings, checks exact coverage, disassembles with GNU objdump, derives relative targets directly from encodings, and verifies every internal branch lands on an instruction boundary inside that same prefix. Every out-of-prefix relative branch in these records is a call. No bytes from an excluded suffix were passed to the decoder.

| Variant / tier | Record indices | Instruction bytes | Unpadded bytes | Allocation bytes | Excluded suffix | Allocation padding |
|---|---|---:|---:|---:|---:|---:|
| original / Liftoff | 0–2 | 984 | 992 | 1024 | 40 | 32 |
| original / TurboFan | 3–5 | 1040 | 1048 | 1088 | 48 | 40 |
| rebuilt / Liftoff | 0–2 | 984 | 992 | 1024 | 40 | 32 |
| rebuilt / TurboFan | 3–5 | 1040 | 1048 | 1088 | 48 | 40 |
| candidate / Liftoff | 0–2 | 2340 | 2348 | 2368 | 28 | 20 |
| candidate / TurboFan | 3–5 | 2464 | 2472 | 2496 | 32 | 24 |

Every group of three has identical instruction, full-allocation, and suffix digests within its own variant/tier. Each suffix includes eight bytes between instruction end and unpadded-binary end, plus the separately listed allocation padding. Those eight bytes were not decoded as instructions.

Both JIT files in each variant have a partial final record. The complete prefixes remain separately parsed: original 60,854,015 validated bytes / 257 trailing bytes; rebuilt 60,845,601 / 479; candidate 60,866,065 / 495. The artifact proves complete retained target records before truncation, not complete JIT files, absence of later tiers, or which code executed last. The first reported partial record is a truncated code-load body in all variants.

There are three identical Liftoff records and three identical TurboFan records per variant. The parser reads but discards the JIT thread ID, so these counts cannot independently assign records to individual workers or establish per-thread execution. This does not block the bounded callback-semantics review: all retained tier variants were reviewed, and module transfer is separately audited. It does block an unqualified per-thread-tier claim.

## Manual native control-flow and arithmetic review

Offsets below are relative to the appropriate instruction prefix, not process addresses. Both baseline and optimized recorded tiers were inspected; duplicated records are exact copies and do not add distinct arithmetic paths.

### Original and rebuilt

- TurboFan loads first/last and calculates len at 0x11–0x1b, returning at 0x177 for len <= 0. It loads the source and destination bases from callback-state offsets +8 and +12 and alpha from +16. At 0x40–0x55, it combines len < 4 with unsigned(destination − source) < 16. Exact alias makes the latter true, so it branches to scalar entry 0x17c. This is a native branch-path inference from pointer classes, not a hardware branch trace.
- For an eligible non-alias span, 0x5b–0x62 computes len rounded down to a multiple of four and broadcasts alpha. The 0x80 SIMD loop and its unrolled copies load four FP32 lanes, multiply, ordered-compare against zero, bitselect, and store four lanes. Count updates are +4 with per-chunk exit checks; the backedge at 0x169 returns to 0x80. The comparison at 0x16f falls through to scalar residual handling when needed.
- The exact-alias scalar path handles an odd leading element around 0x198 and then pairs in the 0x200 loop with exit tests after each unrolled pair. Its arithmetic is vmulss, vucomiss, and a conditional move/copy before the store. The next element is loaded after the previous element is stored, preserving exact-in-place processing without reading future vector lanes from overwritten output.
- Liftoff has the corresponding signed empty-length guard at 0x46–0x50, short-length/unsigned pointer-distance guard at 0x72–0x96, four-lane loop at 0xd3, and scalar path at 0x174/0x19c. Its scalar ordered comparison explicitly rejects unordered via parity handling before selecting the original input. Baseline budget checks and optimized poll checks are not mistaken for arithmetic.

### Candidate exact-in-place branch and fallback

- TurboFan 0x30 compares source and destination bases; 0x3a compares signed len to four. The OR at 0x41 and branch at 0x44 send any non-equal pointer pair or len < 4 to the original-style fallback at 0x4fc. Partial overlap does not enter the new exact-in-place branch. Liftoff has the same tests at 0x5a–0x77 and fallback 0x42c.
- On exact alias with len >= 4, the candidate rounds the positive length down to four at 0x4a–0x4c, broadcasts alpha at 0x72, and runs paired four-lane chunks in the loop starting 0xc0. Each vector loads and stores the same effective address; subsequent chunks advance by 16 bytes, and two chunks advance the logical element count by eight. Exit tests prevent an unrolled iteration from exceeding the rounded vector end. The backedge 0x259 goes to 0xc0. A single remaining four-lane chunk is handled at 0x273–0x29b; len=4 goes directly there.
- The loop’s representative sequence is 0xc4 vmovdqu xmm1, 0xca vmulps xmm2,xmm0,xmm1, 0xce zero-mask setup, 0xd2 vcmpleps zero <= x, 0xd7/0xdb/0xdf masked select, and 0xe4 store to the same address. All operands are XMM-width. Repeated pairs/unrolling do not make this a YMM instruction.
- At 0x29e the loop count is compared to len; exact multiples of four return at 0x421. Otherwise, the valid main-loop remainder is one to three elements. The later auto-vectorization eligibility guard at 0x2cc–0x2d0 rejects that short remainder and reaches the scalar tail at 0x429/0x440. That tail processes one FP32 element at a time with len checks; no vector over-read is required. The presence of additional SIMD-shaped tail code in the allocation does not imply that it runs for a valid short residual.
- The fallback at 0x4fc checks nonpositive len and otherwise uses the same original-style length/pointer-distance test, non-alias SIMD arithmetic, and scalar residual behavior. Short exact-in-place spans use this fallback. Liftoff separately exhibits the explicit exact-in-place four-lane sequence at 0x108 and a remaining vector at 0x1f1, with scalar residual handling and the unchanged fallback family.

### Floating-point select semantics

The reviewed vector sequence computes product = alpha*x and then selects x when ordered x >= +0, product otherwise. The mask implementation is (mask & x) | (~mask & product), not min/max, absolute value, affine arithmetic, or a fused multiply/add. +0 and -0 both satisfy the ordered comparison, so the loaded input zero’s sign bit is retained even when alpha is NaN. Positive numbers and +infinity also select x; negative inputs select the product. NaN inputs fail the ordered predicate and select the product. Scalar vucomiss/jae takes the keep-x branch only for ordered greater/equal; unordered sets carry and falls through to product. Liftoff’s explicit parity test has the same ordered behavior.

Packed arithmetic may calculate a product that is subsequently discarded for nonnegative lanes; the stored value still comes from the original x bits. The review claims the select/arithmetic behavior, not cross-engine NaN payload/sign preservation, exception-flag equivalence, or arbitrary out-of-contract overlap with the callback-state object. Normal Wasm FP32 semantics and valid callback memory/range inputs are assumed.

## Original/rebuilt native comparison without over-normalization

The two full modules are not identical, but the exact callback body is identical. The fresh native prefixes have byte-identical instruction boundaries, local arithmetic, guards, internal relative branches, loop increments, data-memory addressing, and stores. Only these E8 rel32 external-call instructions differ:

| Tier | Relative offsets | Difference |
|---|---|---|
| Liftoff | 0x6, 0x360, 0x37c, 0x3a4, 0x3c8 | Every relative target is +1024 in rebuilt; five-byte E8 call only |
| TurboFan | 0x3a2, 0x3ea | Every relative target is +1024 in rebuilt; five-byte E8 call only |

Liftoff 0x6 is an unconditional generated entry/prologue call after the function identifier is loaded. The 0x360 call is reached from the explicit stack-limit check at 0x2b/0x2f and returns to 0x35; 0x37c, 0x3a4, and 0x3c8 follow negative budget checks and save/restore the live registers before rejoining normal control flow. TurboFan calls 0x3a2 and 0x3ea are in register-save slow paths reached from [r13−0x4f] poll tests at 0x12b/0x130 and 0x30e/0x313, returning to 0x136 and 0x319. The instruction contexts establish their placement outside the arithmetic bodies. Their precise runtime symbol identities are not recoverable from this published evidence.

Therefore “all non-call instruction bytes and internal branches identical” is proven. “The external calls target the same authenticated runtime routines” and “the complete native functions are byte-identical/semantically identical after known relocation normalization” are not claimed. No internal branch, memory immediate, constant, or unexplained difference was normalized away.

## Observed workload and supplementary edge cases

The capture’s untimed alias receipts report, for each original/rebuilt/candidate synthesis, 69 callbacks and 435,901,440 elements: 41 exact-in-place callbacks / 256,615,424 elements and 28 disjoint callbacks / 179,286,016 elements. Pointer-class/guard inference predicts original/rebuilt scalar work for the former and SIMD for the latter; candidate makes all 69 eligible for SIMD. The observed lengths 492,544 / 1,970,176 / 7,880,704 are multiples of four, so these workload observations do not exercise residual tails. Two repeated native-diagnostic output checks per variant, and the separate alias checks, match the common original ON raw/PCM/WAV hashes and finite-output checks; no outputs are included here.

For additional bounded semantic testing, I copied each byte-exact callback body into an isolated minimal Wasm shell and executed synthetic memory cases in Node v24.19.0 / V8 13.6.233.17-node.51. These shells are separate reviewer diagnostics, not rebuilt full modules, Chrome-native replay, or timing inputs. No model or real inference data was used.

- 35,784 cases passed, including 2,556 exact-alias and 23,256 partial-overlap cases.
- 12,528 cases additionally checked 358,920 outputs against an independent scalar ordered-select oracle.
- First indices 0, 1, 3, 7; lengths -2, -1, 0 through 65, 127, 128, 129; source/output separations in both directions, exact alias, and disjoint; nine alpha patterns.
- Synthetic inputs cover positive/negative zero, positive/negative finite values, subnormals, infinities, quiet NaNs, and signaling NaNs. Original/rebuilt were bitwise equal in every case. Original/candidate showed no differing NaN payloads in this particular engine/test set, while the comparison policy still allows Wasm-permitted NaN differences. Sentinels proved no writes outside the requested output span, including zero/negative lengths.

These finite tests supplement rather than replace the manual loop/branch proof, and do not assert exhaustive correctness or Chrome’s tier-specific NaN payload behavior.

## Native evidence and prefix digests

### original
- Canonical native evidence SHA-256: `06f90b4e1e0eb7c184a8c648820ade77b32cd03bc1f83a284653fc18d55113b7`
- Private-dump digest-of-digests receipt: `756ec3e94c2b41b99971d66cb4376c71c2339da7ec302ba2a88ab0fc9ab26826` (underlying dumps unavailable/deleted)
- Liftoff records 0–2:
  - Instruction prefix: `d94c318c9ea9ff888dd4e148d0d78d062db1c44c47ef4b5d0193e03fa2c5cb68`
  - Full allocation receipt: `b2c6a17afa898de531b44744f440be9992e9e9d641b2e2ee0bf2905cc55346c3`
  - Excluded metadata/padding suffix receipt: `d71de2768d3b6da0f508809d49f08fa6022bae1122f7bb6fdeae284d9b8a2148`
- TurboFan records 3–5:
  - Instruction prefix: `13ad202dd777d8a25d51a2e06b8a834d874304b4762b4f0d123df75804e96ee5`
  - Full allocation receipt: `45ba3484463f1a4bce220e67d3cf29f4c87c331fbc548e8d4e55d3bcbaa8fd8d`
  - Excluded metadata/padding suffix receipt: `5770929931e67f848680194c61708ac2b71782cdfc5c75ca25972f687c4b9412`
### rebuilt
- Canonical native evidence SHA-256: `338fc4b9d42dbdf71a0caa8425f6f87975807377aa04e55091cc4a9e62df134d`
- Private-dump digest-of-digests receipt: `c4190d291520e26808b907a46b1a63a250caf4af52205292c706617372d9569e` (underlying dumps unavailable/deleted)
- Liftoff records 0–2:
  - Instruction prefix: `9020d053bb13bd8f373e155d82621dfcebe813672d325cc1d22b70733bcb01cb`
  - Full allocation receipt: `6fcd18a9d392b5337a132eb7506f57880a77e1d6512538f9bc2e1d117531f82b`
  - Excluded metadata/padding suffix receipt: `d71de2768d3b6da0f508809d49f08fa6022bae1122f7bb6fdeae284d9b8a2148`
- TurboFan records 3–5:
  - Instruction prefix: `ff20b3846bd713b66d165360b0fd76ca701cfe0bdbf70c1beb99a4d0d44062a9`
  - Full allocation receipt: `669569e8a78c40432812ee6bf9e9514e6528d9763e37a0040223e136c806c46d`
  - Excluded metadata/padding suffix receipt: `5770929931e67f848680194c61708ac2b71782cdfc5c75ca25972f687c4b9412`
### candidate
- Canonical native evidence SHA-256: `a300d14b49abbd273d94cc0bbeee9c1b2a100fcf360de399b749223d84aa0733`
- Private-dump digest-of-digests receipt: `573fa7db6c6987add7ac90290e6a7f8db76b650ae9c67c807df43dd2d794aeab` (underlying dumps unavailable/deleted)
- Liftoff records 0–2:
  - Instruction prefix: `c01eab17518f74c412017ec4c14e3d0d0586d994234611e8c7332016d70b8392`
  - Full allocation receipt: `f524bb565b5c2554d8f2987e990d14364cf08f937201d01c661ba60c5982b459`
  - Excluded metadata/padding suffix receipt: `8e726cfc4b6c10e75a09942d6d5f10d683aaee8dbb8447530c4fde98db3f12ed`
- TurboFan records 3–5:
  - Instruction prefix: `89e71a27cdd8e07aaa77e324df1c408ae80cc9bacdc97f649ad812ec342899d9`
  - Full allocation receipt: `0443b38d4674301bf6130bd7d605cfd917f172f5cea09f51f635b6764d9ca17b`
  - Excluded metadata/padding suffix receipt: `700c483c92db5a25d5b9b8b6a28b6e3963b42cb4e9c50cb6b6b72d1f73b4f70b`

## Reproducible reviewer evidence

- independent_audit.py: `b99512fbf5450d56194399de625f1e25fd4c70dc826a4ddd6c70d9d48365c86d`
- INDEPENDENT_AUDIT.json: `75d735951210e3cfda1a043e78cbc6978c2cc75c69db86f9d318a56c20f1ad5b`
- independent_edge_cases.js: `00f962c40e23406e63db8fdbec42503e6cfb0417e458845115d644adf313bb13`
- INDEPENDENT_EDGE_CASES.json: `0871643be46f0ff567fc5270f87e0bb491adcc52a9cc03289547a64afd617b64`

independent_audit.py independently checks the immutable file set/hashes, complete module callback/type/table identities, evidence canonical hashes, all native instruction bytes and internal branch boundaries, all 18 prefixes’ disassembly, absence of YMM/ZMM/FMA, and exact original/rebuilt difference sites. INDEPENDENT_AUDIT.json preserves the complete per-record metadata. The edge-case script emits count-only results, with no memory contents exported. Derived local full disassemblies are not needed in the deliverable; the original safe capture remains the instruction evidence source.

Final disposition: callback-operation review complete with the above explicit limits. Unsupported per-thread execution, authenticated external-runtime-call equality, exhaustive post-truncation coverage, profiled/unprofiled byte identity, and performance claims remain outside this review. There is no primary timing result in this capture.
