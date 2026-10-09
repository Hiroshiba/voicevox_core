#!/usr/bin/env bash
# Source-bound profiled capture only; there is no primary timing loop in this driver.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo 'Usage: run_capture_only.sh VERIFIED_ON_MANIFEST OUTPUT_DIRECTORY' >&2
  exit 2
fi
capture_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
research_dir="$(cd -- "$capture_dir/.." && pwd)"
manifest="$1"
output_dir="$2"
mkdir -p -- "$output_dir"
export PYTHONDONTWRITEBYTECODE=1
( cd -- "$research_dir" && sha256sum --check browser/SHA256SUMS && sha256sum --check jit-capture/SHA256SUMS )
uv run --script "$capture_dir/jit_confirmation.py" --capture-only \
  --manifest "$manifest" \
  --harness "$research_dir/extracted/base/voicevox_webcpu_research.py" \
  --dispatch-helper "$research_dir/extracted/base/runtime/core_dispatch_check.js" \
  --output "$output_dir/capture.json"
python "$capture_dir/jit_validate.py" "$output_dir/capture.json" --output "$output_dir/capture-summary.json"
