#!/usr/bin/env python3
"""Pure source sealing and diffing; no build/runtime imports or subprocesses."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLAN_SHA256 = "07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _load(name):
    spec = importlib.util.spec_from_file_location("panel_freeze_" + name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_manifest():
    entries = _load("package_launcher").inventory(ROOT)
    files = {name: dict(bytes=len(data), sha256=digest(data)) for name, data in sorted(entries.items())}
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    return dict(schema="chrome-panel-source-seal-v1", plan_sha256=PLAN_SHA256,
        source_only=True, runtime_execution_enabled=True, runtime_authorization_required=True,
        file_count=len(files), source_bytes=sum(x["bytes"] for x in files.values()),
        source_tree_canonical_sha256=digest(canonical), files=files,
        excluded="dist, volatile source-test receipts and this self-referential MANIFEST; selected deterministic source proofs are included")


def donor_diffs():
    donors = {
        "src/vvx_ksb_20261011.c": ROOT.parent / "ks-full-operator-screen-v3/src/vvx_ksb_20261011.c",
        "harness/operator.c": ROOT.parent / "ks-full-operator-screen-v3/harness/operator.c",
        "browser/host/controller.js": ROOT.parents[1] / "scheduler-actual-operator/browser/host/controller.js",
        "browser/host/worker.js": ROOT.parents[1] / "scheduler-actual-operator/browser/host/worker.js",
        "browser/vendor/operator_browser.py": ROOT.parent / "add-core-speed-experiment-v2/browser/vendor/operator_browser.py",
        "tools/process_guard.py": ROOT.parent / "add-core-speed-experiment-v2/tools/process_guard.py",
    }
    patch, bindings = [], []
    for relative, donor in donors.items():
        current = ROOT / relative
        if not donor.exists() or not current.exists():
            bindings.append(dict(path=relative, donor_available=False))
            continue
        before, after = donor.read_bytes(), current.read_bytes()
        bindings.append(dict(path=relative, donor_available=True, donor_sha256=digest(before),
                             current_sha256=digest(after), changed=before != after))
        patch.extend(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
            fromfile="historical-donor/" + relative, tofile="chrome-panel-order-actions-activation-v1/" + relative))
    return "".join(patch), bindings


def seal():
    manifest = source_manifest()
    (ROOT / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    patch, bindings = donor_diffs()
    (ROOT / "evidence/source-diffs.patch").write_text(patch)
    (ROOT / "evidence/source-diff-bindings.json").write_text(json.dumps(bindings, indent=2, sort_keys=True) + "\n")
    # The experiment directory is entirely new. Preserve a complete additions
    # patch as well as the smaller mechanism-oriented historical donor diffs.
    additions = []
    for name, data in _load("package_launcher").inventory(ROOT).items():
        additions.extend(difflib.unified_diff([], data.decode().splitlines(True),
            fromfile="/dev/null", tofile="chrome-panel-order-actions-activation-v1/" + name))
    (ROOT / "evidence/new-source-tree.patch").write_text("".join(additions))
    parent = ROOT.parent / "chrome-panel-order-screen-v1"
    parent_manifest = json.loads((ROOT / "protocol/admissions/frozen-source-manifest.json").read_text())
    delta, changes = [], []
    current = _load("package_launcher").inventory(ROOT)
    for name in sorted(set(parent_manifest["files"]) | set(current)):
        before = (parent / name).read_bytes() if name in parent_manifest["files"] else b""
        after = current.get(name, b"")
        if before != after:
            changes.append(dict(path=name, old_sha256=digest(before) if before else None,
                                new_sha256=digest(after) if after else None))
            delta.extend(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
                fromfile="frozen-disabled-source/" + name, tofile="review-bound-actions-activation/" + name))
    (ROOT / "evidence/activation-delta.patch").write_text("".join(delta))
    (ROOT / "evidence/activation-delta.json").write_text(json.dumps(changes, sort_keys=True, indent=2) + "\n")
    return manifest


def verify():
    expected = json.loads((ROOT / "MANIFEST.json").read_text())
    if expected != source_manifest():
        raise ValueError("source seal mismatch")
    return expected


def main(argv=None):
    attempts = []
    def audit(event, _):
        if event.startswith(("subprocess.", "socket.", "ctypes.dlopen", "os.exec", "os.spawn", "os.fork", "os.system", "os.posix_spawn")):
            attempts.append(event)
            raise RuntimeError("source seal invocation denied")
    sys.addaudithook(audit)
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    value = verify() if args.verify else seal()
    print(json.dumps(dict(source_only=True, source_tree_canonical_sha256=value["source_tree_canonical_sha256"],
                          file_count=value["file_count"], process_network_native_attempts=attempts), sort_keys=True))


if __name__ == "__main__":
    main()
