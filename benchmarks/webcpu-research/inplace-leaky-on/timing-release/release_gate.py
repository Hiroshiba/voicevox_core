"""Frozen manual-review release, distinct from automatic semantic verification."""
import hashlib,json
from pathlib import Path
CONTRACT_SHA = '359ae34a3c97b8ae1807205a43f00feaace02a63ef1574c77af96532ecbdd78a'
ROOT = Path(__file__).resolve().parent

def load_contract():
    raw=(ROOT/'release_contract.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=CONTRACT_SHA:
        raise ValueError('Reviewed release contract hash mismatch')
    c=json.loads(raw)
    if hashlib.sha256((ROOT/'MANUAL_REVIEW.md').read_bytes()).hexdigest()!=c['manual_review_sha256']:
        raise ValueError('Manual review hash mismatch')
    if hashlib.sha256((ROOT/'timing_quality.py').read_bytes()).hexdigest()!=c['quality_helper_sha256']:
        raise ValueError('Quality policy helper hash mismatch')
    if not c['manual_review_pass'] or not c['independent_review_pass']:
        raise ValueError('Two manual review passes required')
    if c['primary_flags'] != ['--js-flags=--wasm-revectorize'] or c['primary_calls'] != 27:
        raise ValueError('Frozen timing scope')
    return c

def release_diagnostic(provenance, engine=None, reference=None):
    """Only fixed field names and equality booleans; never arbitrary input values."""
    c=load_contract()
    result={'schema':'inplace-on-release-diagnostic-v1','conditions_match':set(provenance)==set(c['conditions']),
            'identity_fields':{mode:{field:provenance.get(mode,{}).get(field)==expected for field,expected in fields.items()}
                               for mode,fields in c['identities'].items()}}
    if engine is not None:
        result['engine_equal']=engine==c['engine']
        result['engine_fields']={field:engine.get(field)==expected for field,expected in c['engine'].items()}
    if reference is not None:
        result['reference_equal']=reference==c['reference']
        result['reference_fields']={field:reference.get(field)==expected for field,expected in c['reference'].items()}
    return result


def release_gate(provenance, engine, reference):
    c=load_contract()
    if engine != c['engine'] or reference != c['reference']:
        raise ValueError('Engine/output reference differs from reviewed capture')
    if set(provenance) != set(c['conditions']):
        raise ValueError('Three conditions required')
    for mode, expected in c['identities'].items():
        if any(provenance[mode].get(key) != value for key,value in expected.items()):
            raise ValueError('Runtime artifact differs from reviewed capture: '+mode)
    return {'schema':'inplace-on-reviewed-release-gate-v1',
            'contract_sha256':CONTRACT_SHA,'manual_review_pass':True,
            'independent_review_pass':True,'profiled_native_capture':True,
            'uninstrumented_code_identity_claimed':False,
            'capture_run_url':c['capture_run_url'], 'native_evidence_sha256':c['evidence_hashes'],
            'scope':c['review_scope']}
