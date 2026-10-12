#!/usr/bin/env python3
"""Execute only pure-Python source tests with process/network/native audit denial.

This harness never launches a subprocess. Wrong-workspace tests invoke source
entrypoints with runpy/importlib in this interpreter, including inherited Actions
environment variables, so the audit measures attempted side effects directly.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import runpy
import sys
import time
import unittest

BLOCKED_PREFIXES = ("subprocess.", "socket.", "ctypes.dlopen", "ctypes.dlsym",
                    "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.system", "pty.spawn")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--generate-protocol", action="store_true")
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    if args.label and re.fullmatch(r"[a-z0-9_-]{1,64}", args.label) is None:
        parser.error("label must contain lowercase letters, digits, underscore or hyphen")
    root = Path(__file__).resolve().parents[1]
    attempts = []
    def audit(event, _arguments):
        if event.startswith(BLOCKED_PREFIXES):
            if len(attempts) < 64:
                attempts.append(event)
            raise RuntimeError("source-only invocation audit denied " + event)
    sys.addaudithook(audit)
    sys.dont_write_bytecode = True
    started = time.monotonic()
    if args.generate_protocol:
        spec = importlib.util.spec_from_file_location("panel_source_protocol", root / "tools/protocol.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.write_protocol(root)
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=args.pattern)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    text = stream.getvalue()
    print(text, end="")
    receipt = dict(schema="chrome-panel-source-test-audit-v1",
        evidence="pure_python_source_mock_tests_not_runtime_qualification",
        pattern=args.pattern, tests_run=result.testsRun,
        failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        process_network_native_attempts=attempts,
        zero_invocation_attempts=not attempts,
        elapsed_seconds=time.monotonic() - started,
        compiler_node_wasm_browser_native_payload_executed=False,
        label=args.label,
        inherited_actions_environment=os.environ.get("GITHUB_ACTIONS") == "true",
        cwd_within_source_tree=Path.cwd().resolve().is_relative_to(root),
        passed=result.wasSuccessful() and not attempts)
    evidence = root / "evidence"
    evidence.mkdir(exist_ok=True)
    suffix = args.pattern.replace("*", "all").replace(".py", "").replace("/", "_")
    if args.label:
        suffix = args.label + "-" + suffix
    (evidence / (suffix + "-source-tests.txt")).write_text(text)
    (evidence / (suffix + "-source-test-audit.json")).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
