# Source-bound profiled JIT capture, version 1

This is a separate capture-only path for the original, rebuilt and candidate
pristine-XNN/splat ON conditions. It has no primary timing loop. The existing
browser source freeze, V8-text extractor and semantic analyzer are unchanged.
Completion means `awaiting_manual_review`, never semantic acceptance or timing
release. Existing per-binary OFF/ON activation, exact waveform/PCM/FP32 equality,
alias classes and complete source/archive proofs remain required.

## Identity and collection

Each diagnostic uses a fresh browser with the same normal tiering policy, plus
`--perf-prof` and `--trace-baseline`. These flags and the module audit are explicit
diagnostic instrumentation. The result does not claim that uninstrumented timing
code is identical. No `--no-liftoff` flag is used for real-target capture.

Before loading the pinned Emscripten glue, the dedicated diagnostic worker audits
all Wasm compilation entry points against a private copy of the exact fetched,
SHA-verified module bytes. It permits exactly one compilation and tracks the
resulting Module. Both Emscripten pthread workers must load the verified glue URL
and receive that same Module object. Foreign bytes, another compilation, unknown
Module instances/transfers, streaming compilation and foreign workers fail closed.
The final audit must equal the initialization audit. Source callback rediscovery
still verifies the exact body hash, absolute function index and unique table slot.
Targets with a custom function name require a separate review; fallback names are
verified from the actual module's name section before capture.

JIT_CODE_LOAD alone is an allocation, not a pure instruction stream. The pinned
V8 writer logs the full WasmCode allocation, including metadata and padding.
The adapter requires a complete matching WasmCode::Disassemble header, joined to
the load privately by exact code start address, tier, function index and allocation
size. V8 defines instruction_size as the minimum of the unpadded size, constant
pool offset, nonzero safepoint-table offset and handler-table offset. Only that
header-sized prefix is decoded. The remaining metadata/padding is separately
counted and hashed, and is never presented as instructions.

On builds without the disassembler, V8 still prints an instruction size and an
address interval. On builds with it, the first instruction address plus the frozen
text extractor independently binds the same prefix bytes. Missing, ambiguous,
mismatched or unsupported headers fail closed. There is no boundary guessing
from return opcodes or trailing zeroes.

Chromium can terminate a renderer with `_exit`, leaving stdio buffers unwritten.
After the target's correctness exercises, a bounded 256-function/262,144-call
synthetic JavaScript drain produces later JIT and baseline-trace records.
`--trace-baseline` traces JS compilation only, avoiding a global Wasm code dump.
It helps the earlier target headers and JIT records reach their readable prefixes.
A partial final JIT record remains explicitly reported. Complete preceding target
records may be used, but malformed nontruncation is rejected without resync. No
claim is made about missing later records, the last executed tier or whole-file
completeness.

## Evidence and privacy

GNU objdump decodes only the source-bound instruction prefix. Every displayed
byte must reconstruct that prefix exactly. Validation independently decodes the
reconstructed bytes again and compares every normalized row. Relative branches
are derived from instruction encodings; operand text is strictly sanitized.
Complete instruction encodings, offsets, sanitized rows and hashes are available
for manual review. Separate native-address fields and unsanitized address operands,
raw JIT files, unfiltered stderr, source symbols, models, tensors and audio are
excluded from published output. Full instruction encodings inherently retain any
address-bearing immediates present in those instructions; they are not claimed to
be address-free. These ephemeral code addresses are not model/user data.

Each capture has a 30-minute timeout, 8 MiB stream bound, 256 MiB private-file bound,
16 JIT-file bound and 64 total private path bound. The child inherits a 256 MiB
per-file size resource limit. Owned process identities are tracked and cleaned up.
JIT files are deleted after cleanup; successful publication requires deletion of
both JIT and temporary model/runtime work directories. No perf recorder, kernel
permission change, sandbox override, credential or security setting is added.
A permission or flag error stops the capture route; it is not retried automatically.

## Source anchors

- [V8 writer and full allocation bytes](https://github.com/v8/v8/blob/0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7/src/diagnostics/perf-jit.cc)
- [V8 instruction boundary and header](https://github.com/v8/v8/blob/0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7/src/wasm/wasm-code-manager.cc)
- [Wasm symbol name/index/tier](https://github.com/v8/v8/blob/0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7/src/logging/log.cc)
- [JS baseline trace scope](https://github.com/v8/v8/blob/0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7/src/codegen/compiler.cc)
- [Chromium immediate renderer shutdown](https://github.com/chromium/chromium/blob/153.0.8010.12/content/renderer/render_thread_impl.cc)
- [POSIX `_exit` implementation](https://github.com/chromium/chromium/blob/153.0.8010.12/base/process/process_posix.cc)

The synthetic Chrome canary established allocation-byte availability; it did not
establish LeakyRelu semantics. The new boundary extraction was cross-checked in one
Node process against the unchanged V8-text extractor, including immediate exit:
Liftoff 136 instruction bytes / 192 allocation bytes, and TurboFan 32 / 64 bytes.
Both instruction-prefix hashes matched. Actual Chrome target semantics require
manual review after this capture succeeds.
