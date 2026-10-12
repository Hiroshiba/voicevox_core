#!/usr/bin/env python3
"""Read/hash historical frozen sources; never run or amend the old experiment."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OLD_MANIFEST_SHA256 = "6193067ce668dd724b730ee511478434ce351a9687d456d20be14ea2991bcc5b"


def check():
    old = ROOT.parent / "ks-full-operator-screen-v3"
    manifest_path = old / "MANIFEST.json"
    data = manifest_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != OLD_MANIFEST_SHA256:
        raise ValueError("historical v3 manifest changed")
    manifest = json.loads(data)
    if len(manifest["files"]) != 69 or manifest["tool_input_files"] != 7077:
        raise ValueError("historical manifest counts changed")
    for name, expected in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe historical source path")
        raw = (old / relative).read_bytes()
        if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise ValueError("historical source changed: " + name)
    return dict(schema="chrome-panel-history-preservation-v1", old_manifest_sha256=OLD_MANIFEST_SHA256,
        historical_source_files_checked=69, historical_tool_pin_count_in_manifest=7077,
        all_historical_source_bytes_unchanged=True, old_experiment_rerun=False,
        scope="read/hash historical69sourcefiles; 7077external tool pins are recorded, not re-executed or newly qualified")


if __name__ == "__main__":
    def deny(event, _):
        if event.startswith(("subprocess.", "socket.", "ctypes.dlopen", "os.system", "os.exec", "os.spawn", "os.fork", "os.posix_spawn")):
            raise RuntimeError("source-only history audit denied invocation")
    sys.addaudithook(deny)
    result = check()
    (ROOT / "evidence/history-preservation.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
