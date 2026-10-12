#!/usr/bin/env python3
"""Actions-only entrypoint, requiring the exact independent review and root GO.

Import is side-effect free. No local runtime or environment-only bypass exists.
"""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--source-summary"]:
        print("SOURCE PACKAGE: five arms; 330 qualification then conditional 600 timing calls; exact review and root GO required")
        return
    if not argv or argv[0] != "--actions":
        raise SystemExit("EXECUTION_ADMISSION_REQUIRED: runner accepts only the reviewed Actions program")
    required = {"--request", "--source-review", "--source-manifest", "--work", "--clock"}
    if len(argv) != 11 or set(argv[1::2]) != required:
        raise SystemExit("EXECUTION_ADMISSION_REQUIRED: complete root request, review, manifest, work and clock required")
    if not sys.flags.isolated or not sys.dont_write_bytecode:
        raise SystemExit("EXECUTION_ADMISSION_REQUIRED: execution requires Python -B -I")
    from pathlib import Path
    # Resolve from this verified package, never the caller's working directory.
    root = Path(__file__).resolve().parent
    for name, module in tuple(sys.modules.items()):
        if name.split(".")[0] in {"tools", "launch", "browser"}:
            origin = getattr(module, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(root):
                raise SystemExit("EXECUTION_ADMISSION_REQUIRED: ambient source-package import rejected")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools.program import main as program_main
    return program_main(argv)


if __name__ == "__main__":
    main()
