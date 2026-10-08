#!/usr/bin/env bash
# Run on an authorized, otherwise idle CI runner. No raw audio/model inputs.
# Inputs are the verified released strict-FP32 archive and its Emscripten SDK.
set -euo pipefail
: "${EMSDK:?}" "${ORT_ARCHIVE:?}"
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
export EMSDK="$(realpath "$EMSDK")" ORT_ARCHIVE="$(realpath "$ORT_ARCHIVE")"
CHROMIUM="${CHROMIUM:-/usr/bin/chromium}"
mkdir -p results
xnn_commit=fe98e0b93565382648129271381c14d6205255e3
if [ ! -f xnnpack.tar.gz ]; then
 curl --fail --location --retry 3 "https://codeload.github.com/google/XNNPACK/tar.gz/$xnn_commit" -o xnnpack.tar.gz
fi
echo '639fa4fda5dbf0e501642db4a93ed1dba91aa4d9f2ce48ed5d01602adc0447cc  xnnpack.tar.gz' | sha256sum --check
# Only this script-generated extraction is reset; never trust stale cached headers.
rm -rf -- "$here/XNNPACK-$xnn_commit"
tar -xzf xnnpack.tar.gz
if [ ! -f pthreadpool.h ]; then
 curl --fail --location --retry 3 https://raw.githubusercontent.com/Maratyszcza/pthreadpool/4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0/include/pthreadpool.h -o pthreadpool.h
fi
echo '546f40bfb687562e812ba226bf4afcb848e3a2bcc2fc8ca781047b37789f0d72  pthreadpool.h' | sha256sum --check
python make_archive_variants.py --archive "$ORT_ARCHIVE" --sdk "$EMSDK" --out "$here/archives"
bash build_archive_probe.sh
mkdir -p archives/original
for variant in original auto-roundtrip loadsplat splat; do
 archive="$ORT_ARCHIVE"
 if [ "$variant" != original ]; then archive="$here/archives/$variant/libonnxruntime_webassembly.a"; fi
 BROWSER_PROBE=1 ORT_ARCHIVE="$archive" OUTPUT="$here/archives/$variant/dispatch_browser.js" bash build_dispatch_probe.sh
done
# Untimed dispatch, numerical validation and positive/negative activation checks.
python run_browser_probe.py --browser "$CHROMIUM" --revec off --dispatch --output results/dispatch-off.json
for mode in off on; do
 python run_browser_probe.py --browser "$CHROMIUM" --revec "$mode" --validate-only --trace --output "results/validation-$mode.json"
done
# No tracing or builds while timing. Each process alternates splat/load order.
for mode in off on; do
 python run_browser_probe.py --browser "$CHROMIUM" --revec "$mode" --output "results/kernel-$mode.json"
done
# Upload only results/, archive PROVENANCE/diffs and checksum records, not archives.
