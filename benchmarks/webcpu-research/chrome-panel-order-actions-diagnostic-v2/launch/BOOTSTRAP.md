# Portable provisioning source

This is a source implementation and review artifact. No download, installer,
compiler, Node, native payload, Wasm, browser, or Actions execution was performed
to produce it. Every runtime I/O/process entry requires the root execution
admission implemented by `launch.source_only`. Importing these modules does not
admit their runtime functions.

## Interfaces and ownership

`bootstrap_fetch.bootstrap_psutil(context, deadline)` creates the fresh
`context.provision_root` and its missing parent. It uses only stdlib HTTP,
archive, filesystem and hash operations; there is no subprocess or native psutil
import. The exact psutil wheel and every imported package file are checked before
the caller can import it. A hard SIGALRM bounds the entire operation at 150 seconds,
with a 120-second work deadline and 30-second cleanup/receipt reserve. It reports
its pre-supervisor observation scope honestly: this is not production process-tree,
RSS or CPU supervision. Transfer and completion/failure scalars are retained.

`provisioning.run(context, deadline)` requires that initial owned-root receipt.
It runs inside the already production-owned provision child. Fixed compiler
commands inherit that ownership, create no new session, and have individual
absolute deadlines. A failure stops the phase and requires the outer owner to
acknowledge descendant cleanup; killing a direct child is never called complete
cleanup. The parent owns the 2,700-second/3-GiB RSS/8-GiB artifact limits, and this
helper also checks its disk/artifact reserve at each boundary. Initial disk must
be 12 GiB; the remaining reserve is 4 GiB.

The context provides `roots.SOURCE`, the executing Python 3.12 `roots.PYTHON`,
`provision_root`, the exact `bootstrap_helper_lock`, and
`host_tools.make={path:'/usr/bin/make',bytes,sha256,...}`. Make is an official Ubuntu
preinstalled prerequisite, observed and pinned once in the allocated job; this
is not a complete OS-tree identity claim. No host patch executable is used.

Return values include `complete`, zero `operator_calls`/`browser_launches`,
`roots` (SDK/DEPS/BASELINE), the five `runtime_helper_package_roots`,
`portable_build_lock`, `baseline_inventory`, and `command_receipts`.
Chrome acquisition remains the parent's separate step.

## Concrete source policy

`bootstrap-inputs.json` contains 29 exact acquisition rows, eight byte-pinned
source patch operations and eleven complete command argv arrays. All compressed
sizes, expanded limits, member limits, source destinations and symlink rosters
are explicit. One GET is attempted per distribution, with no proxy, retry,
mirror or installer. The only allowed redirect is GitHub's normal official
codeload/release-assets redirect for an already SHA256-pinned archive.

Runtime/ORT/SDK-source archives, all 16 selected ORT dependency ZIPs, and all eight
Python wheels have pre-existing archive SHA256s. The Node and Emscripten binary
archive SHA256s were not retained historically. Their exact official Linux-x64
URLs and exact byte counts (25,738,964 and 300,508,180) come from the retained
successful SDK install log. Their downloaded archive hashes are recorded once
in-job, and cannot replace the previously pinned tool-file identity.

Before any SDK tool is executed, the complete 15,673-file, 979,475,838-byte
non-cache execution-file roster is checked against previously observed hashes.
It covers the complete Node tree and the upstream execution/source tree, including
LLVM/Binaryen, Emscripten Python and JS code and node_modules. Exclusions are only
interpreter-specific bytecode, installer `.emsdk_version` markers, and the
Emscripten cache. Every SDK symlink is separately pinned. The cache is checked by
source/header pins and an exact frozen in-job snapshot; no equivalence to every
historical cache file is claimed. Unknown or changed execution files fail before
any tool runs.

Both Linux SDK tarballs strip exactly one common top component, matching the
pinned installer source. Original top directory names are not invented. Extraction
writes regular files before links, rejects traversal, hardlinks, devices, duplicate
paths and unsupported encodings, and permits only exact declared symlinks. All
29 SDK links, 11 dependency links, and the separately created ORT-to-SDK link are
explicitly exposed by `allowed_monitor_links`. Chains and directory-link targets
are resolved and checked after the full link roster is present.

Python package code resides in `deps/python/{psutil,pyee,greenlet,playwright}` and
`deps/python/typing_extensions.py`. Wheel metadata and build packages remain
outside this isolated import parent. No wheel installer or pip is executed.

## Offline configuration and source proof

ORT source is reconstructed from its pinned tarball. Pure Python applies the exact
release and dependency patches with whole-file preimage/postimage SHA256 checks,
full context matching and explicitly pinned hunk positions. Some upstream patch
line numbers differ from the pinned archive by a line; these offsets were resolved
at source-authoring time against the exact retained bytes. Runtime does no offset
search, fuzzy matching or whitespace relaxation. The resulting bytes were checked
against the existing known configured tree using data-only operations.

The direct CMake command bypasses `build.py` and all SDK installers. All 16
`FETCHCONTENT_SOURCE_DIR_*` values point at freshly acquired owned roots;
`FETCHCONTENT_FULLY_DISCONNECTED` and updates-disconnected are on. Git and Patch
package discovery are disabled. An empty cross-compiling emulator and static
try-compile target prevent execution of emitted Wasm. Only the three ONNX proto
header targets are requested. Toolchain/configuration helper processes and compiler
identification are still tool execution and require admission; this is not an
execution-free configuration claim.

All pinned activation dependencies, generated header/input bytes, include ordering
and the complete configured activation command are checked. The pristine runtime
archive has 1,438 ordered members. Member 514 is rebuilt under the original source
and a separately patched VFS overlay; the exact control and candidate IR predicates
come only from the immutable `source_proof.py.txt`, materialized under its exact
hash. No broad donor preparation function or top-level archive-variant script is
imported. Member 757 receives the one pinned loadsplat selection edit, validates
all twelve pointer pairs and preserved target features, roundtrips the exact IR,
and must match the pre-existing forced-object hash. `llvm-ar s` rebuilds the index.
Every other 1,436 payload is verified unchanged, including the duplicate activation.

The returned `panel-portable-build-lock-v1` has fields `schema`,
`source_policy_sha256`, `files`, `allowed_symlinks`, and `baseline_proof`.
It snapshots every retained regular file and exact link in SDK/DEPS/BASELINE after
proof. SOURCE remains separately immutable. Absolute response-file/config paths are
regenerated for the fresh owned roots, never copied from private historical paths.
The compile phase must use `EM_FROZEN_CACHE=1` and verify the entire snapshot before
and after compilation.

## Limits of this evidence

No fresh provisioning attempt has run. Exact equality to independently admitted
final assets is still required by the later compile gate. Logical normalized IR
proofs and in-job generated-file snapshots do not establish cross-host baseline
byte identity. A missing frozen SDK cache input, unexpected CMake behavior, source
pin mismatch, deadline, missing file or unknown descendant must fail the attempt;
there is no widening, fallback cache or retry.

The tests are source/data-only: pure patch/path predicates, JSON pin/roster checks,
inert temporary symlink fixtures, AST order checks, and assertions that runtime
entrypoints remain blocked without admission. They do not call an admitted fetch,
extractor, compiler, installer, browser or native payload.
