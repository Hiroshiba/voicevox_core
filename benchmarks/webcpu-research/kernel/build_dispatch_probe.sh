#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
xnn="$here/XNNPACK-fe98e0b93565382648129271381c14d6205255e3"
: "${EMSDK:?}" "${ORT_ARCHIVE:?}" "${OUTPUT:?}"
export EM_CONFIG="$EMSDK/.emscripten"
options=(-sENVIRONMENT=node -sEXIT_RUNTIME=1)
if [ "${BROWSER_PROBE:-0}" = 1 ]; then
 options=(-sENVIRONMENT=web,worker,node -sMODULARIZE=1 -sEXPORT_NAME=DispatchProbe -sINVOKE_RUN=0)
fi
"$EMSDK/upstream/emscripten/emcc" -O2 -msimd128 -pthread -fwasm-exceptions -fno-fast-math -ffp-contract=off \
 -DDISPATCH_PROBE_MAIN -I "$here" -I "$xnn/src" -I "$xnn/include" \
 "$here/dispatch_probe.c" "$ORT_ARCHIVE" \
 "${options[@]}" -o "$OUTPUT"
