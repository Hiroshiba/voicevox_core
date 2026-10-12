# Source implementation addendum

The sealed revision 3 plan remains unchanged: SHA256
07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702.

Optional interpretation metadata may record bounded cache sizes, sharing counts
and cache-line size from ordinary read-only CPU metadata. Unknown stays unknown.
This adds no gate, counter run, privileged access, hardware change or experiment.
The 45,088-byte panel and 721,408-byte traversal may interact differently with
different cache capacities; metadata cannot establish cache-miss causality.
Mandatory engine/CPU/ISA identity remains the unchanged same-job binding.

The inherited production resource validator rejects target swap growth and host
swap I/O in ALL observed windows, including untimed initialization, setup,
checking and cleanup. This existing stricter safety gate is retained explicitly
by the root's source-integration decision on 2026-10-11. Primary windows also
require zero major faults. Untimed major faults and preexisting swap stock stay
disclosed; they are not relabeled primary faults. Missing observations still fail.

Distinct SharedArrayBuffer wrappers alone do not prove different backing stores.
The common native harness adds one reserved static uint32 alias word (4 bytes per
module, metadata overhead, no allocation or operator call). Under acknowledged
quiescence, the controller compares every prior heap at the SAME byte offset
before and after a guaranteed-changed write to the new heap's reserved word,
restores that word, and rejects any cross-heap change or missing observation.
Raw offsets stay inside the page. This implements the planned distinct-memory
proof without changing the allocator, call counts or seven-region phase gate.

The immutable future runtime request fixes the admitted Chrome distribution and
executable hashes, versions, complete stable flag policy, and OS/architecture
policy. It cannot know the unselectable Actions runner's CPU, boot ID or job ID
before allocation. Those host facts are observed once on that job before the
qualification operator calls, validated, sealed and carried unchanged through
timing. No host hunting, retry or change to expected engine values is permitted.
Actual launch arguments remain recorded per root. Only explicitly enumerated,
validated owned-profile/loopback-IPC argument values may vary across fresh roots;
all stable arguments remain ordered and exact, with unexpected flags rejected.
This is not compiled-asset normalization: every loaded asset still matches its
admitted whole-file hash exactly.

This implementation release is SOURCE_ONLY. All supported build, provision,
Chrome and conditional-runtime entrypoints reject before optional imports,
subprocesses or networking, regardless of Actions environment or working directory.
Pure-Python source generation, packaging, validation and explicitly synthetic
rendering are allowed; synthetic reports must never carry actual Actions claims.

No compilation, runtime qualification, performance sample, CI run or publication
is authorized by the implementation or its source-test results.

## Official Chrome bytes first pinned within the single Actions attempt

Approved prospectively on 2026-10-12 after independent scientific review. The
sealed revision3 plan remains unchanged. The exact official distribution source,
Chrome153.0.8010.12/Linux64, pinned helper source/method and fixed acquisition
bounds are selected before publication or execution. One future authorized CI
provisioning acquisition must validate and seal the complete archive hash, safe
installation roster, executable bytes and immutable distribution support bytes
before any Chrome launch, including the flags-only probe. The browser uses the
explicit admitted executable path; ambient Playwright browser selection is not
an accepted substitute. Every probe/root and the qualification-to-timing transition
rehashes the complete admitted browser installation and carries the same receipt.
This supersedes only the earlier implementation paragraph's requirement that the
future request already contain locally known Chrome distribution/executable hashes.

This is a same-attempt identity claim, not a preknown cross-environment Chrome
binary identity claim. Missing provenance, failed safe installation, incomplete
roster, wrong version/V8/flags, changed bytes, extra files or unknown observations
stop without acquisition retry, replacement version, fallback cache, host search,
rebuild or timing after incomplete qualification. The future provisioning budget
and whole-program6090-second bound include acquisition/install/hash work. Dynamic
OS libraries remain recorded host dependencies rather than a fabricated identical
cross-job OS tree. The source-reviewed runtime helper package rosters are separate
from distribution support and must remain fixed as admitted.

The local acquisition attempt remains STOPPED_NO_RETRY. Its one HEAD and one GET
header check returned text/html195B and inconsistent ETags; no response body,
archive hash or executable pin was retained. That failure is not normalized into
success and does not itself authorize a new fetch. The CI acquisition above is
only prospective source design pending separate execution permission.

Independently reviewed local compiled JS/Wasm/auxiliary-worker whole-file hashes
remain mandatory expected values before CI rebuild and before any Chrome. This
clarification does not relax exact emitted-asset admission, full330-call
qualification, source-release byte verification, fresh600-call timing, same-job
identity continuity, immutable phase records, safety/cleanup gates or independent
terminal review. It adds no operator calls, tests on Chrome or experimental arms.

## Separate prospective local compile profile

On 2026-10-12 the local compile-only admission was explicitly separated from the
production browser ownership interface. A separate source-only proposal may reuse
the independently reviewed bounded compiler/process-group profile from the final
KS full-operator v3 study, preserving its serial pinned tools and finite wall,
CPU, RSS, artifact and cleanup limits. It requires its own independent source
approval and compile GO. No local Chrome fallback or browser/payload execution is
permitted. The production Actions ownership observer in this source package is
unchanged. Every resulting emitted asset still requires independent review and
exact CI rebuild equality before Chrome; no local compile has occurred.
