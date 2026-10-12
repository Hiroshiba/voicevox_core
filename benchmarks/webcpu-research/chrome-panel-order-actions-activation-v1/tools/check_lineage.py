"""Read/hash immutable local source and compile lineage; never execute it.

This is an author-side source-finalization check. A portable CI checkout does
not need these private local directories; it uses the admitted scalar receipts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARENT_MANIFEST = "edb4b5457edae250a80d3862dc0be832df510f18f22ddbc0956d52c0e7838a3a"
CONTROLS = {
    "artifacts.json": "f679042ed9d05e36026ca28e8cd8fb3b1427f9afe620ab40f15d46a9b64fea36",
    "cpu-observations.json": "f3905aa65a40a57a29565d38cbe5dad9b753d22d292ca78b8022f609a6138ed7",
    "supervision.json": "8e4c1171ae60a2ef3e57e3c9f72732601c510c16ed7728f85f7f29f5b4d52d05",
    "compile-complete.json": "e2202fb87d46597322a6c843633f1a34c6e302b380e147becd278003dc6ec1ea",
}


def sha(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def check():
    parent = ROOT.parent / "chrome-panel-order-screen-v1"
    manifest_path = parent / "MANIFEST.json"
    if sha(manifest_path) != PARENT_MANIFEST:
        raise ValueError("immutable parent source manifest changed")
    entries = json.loads(manifest_path.read_text())["files"]
    if len(entries) != 90:
        raise ValueError("parent source inventory changed")
    for name, expected in entries.items():
        path = parent / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["bytes"] or sha(path) != expected["sha256"]:
            raise ValueError("immutable parent source changed: " + name)
    attempt = ROOT.parent / "chrome-panel-order-local-compile-proposal-v1/attempt/compile"
    for name, expected in CONTROLS.items():
        if sha(attempt / name) != expected:
            raise ValueError("immutable compile control changed: " + name)
    ledger = json.loads((attempt / "artifacts.json").read_text())
    files = ledger["files"]
    if len(files) != 185 or set(ledger["excluded_later_control_files"]) != set(CONTROLS):
        raise ValueError("compile ledger scope changed")
    observed = {p.relative_to(attempt).as_posix() for p in attempt.rglob("*") if p.is_file()}
    if observed != set(files) | set(CONTROLS):
        raise ValueError("compile final roster changed")
    for name, expected in files.items():
        path = attempt / name
        if path.is_symlink() or path.stat().st_size != expected["bytes"] or sha(path) != expected["sha256"]:
            raise ValueError("immutable compile artifact changed: " + name)
    return {"schema": "panel-actions-parent-lineage-check-v1", "parent_source_files": 90,
            "compile_files": 189, "compile_ledger_files": 185, "later_control_files": 4,
            "all_bytes_unchanged": True, "code_or_payload_executed": False}
