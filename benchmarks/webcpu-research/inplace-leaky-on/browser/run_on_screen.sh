#!/usr/bin/env bash
# Compatibility entry point: capture only while primary timing is disabled.
set -euo pipefail
browser_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$browser_dir/run_capture_only.sh" "$@"
