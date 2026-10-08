# Bounded macro operator screen

Source-only follow-on to successful untimed browser gate37769424250. No full CORE implication. See PLAN.md for exact scope,81-call primary matrix,81 retained warmups, frozen schedule and limits. Prioritize B/A for grouping and C/B for traversal; report each dilation and all3fresh process sets separately.

Primary interval calls reshape+setup+run, matching pinned ORT1.23.2 XNN Conv Compute's XNN API boundary. Allocation, prepack/initialization, hashing and ORT tensor-wrapper work are excluded explicitly. The clean production harness never redirects callbacks or kernels. Diagnostic and production link the identical generated production object files and archive; build.json records equality, and production-cycle.ll is checked to contain only reshape/setup/run calls plus lifetime intrinsics. The sibling retains detailed thread/count/ownership checks outside primary timing.

All modules use1GiB initial/2GiB maximum, strict FP32, Emscripten4.0.8, original MR4 loadsplat, real pthreadpool2. A/B/C live in separate persistent dedicated workers/heaps. Calls are serialized. Archive IR establishes110-spin bounded waits then futex parking; two1s idleCPU readiness windows and per-round resource snapshots are retained. No sampler runs during primary. `run_browser.py` is untimed unless explicitly passed `--screen`.

Published paths are only macro-timing/** and its new workflow on the named research branch/fork. Read-only token, separate noncanceling concurrency, pinned actions/dependencies, restore-only cache, no unrelated triggers, numeric/hash artifact allowlist. No models, raw audio, output tensors or generated binaries are published.

Sources:
- https://github.com/microsoft/onnxruntime/blob/v1.23.2/onnxruntime/core/providers/xnnpack/nn/conv.cc
- https://github.com/Maratyszcza/pthreadpool/blob/4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0/src/pthreads.c
- https://github.com/Maratyszcza/pthreadpool/blob/4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0/src/threadpool-common.h
