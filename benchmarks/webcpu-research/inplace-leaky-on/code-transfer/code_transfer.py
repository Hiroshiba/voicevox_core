"""Transfer exact capture binaries, not rebuilt approximations. Never copy models."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'browser'))
from source_manifest import verify_manifest, discover, KEYS, MODEL_SHA, SOURCE_REFS, ORIGINAL_BODY, CANDIDATE_BODY
SCHEMA='inplace-on-code-transfer-v1'
FILES={f'{m}/voicevox_benchmark.{ext}' for m in KEYS for ext in ('js','wasm')}
TEMPLATE=json.loads((ROOT/'schema_template.json').read_text())
TEMPLATE_SHA='6d23d6b5044233744f90401aad8e3d0a6c4a2f373e98081c1e4f141d8b511a55'
MAX_CODE=64*1024*1024
class TransferError(ValueError):pass

def need(x):
    if not x: raise TransferError('invalid_code_transfer')
def sha(x):return hashlib.sha256(x).hexdigest()
def digest(x,n=64):need(isinstance(x,str) and re.fullmatch('[a-f0-9]{'+str(n)+'}',x))
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
def regular(path,maximum):
    need(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=maximum)
    return path.read_bytes()
def same_schema(value, template, path=()):
    """Closed shapes; unknown values can never become a free-form public string."""
    if isinstance(template,dict):
        need(isinstance(value,dict) and set(value)==set(template))
        for k,v in template.items():same_schema(value[k],v,path+(k,))
    elif isinstance(template,list):
        need(type(value) is list and value==template)
    elif isinstance(template,str):
        if re.fullmatch('[a-f0-9]{64}',template):digest(value)
        else:need(value==template)
    elif type(template) is bool:need(type(value) is bool and value==template)
    elif type(template) is int:
        need(type(value) is int)
        if path[-1] in {'rebuilt_dependency_count','candidate_dependency_count'}:need(1<=value<=100000)
        else:need(value==template)
    else:raise TransferError('invalid_template')

def validate_record(r):
    need(sha((ROOT/'schema_template.json').read_bytes())==TEMPLATE_SHA)
    need(type(r) is dict and set(r)=={'schema','producer','proof_scope','provenance','source_proof','files','notices'})
    need(r['schema']==SCHEMA and r['proof_scope']=='source_and_1438_native_members_verified_by_producer_only;consumer_rechecks_exact_transferred_code_and_callbacks')
    p=r['producer'];need(set(p)=={'repository','run_id','commit'})
    need(p['repository']=='Hiroshiba/voicevox_core' and type(p['run_id']) is int and p['run_id']>0);digest(p['commit'],40)
    for k in ('provenance','source_proof'):same_schema(r[k],TEMPLATE[k],(k,))
    need(r['source_proof']['source_refs']==SOURCE_REFS)
    need(set(r['files'])==FILES)
    for name,row in r['files'].items():
        need(set(row)=={'sha256','bytes'});digest(row['sha256']);need(type(row['bytes']) is int and 0<row['bytes']<=MAX_CODE)
        mode=name.split('/')[0];ext=name.rsplit('.',1)[1];need(row['sha256']==r['provenance'][mode][ext+'_sha256'])
    notices=r['notices'];need(type(notices) is dict and set(notices)=={'LICENSES.txt'})
    for row in notices.values():
        need(set(row)=={'sha256','bytes'});digest(row['sha256']);need(type(row['bytes']) is int and 0<row['bytes']<=8*1024*1024)
    return True

def verify_code(directory, r):
    validate_record(r);directory=Path(directory);need(directory.is_dir() and not directory.is_symlink())
    found=set()
    for p in directory.rglob('*'):
        need(not p.is_symlink())
        if p.is_file():found.add(p.relative_to(directory).as_posix())
        elif p.is_dir():need(p.relative_to(directory).as_posix() in KEYS)
        else:raise TransferError('invalid_code_transfer')
    need(found==FILES|{'transfer.json','LICENSES.txt'})
    entries={};element_hashes=[]
    for name,row in {**r['files'],**r['notices']}.items():
        b=regular(directory/name,MAX_CODE);need(len(b)==row['bytes'] and sha(b)==row['sha256'])
        if name=='LICENSES.txt':b.decode('utf-8');continue
        if name.endswith('.js'):b.decode('utf-8');continue
        need(b[:8]==b'\0asm\x01\0\0\0')
        mode=name.split('/')[0];expected=CANDIDATE_BODY if mode=='candidate' else ORIGINAL_BODY
        sections,_,target,slot,imports,_=discover(b,expected,918 if mode=='candidate' else 344)
        v=r['provenance'][mode]
        need(imports==52 and target['index']==v['callback_function_index'] and slot['slot']==v['callback_table_slot'] and sha(target['body'])==v['callback_body_sha256']==expected)
        element=[s['payload'] for s in sections if s['id']==9];need(len(element)==1);element_hashes.append(sha(element[0]))
        entries[mode]={'binary':directory/mode/'voicevox_benchmark.js','worker':{'wasm_sha256':v['wasm_sha256'],'js_sha256':v['js_sha256'],'callback_body_sha256':expected,'callback_table':slot,'absolute_function_index':target['index']}}
    need(len(set(element_hashes))==1)
    return entries

def export(manifest, output, licenses, *, repository, run_id, commit):
    output=Path(output);need(not output.exists())
    # Source verification precedes export and precedes any capture/inference.
    entries,provenance,proof=verify_manifest(Path(manifest))
    notice=regular(Path(licenses),8*1024*1024);notice.decode('utf-8')
    r={'schema':SCHEMA,'producer':dict(repository=repository,run_id=run_id,commit=commit),
       'proof_scope':'source_and_1438_native_members_verified_by_producer_only;consumer_rechecks_exact_transferred_code_and_callbacks',
       'provenance':provenance,'source_proof':proof,'files':{},'notices':{'LICENSES.txt':{'sha256':sha(notice),'bytes':len(notice)}}}
    data={}
    for mode in KEYS:
        src=entries[mode]['binary']
        for ext in ('js','wasm'):
            name=f'{mode}/voicevox_benchmark.{ext}';b=regular(src.with_suffix('.'+ext),MAX_CODE)
            need(sha(b)==provenance[mode][ext+'_sha256']);data[name]=b;r['files'][name]={'sha256':sha(b),'bytes':len(b)}
    validate_record(r)
    output.mkdir()
    for name,b in data.items():(output/name).parent.mkdir(exist_ok=True);(output/name).write_bytes(b)
    (output/'LICENSES.txt').write_bytes(notice);(output/'transfer.json').write_bytes(canonical(r))
    verify_code(output,r)
    return {'schema':'inplace-on-export-receipt-v1','transfer_sha256':sha(canonical(r)),'files':len(FILES),'producer':r['producer']}

def verify_capture_binding(directory, capture):
    r=json.loads(regular(Path(directory)/'transfer.json',1024*1024));verify_code(directory,r)
    c=json.loads(regular(Path(capture),16*1024*1024))
    need(c['status']=='awaiting_manual_review' and c['capture_only'] is True and c['trials']==[])
    need(c['provenance']==r['provenance'] and c['source_proof']==r['source_proof'])
    return {'schema':'inplace-on-capture-code-binding-v1','transfer_sha256':sha(canonical(r)),'capture_sha256':sha(Path(capture).read_bytes()),'producer':r['producer'],'timing_permitted':False}

def consume(directory, model, *, expected_transfer_sha256, expected_producer):
    digest(expected_transfer_sha256)
    raw=regular(Path(directory)/'transfer.json',1024*1024);need(sha(raw)==expected_transfer_sha256)
    r=json.loads(raw);need(r['producer']==expected_producer)
    entries=verify_code(directory,r)
    model=Path(model);need(model.is_file() and not model.is_symlink() and sha(model.read_bytes())==MODEL_SHA)
    for e in entries.values():e['model']=model
    return entries,r['provenance'],r['source_proof'],{'schema':'inplace-on-code-consumer-v1','transfer_sha256':sha(raw),'producer':r['producer'],'source_and_archive_proofs_recomputed_here':False,'code_hashes_and_callbacks_recomputed_here':True,'extracted_file_set_and_hashes_verified':True,'downloaded_zip_digest_hard_verified':False}

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='mode',required=True)
    e=sub.add_parser('export');e.add_argument('--manifest',required=True);e.add_argument('--output',required=True);e.add_argument('--licenses',required=True)
    c=sub.add_parser('verify-capture');c.add_argument('--directory',required=True);c.add_argument('--capture',required=True)
    a=p.parse_args()
    if a.mode=='export':r=export(a.manifest,a.output,a.licenses,repository=os.environ['GITHUB_REPOSITORY'],run_id=int(os.environ['GITHUB_RUN_ID']),commit=os.environ['GITHUB_SHA'])
    else:r=verify_capture_binding(a.directory,a.capture)
    print('SOURCE_ON_CODE_TRANSFER '+json.dumps(r,sort_keys=True),flush=True)
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('code_transfer_failed') from None
