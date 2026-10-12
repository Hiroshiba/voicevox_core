#!/usr/bin/env python3
"""Deterministic pure-Python source packaging; never invokes packaged runtime."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("src", "harness", "browser", "tools", "protocol", "launch", "workflow", "tests")
SUFFIXES = {".py", ".c", ".h", ".js", ".json", ".html", ".md", ".txt", ".yml", ".yaml", ".patch", ".in", ".inc"}
SOURCE_PROOFS = ("native-fixture-proof.json", "native-generation.json", "native-selector-proof.json", "protocol-static-proofs.json")


def inventory(root=ROOT):
    paths = [root / "README.md", root / "runner.py"]
    if (root / "LICENSE").exists():
        paths.append(root / "LICENSE")
    for name in SOURCE_PROOFS:
        if (root / "evidence" / name).exists():
            paths.append(root / "evidence" / name)
    for folder in FOLDERS:
        for path in sorted((root / folder).rglob("*")):
            if "__pycache__" in path.parts:
                continue
            if path.is_symlink():
                raise ValueError("symlink source input")
            if path.is_file():
                if path.suffix not in SUFFIXES and not path.name.startswith("LICENSE"):
                    raise ValueError("unapproved source type: " + path.name)
                paths.append(path)
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing or redirected source input")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("source input outside package root")
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(paths)}


def package(root=ROOT):
    entries = inventory(root)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    blob = output.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    text = (root / "tools/launcher_template.py").read_text().replace(
        "__PAYLOAD_SHA256__", repr(digest)).replace("__SOURCE_MANIFEST__", repr(manifest)).replace(
        "__PAYLOAD__", repr(base64.b85encode(blob).decode()))
    directory = root / "dist"
    directory.mkdir(exist_ok=True)
    launcher = directory / "chrome_panel_order_benchmark.py"
    launcher.write_text(text)
    receipt = dict(schema="chrome-panel-standalone-source-v1", source_only=True,
        execution_enabled=True, runtime_authorization_required=True, actual_runtime_evidence_included=False,
        source_files=manifest, payload_sha256=digest,
        launcher_sha256=hashlib.sha256(text.encode()).hexdigest(), launcher_bytes=len(text.encode()))
    (directory / "launcher-manifest.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


if __name__ == "__main__":
    receipt = package()
    print(json.dumps({key: value for key, value in receipt.items() if key != "source_files"}, sort_keys=True))
