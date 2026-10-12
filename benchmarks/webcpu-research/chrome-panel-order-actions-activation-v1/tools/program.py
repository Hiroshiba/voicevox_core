"""One-host conditional program, activated only by reviewed explicit root GO.

Pure deadline/request/state models remain available without execution authority.
"""
import sys
if __name__ == '__main__' and (not sys.flags.isolated or not sys.dont_write_bytecode):
    raise SystemExit('isolated_no_bytecode_required')

import sys
from pathlib import Path
_SOURCE_ROOT=Path(__file__).resolve().parents[1]
for _name,_module in tuple(sys.modules.items()):
    if _name.split('.')[0] in ('tools','launch','browser'):
        _origin=getattr(_module,'__file__',None)
        _paths=list(getattr(_module,'__path__',()))
        if _origin is not None:
            if not Path(_origin).resolve().is_relative_to(_SOURCE_ROOT):raise RuntimeError('source_package_already_shadowed')
        elif not _paths or any(not Path(path).resolve().is_relative_to(_SOURCE_ROOT) for path in _paths):
            raise RuntimeError('source_package_already_shadowed')
if str(_SOURCE_ROOT) not in sys.path:sys.path.insert(0,str(_SOURCE_ROOT))

import copy
import hashlib
import json
import math
from pathlib import Path

from launch.source_only import require_execution_admission
from tools.build_driver import compare_asset_manifests, inventory_asset_tree, validate_asset_manifest, supervise_command, validate_browser_policy, validate_browser_provisioning_receipt, verify_retained_browser, verify_bootstrap_sources, verify_runtime_helpers, verify_helper_import_routes, configure_verified_helper_imports
from tools.protocol import load_json, expected_schedule, PLAN_SHA256
from tools.transition import verify_qualification, validate_timing, validate_bindings, validate_runtime_identity, same, need, digest

PHASES = (('provision',2700),('compile',600),('qualification',900),('transition',30),('timing',1800),('finalization',60))


class DeadlineLedger:
    def __init__(self, started):
        need(type(started) in (int,float) and math.isfinite(started) and started >= 0,'program_start')
        self.started=started; self.deadline=started+6090; self.position=0; self.active=None; self.records=[]; self.failed=False

    def begin(self, phase, now):
        need(not self.failed and self.active is None and self.position < len(PHASES),'phase_state')
        expected,limit=PHASES[self.position];need(phase == expected,'phase_order')
        need(now >= self.started and now < self.deadline,'whole_program_deadline')
        phase_start=self.started if self.position == 0 else now
        end=min(phase_start+limit,self.deadline);need(now < end,'provisioning_deadline_includes_checkout_prechecks')
        self.active={'phase':phase,'started':phase_start,'deadline':end,'ceiling_seconds':limit}
        return end

    def end(self, phase, now):
        need(self.active is not None and self.active['phase'] == phase,'phase_end_order')
        if not self.active['started'] <= now <= self.active['deadline']:
            self.failed=True
            raise ValueError('phase_deadline_no_borrowing')
        self.active['finished']=now;self.records.append(self.active);self.active=None;self.position+=1

    def rpc_deadline(self, now, seconds):
        need(self.active is not None and 0 < seconds <= 60,'rpc_scope')
        end=min(now+seconds,self.active['deadline']-30,self.deadline-30)
        need(end > now,'rpc_cleanup_reserve');return end


IDENTITY_POLICY_FIELDS={'collection_method','chrome_product','v8_version','accepted_flags','launch_argv_policy','os','architecture'}
HOST_FIELDS={'cpu_vendor','cpu_family','cpu_model','cpu_stepping','isa_sha256','boot_id','job_id'}


def validate_request(request):
    need(type(request) is dict and set(request) == {'schema','issuer','authorization','expected','source_release','budgets','scope','provisioning_contract_sha256','build_input_lock_sha256'},'runtime_request_fields')
    need(request.get('issuer') == 'root' and request['schema'] == 'panel-conditional-request-v1' and request['authorization'] == 'separately_reviewed_hash_bound_actions_conditional_GO','runtime_GO_missing')
    same(request['scope'],{'qualification_calls':330,'conditional_timing_calls':600,'same_job_host':True,'build_attempts':1,'local_browser':False,'retry':False},'runtime_scope')
    same(request['budgets'],dict(PHASES),'runtime_budget_changed')
    expected=request['expected'];need(type(expected) is dict and set(expected) == {'schema','bindings','identity_policy','asset_manifest','browser_acquisition_policy'},'request_expected_fields')
    need(expected['schema'] == 'panel-request-expectations-v1','request_expected_schema')
    need(set(expected['identity_policy']) == IDENTITY_POLICY_FIELDS,'request_must_not_select_host')
    need(set(expected['bindings']) == set(__import__('tools.transition',fromlist=['BINDING_HASHES']).BINDING_HASHES)-{'request_sha256'} | {'source_commit'},'request_binding_fields')
    need(type(request['source_release']) is str and len(request['source_release'])==40 and all(c in '0123456789abcdef' for c in request['source_release']) and request['source_release'] == expected['bindings']['source_commit'],'source_release_changed')
    for key,value in expected['bindings'].items():
        if key!='source_commit':need(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'request_binding_hash')
    validate_asset_manifest(expected['asset_manifest'])
    validate_browser_policy(expected['browser_acquisition_policy'])
    need(expected['browser_acquisition_policy']['bootstrap_helper_lock_sha256'] == expected['bindings']['bootstrap_helper_lock_sha256'],'browser_helper_lock_binding')
    need(expected['browser_acquisition_policy']['chrome_product'] == expected['identity_policy']['chrome_product'] and expected['browser_acquisition_policy']['v8_version'] == expected['identity_policy']['v8_version'],'browser_engine_policy_binding')
    return expected


def bind_allocated_host(template,host,request_sha256,work_root,browser_receipt,browser_installation_root):
    need(type(host) is dict and set(host) == HOST_FIELDS,'host_observation_incomplete')
    need(all(type(v) is str and v and len(v) <= 256 for v in host.values()),'host_observation_malformed')
    browser_identity=validate_browser_provisioning_receipt(browser_receipt,template['browser_acquisition_policy'])
    identity={**copy.deepcopy(template['identity_policy']),**copy.deepcopy(host),**browser_identity}
    argv=identity['launch_argv_policy'];need(type(argv) is list and argv and argv[0] == '<ADMITTED_CHROME_EXECUTABLE>','request_chrome_executable_token')
    executable=Path(browser_installation_root)/browser_receipt['installation']['executable_name']
    need(executable.is_absolute(),'browser_executable_absolute')
    identity['launch_argv_policy'][0]=str(executable)
    validate_runtime_identity(identity)
    bindings={**copy.deepcopy(template['bindings']),'job_id':host['job_id'],'host_id':digest(host),'request_sha256':request_sha256}
    validate_bindings(bindings)
    return {'schema':'panel-stage-expectations-v1','bindings':bindings,'identity':identity,'asset_manifest':copy.deepcopy(template['asset_manifest']),
        'owned_temp_roots':{stage:str(Path(work_root).absolute()/stage/'tmp') for stage in ('qualification','timing')}}


def collect_allocated_host():
    require_execution_admission('program.collect_allocated_host')
    import os
    rows=[]
    for block in Path('/proc/cpuinfo').read_text().strip().split('\n\n'):
        data={k.strip():v.strip() for k,v in (line.split(':',1) for line in block.splitlines() if ':' in line)}
        need(all(k in data for k in ('vendor_id','cpu family','model','stepping','flags')),'cpu_observation_missing')
        rows.append({'cpu_vendor':data['vendor_id'],'cpu_family':data['cpu family'],'cpu_model':data['model'],'cpu_stepping':data['stepping'],'isa_sha256':hashlib.sha256(' '.join(sorted(data['flags'].split())).encode()).hexdigest()})
    need(rows and all(row == rows[0] for row in rows),'heterogeneous_cpu_identity')
    needed=('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB')
    need(all(os.environ.get(k) for k in needed),'actions_job_identity_missing')
    return {**rows[0],'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'job_id':':'.join(os.environ[k] for k in needed)}


def verify_source_release(root, manifest, bindings):
    from launch.source_only import verify_source
    return verify_source(root,manifest,bindings['source_tree_sha256'])


def derive_context(source,work,python):
    """Closed path/recipe derivation; there is no caller-supplied execution context."""
    source=Path(source);work=Path(work);provision=work/'provision'/'provisioned'
    roots={'SOURCE':str(source),'PYTHON':str(python),'SDK':str(provision/'sdk'),
           'DEPS':str(provision/'deps'),'BASELINE':str(provision/'baseline'),'OUT':str(work/'compile'/'built')}
    helper=load_json(source/'launch/bootstrap-helper-lock.json')
    packages={name:str(provision/'deps/python'/(name+'.py' if name=='typing_extensions' else name)) for name in helper['runtime_helper_packages']}
    return {'roots':roots,'provision_root':str(provision),'bootstrap_helper_lock':helper,
        'runtime_helper_package_roots':packages,'build_recipe':load_json(source/'launch/build-recipe.json'),
        'provisioning_plan':load_json(source/'launch/provisioning-contract.json'),
        'bootstrap_inputs':load_json(source/'launch/bootstrap-inputs.json'),
        'phase_environment':{'PATH':'','PYTHONDONTWRITEBYTECODE':'1','LC_ALL':'C.UTF-8','LANG':'C.UTF-8',
            'HOME':str(work/'home'),'TMPDIR':str(work/'tmp'),
            **{k:__import__('os').environ.get(k,'') for k in ('GITHUB_ACTIONS','GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB','GITHUB_SHA','RUNNER_TEMP')}}}


def validate_context_paths(context, work_root, source_root, python_path):
    """Require fresh stage-owned outputs, never preexisting SDK/cache fallback."""
    work=Path(work_root)
    need(work.is_absolute() and work == work.resolve() and work.is_dir(),'fresh_work_path')
    roots=context['roots']
    need(type(roots) is dict and set(roots) == {'SOURCE','PYTHON','SDK','DEPS','BASELINE','OUT'},'context_root_tokens')
    need(Path(roots['SOURCE']) == Path(source_root).resolve(),'context_source_path')
    # Preserve a separately admitted virtualenv interpreter entrypoint. Resolving
    # its executable symlink would silently discard that interpreter's environment.
    need(Path(roots['PYTHON']) == Path(python_path).absolute(),'context_python_path')
    provision=work/'provision'/'provisioned'
    need(Path(context['provision_root']) == provision,'context_provision_path')
    for token,name in (('SDK','sdk'),('DEPS','deps'),('BASELINE','baseline')):
        need(Path(roots[token]) == provision/name,'context_fresh_'+token.lower())
    need(Path(roots['OUT']) == work/'compile'/'built','context_fresh_build_path')
    helpers=context['runtime_helper_package_roots']
    need(type(helpers) is dict and set(helpers) == {'playwright','pyee','greenlet','psutil','typing_extensions'},'context_helper_roots')
    for name in helpers:
        path=Path(helpers[name])
        need(path.is_absolute() and path == path.resolve() and path.is_relative_to(provision/'deps'),'context_fresh_helper_path')
    for path in (work,*work.parents):need(not path.is_symlink(),'context_ancestor_symlink')
    return {'provision_root':str(provision),'compile_root':str(work/'compile'/'built')}


def observe_host_tools():
    require_execution_admission('program.observe_host_tools')
    import platform
    need(platform.system()=='Linux' and platform.machine()=='x86_64','host_platform_policy')
    osrelease=Path('/etc/os-release').read_text()
    need('ID=ubuntu' in osrelease and 'VERSION_ID="24.04"' in osrelease,'host_os_policy')
    path=Path('/usr/bin/make');need(path.is_file() and not path.is_symlink(),'host_make_prerequisite')
    data=path.read_bytes();need(data[:6]==b'\x7fELF\x02\x01' and data[18:20]==b'\x3e\x00','host_make_format')
    return {'make':{'path':str(path),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'identity_scope':'official_ubuntu_host_prerequisite_observed_and_pinned_once'}}


def optional_cache_metadata():
    """Bounded ordinary sysfs metadata, explicitly non-gating and possibly unknown."""
    require_execution_admission('program.optional_cache_metadata')
    rows=[]
    try:
        root=Path('/sys/devices/system/cpu/cpu0/cache')
        paths=sorted(root.glob('index[0-9]*'))[:32]
        for directory in paths:
            row={}
            for key in ('level','type','size','coherency_line_size'):
                value=(directory/key).read_text()[:128].strip()
                row[key]=value if value and all(c.isalnum() or c in ' _-' for c in value) else None
            rows.append(row)
        return {'status':'observed' if rows else 'unknown','source':'ordinary_cpu0_cache_sysfs','gating':False,'rows':rows}
    except (OSError,ValueError):
        return {'status':'unknown','source':'ordinary_cpu0_cache_sysfs','gating':False,'rows':[]}


def verify_bound_source_facts(template,context):
    source=Path(context['roots']['SOURCE']);bindings=template['bindings']
    for key,name in {'emitted_review_sha256':'protocol/admissions/local-emitted-admission.json',
        'fixture_manifest_sha256':'protocol/qualification-fixtures.json','schedule_sha256':'protocol/schedule.json',
        'workflow_sha256':'workflow/benchmark-chrome-panel-order.yml','launcher_sha256':'tools/launcher_template.py',
        'transition_verifier_sha256':'tools/transition.py'}.items():
        need(hashlib.sha256((source/name).read_bytes()).hexdigest()==bindings[key],'source_binding_'+key)
    need(bindings['toolchain_lock_sha256']==digest(context['bootstrap_inputs']),'source_toolchain_lock_binding')
    need(bindings['plan_sha256']==PLAN_SHA256,'source_plan_binding')
    same(template['identity_policy'],load_json(source/'launch/chrome-launch-argv-policy.json')['identity_policy'],'source_launch_argv_policy_binding')
    same(template['asset_manifest'],load_json(source/'launch/expected-assets.json'),'source_expected_asset_binding')
    same(template['browser_acquisition_policy'],load_json(source/'launch/browser-acquisition-policy.json'),'source_browser_policy_binding')


def phase_command(source, python, stage, context_path, output_path, deadline, ready, gate):
    need(stage in ('provision','compile','qualification','timing'),'phase_child_scope')
    from launch.source_only import child_python_command
    return child_python_command(Path(source)/'launch/phase_process.py',['--stage',stage,'--context',str(context_path),'--output',str(output_path),'--deadline',str(deadline),'--ready',str(ready),'--start-gate',str(gate)])


def _phase(stage, context, deadline, work, journal):
    require_execution_admission('program._phase')
    import time
    from launch.source_only import set_phase
    set_phase(stage)
    phase_root=Path(work)/stage;phase_root.mkdir(exist_ok=stage=='provision')
    context=copy.deepcopy(context)
    if stage in ('qualification','timing'):context['platform_inputs']['owned_temp_root']=context['expected']['owned_temp_roots'][stage]
    context_file=phase_root/'context.private.json';context_file.write_text(json.dumps(context,sort_keys=True))
    ready=phase_root/'phase-ready.json';gate=phase_root/'phase-start.json';output=phase_root/'receipt.private.json'
    cpu=1200 if stage == 'qualification' else 2400 if stage == 'timing' else 480 if stage == 'compile' else 2700
    limits={'bootstrap_helper_lock':context['bootstrap_helper_lock'],'runtime_helper_package_roots':context['runtime_helper_package_roots'],'wall_seconds':dict(PHASES)[stage],'per_process_cpu_seconds':cpu,'observed_tree_cpu_seconds':cpu,'observed_rss_bytes':2*1024**3 if stage == 'compile' else 3*1024**3,'observed_new_artifacts_bytes':768*1024**2 if stage == 'compile' else 8*1024**3 if stage == 'provision' else 256*1024**2,'minimum_available_disk_bytes':4*1024**3,'minimum_host_available_bytes':1024**3,'ready_path':str(ready),'start_gate_path':str(gate),'stage':stage}
    if stage=='provision':
        from launch.provisioning import allowed_monitor_links
        limits['allowed_symlinks']=allowed_monitor_links(context,prefix='provisioned/')
    command=phase_command(context['roots']['SOURCE'],context['roots']['PYTHON'],stage,context_file,output,deadline,ready,gate)
    try:
        note=supervise_command(command,phase_root,context['phase_environment'],deadline,phase_root,limits,journal)
    except BaseException:
        if output.is_file() and not output.is_symlink() and output.stat().st_size<=32*1024**2:
            failed=load_json(output)
            if type(failed) is dict:
                if stage in ('qualification','timing'):
                    failed['status']='failed'
                else:failed['complete']=False
                failed['outer_failure_observation']={k:journal[-1][k] for k in ('acknowledged','first_failure','cleanup','wall_seconds') if journal and k in journal[-1]}
                return failed
        raise
    need(note['acknowledged'] is True and note['cleanup']['confirmed'] is True,'phase_unknown_outcome')
    receipt=load_json(output)
    if stage in ('qualification','timing'):
        if receipt.get('status') != 'complete' or type(receipt.get('resources')) is not dict:
            # Preserve known failed calls even when final observation failed.
            receipt['outer_failure_observation']={k:note[k] for k in ('acknowledged','first_failure','cleanup','wall_seconds') if k in note}
            return receipt
        r=receipt['resources']
        # Combine measured outer-host/owned-phase observations with actual browser
        # operation observations. Never fill an unavailable observation with zero.
        need(note.get('startup_gate_released') is True and note.get('samples'),'outer_phase_observations_missing')
        samples=note['samples'];r['wall_seconds']=note['wall_seconds'];r['tree_cpu_seconds']=note['tree_cpu_seconds']
        r['peak_rss_bytes']=max(r['peak_rss_bytes'],max(s['rss_bytes'] for s in samples))
        r['min_available_bytes']=min(r['min_available_bytes'],min(s['host_available_bytes'] for s in samples))
        r['peak_artifact_bytes']=max(r['peak_artifact_bytes'],max(s['artifact_bytes'] for s in samples))
        r['observation_count']+=len(samples)
        r['outer_supervision']={'startup_gate_released':note['startup_gate_released'],'samples':samples,'cleanup':note['cleanup']}
        need(r['missing_observations'] == 0,'inner_missing_observation')
    if stage in ('provision','compile'):receipt['outer_supervision']=copy.deepcopy(note)
    return receipt


def run(request_path, context, work_root, started_monotonic, source_manifest):
    require_execution_admission('program.run')
    import os
    import time
    request=load_json(request_path);template=validate_request(request)
    validate_context_paths(context,work_root,_SOURCE_ROOT,sys.executable)
    need(Path(context['roots']['SOURCE']).resolve() == _SOURCE_ROOT,'executing_source_root_mismatch')
    verify_source_release(_SOURCE_ROOT,source_manifest,template['bindings'])
    verify_bound_source_facts(template,context)
    host=collect_allocated_host()
    context['host_tools']=observe_host_tools()
    verify_bootstrap_sources(context['bootstrap_helper_lock'],_SOURCE_ROOT,template['bindings']['bootstrap_helper_lock_sha256'])
    context['browser_acquisition_policy']=template['browser_acquisition_policy']
    context['expected_assets']=template['asset_manifest']
    need(os.environ.get('GITHUB_ACTIONS') == 'true','actions_only_no_local_fallback')
    need(':'.join(os.environ.get(k,'') for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB')) == host['job_id'],'job_binding')
    need(digest(context['provisioning_plan']) == request['provisioning_contract_sha256'] and digest(context['bootstrap_inputs']) == request['build_input_lock_sha256'],'context_hash_binding')
    need(digest(context['build_recipe']) == template['bindings']['build_recipe_sha256'],'context_build_recipe_binding')
    work=Path(work_root);need(work.is_dir() and (work/'admission.private.json').is_file(),'one_admitted_attempt');(work/'home').mkdir();(work/'tmp').mkdir()
    ledger=DeadlineLedger(started_monotonic);journal=[];final={'schema':'panel-conditional-program-v1','status':'failed','first_failure':None,'phases':[],'terminal_audit_required':True,'optional_cache_metadata':optional_cache_metadata(),'request_sha256':digest(request),'request_trigger_commit':require_execution_admission('program.receipt_identity').get('event_commit'),'source_bindings':copy.deepcopy(template['bindings']),'phase_supervision':journal}
    export_attempted=False
    try:
        end=ledger.begin('provision',time.monotonic())
        from launch.bootstrap_fetch import bootstrap_psutil
        final['initial_ownership_bootstrap']=bootstrap_psutil(context,min(end,time.monotonic()+150))
        configure_verified_helper_imports(context['bootstrap_helper_lock'],context['runtime_helper_package_roots'],ownership_only=True)
        provision=_phase('provision',context,end,work,journal);final['provisioning']=provision
        need(provision['complete'] is True and type(provision['operator_calls']) is int and provision['operator_calls'] == 0 and type(provision['browser_launches']) is int and provision['browser_launches'] == 0,'provision_failure')
        context['build_input_lock']=provision['portable_build_lock'];context['baseline_members']=provision['baseline_inventory']
        browser_receipt=provision['browser_provisioning_receipt']
        browser_root=Path(context['provision_root'])/'browser'
        verify_retained_browser(browser_receipt,template['browser_acquisition_policy'],browser_root/'browser-distribution.zip',browser_root/'installation')
        configure_verified_helper_imports(context['bootstrap_helper_lock'],context['runtime_helper_package_roots'])
        expected=bind_allocated_host(template,host,digest(request),work_root,browser_receipt,browser_root/'installation')
        context['expected']=expected
        context['platform_inputs']={'job_id':host['job_id'],'chrome_distribution_path':str(browser_root/'browser-distribution.zip'),'browser_installation_root':str(browser_root/'installation'),'browser_installation_manifest':browser_receipt['installation'],'installation_receipt_sha256':digest(browser_receipt)}
        ledger.end('provision',time.monotonic())
        end=ledger.begin('compile',time.monotonic());build=_phase('compile',context,end,work,journal);final['build']=build
        need(build['complete'] is True and type(build['compile_count']) is int and build['compile_count'] == 1,'one_build_only')
        compare_asset_manifests(expected['asset_manifest'],build['asset_manifest'])
        context['assets_dir']=str(Path(context['roots']['OUT'])/'assets')
        inventory_asset_tree(context['assets_dir'],expected['asset_manifest']) # BEFORE even a flag probe.
        ledger.end('compile',time.monotonic())
        context['phase_records']={};context['fixture_identities']={};context['schedule']=expected_schedule()
        end=ledger.begin('qualification',time.monotonic());qual=_phase('qualification',context,end,work,journal);final['qualification']=qual;ledger.end('qualification',time.monotonic())
        end=ledger.begin('transition',time.monotonic())
        transition=verify_qualification(qual,expected)
        verify_retained_browser(browser_receipt,template['browser_acquisition_policy'],browser_root/'browser-distribution.zip',browser_root/'installation')
        retained=inventory_asset_tree(context['assets_dir'],expected['asset_manifest']);same(retained,qual['final_asset_manifest'],'transition_rehash')
        context['phase_records']=transition['carried_phase_records'];context['fixture_identities']=transition['carried_fixture_identities']
        # No compile entry between stages, no alternate host, no user-pause path.
        ledger.end('transition',time.monotonic())
        end=ledger.begin('timing',time.monotonic());timing=_phase('timing',context,end,work,journal);final['timing']=timing
        validation=validate_timing(timing,expected,transition)
        from tools.statistics import extract_primary_samples,analyze_primary_samples
        stats=analyze_primary_samples(extract_primary_samples(timing['calls'],[c for c in expected_schedule() if c['stage']=='timing']))
        ledger.end('timing',time.monotonic())
        end=ledger.begin('finalization',time.monotonic())
        # Finalization performs no Chrome, compiler, operator, network or retry.
        final.update(status='completed_pending_independent_terminal_audit',qualification=qual,transition=transition,timing=timing,timing_validation=validation,statistics=stats)
        final['phases']=ledger.records+[copy.deepcopy(ledger.active)]
        final['source_release']=request['source_release']
        final['publication_status']='requires_terminal_finalization_and_independent_audit'
        write_public_receipt(work/'program-terminal.json',final)
        ledger.end('finalization',time.monotonic())
        ledger.records[-1]['measurement_scope']='private_terminal_serialization_checkpoint'
        closure={'schema':'panel-finalization-scalar-v1','terminal_receipt_sha256':hashlib.sha256((work/'program-terminal.json').read_bytes()).hexdigest(),'finalization':ledger.records[-1],'status':'complete_pending_independent_audit'}
        write_public_receipt(work/'finalization-scalar.json',closure)
        need(sum(p.stat().st_size for p in (work/'program-terminal.json',work/'finalization-scalar.json')) <= 32*1024**2,'aggregate_public_scalar_budget')
        from tools.scalar_export import export_scalar_artifact
        final['export_deadline_monotonic']=end
        final['phases']=ledger.records
        export_attempted=True
        export_scalar_artifact(work,final,source_bindings=template['bindings'])
        need(time.monotonic() <= end,'finalization_receipt_deadline')
    except BaseException as error:
        revoke_public_export(work)
        final['status']='failed'
        initial_receipt=Path(context['provision_root'])/'bootstrap-psutil-receipt.json'
        if 'initial_ownership_bootstrap' not in final and initial_receipt.is_file() and not initial_receipt.is_symlink() and initial_receipt.stat().st_size<=128*1024:
            final['initial_ownership_bootstrap']=load_json(initial_receipt)
        check=str(error)
        final['first_failure']={'type':type(error).__name__,'phase':ledger.active['phase'] if ledger.active else ledger.records[-1]['phase'] if ledger.records else 'preflight','check':check if check and len(check)<=100 and all(c.islower() or c.isdigit() or c=='_' for c in check) else 'program_gate_failure'}
        final['attempted_phases']=[{k:row[k] for k in ('attempted','acknowledged','returncode','first_failure','cleanup') if k in row} for row in journal]
        if (work/'program-terminal.json').exists():
            write_public_receipt(work/'finalization-failure.json',{'schema':'panel-finalization-failure-v1','first_failure':final['first_failure'],'status':'invalid_no_result_admission'})
        raise
    finally:
        final['phases']=ledger.records;final['source_release']=request['source_release']
        # The fail-closed receipt itself is bounded and contains no logs/tensors.
        if not (work/'program-terminal.json').exists():
            try:
                write_public_receipt(work/'program-terminal.json',final)
            except ValueError:
                # Complete private phase journals remain retained for audit.
                write_public_receipt(work/'program-failure-scalar.json',{'schema':'panel-terminal-failure-v1','status':'invalid_no_result_admission','first_failure':final.get('first_failure'),'check':'public_scalar_budget_or_finalization_failure','known_phase_names':sorted(k for k in ('provisioning','build','qualification','timing') if k in final)})
        if not export_attempted and not (work/'public-scalars').exists():
            from tools.scalar_export import export_scalar_artifact
            final['export_deadline_monotonic']=min(ledger.deadline,time.monotonic()+60)
            export_scalar_artifact(work,final,source_bindings=template['bindings'])
    return final


def revoke_public_export(work):
    """Late failure revokes the owned projection atomically; no stale upload."""
    directory=Path(work)/'public-scalars'
    if directory.exists() or directory.is_symlink():
        need(directory.is_dir() and not directory.is_symlink(),'export_revoke_owned_directory')
        target=Path(work)/'.public-scalars.revoked'
        need(not target.exists() and not target.is_symlink(),'export_revoke_no_overwrite')
        directory.rename(target)


def write_public_receipt(path, receipt):
    payload=json.dumps(receipt,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    need(len(payload) <= 32*1024**2,'public_scalar_budget')
    p=Path(path);need(not p.exists() and not p.is_symlink(),'receipt_no_overwrite')
    names=('program-terminal.json','finalization-scalar.json','finalization-failure.json','program-failure-scalar.json')
    need(p.name in names,'public_scalar_allowlist')
    existing=sum((p.parent/name).stat().st_size for name in names if (p.parent/name).is_file())
    reserve=4096 if p.name=='program-terminal.json' else 0
    need(existing+len(payload)+reserve<=32*1024**2,'aggregate_public_scalar_budget')
    with p.open('xb') as stream:stream.write(payload)


def main(argv=None):
    # Parsing explicit receipt paths imports no optional/runtime helper modules.
    import argparse
    from launch.source_only import open_root_admission
    parser=argparse.ArgumentParser();parser.add_argument('--actions',action='store_true',required=True)
    for name in ('request','source-review','source-manifest','work','clock'):parser.add_argument('--'+name,required=True)
    a=parser.parse_args(argv)
    request,manifest,clock=open_root_admission(a.request,a.source_review,a.source_manifest,a.work,a.clock)
    require_execution_admission('program.main')
    context=derive_context(_SOURCE_ROOT,Path(a.work),sys.executable)
    return run(a.request,context,a.work,clock['started'],manifest)

if __name__ == '__main__':main()
