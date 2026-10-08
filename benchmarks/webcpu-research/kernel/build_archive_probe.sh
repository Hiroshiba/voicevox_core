#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
xnn="$here/XNNPACK-fe98e0b93565382648129271381c14d6205255e3"
: "${EMSDK:?}" "${ORT_ARCHIVE:?}"
export EM_CONFIG="$EMSDK/.emscripten"
"$EMSDK/upstream/emscripten/emcc" -O3 -msimd128 -pthread -fwasm-exceptions -fno-fast-math -ffp-contract=off -fno-vectorize -fno-slp-vectorize \
 -I "$xnn/src" -I "$xnn/include" -I "$here" "$here/kernel_probe.c" "$ORT_ARCHIVE" \
 -sMODULARIZE=1 -sEXPORT_NAME=KernelProbe -sENVIRONMENT=web,worker,node -sALLOW_MEMORY_GROWTH=1 \
 -o "$here/kernel_probe_archive.js"
sha256sum "$ORT_ARCHIVE" "$here/kernel_probe.c" "$here/kernel_probe_archive.js" "$here/kernel_probe_archive.wasm" > "$here/archive-probe-sha256.txt"
