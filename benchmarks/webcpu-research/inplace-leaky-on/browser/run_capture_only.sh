#!/usr/bin/env bash
# Capture only. This entry point cannot reach the future primary timing loop.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo 'Usage: run_capture_only.sh VERIFIED_ON_MANIFEST OUTPUT_DIRECTORY' >&2
  exit 2
fi
browser_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
research_dir="$(cd -- "$browser_dir/.." && pwd)"
manifest="$1"
output_dir="$2"
mkdir -p -- "$output_dir"
export PYTHONDONTWRITEBYTECODE=1
( cd -- "$research_dir" && sha256sum --check browser/SHA256SUMS )
uv run --script "$browser_dir/source_confirmation.py" --capture-only \
  --manifest "$manifest" \
  --harness "$research_dir/extracted/base/voicevox_webcpu_research.py" \
  --dispatch-helper "$research_dir/extracted/base/runtime/core_dispatch_check.js" \
  --output "$output_dir/capture.json"
python "$browser_dir/source_validate.py" --capture-only "$output_dir/capture.json" \
  --output "$output_dir/capture-summary.json"
