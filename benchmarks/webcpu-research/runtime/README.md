# Strict FP32 vocoder dispatch experiment

This directory prepares and checks three real CORE runtime variants: untouched ONNX Runtime, a bitcode roundtrip control, and forced XNNPACK loadsplat dispatch. It changes only the ordinary-SIMD FP32 GEMM/IGEMM dispatch decision. All other archive members, including the real pthreadpool implementation, must remain identical.

The build invalidates both `voicevox_core` and the final wrapper whenever the native archive changes. It verifies the actual linked CORE rlib dispatcher against the intended archive, hashes the proof in the build receipt, and checks actual WASM exports. Cleaning only the final wrapper is insufficient because CORE bundles the native archive.

The paired browser screen uses the original model, fixed vocoder shape, XNN2/global ORT1, disabled spinning, and explicit V8 revectorization OFF or ON. Its dedicated CORE Worker checks all twelve actual dispatcher pointers and real worker hardware concurrency after initialization. Candidates must repeat exactly and match the untouched XNN reference in raw FP32 and PCM before timing.

The separate numerical/profile step runs after timing processes close. It opens only one browser heap at a time, compares CPU2 and untouched XNN outputs, and optionally observes the untouched vocoder's real execution-provider placement. Profile durations are diagnostic and are excluded from latency results.

## Reproduction

Use the scoped `benchmark-webcpu-runtime.yml` workflow for complete commands and pinned dependency versions. Its preparation command supplies explicit `--harness` and `--kernel-dir` paths from the parent directory. Run the lightweight tests before building:

    python benchmarks/webcpu-research/runtime/test_prepare_vocoder_runtime.py
    python benchmarks/webcpu-research/runtime/test_numeric_log.py
    node benchmarks/webcpu-research/runtime/test_dispatch_helper.cjs

The canonical research harness is SHA256 `d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe`. Source pins fail closed on unexpected changes.

Only numeric metrics, hashes, diagnostic logs and report plots are retained as artifacts. Raw FP32, PCM, WAV and model archives remain temporary and private. Never upload the entire runtime-preparation directory, which contains downloaded source/model assets.

The local portable preparation was checked with untouched and forced-loadsplat builds and actual CORE no-model pointer probes. Browser correctness, placement and timing are separate gates; build success alone is not evidence of a speedup.
