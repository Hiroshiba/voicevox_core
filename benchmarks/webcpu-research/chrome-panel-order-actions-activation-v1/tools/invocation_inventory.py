#!/usr/bin/env python3
"""AST/source inventory, not an execution probe. No listed target is invoked."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKENS = ("subprocess", "Popen", "run_owned", "os.system", "posix_spawn", "execv", "spawnv",
          "socket", "urllib", "urlopen", "requests", "ctypes", "playwright", "chromium.launch",
          "xnn_run_operator", "_screen_run", "WebAssembly", "new Worker", "importScripts", "fetch(")


def inventory(root=ROOT):
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ("__pycache__", "dist", "evidence") for part in relative.parts) or not path.is_file():
            continue
        if path.suffix not in (".py", ".js", ".c", ".h", ".yml", ".yaml", ".txt"):
            continue
        text = path.read_text()
        hits = []
        for number, line in enumerate(text.splitlines(), 1):
            found = [token for token in TOKENS if token in line]
            if found:
                hits.append(dict(line=number, tokens=found))
        functions = []
        if path.suffix == ".py" and path.name != "launcher_template.py":
            tree = ast.parse(text, filename=relative.as_posix())
            functions = [dict(name=node.name, line=node.lineno)
                         for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if hits or functions:
            files.append(dict(path=relative.as_posix(), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                potential_invocation_source_hits=hits, functions=functions))
    return dict(schema="chrome-panel-source-invocation-inventory-v1", method="AST and literal source scan only",
        invocation_count=0, exhaustive_runtime_control_flow_claim=False, files=files,
        note="Source hits include comments, references and pure mock tests. They are review leads, not executed calls.")


if __name__ == "__main__":
    output = ROOT / "evidence/invocation-inventory.json"
    output.write_text(json.dumps(inventory(), indent=2, sort_keys=True) + "\n")
