# Portable rebuild and source activation

The local compile and static emitted review are complete. The sealed scalar
receipts in `admissions/` identify all thirteen files from that compile. They
do not establish Chrome numerical qualification or cross-host rebuild equality.

## Ten unchanged compiled assets

The five `screen.js` and five `screen.wasm` files are admitted whole files. All
five JavaScript files are identical. Read-only byte inspection found no
`/workspace/`, `/tmp/`, `/opt/`, `__DATE__`, `__TIME__`, `2026-` or `202610` tokens
in these ten files. The JavaScript locates `screen.wasm` relative to its script
URL. Emscripten creates its backing pthread worker from the supplied main script
URL; there is no separately emitted backing-worker file.

This finite static search is useful portability evidence, not a reproducibility
proof. The CI build must produce the exact admitted file sizes and SHA256 values
before any Chrome launch, including a flag probe. A mismatch ends the attempt;
there is no normalization, new expected hash, automatic rebuild or retry.

## Three explicitly activated host assets

The locally emitted `controller.js` and `worker.js` deliberately retained hard
source-only guards. Activating those guards is a separately reviewed source
delta. The final Actions manifest binds all three new host files (`controller.js`,
`worker.js`, `index.html`) separately from the ten unchanged compiled assets.
It retains the original thirteen-file admission receipt as provenance rather
than pretending activated host bytes match the disabled release.

## Paths, toolchain and timestamps

LLVM inspection files contain absolute `ModuleID` and `source_filename` paths;
dependency response files, SDK configuration and dependency outputs also contain
workspace paths. They are regenerated under the one owned CI root and are not
required to reproduce local diagnostic hashes. Inputs and exact compiler/helper
identities remain pinned, archive member order is fixed, and archive metadata is
deterministic. No flag or final-file rewriting is introduced to hide path drift.
The final ten-file gate is authoritative.

The official release/archive and pinned wheels must be fetched only through the
reviewed bounded provisioning recipe. An existing cache, approximate compiler
version, ambient Python import, replacement browser or preinstalled helper is
not an accepted substitute. Setup before psutil becomes available is explicitly
bounded by the original provisioning and outer-job clocks; it is not described
as production-observed before that observer exists.

Chrome distribution bytes are established once in this Actions attempt, under
the previously approved official version/platform/source policy. The archive,
executable and complete installed support identity are sealed before any launch
and rechecked for every probe/root and the qualification-to-timing transition.
This is a same-attempt identity claim, not a preknown cross-environment hash.
