#!/usr/bin/env python3
"""Standalone source package for the review-bound Actions panel-order program.

Offline modes: --verify-source; --extract-only DIRECTORY;
--render-synthetic INPUT.json OUTPUT.html. --execute requires the exact root GO,
independent source review, fresh source/work roots and original Actions clock.
No actual Actions or operator result is included in this package.
"""
import sys

PAYLOAD_SHA256 = __PAYLOAD_SHA256__
SOURCE_MANIFEST = __SOURCE_MANIFEST__
PAYLOAD = __PAYLOAD__


def blocked():
    raise SystemExit("EXECUTION_ADMISSION_REQUIRED: standalone; no environment-only or local runtime override")


def verified_files():
    import base64
    import hashlib
    import io
    import zipfile
    from pathlib import PurePosixPath
    blob = base64.b85decode(PAYLOAD)
    if hashlib.sha256(blob).hexdigest() != PAYLOAD_SHA256:
        raise ValueError("embedded source package hash mismatch")
    result = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = archive.infolist()
        if len(members) != len(SOURCE_MANIFEST) or {m.filename for m in members} != set(SOURCE_MANIFEST):
            raise ValueError("embedded source inventory mismatch")
        if sum(m.file_size for m in members) > 64 * 1024 * 1024:
            raise ValueError("source package exceeds bounded size")
        for member in members:
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != member.filename or "\\" in member.filename:
                raise ValueError("unsafe source name")
            if member.is_dir() or member.filename in result or member.flag_bits & 1:
                raise ValueError("unexpected source member")
            data = archive.read(member)
            if hashlib.sha256(data).hexdigest() != SOURCE_MANIFEST[member.filename]:
                raise ValueError("source member hash mismatch")
            result[member.filename] = data
    return result


def extract_only(destination):
    from pathlib import Path
    files = verified_files()
    path = Path(destination).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("symlink extraction path")
    if path.exists():
        raise ValueError("source destination must not already exist")
    path.mkdir(parents=True)
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    return len(files)


def execute_actions(argv):
    """Validate source identity before extraction; central admission does the rest.

This function never spawns another Python interpreter. The extracted program
uses its one reviewed CLI and is solely responsible for admitted child actions.
"""
    import argparse
    import hashlib
    import importlib.util
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description="Exact review-bound Actions program")
    for name in ("go", "source-review", "source-root", "work", "clock"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    paths = {name: Path(getattr(args, name.replace("-", "_")))
             for name in ("go", "source-review", "source-root", "work", "clock")}
    for path in paths.values():
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("absolute non-symlink Actions paths required")
    if paths["source-root"].exists() or paths["work"].exists():
        raise ValueError("source and work roots must be fresh")
    for name in ("go", "source-review", "clock"):
        if not paths[name].is_file() or paths[name].stat().st_size > 4 * 1024 * 1024:
            raise ValueError("missing or oversized Actions admission input")
    files = verified_files()
    entries = {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
               for name, data in sorted(files.items())}
    canonical = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request = json.loads(paths["go"].read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite GO")))
    if request.get("expected", {}).get("bindings", {}).get("source_tree_sha256") != canonical:
        raise ValueError("root GO does not bind this embedded source inventory")
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise ValueError("execution requires Python -B -I before source-module imports")
    if any(name.split(".")[0] in {"tools", "launch", "browser"} for name in sys.modules):
        raise ValueError("standalone execution rejects preloaded source-package modules")
    # Admission validation is loaded only from verified embedded source. It
    # checks the review, request, Actions identity and original clock before the
    # central program may provision anything; no authorization is manufactured.
    extract_only(paths["source-root"])
    manifest = {"schema": "chrome-panel-source-seal-v1", "source_only": True,
        "runtime_execution_enabled": True, "runtime_authorization_required": True,
        "plan_sha256": "07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702",
        "file_count": len(entries), "source_bytes": sum(x["bytes"] for x in entries.values()),
        "source_tree_canonical_sha256": canonical, "files": entries,
        "excluded": "dist, volatile source-test receipts and this self-referential MANIFEST; selected deterministic source proofs are included"}
    manifest_path = paths["source-root"] / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    root = str(paths["source-root"])
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("panel_verified_actions_program", paths["source-root"] / "tools/program.py")
    program = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(program)
    return program.main(["--actions", "--request", str(paths["go"]),
        "--source-review", str(paths["source-review"]), "--source-manifest", str(manifest_path),
        "--work", str(paths["work"]), "--clock", str(paths["clock"])])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("--verify-source", "--extract-only", "--render-synthetic", "--execute"):
        blocked()
    if argv[0] == "--verify-source" and len(argv) == 1:
        print("SOURCE PACKAGE verified files:", len(verified_files()))
    elif argv[0] == "--extract-only" and len(argv) == 2:
        print("SOURCE PACKAGE extracted files:", extract_only(argv[1]))
    elif argv[0] == "--execute":
        return execute_actions(argv[1:])
    elif argv[0] == "--render-synthetic" and len(argv) == 3:
        import json
        from pathlib import Path
        files = verified_files()
        namespace = {"__name__": "panel_verified_synthetic_renderer"}
        exec(compile(files["tools/render_report.py"], "<verified synthetic renderer>", "exec"), namespace)
        document = json.loads(Path(argv[1]).read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        rendered = namespace["render_synthetic"](document)
        destination = Path(argv[2]).absolute()
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            raise ValueError("symlink report path")
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(rendered)
        print("SYNTHETIC SOURCE TEST rendered; no Actions result")
    else:
        raise SystemExit("invalid source-only arguments")


if __name__ == "__main__":
    main()
