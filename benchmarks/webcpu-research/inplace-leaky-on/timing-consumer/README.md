# Exact-code ON timing consumer

This consumer is frozen to producer run 38025093941, commit
269527e07303d8c3ab8bf4eabce158e9b72092c5, code artifact 11660531133 and capture
artifact 11660271449, after two fresh bounded manual reviews. This release permits
only the prospective 27-call screen; no timing result is implied. Missing or
changed production pins fail closed. The historical timing contract remains byte-for-byte
unchanged at SHA-256 `359ae34a3c97b8ae1807205a43f00feaace02a63ef1574c77af96532ecbdd78a`.

For any future release, freeze the exact same-fork producer run/commit, immutable
artifact ID and GitHub API advertised ZIP digest, exact `transfer.json` hash,
capture report/summary/native-evidence hashes, both separately written review
documents, full producer provenance/source-proof hashes, all three runtime
identities, engine and exact output reference. Both reviews must name the same
capture report and transfer. Update the explicit contract SHA only after review.

The GitHub workflow downloads by artifact ID and run ID. API metadata validation
is a provenance check. `download-artifact` only warns on ZIP digest mismatch;
the consumer does **not** claim hard verification of raw archive bytes. Its hard
gate rejects extras, directories outside the three modes and symlinks, hashes
the exact transfer bytes, six code files and LICENSES.txt, and re-derives callback
body, function index, table slot and element-section equality from those Wasm
bytes. The producer's source, compiler and 1,438-member archive proofs remain
producer evidence. No local compiler receipt or rebuild claim is generated.

The private model is fetched and independently hash-pinned. It is never included
in the code artifact or public result. No audio, tensors, raw native dump, model,
build cache or log is uploaded. The public result contains the existing closed,
sanitized timing report and summary only.
The full official source archive is 172,305,822 bytes and retains the original
SHA-256 `1b02f8d4743c70be2f501e51f47b4c248fa1160a7677c041ddf1e753353f7081`.
The consumer-only downloader streams to that exact bound plus an overflow probe,
rejects non-200/partial responses, truncation and overflow, and then uses the
unchanged source/model hash verifier. This fixes the old 128 MiB transport read,
which truncated the real archive; no source/model pin was relaxed or replaced.

The shared runner performs the reviewed-transfer preflight before browser
activation. It re-hashes the exact bytes copied into the fresh private serving
directory and makes the copied code/model files read-only, so a source-path
mutation after preflight fails before activation. Runtime Wasm is hash-checked
again and instantiated from that checked buffer. The existing JS worker hashes
a fetched module then imports its URL; it assumes this private, read-only
serving directory is not modified by another privileged process between those
requests. This is not a hostile-host or arbitrary concurrent-write guarantee.
The exact-code report has a distinct schema, so stripping its transfer receipt
does not silently select the historical release mode.
Separate untimed activation/alias/output checks precede the normal
ON timing browsers. The primary flags are exactly
`--js-flags=--wasm-revectorize`, with ordinary tiering and no profiler/native
capture. Three fresh process sets × three balanced matched rounds × three
conditions produce 27 calls and nine pairs per comparison, with five warmups.
No code is rebuilt after capture or in the consumer.

Structural completion is not quality approval. The runner validates structure;
the workflow then explicitly writes `timing_validate.py`'s quality-labelled
summary, and `quality_gate.py` separately recomputes that entire summary and
requires whole-run quality. Stable process identities, full monotonic resource
coverage, no target/host swap growth/activity, at least 1 GiB available and every
sentinel ratio in inclusive [0.90, 1.10] are required. An invalid run retains all
27 rows and is `inconclusive_invalid_quality`, with performance claims disabled.
The summary can still be uploaded after quality failure, clearly labelled.
Every primary matched round is retained. Major-fault sensitivity uses the same
complete-round exclusion mask across all three comparisons.

Exact-code collection is stricter than the frozen historical helper. It reads
every Linux thread's children file recursively, with no best-effort child
skipping. Missing files, access denial, incomplete counters, task enumeration
changes, PID reuse or a different tree on re-enumeration abort the run. Each
idle/call/final snapshot must contain exactly one browser root matching that
condition's recorded PID and creation time. All five Linux process CPU counters
are required. Duplicate identities and process generations shared between the
nine browser generations are rejected. The complete per-condition tree and
counters must remain continuous from idle before/after through every call,
including intervening calls and the before sentinel. A final boundary after
the end sentinel and output checks extends coverage through that period.
These are boundary observations, not a claim that short-lived processes born
and exited entirely between observations were continuously observed.
The workflow and local wrapper first run a live root/child collector self-test.
Hosts lacking the Linux task-children interface fail closed before any browser
measurement; no best-effort fallback is permitted.

The stronger coverage policy/helper are independently pinned in the new
consumer contract and recomputed by the summary/quality gate. The historical
release contract and historical quality helper remain byte-for-byte unchanged.
The synthetic regression identifies a verifier weakness; it is not evidence
that any previous real CT, affine or ON run omitted a process.

The historical transfer-free fixtures and separate synthetic transfer fixtures
are offline software tests, not browser capture, measurement or review evidence.
Distinct hashed review documents bind manual attestations; hashing alone does
not establish semantic correctness or reviewer independence. The parent must
obtain and interpret both real reviews of the fresh native capture before
changing release pins. The frozen native capture was on AMD EPYC 7763, with
Chromium 153.0.8010.12 / V8 15.3.76.4. The semantic scope is bounded local callback
and alias-path behavior, including scalar-to-SIMD128 routing. Per-thread tier
coverage, final executed tier, authenticated external runtime callee identities
and profiled/unprofiled native byte identity remain unproven. The actual timing
host will be reported separately; the old Intel capture is not reused.

Local pipeline, after release: `bash timing-consumer/run_timing.sh CODE_DIRECTORY
MODEL ARTIFACT_METADATA OUTPUT_DIRECTORY`. The workflow runs its structural,
summary and quality stages separately so quality failure remains inspectable.

Both workflows retain explicit manual dispatch. Consumer publication triggers
only on the experimental branch's timing-consumer, timing-release or consumer
workflow changes. The producer's push paths enumerate its build/capture/proof
inputs and exclude timing files and its own workflow file. Thus publishing the
reviewed consumer and this trigger-only producer edit does not launch a duplicate
producer. For a future producer-workflow-only edit, dispatch the producer
explicitly; no repository settings, job permissions or capture steps change.
