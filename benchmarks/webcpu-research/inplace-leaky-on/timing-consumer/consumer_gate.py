"""Independent exact-code release. Missing fresh evidence is a hard stop."""
from __future__ import annotations
import hashlib, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESEARCH = ROOT.parent
sys.path.insert(0, str(RESEARCH / 'code-transfer'))
sys.path.insert(0, str(RESEARCH / 'timing-release'))
from code_transfer import consume, canonical, MODEL_SHA, KEYS, ORIGINAL_BODY, CANDIDATE_BODY
from check_artifact_binding import validate as validate_artifact
from release_gate import CONTRACT_SHA as HISTORICAL_CONTRACT_SHA
from timing_quality import POLICY
from consumer_resources import POLICY as COVERAGE_POLICY

CONTRACT_SHA = 'f6cfdf630f88768e507aa75921bfa9217e7a2d65463bf87c2919da3e27503119'
REPOSITORY = 'Hiroshiba/voicevox_core'
REPOSITORY_ID = 617625583
REVIEW_FILES = ('MANUAL_REVIEW.md', 'INDEPENDENT_REVIEW.md')
REVIEW_SCOPE = 'Bounded local callback/alias semantics for valid ranges and fixed finite alpha: exact-alias scalar-to-SIMD128 routing. Per-thread tier coverage and final executed tier unproven; external runtime callees unauthenticated; profiled/unprofiled native identity unclaimed; no performance result from native capture.'
IDENTITY_FIELDS = {'wasm_sha256', 'js_sha256', 'callback_body_sha256',
                   'callback_function_index', 'callback_table_slot', 'archive_sha256'}

def need(value, message='Invalid exact-code release'):
    if not value:
        raise ValueError(message)

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def digest(value, length=64):
    need(isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value))

def closed(value, names):
    need(type(value) is dict and set(value) == set(names.split()))

def validate_contract(c):
    closed(c, 'schema status historical_contract_sha256 code_transfer capture reviews review_scope primary_flags primary_calls pairs_per_control process_sets conditions warmups_per_browser engine reference identities model_sha256 provenance_sha256 source_proof_sha256 quality_policy quality_helper_sha256 consumer_coverage_policy consumer_coverage_helper_sha256')
    need(c['schema'] == 'inplace-on-exact-code-release-v1')
    need(c['status'] == 'reviewed', 'Fresh capture and two manual reviews are not yet frozen')
    need(c['historical_contract_sha256'] == HISTORICAL_CONTRACT_SHA)
    need(c['review_scope'] == REVIEW_SCOPE)
    need(c['primary_flags'] == ['--js-flags=--wasm-revectorize'])
    need(all(type(c[k]) is int for k in ('primary_calls', 'pairs_per_control', 'process_sets', 'warmups_per_browser')))
    need((c['primary_calls'], c['pairs_per_control'], c['process_sets'], c['warmups_per_browser']) == (27, 9, 3, 5))
    need(c['conditions'] == list(KEYS) and c['model_sha256'] == MODEL_SHA)
    need(c['quality_policy'] == POLICY)
    need(c['consumer_coverage_policy'] == COVERAGE_POLICY)
    digest(c['consumer_coverage_helper_sha256'])
    for key in ('quality_helper_sha256', 'provenance_sha256', 'source_proof_sha256'):
        digest(c[key])
    b = c['code_transfer']
    closed(b, 'producer artifact_id artifact_zip_sha256 transfer_sha256')
    closed(b['producer'], 'repository run_id commit')
    p = b['producer']
    need(p['repository'] == REPOSITORY and type(p['run_id']) is int and p['run_id'] > 0)
    digest(p['commit'], 40)
    need(type(b['artifact_id']) is int and b['artifact_id'] > 0)
    for key in ('artifact_zip_sha256', 'transfer_sha256'):
        digest(b[key])
    x = c['capture']
    closed(x, 'producer artifact_id artifact_zip_sha256 report_sha256 summary_sha256 evidence_hashes profiled_native_capture uninstrumented_code_identity_claimed')
    need(type(x['artifact_id']) is int and x['artifact_id'] > 0)
    digest(x['artifact_zip_sha256'])
    need(x['producer'] == p and x['profiled_native_capture'] is True and x['uninstrumented_code_identity_claimed'] is False)
    for key in ('report_sha256', 'summary_sha256'):
        digest(x[key])
    need(type(x['evidence_hashes']) is dict and set(x['evidence_hashes']) == set(KEYS))
    for value in x['evidence_hashes'].values():
        digest(value)
    need(type(c['reviews']) is list and len(c['reviews']) == 2)
    for review, name in zip(c['reviews'], REVIEW_FILES):
        closed(review, 'file sha256 passed capture_report_sha256 transfer_sha256')
        need(review['file'] == name and review['passed'] is True)
        digest(review['sha256'])
        need(review['capture_report_sha256'] == x['report_sha256'] and review['transfer_sha256'] == b['transfer_sha256'])
    need(len({review['sha256'] for review in c['reviews']}) == 2, 'Two distinct review documents required')
    closed(c['engine'], 'product js_version')
    need(all(isinstance(v, str) and 0 < len(v) <= 128 for v in c['engine'].values()))
    closed(c['reference'], 'mode phase iteration wav_sha256 raw_sha256 pcm_sha256')
    need(c['reference']['mode'] == 'original' and c['reference']['phase'] == 'gate' and type(c['reference']['iteration']) is int and c['reference']['iteration'] == 1)
    for key in ('wav_sha256', 'raw_sha256', 'pcm_sha256'):
        digest(c['reference'][key])
    need(type(c['identities']) is dict and set(c['identities']) == set(KEYS))
    for mode, identity in c['identities'].items():
        need(type(identity) is dict and set(identity) == IDENTITY_FIELDS)
        for key, value in identity.items():
            if key in ('callback_function_index', 'callback_table_slot'):
                need(type(value) is int and value >= 0)
            else:
                digest(value)
        need(identity['callback_body_sha256'] == (CANDIDATE_BODY if mode == 'candidate' else ORIGINAL_BODY))
    need(len({(v['wasm_sha256'], v['js_sha256']) for v in c['identities'].values()}) == 3)
    return c

def load_contract():
    raw = (ROOT / 'consumer_contract.json').read_bytes()
    need(sha(raw) == CONTRACT_SHA, 'Exact-code contract hash mismatch')
    c = validate_contract(json.loads(raw))
    need(sha((RESEARCH / 'timing-release/release_contract.json').read_bytes()) == HISTORICAL_CONTRACT_SHA)
    need(sha((RESEARCH / 'timing-release/timing_quality.py').read_bytes()) == c['quality_helper_sha256'])
    need(sha((ROOT / 'consumer_resources.py').read_bytes()) == c['consumer_coverage_helper_sha256'])
    for review in c['reviews']:
        path = ROOT / review['file']
        need(path.is_file() and not path.is_symlink() and sha(path.read_bytes()) == review['sha256'], 'Missing exact-code manual review')
    return c

def release_diagnostic(provenance, engine=None, reference=None):
    c = load_contract()
    result = {'schema': 'inplace-on-exact-code-diagnostic-v1', 'conditions_match': set(provenance) == set(KEYS),
              'identity_fields': {m: {k: provenance.get(m, {}).get(k) == v for k, v in fields.items()} for m, fields in c['identities'].items()}}
    if engine is not None:
        result['engine_equal'] = engine == c['engine']
    if reference is not None:
        result['reference_equal'] = reference == c['reference']
    return result

def release_gate(provenance, engine, reference):
    c = load_contract()
    need(engine == c['engine'] and reference == c['reference'], 'Fresh capture engine/output mismatch')
    need(sha(canonical(provenance)) == c['provenance_sha256'], 'Producer provenance mismatch')
    need(set(provenance) == set(KEYS))
    for mode, identity in c['identities'].items():
        need(all(provenance[mode].get(k) == v for k, v in identity.items()), 'Transferred identity differs from reviewed capture')
    return {'schema': 'inplace-on-exact-code-reviewed-gate-v1', 'contract_sha256': CONTRACT_SHA,
            'manual_review_pass': True, 'independent_review_pass': True,
            'profiled_native_capture': True, 'uninstrumented_code_identity_claimed': False,
            'capture_report_sha256': c['capture']['report_sha256'], 'native_evidence_sha256': c['capture']['evidence_hashes'],
            'code_transfer': c['code_transfer'], 'review_scope': c['review_scope']}

def expected_receipt(c):
    b = c['code_transfer']
    return {'schema': 'inplace-on-code-consumer-v1', 'transfer_sha256': b['transfer_sha256'], 'producer': b['producer'],
            'source_and_archive_proofs_recomputed_here': False, 'code_hashes_and_callbacks_recomputed_here': True,
            'extracted_file_set_and_hashes_verified': True, 'downloaded_zip_digest_hard_verified': False,
            'artifact_metadata_verified': True, 'artifact_id': b['artifact_id'], 'artifact_api_digest': 'sha256:' + b['artifact_zip_sha256'],
            'consumer_contract_sha256': CONTRACT_SHA}

def validate_receipt(receipt, provenance, proof):
    c = load_contract()
    need(receipt == expected_receipt(c), 'Consumer receipt does not match reviewed transfer')
    need(sha(canonical(provenance)) == c['provenance_sha256'] and sha(canonical(proof)) == c['source_proof_sha256'], 'Producer evidence hash mismatch')
    return True

def preflight(directory, model, artifact_metadata):
    c = load_contract()
    metadata_path = Path(artifact_metadata)
    need(metadata_path.is_file() and not metadata_path.is_symlink() and 0 < metadata_path.stat().st_size <= 1024 * 1024)
    validate_artifact(json.loads(metadata_path.read_bytes()), c['code_transfer'])
    b = c['code_transfer']
    entries, provenance, proof, receipt = consume(directory, model, expected_transfer_sha256=b['transfer_sha256'], expected_producer=b['producer'])
    receipt.update(artifact_metadata_verified=True, artifact_id=b['artifact_id'], artifact_api_digest='sha256:' + b['artifact_zip_sha256'], consumer_contract_sha256=CONTRACT_SHA)
    validate_receipt(receipt, provenance, proof)
    return entries, provenance, proof, receipt

def copy_verified_file(source, destination, expected_sha256, maximum):
    """Hash the same bytes we write, closing source mutation after preflight."""
    source, destination = Path(source), Path(destination)
    need(source.is_file() and not source.is_symlink() and source.stat().st_size > 0)
    need(maximum is None or source.stat().st_size <= maximum)
    raw = source.read_bytes()
    need(len(raw) > 0 and (maximum is None or len(raw) <= maximum) and sha(raw) == expected_sha256, 'Runtime input changed after transfer preflight')
    need(not destination.exists() and not destination.is_symlink())
    destination.write_bytes(raw)
    destination.chmod(0o444)

if __name__ == '__main__':
    import argparse, os
    parser = argparse.ArgumentParser()
    parser.add_argument('--github-output', action='store_true')
    args = parser.parse_args()
    contract = load_contract()
    if args.github_output:
        binding = contract['code_transfer']
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write(f"artifact_id={binding['artifact_id']}\nrun_id={binding['producer']['run_id']}\n")
    print('SOURCE_ON_CONSUMER_RELEASE_VERIFIED', flush=True)
