"""Closed, bounded public projection of private scalar receipts. Pure Python.

No runtime/admission entrypoint. This serializer never opens a process, network,
native helper, tensor or binary. Public evidence is always unreviewed; projection
cannot authorize performance claims or advancement. Private receipts are retained.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import re
import time

LIMIT = 32 * 1024**2
HASH = re.compile(r'[0-9a-f]{64}')
TOKEN = re.compile(r'[A-Za-z0-9_.:+-]{1,160}')
ASSET = re.compile(r'(?:[RABQP]/screen\.(?:js|wasm)|controller\.js|worker\.js|index\.html)')
KEYS = frozenset('''accepted accepted_flags acknowledged actual_selector actual_zero_sentinel admission after after_bytes all_thread_children_verified architecture arm arm_position artifact_bytes asset_manifest assets at_monotonic attempted available_bytes backing_termination_requested backing_workers before before_bytes bindings bootstrap_helper_lock_sha256 boundaries browser_installation_receipt_sha256 buffers_erased build_recipe_sha256 bytes calibration call call_index callbacks caller_regions calls canaries checked checks chrome_distribution_sha256 chrome_executable_sha256 chrome_product chrome_support_tree_sha256 cleanup cleanup_failure collection_method command commands compared_bytes compared_words comparison_words compile_count completed confirmed coverage cpu_family cpu_model cpu_seconds cpu_stepping cpu_vendor descriptor designated differences dilation dilation_position dispatch_ms distinct_backing_verified duration_ms elapsed_ms emitted_review_sha256 empty_export_p99_ms empty_export_samples enabled evidence exact_disjoint exact_uint32 expectation_code failure final_asset_manifest finite_required first_failure fixture_expectations fixture_id fixture_identities fixture_manifest_sha256 fixture_sha256 flag_probe full_class_words generation_matching generations geometry hardware_concurrency heap_after_bytes heap_before_bytes heap_bytes hidden_allocator_padding_covered host host_available_bytes host_swap_in_delta_bytes host_swap_out_delta_bytes identity identity_sha256 indirection indirection_words initializations isa_sha256 label launch_argv_sha256 launcher_sha256 library_deinitialized live_descriptor logical_names major_faults min_available_bytes minimum_alignment minimum_primary_ms missing_observations mod4096 mod64 modules modulo_uint32_offset monotonic_seconds name native_result observation_count operator_calls operator_runs os outer_controller_excluded outer_supervision output output_sha256 ownership_method p99_resolution_censored packed packed_sha256 participants pass_index passed peak_artifact_bytes peak_rss_bytes phase phase_records phases plan_sha256 pool_destroy_returned pool_participants positive_samples prefix_bytes prepare process_cleanup_required processes public_receipt_bytes quantum_ms readonly reads ready receipt_ms reference reference_capture reference_words region regions remaining_processes request_sha256 resource_windows resources retained returncode role root root_receipts rss_bytes run_ack runtime_artifact_bytes sampled_not_kernel_aggregate_quota samples schedule_sha256 schema selector sequence sha256 shutdown source_commit source_review_sha256 source_tree_sha256 stack stage stage_call_index startup_gate_released status subtracted suffix_bytes swap_bytes swap_in_bytes swap_out_bytes target_swap_growth_bytes timed_out timing_calls toolchain_lock_sha256 transition_verifier_sha256 tree_cpu_seconds unknown_generations unresolved_owners v8_version wall_seconds witnesses words worker_failure workflow_sha256 known_calls no_retry cleanup_unconfirmed terminal_resource_observation_missing outer_failure_observation type check complete compile_count started finished deadline ceiling_seconds source_release level size_bytes line_bytes gating rows source'''.split())
KEYS = KEYS | {'optional_cache_metadata', 'size', 'coherency_line_size', 'browser_launches'}
KEYS = KEYS | {'measurement_scope'}
NUMERIC_KEYS = frozenset('''after_bytes arm_position artifact_bytes at_monotonic available_bytes backing_workers before_bytes bytes call_index callbacks caller_regions checked compared_bytes compared_words comparison_words compile_count cpu_seconds designated differences dilation dilation_position dispatch_ms duration_ms elapsed_ms empty_export_p99_ms empty_export_samples expectation_code fixture_id full_class_words hardware_concurrency heap_after_bytes heap_before_bytes heap_bytes host_available_bytes host_swap_in_delta_bytes host_swap_out_delta_bytes indirection_words major_faults min_available_bytes minimum_alignment minimum_primary_ms missing_observations mod4096 mod64 monotonic_seconds native_result observation_count operator_calls operator_runs pass_index peak_artifact_bytes peak_rss_bytes pool_participants positive_samples prefix_bytes public_receipt_bytes quantum_ms reads receipt_ms reference_words region remaining_processes returncode root rss_bytes runtime_artifact_bytes sequence stage_call_index suffix_bytes swap_bytes swap_in_bytes swap_out_bytes target_swap_growth_bytes timing_calls tree_cpu_seconds wall_seconds words'''.split())
IDENTIFIERS = {'boot_id', 'job_id', 'host_id'}
SPECIAL = IDENTIFIERS | {'actual_launch_argv', 'launch_argv_policy'}
MAP_KEYS = {'processes', 'generations', 'phase_records', 'fixture_identities'}
OBJECT_KEYS = {'output', 'packed', 'readonly', 'geometry', 'canaries', 'stack', 'witnesses', 'coverage', 'run_ack', 'checks', 'evidence'}
DICT_KEYS = OBJECT_KEYS | {'bindings','identity','asset_manifest','final_asset_manifest','call','cleanup','resources','flag_probe','first_failure','phase_records','fixture_identities','calibration','outer_supervision','shutdown','before','after','host','processes','generations','outer_failure_observation','optional_cache_metadata'}
LIST_MAX = {'accepted_flags':1,'assets':13,'logical_names':13,'calls':600,'commands':4,'resource_windows':4,
    'regions':7,'phases':7,'participants':2,'initializations':5,'root_receipts':10,'modules':5,
    'samples':100_000,'boundaries':24,'rows':32,'descriptor':13,'fixture_sha256':3}
STAGE_KEYS = {'schema','status','stage','bindings','identity','asset_manifest','final_asset_manifest','compile_count','phase_records','fixture_identities','calls','root_receipts','flag_probe','resources','first_failure','timing_calls','outer_failure_observation'}
CONTEXT_SCHEMA=json.loads((Path(__file__).resolve().parents[1]/'protocol/public-scalar-schema.json').read_text())
CONTEXT_FIELDS=CONTEXT_SCHEMA['object_fields']
CONTEXT_TYPES=CONTEXT_SCHEMA['field_types']

class ProjectionError(ValueError): pass

def _bytes(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

def _hash(value): return hashlib.sha256(_bytes(value)).hexdigest()

def _project(value, key='', depth=0, budget=None):
    budget = [2_000_000] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 24: raise ProjectionError('scalar_structure_budget')
    if key in IDENTIFIERS:
        if type(value) is not str: raise ProjectionError('identifier_type')
        return 'sha256:' + _hash(value)
    if key in ('worker_failure','failure','cleanup_failure') and value is not None:
        return {'present': True, 'private_value_sha256': _hash(value)}
    if key in ('unresolved_owners','unknown_generations'):
        if type(value) is not list: raise ProjectionError('owner_list_type')
        return ['sha256:' + _hash(row) for row in value]
    if value is None: return value
    if key in NUMERIC_KEYS and type(value) not in (int,float):raise ProjectionError('declared_numeric_scalar_type')
    if key in OBJECT_KEYS and type(value) is not dict: raise ProjectionError('scalar_object_type')
    if key=='check_boolean' and type(value) is not bool:raise ProjectionError('declared_boolean_type')
    if type(value) is bool: return value
    if key.endswith('_sha256') and key != 'fixture_sha256' and (type(value) is not str or not HASH.fullmatch(value)):
        raise ProjectionError('scalar_hash_type')
    if key in OBJECT_KEYS and type(value) is not dict: raise ProjectionError('scalar_object_type')
    if type(value) is int:
        if abs(value) > 2**64: raise ProjectionError('scalar_integer_range')
        return value
    if type(value) is float:
        if not math.isfinite(value): raise ProjectionError('nonfinite_scalar')
        return value
    if type(value) is str:
        if TOKEN.fullmatch(value) or ASSET.fullmatch(value) or value == 'HeadlessChrome/153.0.8010.12' or value == '--js-flags=--no-wasm-revectorize': return value
        raise ProjectionError('non_scalar_or_private_string')
    if type(value) is list:
        limit=7 if key=='phase_records.*' else LIST_MAX.get(key)
        if limit is None or len(value)>limit: raise ProjectionError('unlisted_or_oversize_vector')
        if key == 'descriptor':
            if len(value)!=13 or any(type(v) is not int for v in value) or value[0] not in (1,3,5) or value[8] not in (4,32) or value[:1]+value[1:8]+[4]+value[9:] != [value[0],61568,128,4,8,11,512,176,4,128,2,61568,128]:
                raise ProjectionError('descriptor_extent_or_values')
        elif key == 'fixture_sha256':
            if len(value)!=3 or any(type(v) is not str or not HASH.fullmatch(v) for v in value):raise ProjectionError('fixture_hash_vector')
        elif key in ('accepted_flags','logical_names'):
            if any(type(v) is not str for v in value):raise ProjectionError('string_vector_type')
        elif any(type(v) is not dict for v in value):raise ProjectionError('record_vector_type')
        if key=='participants' and any(set(v)!={'label','identity_sha256'} or type(v['label']) is not int or v['label'] not in (0,1) for v in value):
            raise ProjectionError('participant_scalar_schema')
        return [_project(v, key+'[]', depth+1, budget) for v in value]
    if type(value) is dict:
        if key not in MAP_KEYS:
            allowed=CONTEXT_FIELDS.get(key)
            if allowed is None or set(value)-set(allowed):raise ProjectionError('context_scalar_field_roster')
        out = {}
        for name, item in value.items():
            if type(name) is not str: raise ProjectionError('scalar_key_type')
            if item is not None and key not in MAP_KEYS:
                allowed_types=CONTEXT_TYPES.get(key,{}).get(name)
                if allowed_types is None or type(item).__name__ not in allowed_types:raise ProjectionError('context_scalar_field_type')
            dynamic = ((key in ('processes','generations') and HASH.fullmatch(name)) or
                       (key=='phase_records' and name in ('1','3','5')) or
                       (key=='fixture_identities' and re.fullmatch(r'[135]:(?:[0-9]|1[01])',name)))
            if key in MAP_KEYS and not dynamic:raise ProjectionError('dynamic_map_key')
            if name not in KEYS | SPECIAL and not dynamic: raise ProjectionError('unlisted_scalar_field')
            if name == 'actual_launch_argv':
                if type(item) is not list or any(type(v) is not str for v in item) or _hash(item) != value.get('launch_argv_sha256'):
                    raise ProjectionError('private_argv_digest')
                out['runtime_argv_omitted'] = True
            elif name == 'launch_argv_policy':
                if type(item) is not list or any(type(v) is not str for v in item): raise ProjectionError('launch_policy_type')
                out['launch_policy_sha256'] = _hash(item)
            else:
                role=key+'.*' if dynamic else name
                if key=='checks' or (key=='call' and name=='coverage'):role='check_boolean'
                out[name] = _project(item,role,depth+1,budget)
        return out
    raise ProjectionError('non_scalar_type')


def _safe(value, issues, label, role=''):
    try: return _project(value,role)
    except (ProjectionError, ValueError, TypeError):
        issues.append(label)
        try: sha = _hash(value)
        except (ValueError, TypeError): sha = None
        return {'projection_status':'unexportable_scalar_field','private_value_sha256':sha}


def _ownership(row, issues, label):
    if not isinstance(row,dict):issues.append(label);return {'projection_status':'invalid_ownership_note'}
    result={'private_note_sha256':_hash(row)}
    for key in ('attempted','acknowledged','returncode','complete','wall_seconds','tree_cpu_seconds','peak_rss_bytes','peak_artifact_bytes','startup_gate_released','stage','command_id','requires_outer_cleanup_acknowledgement','direct_child_started'):
        if key in row:result[key]=_safe(row[key],issues,label+'.'+key,key)
    for key in ('id','kind','ownership'):
        if key in row:
            if type(row[key]) is str and TOKEN.fullmatch(row[key]):result[key]=row[key]
            else:issues.append(label+'.'+key)
    if 'cleanup' in row:result['cleanup']=_safe({'cleanup':row['cleanup']},issues,label+'.cleanup').get('cleanup')
    if row.get('first_failure') is not None:result['private_first_failure_sha256']=_hash(row['first_failure'])
    if 'min_available_disk_bytes' in row:
        value=row['min_available_disk_bytes']
        if value is None or type(value) is int and value>=0:result['min_available_disk_bytes']=value
        else:issues.append(label+'.disk')
    samples=row.get('samples')
    result['sample_count']=len(samples) if isinstance(samples,list) else None
    result['resource_scope']='direct_sampled_owner' if isinstance(samples,list) else 'outer_phase_only_or_unobserved'
    if isinstance(samples,list):
        if len(samples)>60000:issues.append(label+'.samples');return result
        extrema={}
        for key in ('rss_bytes','artifact_bytes','host_available_bytes','tree_cpu_seconds','generations','target_swap_growth_bytes','host_swap_in_delta_bytes','host_swap_out_delta_bytes'):
            values=[sample.get(key) for sample in samples if isinstance(sample,dict)]
            if len(values)!=len(samples) or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
                issues.append(label+'.'+key);extrema[key]=None
            else:extrema[key]={'minimum':min(values),'maximum':max(values)} if values else None
        result['sample_extrema']=extrema
    return result


def _browser_acquisition(value, issues):
    """Bounded complete acquisition/install evidence, with opaque ETags hashed."""
    try:
        from tools.build_driver import validate_browser_provisioning_receipt
        policy=json.loads((Path(__file__).resolve().parents[1]/'launch/browser-acquisition-policy.json').read_text())
        validate_browser_provisioning_receipt(value,policy)
        if len(_bytes(value))>65536:raise ProjectionError('acquisition_receipt_budget')
        public=json.loads(json.dumps(value))
        for field in ('head','get'):
            tag=public[field].pop('etag')
            public[field]['etag_sha256']=_hash(tag)
        public['private_receipt_canonical_sha256']=_hash(value)
        public['projection_note']='complete_acquisition_install_metadata_except_opaque_ETag_values_are_hashed'
        return public
    except (ValueError,TypeError,KeyError):
        issues.append('browser_acquisition')
        return {'projection_status':'invalid_or_incomplete_acquisition_receipt','private_receipt_sha256':_hash(value)}


def _stage(value, stage, work, issues):
    if value is None: return None
    if type(value) is not dict or set(value) - STAGE_KEYS:
        issues.append(stage+'.unlisted_stage_fields')
        return {'projection_status':'invalid_stage_receipt','private_stage_sha256':_hash(value)}
    out = {}
    for key,item in value.items():
        if key == 'calls':
            if type(item) is not list or len(item) > (330 if stage == 'qualification' else 600):
                issues.append(stage+'.calls');out[key]={'projection_status':'invalid_call_roster'};continue
            # Preserve every attempted position, including failed/unknown records.
            out[key] = [_safe(row,issues,stage+'.call.'+str(i),'calls[]') for i,row in enumerate(item)]
        else:
            out[key] = _safe({key:item},issues,stage+'.'+key).get(key)
    checks = []
    from tools.transition import validate_launch_argv
    for label, row in [('flag_probe',value.get('flag_probe'))] + [(str(i),r) for i,r in enumerate(value.get('root_receipts') or [])]:
        if not isinstance(row,dict) or 'actual_launch_argv' not in row: continue
        try:
            identity=row.get('identity') or value.get('identity')
            result=validate_launch_argv(row['actual_launch_argv'],identity['launch_argv_policy'],str(work/stage/'tmp'))
            valid=result == row.get('launch_argv_sha256')
        except (ValueError,KeyError,TypeError): valid=False
        checks.append({'root_or_probe':label,'launch_policy_validated_before_omission':valid,
                       'private_argv_sha256':row.get('launch_argv_sha256') if HASH.fullmatch(str(row.get('launch_argv_sha256'))) else None})
        if not valid: issues.append(stage+'.launch_policy.'+label)
    out['public_launch_policy_checks'] = checks
    return out


def project_scalar_receipt(work_root, final_receipt, source_bindings=None):
    if type(final_receipt) is not dict: raise ProjectionError('terminal_receipt_type')
    work=Path(work_root); issues=[]
    public={'schema':'panel-public-scalar-projection-v1','evidence':'UNREVIEWED_SCALAR_EVIDENCE',
            'performance_claim_authorized':False,'advancement_authorized':False,
            'private_terminal_canonical_sha256':_hash(final_receipt),
            'identity_representation':{'boot_id':'sha256_of_private_json_string','job_id':'sha256_of_private_json_string','host_id':'sha256_of_private_json_string',
                'process_and_participant_ids':'already_hashed_in_private_scalar_receipts','runtime_argv':'omitted; digest and policy validation retained','launch_policy':'digest only; accepted engine flags retained'},
            'source_bindings':_safe(source_bindings or {},issues,'source_bindings','bindings')}
    for key in ('status','source_release','first_failure','phases','optional_cache_metadata'):
        if key in final_receipt: public[key]=_safe({key:final_receipt[key]},issues,key).get(key)
    for stage in ('qualification','timing'):
        public[stage]=_stage(final_receipt.get(stage),stage,work,issues)
    attempted=final_receipt.get('attempted_phases',[])
    if type(attempted) is not list or len(attempted)>4:
        issues.append('attempted_phases'); attempted=[]
    public['attempted_phase_outcomes']=[]
    for index,row in enumerate(attempted):
        if type(row) is not dict:
            issues.append('attempted_phase.'+str(index));continue
        summary={k:row[k] for k in ('attempted','acknowledged','returncode','cleanup') if k in row}
        result=_ownership(row,issues,'attempted_phase.'+str(index))
        if row.get('first_failure') is not None:
            result['first_failure_present']=True
            result['private_failure_sha256']=_hash(row['first_failure'])
        public['attempted_phase_outcomes'].append(result)
    for stage,keys in {
        'provisioning':('complete','operator_calls','browser_launches','wall_seconds'),
        'build':('complete','compile_count','wall_seconds','asset_manifest','operator_calls','browser_launches'),
        'initial_ownership_bootstrap':('complete','attempted','acknowledged','first_failure','wall_seconds','subprocesses','production_supervised','operator_calls','browser_launches'),
    }.items():
        value=final_receipt.get(stage)
        if isinstance(value,dict):
            selected={k:value[k] for k in keys if k in value}
            # These summary fields contain no paths, command arrays or tool logs.
            public[stage]={k:(v if type(v) in (bool,int,float) or v is None else _safe(v,issues,stage+'.'+k,k)) for k,v in selected.items()}
            if 'outer_supervision' in value:public[stage]['outer_supervision']=_ownership(value['outer_supervision'],issues,stage+'.outer_supervision')
            for field,maximum in (('commands',39 if stage=='build' else 1),):
                if field in value:
                    rows=value[field]
                    if type(rows) is not list or len(rows)>maximum:issues.append(stage+'.'+field)
                    else:public[stage][field]=[_ownership(row,issues,stage+'.'+field+'.'+str(i)) for i,row in enumerate(rows)]
            bootstrap=value.get('bootstrap')
            if isinstance(bootstrap,dict) and 'command_receipts' in bootstrap:
                rows=bootstrap['command_receipts']
                if type(rows) is not list or len(rows)>19:issues.append(stage+'.bootstrap_commands')
                else:public[stage]['bootstrap_commands']=[_ownership(row,issues,stage+'.bootstrap.'+str(i)) for i,row in enumerate(rows)]
    provision=final_receipt.get('provisioning')
    if isinstance(provision,dict) and isinstance(provision.get('browser_provisioning_receipt'),dict):
        browser=provision['browser_provisioning_receipt'];installation=browser.get('installation',{})
        public['provisioning']['browser_acquisition']=_browser_acquisition(browser,issues)
        archive=browser.get('archive',{})
        rows=installation.get('files',[]) if isinstance(installation,dict) else []
        executable=[r for r in rows if isinstance(r,dict) and r.get('name')==installation.get('executable_name')]
        values={'distribution_sha256':archive.get('sha256'),'support_manifest_sha256':browser.get('installation_sha256'),
                'executable_sha256':executable[0].get('sha256') if len(executable)==1 else None}
        public['acquired_browser_identity']={k:v if type(v) is str and HASH.fullmatch(v) else None for k,v in values.items()}
        public['acquired_browser_identity']['runtime_version_requires_stage_flag_probe']=True
    if 'phase_supervision' in final_receipt:
        rows=final_receipt['phase_supervision']
        if type(rows) is not list or len(rows)>4:issues.append('phase_supervision')
        else:public['phase_supervision']=[_ownership(row,issues,'phase_supervision.'+str(i)) for i,row in enumerate(rows)]
    public['projection_complete']=not issues
    public['projection_issue_locations']=issues
    public['interpretation']='terminal_independent_audit_required_even_when_private_status_is_complete'
    payload=_bytes(public)
    if len(payload)>LIMIT-4096: raise ProjectionError('aggregate_public_scalar_budget')
    return public


def export_scalar_artifact(work_root, final_receipt, *, source_bindings=None):
    """Write only two closed JSON files to a fresh exact allowlisted directory."""
    work=Path(work_root)
    if not work.is_absolute() or not work.is_dir() or any(p.is_symlink() for p in (work,*work.parents)):
        raise ProjectionError('export_work_root')
    directory=work/'public-scalars'
    if directory.exists() or directory.is_symlink(): raise ProjectionError('export_no_overwrite')
    pending=work/'.public-scalars.pending'
    if pending.exists() or pending.is_symlink(): raise ProjectionError('export_pending_no_overwrite')
    deadline=final_receipt.get('export_deadline_monotonic')
    if deadline is not None and (type(deadline) not in (int,float) or not math.isfinite(deadline)):
        raise ProjectionError('export_deadline_type')
    def checkpoint():
        if deadline is not None and time.monotonic() >= deadline: raise ProjectionError('export_absolute_deadline')
    checkpoint()
    public=project_scalar_receipt(work,final_receipt,source_bindings)
    payload=_bytes(public)
    checkpoint()
    pending.mkdir()
    with (pending/'receipt.json').open('xb') as stream:stream.write(payload)
    if (pending/'receipt.json').read_bytes()!=payload:raise ProjectionError('export_payload_readback')
    checkpoint()
    manifest={'schema':'panel-public-scalar-export-v1','evidence':'UNREVIEWED_SCALAR_EVIDENCE',
              'performance_claim_authorized':False,'advancement_authorized':False,
              'projection_complete':public['projection_complete'],
              'payload_completed_monotonic':time.monotonic(),
              'manifest_and_atomic_commit_follow_this_observation':True,
              'files':{'receipt.json':{'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()}}}
    marker=_bytes(manifest)
    if len(payload)+len(marker)>LIMIT: raise ProjectionError('aggregate_public_scalar_budget')
    with (pending/'manifest.json').open('xb') as stream:stream.write(marker)
    if (pending/'manifest.json').read_bytes()!=marker:raise ProjectionError('export_manifest_readback')
    if {p.name for p in pending.iterdir()} != {'receipt.json','manifest.json'}: raise ProjectionError('export_roster')
    checkpoint()
    pending.rename(directory)
    try:checkpoint()
    except BaseException:
        directory.rename(pending)
        raise
    return {'directory':str(directory),'files':2,'bytes':len(payload)+len(marker),
            'manifest_sha256':hashlib.sha256(marker).hexdigest(),'projection_complete':public['projection_complete'],
            'completed_monotonic':time.monotonic(),'performance_claim_authorized':False}
