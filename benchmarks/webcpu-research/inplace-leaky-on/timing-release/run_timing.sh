#!/usr/bin/env bash
set -euo pipefail
[[ $# -eq 2 ]] || { echo 'Usage: run_timing.sh MANIFEST OUTPUT_DIRECTORY' >&2; exit 2; }
dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
research="$(cd -- "$dir/.." && pwd)"
mkdir -p "$2"
(cd "$research"; sha256sum --check browser/SHA256SUMS; sha256sum --check timing-release/SHA256SUMS)
uv run --script "$dir/timing_confirmation.py" --manifest "$1" \
 --harness "$research/extracted/base/voicevox_webcpu_research.py" \
 --dispatch-helper "$research/extracted/base/runtime/core_dispatch_check.js" \
 --output "$2/timing.json"
python "$dir/timing_validate.py" "$2/timing.json" --output "$2/timing-summary.json"
