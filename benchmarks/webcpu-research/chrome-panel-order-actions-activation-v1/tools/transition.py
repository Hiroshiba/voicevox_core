"""Independent, closed-world qualification receipt verifier, no runtime imports.

Validation is not execution authority. This release cannot create a timing GO.
Canonical JSON rejects nonfinite numbers; file readers reject duplicate keys.
"""
if __name__ == '__main__':
    raise SystemExit('SOURCE_ONLY_EXECUTION_BLOCK: tools.transition')

import copy
import hashlib
import json
import math
import re

from launch.source_only import require_execution_admission
from tools.build_driver import compare_asset_manifests
from tools.protocol import expected_schedule, canonical_bytes, PLAN_SHA256, REGION_BYTES, REGION_ALIGNMENT

WORDS = 7_880_704
HASH = re.compile(r'^[0-9a-f]{64}$')
BINDING_HASHES = ('plan_sha256','source_tree_sha256','source_review_sha256','emitted_review_sha256','build_recipe_sha256','toolchain_lock_sha256','fixture_manifest_sha256','schedule_sha256','workflow_sha256','launcher_sha256','transition_verifier_sha256','request_sha256','bootstrap_helper_lock_sha256')
IDENTITY_FIELDS = {'collection_method','chrome_product','v8_version','chrome_distribution_sha256','chrome_executable_sha256','chrome_support_tree_sha256','browser_installation_receipt_sha256','accepted_flags','launch_argv_policy','os','architecture','cpu_vendor','cpu_family','cpu_model','cpu_stepping','isa_sha256','boot_id','job_id'}
CHECKS = {'prepare','phase','readonly','canaries','stack','output','packed','indirection','selector','coverage','fixture_expectations'}


class GateRejected(ValueError):
    pass


def need(ok, reason):
    if not ok: raise GateRejected(reason)


def exact(value, fields, reason):
    need(type(value) is dict and set(value) == set(fields), reason)


def same(a, b, reason):
    need(canonical_bytes(a) == canonical_bytes(b), reason)


def uint(value, maximum=2**53-1):
    return type(value) is int and 0 <= value <= maximum


def finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value >= 0


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_bindings(value):
    exact(value, {*BINDING_HASHES, 'source_commit','job_id','host_id'}, 'binding_fields')
    for key in BINDING_HASHES: need(type(value[key]) is str and HASH.fullmatch(value[key]), 'binding_hash_'+key)
    need(value['plan_sha256'] == PLAN_SHA256, 'different_plan')
    need(type(value['source_commit']) is str and re.fullmatch('[0-9a-f]{40}', value['source_commit']), 'source_commit')
    for key in ('job_id','host_id'): need(type(value[key]) is str and 0 < len(value[key]) <= 128, 'binding_identity')


def validate_runtime_identity(value):
    exact(value, IDENTITY_FIELDS, 'runtime_identity_fields')
    need(value['collection_method'] == 'proc_cpuinfo_chrome_bytes_cdp_v1', 'identity_collection_method')
    need(value['chrome_product'] == 'HeadlessChrome/153.0.8010.12' and value['v8_version'] == '15.3.76.4', 'engine_version')
    same(value['accepted_flags'], ['--js-flags=--no-wasm-revectorize'], 'accepted_flags')
    argv = value['launch_argv_policy']
    need(type(argv) is list and argv and all(type(s) is str and 0 < len(s) <= 4096 for s in argv), 'launch_argv')
    need('--js-flags=--no-wasm-revectorize' in argv, 'launch_off_flag')
    for forbidden in ('--no-liftoff','--liftoff-only','--no-wasm-tier-up','--trace','--prof','--jitless'):
        need(not any(forbidden in token for token in argv), 'forbidden_engine_flag')
    for key in ('chrome_distribution_sha256','chrome_executable_sha256','chrome_support_tree_sha256','browser_installation_receipt_sha256','isa_sha256'):
        need(type(value[key]) is str and HASH.fullmatch(value[key]), 'identity_hash')
    for key in IDENTITY_FIELDS - {'accepted_flags','launch_argv_policy','collection_method'}:
        need(type(value[key]) is str and 0 < len(value[key]) <= 256, 'identity_value')
    return digest(value)


def validate_phases(value):
    exact(value, {'1','3','5'}, 'phase_dilations')
    for rows in value.values():
        need(type(rows) is list and len(rows) == 7, 'phase_regions')
        for i, row in enumerate(rows):
            exact(row, {'region','mod64','mod4096'}, 'phase_fields')
            need(type(row['region']) is int and row['region'] == i and uint(row['mod64'],63) and uint(row['mod4096'],4095), 'phase_types')
            need(row['mod64'] == row['mod4096'] % 64 and row['mod4096'] % REGION_ALIGNMENT[i] == 0, 'phase_alignment')
    return digest(value)


def validate_cleanup(value):
    exact(value, {'confirmed','remaining_processes','unresolved_owners','unknown_generations','wall_seconds'}, 'cleanup_fields')
    need(value['confirmed'] is True and type(value['remaining_processes']) is int and value['remaining_processes'] == 0 and value['unresolved_owners'] == [] and value['unknown_generations'] == [], 'cleanup_incomplete')
    need(finite(value['wall_seconds']) and value['wall_seconds'] <= 30, 'cleanup_deadline')


def validate_resource_window(value, retained, stage):
    exact(value, {'command','retained','before','after','attempted','acknowledged'}, 'resource_window_fields')
    need(value['retained'] is retained and value['attempted'] is True and value['acknowledged'] is True,'resource_unknown_call_outcome')
    before, after = value['before'], value['after']
    for snapshot in (before, after):
        exact(snapshot, {'generations','processes','host','runtime_artifact_bytes','public_receipt_bytes'}, 'snapshot_fields')
        generations = snapshot['generations']
        need(type(generations) is dict and generations and all(type(x) is str and HASH.fullmatch(x) and y == x for x,y in generations.items()), 'generation_roster')
        need(type(snapshot['processes']) is dict and set(snapshot['processes']) == set(generations), 'missing_process_observation')
        exact(snapshot['host'], {'available_bytes','swap_in_bytes','swap_out_bytes'}, 'host_observation_fields')
        need(all(uint(x) for x in snapshot['host'].values()) and snapshot['host']['available_bytes'] >= 1024**3, 'host_memory')
        need(uint(snapshot['runtime_artifact_bytes'],256*1024**2) and uint(snapshot['public_receipt_bytes'],32*1024**2), 'artifact_budget')
        rss = 0
        for process in snapshot['processes'].values():
            exact(process, {'cpu_seconds','rss_bytes','major_faults','swap_bytes'}, 'process_observation_fields')
            need(finite(process['cpu_seconds']) and process['cpu_seconds'] <= (1200 if stage == 'qualification' else 2400), 'process_cpu_budget')
            need(all(uint(process[k]) for k in ('rss_bytes','major_faults','swap_bytes')), 'process_counter_type')
            rss += process['rss_bytes']
        need(rss <= 3*1024**3, 'rss_budget')
    same(before['generations'], after['generations'], 'resource_generation_change')
    for key in ('swap_in_bytes','swap_out_bytes'): need(after['host'][key] == before['host'][key], 'host_swap_io')
    for key in before['processes']:
        a, b = before['processes'][key], after['processes'][key]
        need(b['cpu_seconds'] >= a['cpu_seconds'] and b['major_faults'] >= a['major_faults'], 'nonmonotone_counters')
        need(b['swap_bytes'] <= a['swap_bytes'], 'target_swap_growth')
        if retained: need(b['major_faults'] == a['major_faults'], 'primary_major_fault')


def validate_call_record(record, call, phases):
    exact(record, {'call','commands','checks','evidence','duration_ms','worker_failure','resource_windows'}, 'call_record_fields')
    same(record['call'], call, 'call_schedule_identity')
    need(record['worker_failure'] is None, 'worker_failure')
    need(record['duration_ms'] is None if not call['retained'] else finite(record['duration_ms']) and record['duration_ms'] > 0, 'retained_duration_policy')
    exact(record['checks'], CHECKS, 'check_roster')
    need(all(value is True for value in record['checks'].values()), 'failed_check')
    need(type(record['commands']) is list and len(record['commands']) == 4, 'command_count')
    previous = -1
    for name, command in zip(('prepare','run','verify','delete'), record['commands']):
        fields = {'command','attempted','acknowledged','dispatch_ms','receipt_ms'} | ({'native_result'} if name in ('run','delete') else set())
        exact(command, fields, 'command_fields')
        need(command['command'] == name and command['attempted'] is True and command['acknowledged'] is True, 'unknown_call_outcome')
        need(finite(command['dispatch_ms']) and finite(command['receipt_ms']) and previous <= command['dispatch_ms'] <= command['receipt_ms'] and command['receipt_ms']-command['dispatch_ms'] <= 30_000, 'command_deadline_order')
        previous = command['receipt_ms']
        if 'native_result' in command: need(type(command['native_result']) is int and command['native_result'] == 0, 'native_result')
    need(type(record['resource_windows']) is list and len(record['resource_windows']) == 4, 'resource_windows_missing')
    for name, window in zip(('prepare','run','verify','delete'), record['resource_windows']):
        need(window.get('command') == name, 'resource_window_order')
        validate_resource_window(window, call['retained'] and name == 'run', call['stage'])
    e = record['evidence']
    exact(e, {'geometry','fixture_sha256','run_ack','output','readonly','packed','witnesses','coverage','canaries','stack'}, 'evidence_fields')
    g = e['geometry']
    exact(g, {'regions','descriptor','phases','indirection_words','actual_zero_sentinel','modulo_uint32_offset','actual_selector','live_descriptor'}, 'geometry_fields')
    same(g['regions'], [{'region':i,'bytes':REGION_BYTES[i],'minimum_alignment':REGION_ALIGNMENT[i]} for i in range(7)], 'region_extent_order')
    same(g['descriptor'],[call['dilation'],61568,128,4,8,11,512,176,4 if call['arm'] in ('R','A') else 32,128,2,61568,128],'live_descriptor_values')
    same(g['phases'], phases[str(call['dilation'])], 'phase_drift')
    need(type(g['indirection_words']) is int and g['indirection_words'] == 677248, 'indirection_count')
    need(all(g[k] is True for k in ('actual_zero_sentinel','modulo_uint32_offset','actual_selector','live_descriptor')), 'selector_or_indirection')
    same(e['readonly'], {'regions':6,'bytes':35675216,'differences':0}, 'readonly_extent_or_change')
    same(e['stack'], {'before':True,'after':True}, 'stack_cookie')
    same(e['canaries'], {'caller_regions':4,'prefix_bytes':256,'suffix_bytes':256,'before_bytes':2048,'after_bytes':2048,'compared_bytes':4096,'differences':0,'hidden_allocator_padding_covered':False}, 'canary_extent_or_change')
    exact(e['packed'], {'bytes','differences','packed_sha256'}, 'packed_fields')
    need(type(e['packed']['bytes']) is int and type(e['packed']['differences']) is int and e['packed']['bytes'] == 721408 and e['packed']['differences'] == 0 and HASH.fullmatch(e['packed']['packed_sha256']), 'packed_complete')
    exact(e['output'], {'words','reference_capture','compared_words','finite_required','exact_uint32','output_sha256'}, 'output_fields')
    same({k:e['output'][k] for k in e['output'] if k != 'output_sha256'}, {'words':WORDS,'reference_capture':call['reference'],'compared_words':call['comparison_words'],'finite_required':call['fixture_id']==0,'exact_uint32':not call['reference']}, 'output_count_or_oracle')
    need(type(e['output']['output_sha256']) is str and HASH.fullmatch(e['output']['output_sha256']), 'output_hash')
    need(type(e['fixture_sha256']) is list and len(e['fixture_sha256']) == 3 and all(type(h) is str and HASH.fullmatch(h) for h in e['fixture_sha256']), 'fixture_hashes')
    w = e['witnesses']
    if call['fixture_id']:
        exact(w, {'designated','checked','passed','fixture_id','expectation_code','full_class_words'}, 'witness_fields')
        code = {1:1,2:3,3:4,4:2,5:2,6:2,7:2,8:2,9:2,10:2,11:2}[call['fixture_id']]
        same(w, {'designated':512,'checked':512,'passed':True,'fixture_id':call['fixture_id'],'expectation_code':code,'full_class_words':WORDS if call['fixture_id'] <= 3 else 0}, 'missing_or_wrong_witness')
    else: same(w, {'designated':0,'checked':0,'passed':True}, 'timing_witness_policy')
    cv = e['coverage']
    exact(cv, {'enabled','participants','callbacks','exact_disjoint'}, 'coverage_fields')
    need(cv['enabled'] is call['coverage'] and cv['exact_disjoint'] is call['coverage'], 'coverage_mode')
    if call['coverage']:
        need(type(cv['participants']) is list and len(cv['participants']) == 2, 'participant_count')
        for participant in cv['participants']:
            exact(participant, {'label','identity_sha256'}, 'participant_fields')
            need(type(participant['label']) is int and participant['label'] in (0,1) and type(participant['identity_sha256']) is str and HASH.fullmatch(participant['identity_sha256']), 'participant_identity')
        need(len({p['identity_sha256'] for p in cv['participants']}) == 2 and len({p['label'] for p in cv['participants']}) == 2, 'participants_not_distinct')
        need(type(cv['callbacks']) is int and cv['callbacks'] == (15392 if call['arm'] in ('R','A') else 1924), 'callback_grid')
    else:
        same(cv,{'enabled':False,'participants':[],'callbacks':0,'exact_disjoint':False},'uninstrumented_coverage')
    ack = e['run_ack']
    exact(ack, {'completed','native_result','elapsed_ms','heap_before_bytes','heap_after_bytes','sequence'}, 'run_ack_fields')
    need(type(ack['heap_before_bytes']) is int and type(ack['heap_after_bytes']) is int and ack['completed'] is True and type(ack['native_result']) is int and ack['native_result'] == 0 and ack['heap_before_bytes'] == ack['heap_after_bytes'] == 134217728, 'run_ack_or_heap')
    need(uint(ack['sequence']) and ack['sequence'] > 0,'run_sequence')
    same(ack['elapsed_ms'], record['duration_ms'], 'duration_mismatch')
    return (tuple(e['fixture_sha256']),e['output']['output_sha256'],e['packed']['packed_sha256'])


def validate_resources(value, stage, count):
    exact(value, {'wall_seconds','tree_cpu_seconds','peak_rss_bytes','min_available_bytes','peak_artifact_bytes','public_receipt_bytes','observation_count','missing_observations','ownership_method','all_thread_children_verified','generation_matching','boundaries','sampled_not_kernel_aggregate_quota','outer_controller_excluded','outer_supervision'}, 'stage_resources_fields')
    need(finite(value['wall_seconds']) and value['wall_seconds'] <= (900 if stage == 'qualification' else 1800), 'stage_wall_budget')
    need(finite(value['tree_cpu_seconds']) and value['tree_cpu_seconds'] <= (1200 if stage == 'qualification' else 2400), 'stage_cpu_budget')
    need(uint(value['peak_rss_bytes'],3*1024**3) and uint(value['min_available_bytes']) and value['min_available_bytes'] >= 1024**3, 'stage_memory_budget')
    need(uint(value['peak_artifact_bytes'],256*1024**2) and uint(value['public_receipt_bytes'],32*1024**2), 'stage_disk_budget')
    need(uint(value['observation_count']) and value['observation_count'] >= 8*count+4 and type(value['missing_observations']) is int and value['missing_observations'] == 0, 'missing_resource_observations')
    need(value['ownership_method'] == 'subreaper_pidfd_all_thread_children' and value['all_thread_children_verified'] is True and value['generation_matching'] is True, 'ownership_evidence')
    labels=['before_probe','after_probe']
    for root in ([0] if stage == 'qualification' else range(1,11)):labels += ['before_root_'+str(root),'after_root_'+str(root)]
    labels += ['terminal']
    need(type(value['boundaries']) is list and len(value['boundaries']) == len(labels),'resource_boundaries')
    previous=-1; swap=None
    for label,row in zip(labels,value['boundaries']):
        exact(row,{'label','host','at_monotonic'},'boundary_fields')
        need(row['label'] == label and finite(row['at_monotonic']) and row['at_monotonic'] >= previous,'boundary_order');previous=row['at_monotonic']
        exact(row['host'],{'available_bytes','swap_in_bytes','swap_out_bytes'},'boundary_host_fields')
        need(all(uint(x) for x in row['host'].values()) and row['host']['available_bytes'] >= 1024**3,'boundary_host')
        current=(row['host']['swap_in_bytes'],row['host']['swap_out_bytes'])
        if swap is not None:need(current == swap,'boundary_swap_io')
        swap=current
    need(value['sampled_not_kernel_aggregate_quota'] is True and value['outer_controller_excluded'] is True,'resource_scope_disclosure')
    outer=value['outer_supervision'];exact(outer,{'startup_gate_released','samples','cleanup'},'outer_supervision_fields')
    need(outer['startup_gate_released'] is True and type(outer['samples']) is list and outer['samples'],'outer_startup_unobserved')
    for sample in outer['samples']:
        exact(sample,{'monotonic_seconds','rss_bytes','artifact_bytes','host_available_bytes','tree_cpu_seconds','generations','target_swap_growth_bytes','host_swap_in_delta_bytes','host_swap_out_delta_bytes'},'outer_sample_fields')
        need(finite(sample['monotonic_seconds']) and uint(sample['rss_bytes'],3*1024**3) and uint(sample['artifact_bytes'],256*1024**2) and uint(sample['host_available_bytes']) and sample['host_available_bytes'] >= 1024**3 and finite(sample['tree_cpu_seconds']) and sample['tree_cpu_seconds'] <= (1200 if stage == 'qualification' else 2400) and uint(sample['generations']) and sample['generations'] > 0,'outer_sample_invalid')
        need(all(type(sample[k]) is int and sample[k] == 0 for k in ('target_swap_growth_bytes','host_swap_in_delta_bytes','host_swap_out_delta_bytes')),'outer_swap_growth')
    cleanup=outer['cleanup'];exact(cleanup,{'confirmed','remaining_processes','returncode','failure','cleanup_failure','timed_out'},'outer_cleanup_fields')
    need(type(cleanup['remaining_processes']) is int and type(cleanup['returncode']) is int and cleanup['confirmed'] is True and cleanup['remaining_processes'] == 0 and cleanup['returncode'] == 0 and cleanup['failure'] is None and cleanup['cleanup_failure'] is None and cleanup['timed_out'] is False,'outer_cleanup_failure')


def validate_root(root, expected, stage):
    exact(root, {'root','initializations','shutdown','cleanup','identity','actual_launch_argv','launch_argv_sha256'}, 'root_receipt_fields')
    same(root['identity'],expected['identity'],'root_identity_changed')
    need(validate_launch_argv(root['actual_launch_argv'],expected['identity']['launch_argv_policy'],expected['owned_temp_roots'][stage]) == root['launch_argv_sha256'],'root_argv_hash')
    need(type(root['initializations']) is list and len(root['initializations']) == 5, 'module_initializations')
    for arm, row in zip(('R','A','B','Q','P'), root['initializations']):
        exact(row, {'ready','arm','stage','sequence','heap_bytes','backing_workers','pool_participants','hardware_concurrency','calibration','distinct_backing_verified'}, 'initialization_fields')
        need(all(type(row[k]) is int for k in ('heap_bytes','backing_workers','pool_participants')) and row['ready'] is True and row['arm'] == arm and row['stage'] == stage and row['heap_bytes'] == 134217728 and row['backing_workers'] == 1 and row['pool_participants'] == 2 and row['distinct_backing_verified'] is True, 'module_contract')
        need(uint(row['sequence']) and row['sequence'] > 0 and uint(row['hardware_concurrency']) and row['hardware_concurrency'] > 0, 'initialization_identity')
        need(row['calibration'] is None if stage == 'qualification' else type(row['calibration']) is dict, 'calibration_stage')
    shutdown = root['shutdown']
    exact(shutdown, {'shutdown','calls','phase_records','fixture_identities','modules','process_cleanup_required','buffers_erased'}, 'shutdown_fields')
    need(type(shutdown['calls']) is int and shutdown['shutdown'] is True and shutdown['process_cleanup_required'] is True and shutdown['buffers_erased'] is True and shutdown['calls'] == (330 if stage == 'qualification' else 60), 'shutdown_incomplete')
    need(type(shutdown['modules']) is list and len(shutdown['modules']) == 5, 'shutdown_modules')
    for arm, row in zip(('R','A','B','Q','P'), shutdown['modules']):
        exact(row, {'arm','shutdown','pool_destroy_returned','library_deinitialized','backing_termination_requested','process_cleanup_required','sequence'}, 'module_shutdown_fields')
        need(row['arm'] == arm and row['shutdown'] is True and row['pool_destroy_returned'] is True and row['library_deinitialized'] is True and row['backing_termination_requested'] is True and row['process_cleanup_required'] is True and uint(row['sequence']) and row['sequence'] > 0, 'module_shutdown')
    validate_cleanup(root['cleanup'])


def verify_qualification(receipt, expected):
    """A complete synthetic/verifiable receipt is necessary, never a GO alone."""
    exact(expected, {'schema','bindings','identity','asset_manifest','owned_temp_roots'}, 'expectation_fields')
    need(expected['schema'] == 'panel-stage-expectations-v1', 'expectation_schema')
    validate_bindings(expected['bindings']); validate_runtime_identity(expected['identity'])
    exact(receipt, {'schema','status','stage','bindings','identity','asset_manifest','final_asset_manifest','compile_count','phase_records','fixture_identities','calls','root_receipts','flag_probe','resources','first_failure','timing_calls'}, 'qualification_receipt_fields')
    need(receipt['schema'] == 'panel-qualification-receipt-v1' and receipt['status'] == 'complete' and receipt['stage'] == 'qualification', 'qualification_not_complete')
    same(receipt['bindings'],expected['bindings'],'source_or_review_binding_changed')
    same(receipt['identity'],expected['identity'],'platform_changed')
    need(receipt['bindings']['job_id'] == receipt['identity']['job_id'], 'job_identity_mismatch')
    compare_asset_manifests(expected['asset_manifest'],receipt['asset_manifest'])
    compare_asset_manifests(expected['asset_manifest'],receipt['final_asset_manifest'])
    need(type(receipt['compile_count']) is int and receipt['compile_count'] == 1 and type(receipt['timing_calls']) is int and receipt['timing_calls'] == 0, 'build_or_timing_scope')
    need(receipt['first_failure'] is None, 'qualification_failure')
    phase_hash = validate_phases(receipt['phase_records'])
    schedule = [c for c in expected_schedule() if c['stage'] == 'qualification']
    need(type(receipt['calls']) is list and len(receipt['calls']) == 330, 'qualification_call_count')
    identities = {}; participants = {}
    for call, record in zip(schedule, receipt['calls']):
        identity = validate_call_record(record,call,receipt['phase_records'])
        if call['coverage']:
            ids=sorted(p['identity_sha256'] for p in record['evidence']['coverage']['participants'])
            if call['arm'] in participants:same(participants[call['arm']],ids,'pool_participant_identity_drift')
            else:participants[call['arm']]=ids
        key = (call['dilation'],call['fixture_id'])
        if key in identities: same(identities[key],identity,'fixture_reference_identity_changed')
        else: need(call['reference'] is True,'reference_seed_missing'); identities[key] = identity
    need(len(identities) == 33 and sum(c['reference'] for c in schedule) == 33 and sum(c['comparison_words'] for c in schedule) == 2340569088,'qualification_matrix')
    roots = receipt['root_receipts']
    need(type(roots) is list and len(roots) == 1 and type(roots[0]['root']) is int and roots[0]['root'] == 0,'qualification_root_count')
    validate_root(roots[0],expected,'qualification')
    same(roots[0]['shutdown']['phase_records'],receipt['phase_records'],'phase_reset_at_shutdown')
    expected_fixtures = {f'{d}:{f}':{'fixture_sha256':list(value[0]),'output_sha256':value[1],'packed_sha256':value[2]} for (d,f),value in identities.items()}
    same(roots[0]['shutdown']['fixture_identities'],expected_fixtures,'shutdown_fixture_identity')
    same(receipt['fixture_identities'],expected_fixtures,'receipt_fixture_identity')
    probe = receipt['flag_probe']
    exact(probe, {'operator_calls','wall_seconds','accepted','identity','cleanup','actual_launch_argv','launch_argv_sha256'}, 'probe_fields')
    need(type(probe['operator_calls']) is int and probe['operator_calls'] == 0 and finite(probe['wall_seconds']) and probe['wall_seconds'] <= 60 and probe['accepted'] is True, 'flag_probe')
    need(validate_launch_argv(probe['actual_launch_argv'],expected['identity']['launch_argv_policy'],expected['owned_temp_roots'][receipt['stage']]) == probe['launch_argv_sha256'],'probe_argv_hash')
    same(probe['identity'],expected['identity'],'probe_identity_changed'); validate_cleanup(probe['cleanup'])
    validate_resources(receipt['resources'],'qualification',330)
    return {'schema':'panel-transition-validation-v1','status':'PASS','qualification_receipt_sha256':digest(receipt),'phase_records_sha256':phase_hash,'carried_phase_records':copy.deepcopy(receipt['phase_records']),'carried_fixture_identities':copy.deepcopy(receipt['fixture_identities']),'asset_manifest_sha256':digest(expected['asset_manifest']),'identity_sha256':digest(expected['identity']),'bindings_sha256':digest(expected['bindings']),'qualification_calls':330,'references':33,'comparisons':297,'comparison_words':2340569088,'competing_nan_designated_witnesses':6144,'timing_calls_permitted_by_receipt':600,'execution_authority':False}


def admit_timing(*args, **kwargs):
    require_execution_admission('transition.admit_timing')
    return verify_qualification(*args, **kwargs)


def main(*args, **kwargs):
    require_execution_admission('transition.main')


def validate_timing(receipt, expected, transition):
    """Validate every timing call/root before any estimator can consume samples."""
    need(transition.get('status') == 'PASS' and transition.get('qualification_calls') == 330, 'qualification_transition_required')
    need(transition.get('bindings_sha256') == digest(expected['bindings']) and transition.get('identity_sha256') == digest(expected['identity']) and transition.get('asset_manifest_sha256') == digest(expected['asset_manifest']), 'transition_binding_changed')
    exact(receipt, {'schema','status','stage','bindings','identity','asset_manifest','final_asset_manifest','compile_count','phase_records','fixture_identities','calls','root_receipts','flag_probe','resources','first_failure','timing_calls'}, 'timing_receipt_fields')
    need(receipt['schema'] == 'panel-timing-receipt-v1' and receipt['status'] == 'complete' and receipt['stage'] == 'timing', 'timing_incomplete')
    same(receipt['bindings'],expected['bindings'],'timing_bindings_changed'); same(receipt['identity'],expected['identity'],'timing_platform_changed')
    same(receipt['phase_records'],transition['carried_phase_records'],'timing_phase_reset')
    compare_asset_manifests(expected['asset_manifest'],receipt['asset_manifest']); compare_asset_manifests(expected['asset_manifest'],receipt['final_asset_manifest'])
    need(type(receipt['compile_count']) is int and type(receipt['timing_calls']) is int and receipt['compile_count'] == 1 and receipt['timing_calls'] == 600 and receipt['first_failure'] is None,'timing_failure_or_rebuild')
    schedule=[c for c in expected_schedule() if c['stage'] == 'timing']
    need(type(receipt['calls']) is list and len(receipt['calls']) == 600,'timing_call_count')
    roots=receipt['root_receipts'];need(type(roots) is list and len(roots) == 10,'timing_root_count')
    fixture_hashes={}; calibrations={}; participant_sets={}
    for index,root in enumerate(roots,1):
        need(type(root['root']) is int and root['root'] == index,'fresh_timing_root_order')
        validate_root(root,expected,'timing')
        same(root['shutdown']['phase_records'],receipt['phase_records'],'timing_root_phase_drift')
        for init in root['initializations']:
            c=init['calibration'];exact(c,{'quantum_ms','positive_samples','reads','empty_export_samples','empty_export_p99_ms','p99_resolution_censored','minimum_primary_ms','subtracted'},'timing_calibration_fields')
            need(finite(c['quantum_ms']) and c['quantum_ms'] > 0 and type(c['positive_samples']) is int and c['positive_samples'] == 1000 and uint(c['reads'],1_000_000) and c['reads'] >= 1001 and type(c['empty_export_samples']) is int and c['empty_export_samples'] == 1000 and finite(c['empty_export_p99_ms']) and c['subtracted'] is False,'calibration_samples')
            need(c['p99_resolution_censored'] is (c['empty_export_p99_ms'] == 0),'p99_resolution_censoring')
            threshold=max(200*c['quantum_ms'],100*c['empty_export_p99_ms']); need(c['minimum_primary_ms'] == threshold,'calibration_threshold')
            calibrations[(index,init['arm'])]=threshold
    for call,record in zip(schedule,receipt['calls']):
        identity=validate_call_record(record,call,receipt['phase_records'])
        if call['coverage']:
            key_participant=(call['root'],call['arm']);ids=sorted(p['identity_sha256'] for p in record['evidence']['coverage']['participants'])
            if key_participant in participant_sets:same(participant_sets[key_participant],ids,'timing_participant_drift')
            else:participant_sets[key_participant]=ids
        key=call['dilation']
        if key in fixture_hashes:same(fixture_hashes[key],identity,'timing_cross_root_fixture_identity')
        else:need(call['reference'],'timing_reference_missing');fixture_hashes[key]=identity
        if call['retained']:need(record['duration_ms'] > calibrations[(call['root'],call['arm'])],'timer_resolution_failure')
    carried=copy.deepcopy(transition['carried_fixture_identities'])
    for dilation,value in fixture_hashes.items():carried[f'{dilation}:0']={'fixture_sha256':list(value[0]),'output_sha256':value[1],'packed_sha256':value[2]}
    same(receipt['fixture_identities'],carried,'timing_fixture_identity_carry')
    for root in roots:same(root['shutdown']['fixture_identities'],carried,'timing_root_fixture_carry')
    validate_resources(receipt['resources'],'timing',600)
    probe=receipt['flag_probe'];exact(probe,{'operator_calls','wall_seconds','accepted','identity','cleanup','actual_launch_argv','launch_argv_sha256'},'timing_probe_fields')
    need(type(probe['operator_calls']) is int and probe['operator_calls'] == 0 and probe['accepted'] is True and finite(probe['wall_seconds']) and probe['wall_seconds'] <= 60,'timing_probe')
    need(validate_launch_argv(probe['actual_launch_argv'],expected['identity']['launch_argv_policy'],expected['owned_temp_roots'][receipt['stage']]) == probe['launch_argv_sha256'],'timing_probe_argv_hash')
    same(probe['identity'],expected['identity'],'timing_probe_identity');validate_cleanup(probe['cleanup'])
    return {'schema':'panel-timing-validation-v1','status':'PASS','calls':600,'references':30,'comparisons':570,'comparison_words':4492001280,'retained':150,'receipt_sha256':digest(receipt),'execution_authority':False}


def validate_launch_argv(actual,policy,owned_temp_root):
    need(type(actual) is list and type(policy) is list and 0 < len(actual) == len(policy) <= 128,'argv_count')
    need(all(type(x) is str and 0 < len(x) <= 4096 for x in actual+policy) and sum(map(len,actual)) <= 32768,'argv_bounds')
    for value,expected in zip(actual,policy):
        if '<OWNED_PROFILE_DIR>' in expected:
            need(expected == '--user-data-dir=<OWNED_PROFILE_DIR>' and re.fullmatch(re.escape('--user-data-dir='+owned_temp_root+'/playwright_chromiumdev_profile-')+r'[A-Za-z0-9_-]{1,80}',value),'argv_profile')
        elif '<LOOPBACK_PORT>' in expected:
            need(expected == '--remote-debugging-port=<LOOPBACK_PORT>' and re.fullmatch(r'--remote-debugging-port=[0-9]{1,5}',value) and int(value.rsplit('=',1)[1]) <= 65535,'argv_port')
        else:need(value == expected and '<' not in expected and '>' not in expected,'argv_literal')
    need([x for x in actual if x.startswith('--js-flags=')] == ['--js-flags=--no-wasm-revectorize'],'argv_js_flags')
    return digest(actual)
