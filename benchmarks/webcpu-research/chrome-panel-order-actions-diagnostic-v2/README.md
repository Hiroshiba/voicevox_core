# P/Q provisioning diagnostic repair, version 2

This is a narrow prospective repair of the failed setup in run 38212739276.
Its exact inner cause remains unknown. See `protocol/diagnostic-repair-addendum.md`.
The first release/run remain immutable; this source grants no retry or execution.

# Chrome panel-order Actions activation source

This is a separately reviewable activation of the frozen five-arm R/A/B/Q/P
protocol. It contains public source and scalar provenance only. It does not
contain compiled binaries, Chrome qualification, timing measurements, or a
performance claim. Source preparation and pure-Python mock tests do not authorize
a job. Execution requires the exact independent source review and an explicit,
hash-bound root request for one Actions attempt.

The original disabled source release and the completed local compile attempt
remain immutable. The local compile and independent emitted-code review passed;
`protocol/admissions/` preserves the exact scalar admission receipts. The ten
compiled JS/Wasm files must reproduce exactly in Actions. Three activated host
files are bound by a separate reviewed manifest, never substituted into the old
receipt or normalized after a mismatch.

Plan SHA256: `07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702`.
Emitted review SHA256:
`34d559ce9e136ce58d274423b3de11202213e0b9d5a4c5a95a58faca029a574e`.
The explicit original implementation addenda and `protocol/portability-notes.md`
describe the scope and remaining cross-host uncertainty.

## Fixed experiment

- Chrome 153.0.8010.12 / V8 15.3.76.4, explicit revectorization OFF, ordinary tiering
- Full XNN FP32 operator C128/K128/KS11/M61568, D1/3/5, actual two-participant pool
- R accepted; A unchanged rebuild; B MC32 original calls; Q row-major inline;
  P panel-major inline. Q/P share the same arithmetic body and reduction order.
- 33 full-shape qualification fixtures: 330 calls, followed only after complete
  fail-closed qualification success by ten fresh timing roots and 600 calls
- Maximum 930 calls, 63 reference captures, 867 complete uint32 comparisons,
  6,832,570,368 comparison words; no replacement samples, retry or host hunting
- Five fixed 128 MiB memories and three reused buffers: 739,008,080 explicit bytes,
  excluding engine metadata; no retained 33-output set or GC-dependent copies
- Exact seven-region mod64/mod4096 phase records and unchanged allocator;
  finite timing and special-value qualification remain distinct criteria
- One Actions job and host; 6,090-second program within a 105-minute outer job;
  complete cleanup and sealed qualification receipt before conditional timing

This is matched-layout feasibility. Warmups do not establish a settled JIT tier;
ten roots do not guarantee independence, power or subpercent precision.
Advancement would mean another production-layout/full-CORE decision, not adoption
or model execution. The historical KS v3 result remains terminal and untouched.

## Portable entrypoints and evidence

`runner.py --actions` delegates to the single admission-bound Actions program.
The generated standalone Python file embeds the same complete source and can
verify it, extract it, or render explicitly synthetic input offline. Its
`--execute` mode requires absolute root GO/review/clock paths and fresh source
and work roots, then uses that same Actions program. An inherited Actions
variable, writable checkout, approximate tool version or arbitrary environment
flag is not authority. Local Chrome remains unavailable and unsupported.

The immutable request binds source, review, recipes, exact emitted assets and
host activation. The official Chrome distribution is acquired at most once
under fixed version/platform/source policy, validated before any launch, and
sealed for every probe, qualification root, timing root and transition. It is a
same-attempt browser identity, not a preknown local browser hash. All final
compiled assets must match before even a Chrome flag probe; mismatch stops the
attempt for review without rebuilding or changing expected hashes.

`tools/audit_source_tests.py` runs only pure-Python source/data/mock tests under
process, network and native-library invocation denial. No JavaScript evaluation
is part of those tests. Synthetic HTML is prominently labeled and is never an
actual Actions report. A complete actual scalar job receipt and independent
terminal review are required before any performance-result report or claim.

A successful or failed attempt exports only two bounded scalar JSON files. They
retain per-call gates/timings, acquisition and source/asset/engine identities, and
ownership/resource/cleanup evidence for independent reconstruction. Contextual
schemas exclude raw tensors, private argv and raw host identifiers; partial or
malformed evidence cannot become a successful result claim.

The prospective workflow remains outside `.github/workflows` until publication
and one-job authorization are separately approved. No workflow has been pushed,
dispatched or run by preparation of this source release.
