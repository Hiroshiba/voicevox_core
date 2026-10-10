"""Read immutable artifact API metadata; never claim raw ZIP digest validation."""
import argparse, json, os, urllib.request
from pathlib import Path
from consumer_gate import load_contract, REPOSITORY, validate_artifact
from check_artifact_binding import NoRedirect

def fetch(binding):
    url = f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{binding["artifact_id"]}'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
        if response.geturl() != url:
            raise ValueError('Unexpected metadata redirect')
        raw = response.read(1024 * 1024)
        if len(raw) >= 1024 * 1024:
            raise ValueError('Oversized artifact metadata')
    metadata = json.loads(raw)
    validate_artifact(metadata, binding)
    result = {k: metadata[k] for k in ('id', 'name', 'expired', 'digest')}
    result['workflow_run'] = {k: metadata['workflow_run'][k] for k in ('id', 'head_sha', 'repository_id', 'head_repository_id')}
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    binding = load_contract()['code_transfer']
    metadata = fetch(binding)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('New metadata destination required')
    args.output.write_text(json.dumps(metadata, sort_keys=True) + '\n')
    print('SOURCE_ON_ARTIFACT_API_METADATA_VERIFIED', flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('artifact_metadata_verification_failed') from None
