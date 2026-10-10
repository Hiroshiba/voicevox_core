#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 4 ]] || { echo 'Usage: run_timing.sh CODE_DIRECTORY MODEL ARTIFACT_METADATA OUTPUT_DIRECTORY' >&2; exit 2; }
dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
research="$(cd -- "$dir/.." && pwd)"
(cd "$research"; sha256sum --check browser/SHA256SUMS; sha256sum --check timing-release/SHA256SUMS; sha256sum --check code-transfer/SHA256SUMS; sha256sum --check timing-consumer/SHA256SUMS)
# No compiler, producer preparation, native profiler or model/code rebuild here.
python "$dir/consumer_gate.py"
python "$dir/consumer_resources.py" --self-test
mkdir -p "$4"
uv run --script "$research/timing-release/timing_confirmation.py" \
 --code-directory "$1" --model "$2" --artifact-metadata "$3" \
 --harness "$research/extracted/base/voicevox_webcpu_research.py" \
 --dispatch-helper "$research/extracted/base/runtime/core_dispatch_check.js" \
 --output "$4/timing.json"
# The runner checks structure. Persist the explicit quality-labelled summary next.
python "$research/timing-release/timing_validate.py" "$4/timing.json" --output "$4/timing-summary.json"
# Complete 27 rows alone cannot pass this independent recomputation.
python "$dir/quality_gate.py" "$4/timing.json" "$4/timing-summary.json"
