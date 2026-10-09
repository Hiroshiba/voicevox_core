# Exact ON code transfer

The capture producer exports exactly three JavaScript/Wasm pairs before inference.
Models, queries, audio, tensors, native archives, build caches and logs are excluded.
Plain LICENSES.txt is included; repository notice materials are losslessly compressed
and pinned both before and after bounded decompression. The fresh dependency-notice
inventory gate must pass before export. This is a notice audit, not legal assurance.

After native capture, verify-capture requires identical producer code/proof hashes.
Manual review must then freeze the new whole-module identities and immutable artifact
metadata before any 27-call timing run. No old release contract is relaxed here.

The consumer independently hashes the closed extracted file set and derives callback
bodies, indices, table slots and element sections. The archive/source proof is original
producer evidence, not a claim that the consumer rebuilt or rechecked all 1,438 objects.
No synthetic compiler receipt is generated. Model preparation is separate and private.

Official download-artifact is pinned at 634f93cb2916e3fdff6788551b99b062d0335ce0.
It only warns on a downloaded ZIP digest mismatch. GitHub API artifact metadata pins
are distinct from the hard byte gate: exact transfer.json, all six code files, notices,
and rejection of unexpected files or symlinks. The receipt explicitly says the raw ZIP
digest was not hard-verified by the consumer.

Offline tests are synthetic verifier/mutation tests, not measurement evidence.
