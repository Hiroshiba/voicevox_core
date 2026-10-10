# Fresh immutable ON capture: first manual review

Result: PASS for the intended exact-alias scalar-to-SIMD128 change, for valid callback ranges and the model's fixed finite alpha. This is a fresh review of run 38025093941's actual transferred code and captured native instruction prefixes. It is not a performance result or an exhaustive theorem about arbitrary IEEE NaN payloads.

Run: https://github.com/Hiroshiba/voicevox_core/actions/runs/38025093941
Commit: 269527e07303d8c3ab8bf4eabce158e9b72092c5. Job: 114134148711. Attempt 1 completed successfully.
Reviewed: 2026-10-10 UTC.

## Immutable inputs and local verification

- Code artifact 11660531133, `webcpu-pristine-on-immutable-code`, ZIP SHA256 667cc99dcf19ce1b2fa0049d60c98fb175cae660baa398ff71103cc58a099e76, 11,273,326 bytes.
- Capture artifact 11660271449, `webcpu-pristine-on-jit-capture`, ZIP SHA256 bbce107f8a0a87768ec7be2a050af4a5004756a6598b68746dd4cf4167c7e856, 115,197 bytes.
- Both actual downloaded ZIP byte digests match the immutable GitHub API metadata for the exact same repository, producer run and commit. The run was read back as completed/success.
- Original capture JSON SHA256: 1835280c55bdd8d0388b0c26361d454d18d92b9df0b61e35623115b333055b30. These are the original artifact bytes, not a reconstruction from logs.
- Exact transfer JSON SHA256: 5bac0aa2d18d56a6f62d458424f532f0ecb28babf9bc69ddc9939d8beb85f982.
- The unchanged `jit_validate.validate` passes locally, including a fresh GNU objdump re-decode of every instruction encoding and validation of all capture/output/module-binding fields. The unchanged `code_transfer.verify_capture_binding` passes locally, independently rehashing the closed set of six JS/Wasm files and LICENSES, rediscovering callback body/index/table bindings and matching the entire producer provenance and source-proof records.
- Code ZIP contains exactly six code files, transfer.json and LICENSES.txt. Capture ZIP contains exactly capture.json and capture-summary.json. ZIP paths were checked against absolute/traversal/symlink entries before extraction; the code verifier also rejects unexpected extracted files.
- Published source files were recovered from exact commit 269527e and all 92 Git blob identities verified. Browser, JIT, code-transfer and historical timing source manifests pass; fresh local tests pass (148 Python, 10 JS).
- The real producer notice gate passed before export: 176 selected Cargo packages, 178 registry archives, 107 observed target registry packages, 110 host packages, 4 Git packages, 3 OpenJTalk source trees and 1,891 unchanged Eigen files. This is a notice/source audit, not legal assurance. The extracted LICENSES.txt is byte-identical to the source-pinned notice bundle: 3,142,129 bytes, SHA256 7d6a46636017ab02c58ee49042318b061656e75a2097238e6b0e3e0459584599. The sanitized receipt is retained separately.

## Actual environment and binding

This capture ran on AMD EPYC 7763, 4 visible logical CPUs, Ubuntu 24.04.5, Chromium 153.0.8010.12 / V8 15.3.76.4. It must not be presented as the older Intel capture. All conditions used original auto-XNN splat with 12 splat pointers, zero loadsplat pointers, HC4 and relaxed_simd=false. Per-binary OFF diagnostic had 0 transformation groups / 0 nodes; ON had 16 groups / 695 nodes. These are module totals, not evidence of this target's SIMD width.

Each variant's fresh audited diagnostic confirms one exact module compilation, one tracked module, two known pthread module transfers and zero violations. The final audited state is checked by the unchanged validator. Callback absolute index is 8287, unique table slot 10977, and the exact transferred Wasm bodies match the pinned original or candidate body hash.

There are three identical Liftoff instruction prefixes and three identical TurboFan prefixes per variant. Prefix sizes are 984/1040 bytes for original and rebuilt, 2340/2464 for candidate. The complete native allocation is larger; only the V8-header-bound instruction prefix was decoded, with excluded metadata/padding separately hashed. Every captured prefix has a verified private address/tier/size header join.

The published JIT blocks do not bind each record to a thread ID. The module-transfer audit does not prove which of the six records belongs to which thread, nor final-executed-tier or per-thread optimized coverage. The JIT files explicitly have partial trailing records; only the complete preceding target records are relied on. No whole-file completeness claim is made.

## Fresh instruction review

All offsets below are hexadecimal. Instruction bytes from this capture were independently reconstructed and decoded locally, including immediates that are intentionally abstracted in sanitized operand text. The following routing and arithmetic apply to all three byte-identical prefixes within each variant/tier.

### Original and rebuilt

TurboFan computes signed count=end-start at 0x19-0x1b and exits for nonpositive count at 0x1f. The input/output bases are fields +8/+12. At 0x40-0x55 it combines count<4 with unsigned output-minus-input gap<16. Exact alias gives gap=0 and branches to scalar setup 0x17c, never the vector loop at 0x80. Scalar 0x200 and its successors load one float, compute a separate vmulss alpha*x, compare x against zero using vucomiss, retain x for ordered x>=0 with jae, otherwise select the product, and store one float. The odd-prefix scalar path has the same rule. Scalar iteration/count exits and safe-disjoint packed path are retained.

Liftoff independently has the same overlap gate at 0x80-0x96, selecting scalar setup 0x174 for exact alias. Its scalar compare explicitly uses jnp then setae, so unordered values select the multiplied branch. This agrees with the intended ordered comparison.

Fresh original/rebuilt instruction rows and encodings differ only in five Liftoff and two TurboFan 5-byte e8 rel32 call displacements. All non-call instruction bytes match. Full function byte identity is not claimed, and the sanitized evidence does not authenticate the ultimate targets of those external relative calls.

### Candidate exact-alias path

TurboFan compares the input and output base pointers at 0x30 and combines inequality with signed count<4 at 0x3a-0x41. The branch at 0x44 goes to old fallback 0x4fc when either is true. Therefore valid exact alias with count>=4 reaches the new packed loop.

The masked count N=count&~3 is established at 0x4a-0x52. The main loop beginning 0xc0 loads four floats from the in-place address, computes vmulps alpha*x, computes an ordered zero<=x mask with vcmpleps, and selects original x under that mask using vpandn/vpand/vpor. The same address is stored at 0xe4. A second four-float block is processed at +16 bytes, followed by an eight-element increment; compiler unrolling retains the same operation. The possible final four-float block is handled at 0x273-0x29b. All packed registers are XMM128.

The remaining-count guard is safe for residue 1-3: ecx=N-1, then r8=(N-1)&~3=N-4, r12=max(N+1,count)=count, and the compared r8 at 0x2cc becomes count-N. It is at most 3, so jbe reaches scalar remainder 0x429 rather than the longer compiler-generated residual-vectorization path. The actual scalar remainder at 0x440 uses separate vmulss x*alpha, ordered vucomiss/jae selection, same-address store, and per-element count exits. For the fixed finite alpha this operand ordering does not change the relevant result; arbitrary two-NaN-operand payload equivalence is not asserted.

The unequal-pointer/short-count fallback begins 0x4fc, exits for nonpositive count, then preserves count<4 OR unsigned-gap<16 at 0x510-0x529. The safe-disjoint packed loop at 0x540 and scalar fallback 0x645 retain the original strict product/ordered-select operations. No optimization of partial-overlap inputs is claimed.

Liftoff independently checks base-pointer equality at 0x5a and signed count>=4 at 0x6a, branches to fallback 0x42c at 0x77 otherwise, and executes the exact-alias packed arithmetic at 0x113-0x137. Its one-vector case at 0x1fe and scalar tail at 0x76a retain the same comparison/product/selection. Liftoff fallback 0x472-0x4a0 retains the old short-count and gap gates. Scalar paths explicitly make unordered comparisons false using jnp/setae.

No YMM/ZMM or FMA instruction occurs in any of the six unique tier/variant prefixes. The observed change is scalar-to-SIMD128 exact-alias routing, not proof of SIMD256 target revectorization.

## Correctness, provenance and remaining limits

All actual two-repeat alias gates and native diagnostic output checks pass against the same original ON FP32/PCM/WAV hashes. Each alias exercise confirms 41 exact-in-place calls / 256,615,424 elements and 28 disjoint calls / 179,286,016 elements. Original/rebuilt classify the exact-in-place work as scalar, candidate as SIMD. These classes plus the instruction routing support the intended interpretation; diagnostic elapsed fields are not primary timing observations.

Original reference hashes: raw FP32 410a3a55cf81b3e35d003ce752aaaf613a5600cfeddf82c7c6970928a5a646d8; PCM 100890180c1067bcc316674be9012130157beb18f794688920fbfccd1dbb51a0; WAV a657c8dddfbebe6796739fd6090f4adeea3a1af05acc1e3db0da8b70b1b09576.

The source proof and ordered 1,438-native-member proof are producer evidence. The consumer independently verifies transferred code bytes/callbacks and does not claim to rebuild or revalidate the original source archive locally. Existing native-source and exceptional-value gates remain relevant; this manual review does not expand them.

The capture is explicitly profiled/instrumented. It proves observed baseline/optimized target code, not byte-identical uninstrumented timing code. Private JIT and temporary model directories were deleted, and all six observed processes per variant were cleaned up. The capture retains semantic_verified=false and awaiting_manual_review as designed. Primary timing calls remain zero. A separate contract may release only the bounded 27-call screen after the second independent review, binding these exact code bytes without rebuilding and retaining fresh runtime/output/activation and quality gates. No performance gain or adoption recommendation follows from this capture.

## Fresh identities


### original

- wasm_sha256: ce115aa7ab547badd83a2b4e443e05c123b4e41dbc5b3f341407df198f1297c7
- js_sha256: 48b0db6eec9f1dbe8368a6e940446c6122775b61a01faf995842b61953247985
- callback_body_sha256: 30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6
- archive_sha256: 407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd
- receipt_sha256: 1c0b0a98b70dad2886a4b08aa6ff9e53b6a74faf37cf77348c0e89eb5030ec22
- native_evidence_sha256: 06f90b4e1e0eb7c184a8c648820ade77b32cd03bc1f83a284653fc18d55113b7
- Liftoff instruction SHA256: d94c318c9ea9ff888dd4e148d0d78d062db1c44c47ef4b5d0193e03fa2c5cb68
- TurboFan instruction SHA256: 13ad202dd777d8a25d51a2e06b8a834d874304b4762b4f0d123df75804e96ee5

### rebuilt

- wasm_sha256: 5d1dd443c03fd5b26cd3e678cd61aea69dbe451eeece1b2f6b7ab56d87b9f8e6
- js_sha256: e78f8974071755a0a4dbe05626b43e7fc80f6bdd8c9cd6336e3d60002a1f9eaa
- callback_body_sha256: 30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6
- archive_sha256: cc316272fe3dd876e40c38ddf70c6b57519eca1df6719cb17b5aa086e3421718
- receipt_sha256: 5507f450a78cb841fbcaac7f85063e38a237d1abc4b5babe6604e318a3afc559
- native_evidence_sha256: 338fc4b9d42dbdf71a0caa8425f6f87975807377aa04e55091cc4a9e62df134d
- Liftoff instruction SHA256: 9020d053bb13bd8f373e155d82621dfcebe813672d325cc1d22b70733bcb01cb
- TurboFan instruction SHA256: ff20b3846bd713b66d165360b0fd76ca701cfe0bdbf70c1beb99a4d0d44062a9

### candidate

- wasm_sha256: 2d45afc211b0db41c93bcaffbcad21a7d8d6960fa743eed2e8520279b36ee2ff
- js_sha256: e78f8974071755a0a4dbe05626b43e7fc80f6bdd8c9cd6336e3d60002a1f9eaa
- callback_body_sha256: 5b2c6e2ddf5d836b4bde745de8fe76b845014669a9718d574fbed5dd5df27c3b
- archive_sha256: efe2ca2ee33478974b31c01597e490763e2c7f32924b38d94917d2f94d5ca204
- receipt_sha256: 3e9c284e555974580c22a45ad783883d00740b756a6a6d54ce522c4849d90ba5
- native_evidence_sha256: a300d14b49abbd273d94cc0bbeee9c1b2a100fcf360de399b749223d84aa0733
- Liftoff instruction SHA256: c01eab17518f74c412017ec4c14e3d0d0586d994234611e8c7332016d70b8392
- TurboFan instruction SHA256: 89e71a27cdd8e07aaa77e324df1c408ae80cc9bacdc97f649ad812ec342899d9
