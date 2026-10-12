"""Explicit reviewed root-GO admission. Importing this module grants nothing.

No environment variable or inherited Actions marker is authority. Every new
Python ownership child revalidates the source, root receipt and source review.
"""
import hashlib
import json
from pathlib import Path
import sys

_STATE = None
CHILD_TARGETS = frozenset(('launch/phase_process.py','launch/compiler_process.py',
    'launch/browser_acquisition.py','launch/provisioning.py','tools/process_guard.py',
    'browser/vendor/operator_browser.py'))

class SourceOnlyBlocked(RuntimeError):
    pass

def _need(ok, reason):
    if not ok: raise SourceOnlyBlocked(reason)

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _read(path):
    p=Path(path)
    _need(p.is_absolute() and not p.is_symlink() and p.is_file(),'admission_absolute_regular_file')
    _need(p.stat().st_size <= 8*1024**2,'admission_file_budget')
    def closed_pairs(items):
        out={}
        for key,value in items:
            _need(key not in out,'admission_duplicate_json_key');out[key]=value
        return out
    def nonfinite(value):raise SourceOnlyBlocked('admission_nonfinite_json')
    return json.loads(p.read_text(),object_pairs_hook=closed_pairs,parse_constant=nonfinite)

def verify_source(root, manifest, expected):
    root=Path(root).resolve()
    _need(manifest.get('schema') == 'chrome-panel-source-seal-v1','admission_source_manifest_schema')
    rows=manifest.get('files')
    _need(type(rows) is dict and rows and _digest(rows) == expected == manifest.get('source_tree_canonical_sha256'),'admission_source_manifest_binding')
    for name,row in rows.items():
        p=Path(name)
        _need(not p.is_absolute() and p.as_posix() == name and '..' not in p.parts and '\\' not in name,'admission_source_name')
        _need(type(row) is dict and set(row)=={'bytes','sha256'} and type(row['bytes']) is int,'admission_source_row')
        target=root/p
        _need(target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(root),'admission_source_missing')
        _need(all(not parent.is_symlink() for parent in target.parents if parent != root),'admission_source_redirect')
        data=target.read_bytes()
        _need(len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256'],'admission_source_byte_drift')
    for folder in ('src','harness','browser','tools','protocol','launch','workflow','tests'):
        for p in (root/folder).rglob('*'):
            _need(not p.is_symlink(),'admission_source_symlink')
            if p.is_file():_need(p.relative_to(root).as_posix() in rows,'admission_source_unlisted')
    for p in root.glob('*.py'):_need(p.name in rows,'admission_root_entry_unlisted')
    return expected

def validate_documents(request, review, manifest, source_root, review_sha256):
    _need(type(request) is dict and set(request)=={'schema','issuer','authorization','expected','source_release','budgets','scope','provisioning_contract_sha256','build_input_lock_sha256'},'root_request_fields')
    _need(_digest(request['scope'])==_digest({'qualification_calls':330,'conditional_timing_calls':600,'same_job_host':True,'build_attempts':1,'local_browser':False,'retry':False}),'root_scope_changed')
    _need(_digest(request['budgets'])==_digest({'provision':2700,'compile':600,'qualification':900,'transition':30,'timing':1800,'finalization':60}),'root_budgets_changed')
    _need(request.get('schema') == 'panel-conditional-request-v1' and request.get('issuer') == 'root','explicit_root_GO_required')
    _need(request.get('authorization') == 'separately_reviewed_hash_bound_actions_conditional_GO','explicit_root_GO_required')
    bindings=request['expected']['bindings']
    _need(review_sha256 == bindings['source_review_sha256'],'source_review_file_binding')
    _need(set(review)=={'schema','status','independent','source_tree_sha256','compiled_assets_review_sha256','host_manifest_sha256','reviewer','scope'},'source_review_fields')
    _need(review['schema']=='panel-actions-source-review-v1' and review['status']=='PASS' and review['independent'] is True and review['scope']=='one_same_job_330_then_conditional_600','source_review_not_admitted')
    _need(type(review['reviewer']) is str and bool(review['reviewer']),'source_reviewer_missing')
    _need(review['source_tree_sha256']==bindings['source_tree_sha256'] and review['compiled_assets_review_sha256']==bindings['emitted_review_sha256'],'source_review_scope_binding')
    verify_source(source_root,manifest,bindings['source_tree_sha256'])
    host=Path(source_root)/'browser/host-manifest.json'
    _need(hashlib.sha256(host.read_bytes()).hexdigest()==review['host_manifest_sha256'],'reviewed_host_manifest_binding')
    return bindings

def _load_authority(request_path,review_path,manifest_path,source_root):
    request=_read(request_path);review=_read(review_path);manifest=_read(manifest_path)
    validate_documents(request,review,manifest,source_root,hashlib.sha256(Path(review_path).read_bytes()).hexdigest())
    return request,review,manifest

def open_root_admission(request_path,review_path,manifest_path,work_root,clock_path):
    """Called only by explicit program CLI; validates before creating one work root."""
    global _STATE
    import os
    import time
    _need(_STATE is None,'admission_already_open')
    _need(sys.flags.isolated == 1 and sys.dont_write_bytecode,'isolated_no_bytecode_interpreter_required')
    root=Path(__file__).resolve().parents[1]
    request,review,manifest=_load_authority(request_path,review_path,manifest_path,root)
    clock=_read(clock_path)
    _need(set(clock)=={'started','program_wall_seconds'} and type(clock['started']) in (int,float) and type(clock['program_wall_seconds']) is int and clock['program_wall_seconds']==6090,'admission_clock')
    _need(0 <= time.monotonic()-clock['started'] < 2700,'admission_provision_clock_expired')
    _need(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_RUN_ATTEMPT')=='1','one_first_attempt_actions_job_required')
    event_commit=os.environ.get('GITHUB_SHA','')
    _need(len(event_commit)==40 and all(c in '0123456789abcdef' for c in event_commit),'admission_event_commit')
    import sysconfig
    _need(sys.version_info[:2]==(3,12) and sys.implementation.name=='cpython' and sysconfig.get_config_var('SOABI')=='cpython-312-x86_64-linux-gnu','admitted_python_312_x86_64_abi_required')
    job=':'.join(os.environ.get(k,'') for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB'))
    _need(all(job.split(':')),'admission_job_missing')
    work=Path(work_root)
    runner_temp=Path(os.environ.get('RUNNER_TEMP',''))
    _need(Path(clock_path)==runner_temp/'panel-program-clock.json','fixed_first_step_clock_required')
    _need(runner_temp.is_absolute() and work==runner_temp/'panel-order-screen-attempt','fixed_single_job_attempt_root')
    _need(work.is_absolute() and work==work.resolve() and not work.exists(),'admission_fresh_work_required')
    _need(not any(p.is_symlink() for p in (work,*work.parents)),'admission_work_redirect')
    state={'schema':'panel-actions-child-admission-v1','request_path':str(Path(request_path)),
        'review_path':str(Path(review_path)),'manifest_path':str(Path(manifest_path)),
        'source_root':str(root),'work_root':str(work),'clock_path':str(Path(clock_path)),'clock_sha256':hashlib.sha256(Path(clock_path).read_bytes()).hexdigest(),
        'job_id':job,'event_commit':event_commit,'parent_pid':os.getpid(),'parent_start_ticks':_process_start_ticks(os.getpid()),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'request_sha256':_digest(request),'source_tree_sha256':manifest['source_tree_canonical_sha256']}
    work.mkdir(parents=True)
    (work/'child-leases').mkdir()
    descriptor=work/'admission.private.json'
    with descriptor.open('x') as stream:json.dump(state,stream,sort_keys=True,separators=(',',':'))
    _STATE={**state,'descriptor_path':str(descriptor),'descriptor_sha256':hashlib.sha256(descriptor.read_bytes()).hexdigest(),'phase':'provision'}
    return request,manifest,clock

def child_admission(descriptor_path):
    global _STATE
    import os
    import time
    _need(_STATE is None and sys.flags.isolated==1 and sys.dont_write_bytecode,'child_isolated_unadmitted_required')
    state=_read(descriptor_path)
    _need(set(state)=={'schema','request_path','review_path','manifest_path','source_root','work_root','clock_path','clock_sha256','job_id','event_commit','parent_pid','parent_start_ticks','boot_id','request_sha256','source_tree_sha256'},'child_descriptor_fields')
    _need(state['schema']=='panel-actions-child-admission-v1' and Path(state['source_root'])==Path(__file__).resolve().parents[1],'child_source_root')
    _need(Path(descriptor_path)==Path(state['work_root'])/'admission.private.json','child_descriptor_location')
    _need(state['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip() and _process_start_ticks(state['parent_pid'])==state['parent_start_ticks'],'child_root_generation_changed')
    request,review,manifest=_load_authority(state['request_path'],state['review_path'],state['manifest_path'],state['source_root'])
    _need(_digest(request)==state['request_sha256'] and manifest['source_tree_canonical_sha256']==state['source_tree_sha256'],'child_authority_changed')
    job=':'.join(os.environ.get(k,'') for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB'))
    _need(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_RUN_ATTEMPT')=='1' and job==state['job_id'] and os.environ.get('GITHUB_SHA')==state['event_commit'],'child_job_changed')
    _need(hashlib.sha256(Path(state['clock_path']).read_bytes()).hexdigest()==state['clock_sha256'],'child_clock_changed')
    clock=_read(state['clock_path']);_need(0 <= time.monotonic()-clock['started'] <6090,'child_whole_program_deadline')
    _STATE={**state,'descriptor_path':str(descriptor_path),'descriptor_sha256':hashlib.sha256(Path(descriptor_path).read_bytes()).hexdigest(),'phase':'unleased'}
    return dict(_STATE)

def require_execution_admission(entrypoint):
    if _STATE is None:raise SourceOnlyBlocked('SOURCE_ONLY_EXECUTION_BLOCK: '+str(entrypoint))
    _need(hashlib.sha256(Path(_STATE['descriptor_path']).read_bytes()).hexdigest()==_STATE['descriptor_sha256'],'admission_descriptor_changed')
    return dict(_STATE)

def child_python_command(script,args):
    state=require_execution_admission('launch.child_python_command')
    target=Path(script).absolute();root=Path(state['source_root'])
    _need(target.is_relative_to(root) and target.relative_to(root).as_posix() in CHILD_TARGETS,'child_target_unapproved')
    _need(type(args) in (list,tuple) and all(type(x) is str for x in args),'child_arguments')
    import secrets
    phase=state['phase']
    _need(phase in ('provision','compile','qualification','timing'),'child_phase_unadmitted')
    relative=target.relative_to(root).as_posix()
    _need(relative not in ('launch/compiler_process.py',) or phase=='compile','compiler_outside_build')
    _need(relative not in ('launch/browser_acquisition.py','launch/provisioning.py') or phase=='provision','acquisition_outside_provision')
    _need(relative!='browser/vendor/operator_browser.py' or phase in ('qualification','timing'),'browser_outside_measured_phase')
    lease={'schema':'panel-child-lease-v1','descriptor_sha256':state['descriptor_sha256'],'script':str(target),'args':list(args),'phase':phase}
    lease_path=Path(state['work_root'])/'child-leases'/(secrets.token_hex(16)+'.json')
    with lease_path.open('x') as stream:json.dump(lease,stream,sort_keys=True,separators=(',',':'))
    return [sys.executable,'-B','-I',str(root/'launch/child_entry.py'),'--admission',state['descriptor_path'],'--lease',str(lease_path),'--script',str(target),'--',*args]

def validate_child_lease(state,lease,script,args):
    _need(lease.get('phase') in ('provision','compile','qualification','timing'),'child_lease_phase')
    _need(lease=={'schema':'panel-child-lease-v1','descriptor_sha256':state['descriptor_sha256'],'script':str(script),'args':list(args),'phase':lease['phase']},'child_lease_binding')
    return lease['phase']

def consume_child_lease(state,lease_path,script,args):
    p=Path(lease_path)
    _need(p.parent==Path(state['work_root'])/'child-leases' and len(p.stem)==32,'child_lease_location')
    lease=_read(p)
    phase=validate_child_lease(state,lease,script,args)
    _STATE['phase']=phase
    with p.with_suffix('.consumed').open('x') as stream:stream.write(_digest(lease))
    return True


def _process_start_ticks(pid):
    _need(type(pid) is int and pid>0,'admission_parent_pid')
    raw=Path('/proc',str(pid),'stat').read_text();tail=raw[raw.rfind(')')+2:].split()
    _need(len(tail)>19 and tail[19].isdigit(),'admission_parent_generation')
    return int(tail[19])

def set_phase(phase):
    state=require_execution_admission('launch.set_phase')
    order=('provision','compile','qualification','timing')
    _need(phase in order and state['phase'] in order and order.index(phase) in (order.index(state['phase']),order.index(state['phase'])+1),'admission_phase_order')
    _STATE['phase']=phase


def main(*args,**kwargs):
    raise SourceOnlyBlocked('SOURCE_ONLY_EXECUTION_BLOCK: launch.source_only.direct')

if __name__=='__main__':main()
