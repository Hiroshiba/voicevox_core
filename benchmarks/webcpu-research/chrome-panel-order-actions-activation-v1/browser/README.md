# Chrome panel-order Actions activation source

This directory prepares execution-capable source, without executing JS, Wasm,
compiler, native payload or browser during preparation. Every Python runtime entry
uses the shared immutable root CI request admission. Ordinary launches, inherited
Actions variables, direct donor imports and wrong-workspace paths cannot grant
that admission. One root request covers exactly the same-host 330-call
qualification followed, only on passing all gates, by the 600-call timing stage.
No second approval, environment bypass, local Chrome route or retry is introduced.
Independent source and emitted-code PASS plus the final root authorization remain
required before any future execution.

The three reviewed activation host files are separately byte-bound in
`host-manifest.json`; they do not change the ten compiled arm JS/Wasm files.
The Emscripten backing worker reuses its arm's unchanged `screen.js`. The page and
operator worker retain cross-origin-isolated HTTP/127.0.0.1 realm checks as defense
in depth. Those checks are not authority: only the admitted Python driver may
verify and serve the host files and immutable compiled assets.

## Runtime source contract

The phase child calls `browser.run(stage, context, deadline)`. Its context contains
the complete sealed schedule, expected source/assets/platform bindings, an
already-verified asset directory, carried phase records and fixture hashes, a
private runtime directory, and platform input paths. The phase parent supplies
independent all-thread ownership sampling across the entire child lifetime,
including probe, startup and cleanup. The child must inherit exact RLIMIT_CPU
1200 seconds for qualification or 2400 for timing. These are separate phase
processes in one job on one host. Sampled CPU/RSS/disk ceilings are not kernel
aggregate quotas.

One page owns five dedicated operator workers. `init` allocates the three reused
workspaces and validates all 930 calls; five sequential `init_arm` calls initialize
R/A/B/Q/P. Each arm initializes its actual two-participant pool once. Only timing
initialization performs clock calibration, immediately after that arm and before
the next. Per call, the page receives prepare, run, verify, delete. Exactly one
operator is live globally; each call uses fresh create/reshape/setup/run/delete.
All calls, including unretained ones, are checked. The worker's retained bracket
is exactly performance.now, screen_run, performance.now.

The worker shares its 128 MiB backing SAB once. It marks mutation, completion
sequence and quiescence with Atomics. The controller must own an inspection lease
before reading or writing the heap, including during asynchronous hashing. All
five heaps are tested for underlying aliasing: at a new module's reserved static
probe offset, read the same offset in every prior heap, complement the new probe,
require prior words unchanged, and restore in finally. Every inspected heap is
leased. No raw probe address is public.

## Frozen large-buffer lifetime

Five heaps consume 671,088,640 bytes. Exactly one output reference is 31,522,816
bytes, one complete readonly snapshot workspace is 35,675,216 bytes, and one
packed reference is 721,408 bytes. Total application large buffers: 739,008,080
bytes. Small control/receipt metadata and engine-owned crypto/JS overhead are
outside this explicit total, not claimed absent.

The six readonly regions are copied into the single scratch workspace before run.
Fixture hashes use nonshared views into that workspace and are awaited serially.
After run, every readonly byte is compared before scratch may be overwritten.
Packed comparison finishes before scratch is reused for its hash; output exact
comparison finishes before scratch is reused for output hashing. Live output and
packed references remain unchanged until that fixture is finished. No per-call
full-size copies, retained fixture-output collection, garbage-collection request
or collection-dependent memory reclamation exists.

The first special R reference relies on source-proven stores and actual coverage;
NaN-poison absence is not used as a universal oracle. Later qualification calls
receive bitwise-complement reference poison. All output comparisons are uint32.
Timing fixture 0 retains its all-finite gate. The declared finite/zero fixture
families 1/3 and 2 additionally receive their own full class checks; all 512
designated signed-zero witnesses require the exact even-channel negative-zero,
odd-channel positive-zero words. NaN families check their frozen designated
witnesses and exact whole output words against R. Hashes are scalar identity
bindings, never a substitute for the within-root raw comparisons.

Phases retain mod64 and mod4096 for all seven regions. They are seeded only by the
first qualification R call at each dilation and carried through every fixture,
root and stage. Timing cannot seed or reset them. The seven-region indexing order
is fixed; physical allocator addresses need not monotonically increase.

## Safety and receipts

Production Add-v2 safety sources are preserved byte-for-byte under
`vendor/reference/*.py.txt` as non-entrypoint review text. Derived Python modules
have shared admission checks before runtime imports. `source-bindings.json`
records every original/derived hash and reversible narrow delta: admission gate,
60-second root-startup wait, bounded actual argv/executable capture, and the
supervisor's explicit inherited per-process CPU limit before owned launch. No new
local fallback was substituted. Actual root and probe argv are recorded unchanged;
only explicitly declared generated profile/port values may vary under a frozen
semantic argv policy. JS/tiering flags and all remaining arguments match exactly.
Asset files always require complete byte identity, without normalization.
The browser itself follows the separately clarified route: exact official
version, platform, source, acquisition method and helper are fixed before one
future CI acquisition. Its archive, installed executable and complete support
tree are then hashed and sealed before any browser launch. This does not require
a locally pre-known Chrome archive hash or claim cross-environment Chrome byte
identity. The one job's measured browser bytes are immutable thereafter.
Every probe/root verifies the retained archive and complete installation before
launch, and the platform/transition checks repeat the full rehash. The owned child
receives the admitted executable path and SHA256 explicitly, rehashes it before
launch, and uses `executable_path`; implicit Playwright cache selection and
fallback are prohibited. Runtime identity binds both the support-tree hash and
the provisioning-receipt hash. Exact local-to-CI final JS/Wasm/worker admission is
unchanged by this Chrome acquisition clarification. Fresh owned Python supervisors,
RPC children and probe children use an explicit admission-descriptor argument via
the shared isolated child wrapper. Each child revalidates source/request/job
bindings and verified helper import routes; no environment variable grants access.
The retained unrelated donor workloads and unowned legacy launch methods remain
unconditionally disabled, and helper pins bind the activated derivatives.
Before any optional helper import, `browser.run` verifies the complete admitted
helper package bytes and configures only their isolated import paths. Existing
or already-loaded package routes must match; shadow imports are rejected.
The served directory is the sealed asset root: arm modules resolve at
`/R/screen.js`, `/A/screen.js` and so on. TMPDIR/TMP/TEMP point inside that stage's
separately owned runtime `tmp` subtree, so Chrome/Playwright profiles and temporary
files remain within the 256 MiB observed-artifact inventory. The frozen argv
profile placeholder accepts only this program-bound subtree.

Every command records its attempt and acknowledgement before later checks. A
known run's duration survives heap, guard, diagnostic or later verification
failure. Unknown outcome is terminal and is never retried or replaced. The page
exports bounded scalars/hashes only: participant pointer identities are verified
privately and represented publicly by per-arm hashes. Raw tensors and region
addresses never enter Python, CDP result JSON or disk.

The preserved donor all-window zero target-swap-growth and host-swap-I/O rules are
an explicit source-stage safety addendum. Untimed major faults and preexisting
swap stock remain disclosed. Successful cleanup must fit the stage deadline;
outer failure-only cleanup can admit no additional work or valid result. Browser
worker termination requests do not prove OS process cleanup: the production
subreaper/pidfd/all-thread-children proof remains mandatory before any next root.
The flag probe retains separate clocks: its entire launch/probe/cleanup interval
is at most 60 seconds, while actual cleanup begins immediately before browser
close in the owned child and ends after the parent's owned close/reap. That
contained interval must be at most 30 seconds. A 45-second probe with 2-second
cleanup is not mislabeled as 45 seconds of cleanup.

Tests in `tests/test_browser_source.py` are pure Python source/mock checks, run
under `tools/audit_source_tests.py`. They do not parse by evaluating JS, import
production runtime donors, or establish Chrome/Wasm numerical correctness.
