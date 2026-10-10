"""Stream the exact pinned source archive; package only the private model, no code build."""
import argparse, io, sys, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'code-transfer'))
from prepare_model_only import package, CORE, ARCHIVE_SHA, MODEL_SHA

# Full official HTTP-200 archive, independently read and matched to ARCHIVE_SHA.
# The producer-era 128 MiB download bound silently truncated this 172 MB archive.
ARCHIVE_BYTES = 172305822
SOURCE_URL = 'https://github.com/yamachu/voicevox_core/archive/' + CORE + '.tar.gz'
FINAL_URL = 'https://codeload.github.com/yamachu/voicevox_core/tar.gz/' + CORE

def read_archive(response, expected_size=ARCHIVE_BYTES):
    if response.status != 200 or response.headers.get('Content-Range') is not None:
        raise ValueError('Partial model-source response forbidden')
    if response.geturl() not in (SOURCE_URL, FINAL_URL):
        raise ValueError('Unexpected model-source destination')
    length = response.headers.get('Content-Length')
    if length is not None and int(length) != expected_size:
        raise ValueError('Model-source declared size mismatch')
    output = io.BytesIO()
    while True:
        chunk = response.read(min(8 * 1024 * 1024, expected_size + 1 - output.tell()))
        if not chunk:
            break
        output.write(chunk)
        if output.tell() > expected_size:
            raise ValueError('Model-source archive overflow')
    if output.tell() != expected_size:
        raise ValueError('Truncated model-source archive')
    return output.getvalue()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('New private model destination required')
    if args.archive:
        if not args.archive.is_file() or args.archive.is_symlink() or args.archive.stat().st_size != ARCHIVE_BYTES:
            raise ValueError('Pinned model-source archive size mismatch')
        raw = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
            raw = read_archive(response)
    # This unchanged helper checks ARCHIVE_SHA before unpacking and MODEL_SHA after.
    data = package(raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    args.output.chmod(0o600)
    print('SOURCE_ON_PRIVATE_MODEL_READY ' + MODEL_SHA, flush=True)

if __name__ == '__main__':
    main()
