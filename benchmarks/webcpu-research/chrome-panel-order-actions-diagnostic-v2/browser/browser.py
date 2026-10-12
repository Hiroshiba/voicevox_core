"""Admission-bound Chrome panel driver and pure-data validation models.

This activation source has not been executed. The shared immutable root request
gate precedes every runtime entry; production donors use the same child admission.
The pure models support failure-path review; they are not execution evidence.
"""
if __name__ == '__main__':
    raise SystemExit('SOURCE_ONLY_EXECUTION_BLOCK: browser.browser')

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from launch.source_only import require_execution_admission
from tools.protocol import expected_schedule, canonical_bytes, REGION_BYTES, REGION_ALIGNMENT

ARMS = ('R', 'A', 'B', 'Q', 'P')
WORDS = 7_880_704
HEAP_BYTES = 134_217_728
READONLY_BYTES = 35_675_216
PACKED_BYTES = 721_408
LARGE_BUFFER_BYTES = 739_008_080
PLAN_SHA256 = '07456a44febb29f820920fb68fe4f62071676b05c0f933495db076ce06ace702'
STAGE_LIMITS = {
    'qualification': {'wall_seconds': 900, 'tree_cpu_seconds': 1200, 'per_process_cpu_seconds': 1200, 'calls': 330, 'roots': 1},
    'timing': {'wall_seconds': 1800, 'tree_cpu_seconds': 2400, 'per_process_cpu_seconds': 2400, 'calls': 600, 'roots': 10},
}


class BrowserGateRejected(ValueError):
    pass


def need(ok, check):
    if not ok:
        raise BrowserGateRejected(check)


def uint(value, limit=2**32-1):
    return type(value) is int and 0 <= value <= limit


def finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def configuration():
    return {
        'schema': 'chrome-panel-browser-source-contract-v1', 'status': 'EXECUTION_CAPABLE_SOURCE_NOT_EXECUTED',
        'execution_admitted': False, 'execution_capable': True, 'plan_sha256': PLAN_SHA256,
        'execution_authority': 'one_hash_bound_root_ci_request_with_explicit_child_descriptors',
        'chromium': '153.0.8010.12', 'v8': '15.3.76.4',
        'flags': ['--js-flags=--no-wasm-revectorize'], 'normal_tiering': True,
        'local_browser_permitted': False, 'maximum_chrome_roots': 1,
        'operator_workers_per_root': 5, 'initialization_order': list(ARMS),
        'real_pool_participants': 2, 'emscripten_backing_workers_per_module': 1,
        'fixed_heap_bytes': HEAP_BYTES, 'large_application_buffer_bytes': LARGE_BUFFER_BYTES,
        'output_reference_bytes': WORDS*4, 'readonly_workspace_bytes': READONLY_BYTES,
        'packed_reference_bytes': PACKED_BYTES,
        'calibration': 'timing_only_immediately_after_each_arm_init_before_next_arm',
        'commands_per_call': ['prepare', 'run', 'verify', 'delete'],
        'resource_windows_include': ['controller_init', 'module_init_and_calibration', 'prepare', 'run_acknowledgement', 'verify', 'delete', 'shutdown'],
        'primary_clock': 'performance.now(); _screen_run(); performance.now()',
        'raw_output_transport': False, 'phase_record_reset': False,
        'source_donor_architecture': 'generation_safe_subreaper_pidfd_all_thread_children',
        'browser_byte_identity_scope': 'one_official_ci_acquisition_then_immutable_within_job',
        'browser_prelaunch_binding': 'complete_archive_executable_support_tree_and_explicit_executable_path',
        'unadmitted_donor_imports_blocked': True, 'legacy_donor_workloads_disabled': True,
        'runtime_caps': copy.deepcopy(STAGE_LIMITS),
        'root_launch_and_module_init_seconds': 60, 'single_rpc_seconds': 30,
        'flag_probe_seconds': 60, 'cleanup_reserve_seconds': 30,
        'failure_only_cleanup_seconds': 30, 'summed_owned_rss_bytes': 3*1024**3,
        'host_available_minimum_bytes': 1024**3, 'new_runtime_artifacts_peak_bytes': 256*1024**2,
        'public_scalar_receipts_maximum_bytes': 32*1024**2,
        'aggregate_quota_claimed': False, 'main_controller_resource_scope': 'excluded_and_disclosed',
        'optional_cache_metadata': 'bounded_best_effort_unknown_allowed_no_gate_no_privileged_counters',
    }


def validate_schedule(document):
    calls = document['calls'] if type(document) is dict and 'calls' in document else document
    need(type(calls) is list and calls == expected_schedule(), 'exact_930_call_schedule')
    # Equality alone permits True == 1: bind JSON types with canonical bytes.
    need(canonical_bytes(calls) == canonical_bytes(expected_schedule()), 'schedule_types')
    return calls


def validate_phases(phases, *, complete):
    need(type(phases) is dict and set(phases) <= {'1', '3', '5'}, 'phase_keys')
    if complete:
        need(set(phases) == {'1', '3', '5'}, 'carried_phases_incomplete')
    for rows in phases.values():
        need(type(rows) is list and len(rows) == 7, 'phase_regions')
        for i, row in enumerate(rows):
            need(type(row) is dict and set(row) == {'region', 'mod64', 'mod4096'}, 'phase_fields')
            need(type(row['region']) is int and row['region'] == i and uint(row['mod4096'], 4095)
                 and uint(row['mod64'], 63) and row['mod64'] == row['mod4096'] % 64
                 and row['mod4096'] % REGION_ALIGNMENT[i] == 0, 'phase_value')
    return copy.deepcopy(phases)


class PhaseCarry:
    """Pure state model: first qualification R per D seeds once, never reset."""
    def __init__(self, phases=None):
        self.records = validate_phases({} if phases is None else phases, complete=False)

    def observe(self, call, rows):
        key = str(call['dilation'])
        validate_phases({key: rows}, complete=False)
        if key not in self.records:
            need(call['stage'] == 'qualification' and call['arm'] == 'R' and call['coverage'] is True
                 and call['fixture_id'] == 1, 'phase_initial_seed_only')
            self.records[key] = copy.deepcopy(rows)
        else:
            need(canonical_bytes(self.records[key]) == canonical_bytes(rows), 'phase_drift_terminal')
        return copy.deepcopy(self.records[key])

    def transition(self):
        return validate_phases(self.records, complete=True)


class BufferLiveness:
    """Small synthetic state machine, no tensors or large allocations."""
    def __init__(self):
        self.owner = None
        self.sequence = None
        self.lease = False
        self.scratch_obligation = False
        self.hash_pending = False
        self.running = False
        self.reference_key = None

    def acquire(self, arm, completed_sequence, dispatched_sequence):
        need(not self.running and not self.lease and completed_sequence == dispatched_sequence, 'quiescence_handshake')
        self.owner, self.sequence, self.lease = arm, completed_sequence, True

    def snapshot(self):
        need(self.lease and not self.hash_pending and not self.scratch_obligation, 'snapshot_reuse')
        self.scratch_obligation = True

    def release(self):
        need(self.lease and not self.hash_pending, 'lease_release')
        self.lease = False

    def run(self):
        need(not self.lease and self.scratch_obligation and not self.hash_pending and not self.running, 'run_liveness')
        self.running = True

    def acknowledge(self):
        need(self.running, 'run_acknowledgement')
        self.running = False

    def compare_readonly(self):
        need(self.lease and self.scratch_obligation and not self.running, 'readonly_comparison')
        self.scratch_obligation = False

    def start_output_hash(self):
        need(self.lease and not self.scratch_obligation and not self.hash_pending and not self.running, 'scratch_hash_before_comparison')
        self.hash_pending = True

    def finish_hash(self):
        need(self.hash_pending, 'hash_completion')
        self.hash_pending = False


class LifecycleModel:
    """Mock-only event model: unknown outcome is terminal, never replayed."""
    def __init__(self, schedule, stage, root):
        validate_schedule(schedule)
        self.calls = [c for c in schedule if c['stage'] == stage and c['root'] == root]
        need(len(self.calls) == (330 if stage == 'qualification' else 60), 'root_schedule')
        self.stage, self.root = stage, root
        self.initialized, self.calibrated, self.index = [], [], 0
        self.state, self.current, self.failure = 'init', None, None
        self.events = []

    def event(self, name, *, arm=None, call=None, acknowledged=True, native_result=0, elapsed_ms=None):
        need(self.failure is None, 'terminal_no_retry')
        event = {'command': name, 'attempted': True, 'acknowledged': acknowledged, 'native_result': native_result, 'elapsed_ms': elapsed_ms}
        self.events.append(event)  # Preserve attempt before later quality checks.
        try:
            need(acknowledged is True, 'unknown_outcome')
            need(type(native_result) is int and native_result == 0, 'native_failure')
            if name == 'init_arm':
                need(self.state == 'init' and len(self.initialized) < 5 and arm == ARMS[len(self.initialized)], 'init_order')
                need(self.stage != 'timing' or self.calibrated == self.initialized, 'calibrate_before_next_init')
                self.initialized.append(arm)
                if self.stage == 'qualification' and len(self.initialized) == 5:
                    self.state = 'idle'
            elif name == 'calibrate':
                need(self.stage == 'timing' and self.state == 'init' and self.initialized == self.calibrated + [arm], 'calibration_order')
                self.calibrated.append(arm)
                if len(self.calibrated) == 5:
                    self.state = 'idle'
            elif name == 'prepare':
                need(self.state == 'idle' and self.current is None and self.index < len(self.calls)
                     and canonical_bytes(call) == canonical_bytes(self.calls[self.index]), 'call_order')
                self.current, self.state = copy.deepcopy(call), 'prepared'
            elif name == 'run':
                need(self.state == 'prepared', 'run_once')
                need(finite(elapsed_ms, True) if self.current['retained'] else elapsed_ms is None, 'timing_only_retained')
                self.state = 'ran'
            elif name == 'verify':
                need(self.state == 'ran', 'verify_after_run'); self.state = 'verified'
            elif name == 'delete':
                need(self.state == 'verified', 'delete_after_checks'); self.current = None; self.index += 1; self.state = 'idle'
            elif name == 'shutdown':
                need(self.state == 'idle' and self.current is None and self.index == len(self.calls), 'complete_root_only'); self.state = 'closed'
            else:
                raise BrowserGateRejected('unknown_command')
        except BrowserGateRejected as error:
            self.failure = str(error)
            raise


def validate_calibration(value):
    keys = {'quantum_ms','positive_samples','reads','empty_export_samples','empty_export_p99_ms',
            'p99_resolution_censored','minimum_primary_ms','subtracted'}
    need(type(value) is dict and set(value) == keys, 'calibration_fields')
    need(finite(value['quantum_ms'], True) and value['positive_samples'] == 1000
         and type(value['reads']) is int and 1001 <= value['reads'] <= 1_000_000
         and value['empty_export_samples'] == 1000 and finite(value['empty_export_p99_ms']), 'calibration_samples')
    need(value['subtracted'] is False and value['p99_resolution_censored'] is (value['empty_export_p99_ms'] == 0), 'calibration_policy')
    need(value['minimum_primary_ms'] == max(200*value['quantum_ms'], 100*value['empty_export_p99_ms']), 'calibration_threshold')
    return value['minimum_primary_ms']


def validate_probe_timing(probe_seconds, cleanup_seconds):
    """Probe work and its contained actual cleanup have separate ceilings."""
    need(finite(probe_seconds) and probe_seconds <= 60, 'flag_probe_deadline')
    need(finite(cleanup_seconds) and cleanup_seconds <= min(30,probe_seconds), 'probe_cleanup_deadline')
    return cleanup_seconds


def validate_browser_installation_binding(installation, archive_sha256, receipt_sha256, expected):
    """Pure scalar binding; acquisition and complete-file rehash are separate."""
    need(type(installation) is dict and set(installation) == {'chrome_support_tree_sha256','chrome_executable_sha256'},
         'browser_installation_verification_fields')
    actual = dict(installation, chrome_distribution_sha256=archive_sha256,
        browser_installation_receipt_sha256=receipt_sha256)
    for key, value in actual.items():
        need(type(value) is str and re.fullmatch(r'[0-9a-f]{64}',value) is not None
             and value == expected.get(key), 'retained_browser_identity_'+key)
    return actual


def validate_resource_window(before, after, *, retained, stage):
    """Pure review projection; production strict snapshots remain mandatory."""
    need(stage in STAGE_LIMITS, 'stage')
    for value in (before, after):
        need(type(value) is dict and set(value) == {'generations','processes','host','runtime_artifact_bytes','public_receipt_bytes'}, 'resource_fields')
        need(type(value['generations']) is dict and 0 < len(value['generations']) <= 65536
             and all(type(k) is str and k and type(v) is str and v for k,v in value['generations'].items())
             and type(value['processes']) is dict and set(value['processes']) == set(value['generations']), 'complete_owned_tree')
        need(type(value['host']) is dict and set(value['host']) == {'available_bytes','swap_in_bytes','swap_out_bytes'}
             and all(uint(v, 2**63-1) for v in value['host'].values()), 'host_observation')
        need(uint(value['runtime_artifact_bytes'],2**63-1) and uint(value['public_receipt_bytes'],2**63-1), 'artifact_observation')
        need(value['host']['available_bytes'] >= 1024**3, 'available_memory')
        need(0 <= value['runtime_artifact_bytes'] <= 256*1024**2 and 0 <= value['public_receipt_bytes'] <= 32*1024**2, 'artifact_cap')
        need(sum(p['rss_bytes'] for p in value['processes'].values()) <= 3*1024**3, 'summed_rss_cap')
        for p in value['processes'].values():
            need(set(p) == {'cpu_seconds','rss_bytes','major_faults','swap_bytes'} and all(finite(x) for x in p.values()), 'resource_counter')
            need(p['cpu_seconds'] <= STAGE_LIMITS[stage]['per_process_cpu_seconds'], 'process_cpu_cap')
    need(before['generations'] == after['generations'], 'generation_matching')
    for key in ('swap_in_bytes','swap_out_bytes'):
        need(before['host'][key] == after['host'][key], 'host_swap_io')
    cpu = 0
    for key, a in before['processes'].items():
        b = after['processes'][key]
        need(b['cpu_seconds'] >= a['cpu_seconds'] and b['major_faults'] >= a['major_faults'], 'counter_monotone')
        need(b['swap_bytes'] <= a['swap_bytes'], 'target_swap_growth')
        if retained:
            need(b['major_faults'] == a['major_faults'], 'primary_major_faults')
        cpu += b['cpu_seconds'] - a['cpu_seconds']
    return {'cpu_delta_seconds': cpu, 'rss_scope': 'summed_owned_process_RSS_not_unique_physical',
            'sampling_not_aggregate_quota': True, 'exited_process_cpu_may_be_missed': True}


def optional_cache_metadata(rows):
    """Bounded already-collected metadata. Unknown is descriptive, not a gate."""
    if type(rows) is not list:
        return {'status': 'unknown', 'caches': []}
    result = []
    for row in rows[:32]:
        if type(row) is not dict:
            continue
        if type(row.get('level')) is int and 1 <= row['level'] <= 4 and row.get('type') in ('Data','Instruction','Unified'):
            result.append({k: row[k] for k in ('level','type','size_bytes','line_bytes')
                           if k in row and (k in ('level','type') or uint(row[k], 2**40))})
    return {'status': 'reported' if result else 'unknown', 'caches': result, 'performance_gate': False}


# Every runtime entry calls shared immutable request admission BEFORE paths,
# environment, optional imports or any action. Environment is not authority.
def load_production_driver():
    require_execution_admission('browser.load_production_driver')
    import importlib.util
    import sys
    root = Path(__file__).resolve().parent
    bindings = json.loads((root/'vendor/source-bindings.json').read_text())
    for row in bindings['records']:
        source = root.parent/row['guarded']
        need(hashlib.sha256(source.read_bytes()).hexdigest() == row['guarded_sha256'], 'production_donor_pin')
    spec = importlib.util.spec_from_file_location('_panel_production_driver', root/'vendor/operator_browser.py')
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    return driver


def launch(integration=None, url=None):
    require_execution_admission('browser.launch')
    return integration.open(url)


def flag_probe(integration=None, helpers=None):
    require_execution_admission('browser.flag_probe')
    return integration.probe(helpers)


def qualify(*args, **kwargs):
    require_execution_admission('browser.qualify')
    return run('qualification', *args, **kwargs)


def timing(*args, **kwargs):
    require_execution_admission('browser.timing')
    return run('timing', *args, **kwargs)


def run(stage=None, context=None, deadline=None):
    require_execution_admission('browser.run')
    need(type(context) is dict, 'stage_context')
    expected = context['expected']
    from tools.build_driver import configure_verified_helper_imports
    # The isolated phase interpreter may resolve only complete byte-verified
    # helper packages, with no replacement of existing or loaded routes.
    configure_verified_helper_imports(context['bootstrap_helper_lock'], context['runtime_helper_package_roots'])
    packages = context['bootstrap_helper_lock']['runtime_helper_packages']
    need(packages['playwright']['version'] == '1.63.0' and packages['psutil']['version'] == '7.2.2',
         'bound_dependency_versions')
    journal = ScalarJournal(context['runtime_dir'], stage)
    platform_inputs = copy.deepcopy(context['platform_inputs'])
    platform_inputs['owned_temp_root'] = str(journal.root/'tmp')
    need(platform_inputs['owned_temp_root'] == expected['owned_temp_roots'][stage], 'program_owned_temp_scope')
    return _run_stage(stage=stage, schedule_document=context['schedule'], assets_root=context['assets_dir'],
        bindings=expected['bindings'], identity=expected['identity'], asset_manifest=expected['asset_manifest'],
        phase_records=context['phase_records'], fixture_identities=context['fixture_identities'],
        platform_inputs=platform_inputs, stage_deadline=deadline, audit=journal.write,
        artifact_meter=journal.artifact_meter)


def _run_stage(stage=None, schedule_document=None, assets_root=None, bindings=None, identity=None, asset_manifest=None,
        phase_records=None, fixture_identities=None, platform_inputs=None, stage_deadline=None, audit=None, artifact_meter=None):
    require_execution_admission('browser.run_stage')
    import resource
    import time
    import os
    import psutil
    from playwright._repo_version import version as playwright_version
    from tools.build_driver import validate_asset_manifest
    need(stage in STAGE_LIMITS, 'stage')
    owned_temp = Path(platform_inputs['owned_temp_root'])
    need(owned_temp.is_absolute() and not owned_temp.is_symlink(), 'owned_temp_root')
    owned_temp.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Inherited by ownership supervisors, RPC children, Node/Playwright driver
    # and Chrome. All new profiles/temp are inside the sampled runtime subtree.
    for name in ('TMPDIR','TMP','TEMP'): os.environ[name] = str(owned_temp)
    need(playwright_version == '1.63.0' and psutil.__version__ == '7.2.2', 'dependency_versions')
    need(resource.getrlimit(resource.RLIMIT_CPU) == (STAGE_LIMITS[stage]['per_process_cpu_seconds'],)*2,
         'inherited_per_process_cpu_limit')
    started = time.monotonic()
    need(finite(stage_deadline, True) and 0 < stage_deadline-started <= STAGE_LIMITS[stage]['wall_seconds'], 'stage_deadline')
    calls = validate_schedule(schedule_document)
    validate_asset_manifest(asset_manifest)
    _verify_runtime_assets(assets_root, asset_manifest)
    if stage == 'timing':
        validate_phases(phase_records, complete=True)
        need(len(fixture_identities) >= 33, 'qualification_fixture_identity_carry')
    else:
        need(phase_records == {} and fixture_identities == {}, 'qualification_starts_empty')
    driver = load_production_driver()
    base, observer = driver.load_helpers(Path(__file__).resolve().parent/'vendor')
    report = dict(schema='panel-'+stage+'-receipt-v1', status='incomplete', stage=stage,
        bindings=copy.deepcopy(bindings), identity=copy.deepcopy(identity), asset_manifest=copy.deepcopy(asset_manifest),
        final_asset_manifest=None, compile_count=1, phase_records=copy.deepcopy(phase_records),
        fixture_identities=copy.deepcopy(fixture_identities), calls=[], root_receipts=[], flag_probe=None,
        resources=None, first_failure=None, timing_calls=0)
    integration = ProductionIntegration(driver, base, observer, psutil, time.monotonic,
        audit, stage, stage_deadline, artifact_meter, platform_inputs, identity)
    try:
        integration.host_boundary('before_probe')
        probe_started = time.monotonic()
        probe = flag_probe(integration, Path(__file__).resolve().parent/'vendor')
        need(probe['engine'] == {'product':'HeadlessChrome/153.0.8010.12','js_version':'15.3.76.4'}
             and probe.get('argv_matches') is True and probe.get('cleanup_confirmed') is True, 'flag_acceptance')
        probe_identity = integration.platform_identity(identity, probe)
        report['flag_probe'] = dict(operator_calls=0, wall_seconds=time.monotonic()-probe_started,
            accepted=True, identity=probe_identity, cleanup=probe['cleanup'],
            actual_launch_argv=probe['actual_launch_argv'], launch_argv_sha256=probe['launch_argv_sha256'])
        integration.host_boundary('after_probe')
        # Serve only already hash-bound assets. The production server supplies
        # COOP/COEP; no direct browser route exists outside the owned adapter.
        with driver.serve_assets(Path(assets_root)) as url:
            for root in ([0] if stage == 'qualification' else range(1,11)):
                _verify_runtime_assets(assets_root, asset_manifest)
                root_receipt = dict(root=root, initializations=[], shutdown=None, cleanup=None, identity=None)
                report['root_receipts'].append(root_receipt)
                integration.host_boundary('before_root_'+str(root))
                browser_info = launch(integration, url)
                need(browser_info['engine'] == probe['engine'], 'root_engine_identity')
                root_receipt['identity'] = integration.platform_identity(identity)
                root_receipt['actual_launch_argv'] = browser_info['actual_launch_argv']
                root_receipt['launch_argv_sha256'] = browser_info['launch_argv_sha256']
                integration.command('init', stage=stage, root=root, schedule=calls,
                    phase_records=report['phase_records'], fixture_identities=report['fixture_identities'])
                calibrations = {}
                for arm in ARMS:
                    init = integration.command('init_arm', arm=arm)
                    need(init['arm'] == arm and init['stage'] == stage and init['ready'] is True
                         and init['heap_bytes'] == HEAP_BYTES and init['backing_workers'] == 1
                         and init['pool_participants'] == 2 and init['distinct_backing_verified'] is True, 'module_init')
                    if stage == 'timing':
                        calibrations[arm] = validate_calibration(init['calibration'])
                    else:
                        need(init['calibration'] is None, 'no_qualification_calibration')
                    root_receipt['initializations'].append(init)
                integration.platform_identity(identity)
                for call in [c for c in calls if c['stage'] == stage and c['root'] == root]:
                    record = {'call':copy.deepcopy(call), 'commands':[], 'resource_windows':[], 'checks':{}, 'evidence':{}, 'duration_ms':None, 'worker_failure':None}
                    report['calls'].append(record)
                    audit('call_attempt', record)
                    for command in ('prepare','run','verify','delete'):
                        kwargs = {'call':call} if command == 'prepare' else {'retained':call['retained']} if command == 'run' else {}
                        value = integration.command(command, receipt_target=record, **kwargs)
                        if command == 'run':
                            record['duration_ms'] = value.get('elapsed_ms')
                            record['evidence']['run_ack'] = value
                            audit('known_run_receipt', record)
                            need(value['completed'] is True and value['native_result'] == 0, 'native_run')
                            if call['retained']:
                                need(finite(value['elapsed_ms'], True) and value['elapsed_ms'] > calibrations[call['arm']], 'primary_clock_quality')
                            else:
                                need(value['elapsed_ms'] is None, 'no_warmup_performance_sample')
                        elif command in ('verify','delete'):
                            record.update(value)
                        audit('call_progress', record)
                    need(all(record['checks'].values()) and record['call'] == call, 'call_complete')
                    if stage == 'timing': report['timing_calls'] += 1
                closed = integration.command('shutdown')
                need(closed['shutdown'] is True and closed['buffers_erased'] is True
                     and closed['calls'] == (330 if stage == 'qualification' else 60), 'root_shutdown')
                for dilation, rows in report['phase_records'].items():
                    need(rows == closed['phase_records'][dilation], 'phase_carry_no_reset')
                report['phase_records'] = validate_phases(closed['phase_records'], complete=True)
                report['fixture_identities'] = closed['fixture_identities']
                root_receipt['shutdown'] = closed
                root_receipt['cleanup'] = integration.close()
                integration.host_boundary('after_root_'+str(root))
                audit('root_complete', root_receipt)
        _verify_runtime_assets(assets_root, asset_manifest)
        integration.verify_installation()
        report['final_asset_manifest'] = copy.deepcopy(asset_manifest)
        report['resources'] = integration.finish_resources(started)
        need(len(report['calls']) == STAGE_LIMITS[stage]['calls'] and integration.clock() <= stage_deadline, 'stage_completion')
        report['status'] = 'complete'
    except BaseException as error:
        report['first_failure'] = {'check': str(error) if isinstance(error, BrowserGateRejected) else 'runtime_gate',
            'known_calls':len(report['calls']), 'no_retry':True}
        if integration.active:
            try:
                cleanup = integration.close()
                report['root_receipts'][-1]['cleanup'] = cleanup
            except BaseException:
                report['first_failure']['cleanup_unconfirmed'] = True
        try:
            report['resources'] = integration.finish_resources(started, failed=True)
        except BaseException:
            report['resources'] = None
            report['first_failure']['terminal_resource_observation_missing'] = True
    audit('stage_terminal', report)
    return report


class ScalarJournal:
    """Bounded private runtime receipts, never model/tensor/heap payloads."""
    def __init__(self, directory, stage):
        require_execution_admission('browser.ScalarJournal')
        need(stage in STAGE_LIMITS, 'journal_stage')
        self.root = Path(directory)
        need(self.root.is_absolute() and not self.root.is_symlink(), 'journal_root')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.root/(stage+'-journal.jsonl')
        need(not self.path.exists(), 'no_journal_replacement')
        self.stream = self.path.open('xb')
        self.stage = stage

    def write(self, event, value):
        require_execution_admission('browser.ScalarJournal.write')
        if event == 'stage_terminal':
            path = self.root/(self.stage+'-receipt.json')
            need(not path.exists(), 'no_receipt_replacement')
            encoded = canonical_bytes(value)+b'\n'
            need(self.artifact_meter()['public_receipt_bytes']+len(encoded) <= 32*1024**2, 'public_receipt_cap')
            with path.open('xb') as stream:
                stream.write(encoded); stream.flush()
            self.stream.close()
            return
        # Retain each event's identity/hash without repeatedly duplicating a
        # growing call/root report. The acknowledgement carries its known scalar
        # payload once; final complete resource windows live in the stage receipt.
        row = {'event':event,'sha256':hashlib.sha256(canonical_bytes(value)).hexdigest()}
        if type(value) is dict:
            if 'command' in value: row['command'] = value['command']
            if 'attempted' in value: row['attempted'] = value['attempted']
            if 'acknowledged' in value: row['acknowledged'] = value['acknowledged']
            if type(value.get('call')) is dict: row['call_index'] = value['call']['call_index']
            if event in ('acknowledgement','known_run_receipt','command_final'):
                response = value.get('response', value.get('evidence',{}).get('run_ack'))
                if type(response) is dict:
                    if response.get('ok') is False:
                        row['terminal_response'] = response
                    else:
                        response = response.get('value',response)
                        row['known_run'] = {k:response[k] for k in ('completed','native_result','elapsed_ms','heap_before_bytes','heap_after_bytes') if k in response}
        encoded = canonical_bytes(row)+b'\n'
        need(self.artifact_meter()['public_receipt_bytes']+len(encoded) <= 32*1024**2, 'public_receipt_cap')
        self.stream.write(encoded); self.stream.flush()

    def artifact_meter(self):
        require_execution_admission('browser.ScalarJournal.artifact_meter')
        total = public = 0
        for path in self.root.rglob('*'):
            need(not path.is_symlink(), 'runtime_artifact_symlink')
            if path.is_file():
                size = path.stat().st_size; total += size
                if path.suffix in ('.json','.jsonl'): public += size
        return {'runtime_artifact_bytes':total,'public_receipt_bytes':public}


def validate_launch_argv(actual, policy, owned_temp_root=None):
    need(type(actual) is list and type(policy) is list and 0 < len(actual) == len(policy) <= 128, 'argv_count')
    need(all(type(x) is str and 0 < len(x) <= 4096 for x in actual+policy) and sum(map(len,actual)) <= 32768, 'argv_bounds')
    for value, expected in zip(actual,policy):
        if '<OWNED_PROFILE_DIR>' in expected:
            need(type(owned_temp_root) is str and owned_temp_root.startswith('/') and '..' not in Path(owned_temp_root).parts, 'owned_profile_scope')
            need(expected == '--user-data-dir=<OWNED_PROFILE_DIR>' and
                 re.fullmatch(re.escape('--user-data-dir='+owned_temp_root.rstrip('/')+'/')+r'playwright_chromiumdev_profile-[A-Za-z0-9_-]{1,80}', value) is not None, 'owned_profile_argument')
        elif '<LOOPBACK_PORT>' in expected:
            need(expected == '--remote-debugging-port=<LOOPBACK_PORT>' and
                 re.fullmatch(r'--remote-debugging-port=[0-9]{1,5}',value) is not None and int(value.rsplit('=',1)[1]) <= 65535, 'loopback_port_argument')
        else:
            need(value == expected and '<' not in expected and '>' not in expected, 'argv_exact_literal')
    need([x for x in actual if x.startswith('--js-flags=')] == ['--js-flags=--no-wasm-revectorize'], 'exact_javascript_flags')
    return hashlib.sha256(canonical_bytes(actual)).hexdigest()


def main(*args, **kwargs):
    require_execution_admission('browser.main')
    # tools/program.py owns CLI parsing and immutable admission; it passes the
    # already-validated stage arguments rather than providing a second route.
    return run(*args, **kwargs)


def _verify_runtime_assets(directory, manifest):
    require_execution_admission('browser.verify_runtime_assets')
    from tools.build_driver import validate_asset_manifest
    validate_asset_manifest(manifest)
    root = Path(directory)
    need(root.is_absolute() and root.is_dir() and not root.is_symlink(), 'asset_root')
    actual = sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())
    need(actual == manifest['logical_names'], 'assets_missing_or_extra')
    for row in manifest['assets']:
        path = root/row['name']
        need(not path.is_symlink() and path.is_file() and path.stat().st_size == row['bytes'], 'asset_bytes')
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''): h.update(chunk)
        need(h.hexdigest() == row['sha256'], 'asset_sha256')
    return True


class ProductionIntegration:
    """Admission-bound orchestration wired to pinned production ownership.

    Admission includes source/emitted/assets/platform binding from tools/program.
    No alternate Playwright or local process-group route is available here.
    """
    def __init__(self, driver, base, observer, psutil, clock, audit, stage, deadline, artifact_meter, platform_inputs, expected_identity):
        require_execution_admission('browser.ProductionIntegration')
        self.driver, self.base, self.observer, self.psutil = driver, base, observer, psutil
        self.clock, self.audit, self.stage = clock, audit, stage
        self.deadline, self.active, self.notes = deadline, {}, []
        self.artifact_meter, self.last_host, self.boundaries = artifact_meter, None, []
        self.cpu_highwater, self.tree_cpu_seconds, self.peak_rss, self.min_available = {}, 0, 0, None
        self.peak_artifacts, self.peak_public = 0, 0
        self.platform_inputs = platform_inputs
        self.expected_identity = copy.deepcopy(expected_identity)
        self.chrome_executable = None

    def verify_installation(self):
        require_execution_admission('browser.ProductionIntegration.verify_installation')
        from tools.build_driver import verify_browser_installation
        distribution = Path(self.platform_inputs['chrome_distribution_path'])
        need(distribution.is_absolute() and distribution.is_file() and not distribution.is_symlink(), 'chrome_distribution_file')
        h = hashlib.sha256()
        with distribution.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk)
        manifest = self.platform_inputs['browser_installation_manifest']
        directory = Path(self.platform_inputs['browser_installation_root'])
        installed = verify_browser_installation(directory, manifest)
        actual = validate_browser_installation_binding(installed,h.hexdigest(),
            self.platform_inputs['installation_receipt_sha256'],self.expected_identity)
        executable = directory/manifest['executable_name']
        need(executable.is_absolute() and executable.is_file() and not executable.is_symlink(), 'admitted_executable_path')
        self.chrome_executable = str(executable)
        return actual

    def host_boundary(self, label):
        require_execution_admission('browser.ProductionIntegration.host_boundary')
        host = self.base.host_snapshot(self.psutil)
        need(all(uint(host.get(k),2**63-1) for k in ('available_bytes','swap_in_bytes','swap_out_bytes')), 'host_complete')
        need(host['available_bytes'] >= 1024**3, 'available_memory')
        if self.last_host is not None:
            need(all(host[k] == self.last_host[k] for k in ('swap_in_bytes','swap_out_bytes')), 'all_window_host_swap_safety')
        self.last_host = host
        self.min_available = host['available_bytes'] if self.min_available is None else min(self.min_available,host['available_bytes'])
        self.boundaries.append({'label':label,'host':host,'at_monotonic':self.clock()})
        self.audit('host_boundary',self.boundaries[-1])
        return host

    def platform_identity(self, expected, capture=None):
        require_execution_admission('browser.ProductionIntegration.platform_identity')
        import platform
        browser = self.active.get('root')
        info = capture if capture is not None else browser.info
        need(info is not None, 'platform_capture')
        actual_argv_sha = validate_launch_argv(info['actual_launch_argv'],expected['launch_argv_policy'],self.platform_inputs['owned_temp_root'])
        need(actual_argv_sha == info['launch_argv_sha256'], 'actual_argv_digest')
        retained_browser = self.verify_installation()
        need(info['chrome_executable_sha256'] == retained_browser['chrome_executable_sha256'], 'actual_loaded_executable_identity')
        blocks = Path('/proc/cpuinfo').read_text().strip().split('\n\n')
        cpus = []
        for block in blocks:
            data = dict(line.split(':',1) for line in block.splitlines() if ':' in line)
            data = {k.strip():v.strip() for k,v in data.items()}
            need(all(k in data for k in ('vendor_id','cpu family','model','stepping','flags')), 'cpu_identity_missing')
            cpus.append({k:data[k] for k in ('vendor_id','cpu family','model','stepping')})
            cpus[-1]['isa_sha256'] = hashlib.sha256(' '.join(sorted(data['flags'].split())).encode()).hexdigest()
        need(cpus and all(x == cpus[0] for x in cpus), 'cpu_identity_inconsistent')
        actual = {'collection_method':'proc_cpuinfo_chrome_bytes_cdp_v1',
            **retained_browser,
            'chrome_product':info['engine']['product'], 'v8_version':info['engine']['js_version'],
            'os':platform.system(),'architecture':platform.machine(),
            'cpu_vendor':cpus[0]['vendor_id'],'cpu_family':cpus[0]['cpu family'],
            'cpu_model':cpus[0]['model'],'cpu_stepping':cpus[0]['stepping'],'isa_sha256':cpus[0]['isa_sha256'],
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'job_id':self.platform_inputs['job_id'],
            'accepted_flags':['--js-flags=--no-wasm-revectorize'], 'launch_argv_policy':copy.deepcopy(expected['launch_argv_policy'])}
        need(actual == expected, 'same_job_platform_identity')
        return actual

    def normalized_snapshot(self, snapshot):
        require_execution_admission('browser.ProductionIntegration.normalized_snapshot')
        processes, generations = {}, {}
        for identity, row in snapshot['processes'].items():
            token = hashlib.sha256(canonical_bytes(list(identity))).hexdigest()
            processes[token] = {k:row[k] for k in ('cpu_seconds','rss_bytes','major_faults','swap_bytes')}
            generations[token] = token
            previous = self.cpu_highwater.get(token,0)
            need(finite(row['cpu_seconds']) and row['cpu_seconds'] >= previous, 'owned_cpu_monotone')
            self.tree_cpu_seconds += row['cpu_seconds']-previous
            self.cpu_highwater[token] = row['cpu_seconds']
        host = snapshot['host']
        observed = self.artifact_meter()
        need(type(observed) is dict and set(observed) == {'runtime_artifact_bytes','public_receipt_bytes'}, 'artifact_measurement')
        value = dict(generations=generations,processes=processes,
            host={k:host[k] for k in ('available_bytes','swap_in_bytes','swap_out_bytes')},**observed)
        validate_resource_window(value,value,retained=False,stage=self.stage)
        need(self.tree_cpu_seconds <= STAGE_LIMITS[self.stage]['tree_cpu_seconds'], 'observed_tree_cpu_cap')
        self.peak_rss = max(self.peak_rss,sum(p['rss_bytes'] for p in processes.values()))
        self.min_available = host['available_bytes'] if self.min_available is None else min(self.min_available,host['available_bytes'])
        self.peak_artifacts = max(self.peak_artifacts,observed['runtime_artifact_bytes'])
        self.peak_public = max(self.peak_public,observed['public_receipt_bytes'])
        if self.last_host is not None:
            need(all(host[k] == self.last_host[k] for k in ('swap_in_bytes','swap_out_bytes')), 'all_window_host_swap_safety')
        self.last_host = host
        return value

    def probe(self, helpers):
        require_execution_admission('browser.ProductionIntegration.probe')
        started = self.clock()
        self.driver.assert_no_unresolved_owners()
        retained = self.verify_installation()
        end = min(self.deadline - 30, started + 60)
        need(end > self.clock(), 'cleanup_reserve')
        value = self.driver.strict_flag_acceptance_probe(self.base, helpers, deadline=end, cleanup_deadline=self.deadline,
            chrome_executable=self.chrome_executable, chrome_executable_sha256=retained['chrome_executable_sha256'])
        self.driver.assert_no_unresolved_owners()
        need(value['cleanup_confirmed'] is True, 'probe_cleanup')
        cleanup_seconds = validate_probe_timing(self.clock()-started,value.get('cleanup_wall_seconds'))
        value['cleanup'] = dict(confirmed=True,remaining_processes=0,unresolved_owners=[],unknown_generations=[],wall_seconds=cleanup_seconds)
        return value

    def open(self, url):
        require_execution_admission('browser.ProductionIntegration.open')
        need(not self.active and self.clock() < self.deadline - 30, 'one_root_and_reserve')
        self.driver.assert_no_unresolved_owners()
        retained = self.verify_installation()
        # Derived donor changes only launch wait 600->60; owner's ceiling stays
        # fixed at the stage workload deadline, allowing bounded RPC rearming.
        browser = self.driver.OperatorBrowser(None, url, self.base, self.psutil,
            deadline=self.deadline - 30, cleanup_deadline=self.deadline,
            chrome_executable=self.chrome_executable, chrome_executable_sha256=retained['chrome_executable_sha256'])
        self.active['root'] = browser
        need(browser._owner.owned_enumeration_verified is True, 'production_ownership')
        return copy.deepcopy(browser.info)

    def command(self, command, *, receipt_target=None, **payload):
        require_execution_admission('browser.ProductionIntegration.command')
        need(len(self.active) == 1 and self.clock() < self.deadline - 30, 'active_owned_root')
        browser = self.active['root']
        original = browser.deadline
        browser.deadline = min(self.deadline - 30, self.clock() + (60 if command == 'init_arm' else 30))
        note = {'command': command, 'attempted': False, 'acknowledged': False, 'response': None}
        self.notes.append(note)
        snapshots = {}
        retained = command == 'run' and payload.pop('retained', False)
        def snapshot(phase, samples):
            need(set(samples) == {'root'}, 'one_observed_root')
            snapshots[phase] = self.normalized_snapshot(samples['root'])
            self.audit('resource_'+phase, snapshots[phase])
        def response(value):
            # Checkpoint known acknowledgement BEFORE validating any output.
            note['response'] = value
            if receipt_target is not None and type(value) is dict:
                if value.get('ok') is False and type(value.get('current')) is dict:
                    receipt_target.update(value['current'])
                elif value.get('ok') is True and type(value.get('value')) is dict:
                    v = value['value']
                    if command == 'run':
                        receipt_target['duration_ms'] = v.get('elapsed_ms')
                        receipt_target['evidence']['run_ack'] = copy.deepcopy(v)
                    elif command in ('verify','delete'):
                        receipt_target.update(v)
            self.audit('acknowledgement', note)
        try:
            value, resources = self.driver.observed_call(self.active, 'root', command, self.observer, self.psutil,
                timed=retained, observation=note,
                on_response=response, on_stage=lambda: self.audit('observation', note),
                on_snapshot=snapshot, **payload)
            need(resources['passed'] is True, 'resource_window')
            need(set(snapshots) == {'before','after'}, 'complete_resource_window')
            validate_resource_window(snapshots['before'], snapshots['after'], retained=retained, stage=self.stage)
            note['normalized_window'] = dict(command=command,retained=retained,**snapshots)
            need(value.get('ok') is True, 'controller_terminal')
            return value['value']
        finally:
            browser.deadline = original
            if receipt_target is not None:
                window = dict(command=command,retained=retained,
                    before=snapshots.get('before'),after=snapshots.get('after'),
                    attempted=note['attempted'],acknowledged=note['acknowledged'])
                receipt_target['resource_windows'].append(window)
            self.audit('command_final', note)

    def close(self):
        require_execution_admission('browser.ProductionIntegration.close')
        started = self.clock()
        need(len(self.active) == 1, 'owned_close')
        browser = self.active['root']
        value = browser.close(deadline=self.deadline)
        self.driver.validate_cleanup(value)
        need(value['confirmed'] is True and value['remaining_processes'] == 0, 'complete_cleanup')
        self.active.clear()
        self.driver.assert_no_unresolved_owners()
        return dict(confirmed=True,remaining_processes=0,unresolved_owners=[],unknown_generations=[],wall_seconds=self.clock()-started)

    def finish_resources(self, started, failed=False):
        require_execution_admission('browser.ProductionIntegration.finish_resources')
        self.host_boundary('terminal')
        return dict(wall_seconds=self.clock()-started,tree_cpu_seconds=self.tree_cpu_seconds,
            peak_rss_bytes=self.peak_rss,min_available_bytes=self.min_available,
            peak_artifact_bytes=self.peak_artifacts,public_receipt_bytes=self.peak_public,
            observation_count=sum('normalized_window' in n for n in self.notes)*2+len(self.boundaries),
            missing_observations=sum('normalized_window' not in n for n in self.notes),
            ownership_method='subreaper_pidfd_all_thread_children',all_thread_children_verified=not failed,
            generation_matching=not failed,boundaries=copy.deepcopy(self.boundaries),
            sampled_not_kernel_aggregate_quota=True,outer_controller_excluded=True)
