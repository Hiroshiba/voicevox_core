# Actual Chrome ON capture review

Run: https://github.com/Hiroshiba/voicevox_core/actions/runs/37993934600
Commit: c59b256d561b21df19ecfa2729ef91737ed13cba. Job 114034966984. Completed success.

## Receipt and scope

The complete sanitized log records were reconstructed into `reconstructed-capture.json`. Every repeated log chunk was identical, every sequence was complete, each of the three canonical native evidence hashes matched the CI summary, and the unchanged `jit_validate.validate` passed locally, including independent GNU objdump decoding of every instruction byte. The reconstructed report preserves values but has different JSON key insertion order; it is not represented as the original artifact's byte-identical JSON.

CI artifact report hash: 6f930761044c283a356a917d853dde2822eaee5a12ba781808a1312729a38ad4.
Native evidence hashes: original 40c38858871ee1e8ea7befc8ea9b64d2c99f7bb1051947605a97b41b4c0eef88; rebuilt 2865b4f3d14b98910a1da892614ab6d1877b48307279f94f3e2eb55e7a9fed46; candidate 33d56e249f68611506f43ff277d343a85abaa39665e0f9c0fb10b7abc296ef37.

Chrome 153.0.8010.12 / V8 15.3.76.4; Intel Xeon Platinum 8573C, 4 logical CPUs. All conditions verified HC4, 12 splat pointers, zero loadsplat pointers. Each variant verified one exact module compilation, two known pthread module transfers, zero audit violations. Per-binary OFF trace: zero transformation groups/nodes; ON trace: 16 groups / 695 nodes. These are module totals, not proof of revectorizing the target.

Each condition has three identical Liftoff instruction blocks and three identical TurboFan instruction blocks. Original/rebuilt: 984 / 1040 instruction bytes. Candidate: 2340 / 2464 instruction bytes. Original vs rebuilt differ only in external relative-call displacements (5 Liftoff calls, 2 TurboFan calls); all other instruction rows/bytes match. Do not label the full native functions byte-identical.

## Manual x86-64 routing and arithmetic review

Offsets below are hexadecimal unless stated otherwise. Re-decoded bytes were used where sanitized operand text said `<unsupported>`.

Original/rebuilt TurboFan: argument count is end-start (0x19–0x1b). Positive count enters the loop setup; input/output bases come from callback fields +8/+12. At 0x46–0x55 the unsigned output-minus-input gap is compared with 16; count<4 is ORed with gap<16. Exact alias gives gap=0 and branches to scalar setup 0x17c. Scalar 0x200 and its unrolled successors load one float, multiply with `vmulss`, compare x against zero using `vucomiss`, retain x only for ordered x>=0 (`jae`), otherwise select alpha*x, and store one float. The vector branch at 0x80 is not reached for exact alias. Original/rebuilt Liftoff independently has the same gap gate at 0x80–0x96, with scalar destination 0x174.

Candidate TurboFan: compares input and output base pointers at 0x30, ORs pointer inequality with signed count<4, and branches at 0x44 to old fallback 0x4fc. Thus exact alias with count>=4 enters the new packed loop. The masked count rounds down to a multiple of 4; the pair loop at 0xc0 processes two 4-float blocks per iteration. A single remaining 4-float block is handled at 0x273, and 0x29e exits when the processed count reaches count. Packed arithmetic at 0xca–0xe4 uses separate `vmulps`, an ordered `0<=x` mask (`vcmpleps`), and bitwise select using `vpandn` / `vpand` / `vpor`; the original x is selected on nonnegative lanes, alpha*x otherwise. Load and store address are the same and advance by 16 bytes per block. There is no FMA, relaxed SIMD or 256-bit arithmetic in this target.

The candidate scalar tail begins at 0x440 (reachable via 0x429) and performs the same scalar comparison/product selection, incrementing with count checks. The compiler retains a longer residual-vectorization path guarded by remaining-count/overlap checks; actual post-main remainder is at most 3, so the 0x2cc remaining-count<=19 guard leads to scalar remainder. The unequal-pointer/short-count fallback at 0x4fc returns for nonpositive count and otherwise preserves the old count<4 OR unsigned-gap<16 split at 0x510–0x529, the same separate packed arithmetic for safe disjoint buffers, and scalar fallback at 0x645. No partial-overlap optimization was added.

Candidate Liftoff independently checks pointer equality at 0x5a and count>=4 at 0x6a, branches to old fallback at 0x77 when either fails, and uses the same strict packed compare/multiply/bitselect at 0x113–0x137 on the exact-alias route. All three captured blocks per tier are byte-identical within each variant; this routing review applies to each.

## Correctness and limits

The unmodified validation passed all actual ON output checks and original-reference hashes: two alias gate checks plus three native diagnostic exercises and two repeated native checks per variant. Source/archive receipts, exact alias/disjoint counts (41/28), and FP32/PCM/WAV equality remain required. Manual instruction review establishes the intended routing and ordered compare/select operation; it is not a new exhaustive proof of all IEEE NaN payload behavior. Existing strict source/boundary gates remain the exceptional-value evidence.

All JIT files had explicitly reported partial trailing records. The complete preceding target records and exact V8 instruction-boundary/header join were validated; there is no whole-file completeness or final-executed-tier claim. Private JIT and model work directories were deleted successfully.

Outcome: actual profiled Chrome ON capture supports the expected exact-alias scalar-to-SIMD change, including preserved unequal-pointer fallback and scalar remainder. This is diagnostic evidence, not evidence that the uninstrumented timing code is byte-identical. Primary timing calls: 0. Capture schema remains semantic_verified=false; this independent manual note does not mutate the frozen parser/receipt or enable timing. A separately reviewed 27-call ON screen is the next proposed step, with timing instrumentation absent and ordinary exactness/runtime gates retained.

Independent second review completed at 2026-10-09 21:59:50 UTC and agrees with PASS for valid callback ranges and the model's fixed finite alpha. In the tail, N=n&~3, ecx=N-1 survives the main loop, r8=N-4, r12=n for residue1–3, hence the remainder guard is at most3 and goes directly to scalar code. Candidate tail x*alpha and original alpha*x have the same relevant fixed-finite-alpha behavior; unrestricted two-NaN-operand payload equivalence is not claimed. Timing release requires its own reviewed driver and retains the original capture's semantic_verified=false as historical data.
