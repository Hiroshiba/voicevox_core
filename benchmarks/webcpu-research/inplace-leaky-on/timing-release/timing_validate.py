#!/usr/bin/env python3
"""Three-control confirmation validation, paired summaries and privacy-safe logging."""
import sys
import argparse, itertools, json, math, random, re, statistics
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'browser'))
from release_gate import release_gate
from timing_quality import evaluate_quality
from source_manifest import KEYS, FLAGS, QUERY_SHA, MODEL_SHA, PRISTINE_SHA, ORIGINAL_BODY, CANDIDATE_BODY, SOURCE_REFS, need, sha
from source_resources import validate_host, validate_snapshot, validate_processes, idle_cpu, resource_observations
XNN_RUNTIME = {'shared_memory': True, 'pthreads': 1, 'spin_off': True, 'fixed_length': 962, 'fixed_matches': 1, 'xnn_threads': 2, 'xnn_sessions': 1, 'configured_ort_global_threads': 1}

def schedule():
    orders = list(itertools.permutations(KEYS)) + [KEYS, KEYS[1:] + KEYS[:1], KEYS[2:] + KEYS[:2]]
    random.Random(1729).shuffle(orders)
    return [{'process_set': i // 3 + 1, 'pair': i % 3 + 1, 'order': list(x)} for i, x in enumerate(orders)]

def design():
    return {'seed': 1729, 'primary_calls': 27, 'process_sets': 3, 'rounds_per_set': 3, 'conditions': list(KEYS), 'observations_per_condition': 9, 'permutation_multiset': 'All six permutations plus the three forward cyclic orders, then Random(1729).shuffle, partitioned sequentially into three process sets.', 'position_counts': {m: [sum((x['order'][i] == m for x in schedule())) for i in range(3)] for m in KEYS}, 'creation_order_policy': 'Rotate condition launch/warmup/check order by process set, so each condition occupies each startup position once.', 'pair_order_counts': {a + '_before_' + b: sum((x['order'].index(a) < x['order'].index(b) for x in schedule())) for a, b in itertools.combinations(KEYS, 2)}, 'limitation': 'Exact global positional balance; unavoidable 4/5 relative pair-order imbalance in nine rounds. Three process sets are the independent clusters; all rows retained.'}

def keys(x, s):
    need(isinstance(x, dict) and (not set(x) - set(s.split())), 'Unexpected result field')

def digest(x):
    need(isinstance(x, str) and re.fullmatch('[a-f0-9]{64}', x), 'Invalid hash')

def positive(x):
    need(type(x['elapsed_s']) in (int, float) and math.isfinite(x['elapsed_s']) and (x['elapsed_s'] > 0) and (x['wav_bytes'] == 478252), 'Invalid latency')

def alias(x, mode, p):
    keys(x, 'variant table_slot function_index callbacks elements classes predicted_branch length_histogram restored metadata_errors scope')
    need(x['variant'] == mode and x['table_slot'] == p['callback_table_slot'] and (x['function_index'] == p['callback_function_index']), 'Alias identity')
    need(x['restored'] and x['metadata_errors'] == 0 and (x['callbacks'] == 69) and (x['elements'] == 435901440), 'Alias coverage')
    need(x['classes'] == {'exact_inplace': {'calls': 41, 'elements': 256615424}, 'disjoint': {'calls': 28, 'elements': 179286016}} and x['length_histogram'] == {'492544': 1, '1970176': 17, '7880704': 51}, 'Alias distribution')
    expected = {'simd': {'calls': 69, 'elements': 435901440}, 'scalar': {'calls': 0, 'elements': 0}} if mode == 'candidate' else {'simd': {'calls': 28, 'elements': 179286016}, 'scalar': {'calls': 41, 'elements': 256615424}}
    need(x['predicted_branch'] == expected, 'Alias guard classification')

def validate(r, *, capture_only=False):
    keys(r, 'schema capture_only status stage schedule normal_tiering browser_flags warmups_per_browser provenance source_proof environment source_hashes model_sha256 query_sha256 browser_gate process_sets trials sampling_policy distribution worker_source_sha256 activation initial_host release_gate reference gates_complete_before_timing design private_temporary_directory_deleted private_native_dump_retained')
    need(r['capture_only'] is capture_only and r['schema'] == 'inplace-leaky-on-reviewed-timing-v1' and r['status'] == ('awaiting_manual_review' if capture_only else 'complete') and r['stage'] == r['status'] and r['normal_tiering'] and r['browser_flags'] == FLAGS, 'Completion/configuration')
    need(not capture_only, 'Timing-only schema')
    need(r['private_temporary_directory_deleted'] is True and r['private_native_dump_retained'] is False, 'Completed primary cleanup')
    need(r['model_sha256'] == MODEL_SHA and r['query_sha256'] == QUERY_SHA, 'Inputs')
    need(r['schedule'] == schedule() and r['warmups_per_browser'] == 5, 'Frozen27-call design')
    need(r['design'] == design() and r['gates_complete_before_timing'] is (False if capture_only else True), 'Frozen design/pre-timing gates')
    a = r['activation']
    need(set(a) == set(KEYS), 'Per-binary activation coverage')
    engine = None
    for mode, record in a.items():
        keys(record, 'off on wasm_sha256 js_sha256 verified')
        need(record['verified'] is True, 'Verified ON activation')
        for name in ['off', 'on']:
            x = record[name]
            keys(x, 'product js_version transformed_groups revectorizable_nodes flag_rejected launch_configuration_matches trace_sha256')
            digest(x['trace_sha256'])
            need(not x['flag_rejected'] and x['launch_configuration_matches'], 'Accepted diagnostic flags')
            need(type(x['transformed_groups']) is int and type(x['revectorizable_nodes']) is int, 'Integer activation counts')
            need(x['transformed_groups'] == 0 and x['revectorizable_nodes'] == 0 if name == 'off' else x['transformed_groups'] > 0 and x['revectorizable_nodes'] > 0, 'Actual OFF/ON activation')
            current = {field: x[field] for field in ['product', 'js_version']}
            engine = current if engine is None else engine
            need(current == engine, 'Diagnostic engines differ')
    p = r['provenance']
    need(set(p) == set(KEYS), 'Three provenance records')
    need(len({(v['wasm_sha256'], v['js_sha256']) for v in p.values()}) == 3, 'Distinct source binaries')
    for mode in KEYS:
        need(a[mode]['wasm_sha256'] == p[mode]['wasm_sha256'] and a[mode]['js_sha256'] == p[mode]['js_sha256'], 'Per-binary activation identity')
    reference = r['reference']
    keys(reference, 'mode phase iteration raw_sha256 pcm_sha256 wav_sha256')
    need(reference['mode'] == 'original' and reference['phase'] == 'gate' and (reference['iteration'] == 1), 'Original ON reference')
    for field in ['raw_sha256', 'pcm_sha256', 'wav_sha256']:
        digest(reference[field])
    need(not capture_only, 'This validator is timing only')
    need(r['release_gate'] == release_gate(p, engine, reference), 'Frozen reviewed capture release')
    for mode, v in p.items():
        keys(v, 'receipt_sha256 archive_sha256 wasm_sha256 js_sha256 callback_body_sha256 callback_table_slot callback_function_index build_toolchain native_bundle')
        for name in ['receipt_sha256', 'archive_sha256', 'wasm_sha256', 'js_sha256']:
            digest(v[name])
        need(v['build_toolchain'] == {'core': '9b539761f3e152b966e08c2de0784129fe8cf68d', 'rust': '1.96.0', 'emscripten': '4.0.8', 'optimization': 'z', 'graph_level': 1}, 'Toolchain')
        need(v['callback_body_sha256'] == (CANDIDATE_BODY if mode == 'candidate' else ORIGINAL_BODY), 'Source callback pin')
        b = v['native_bundle']
        keys(b, 'verified runtime_sha256 dispatcher_sha256 invalidated_packages activation_member_sha256 activation_occurrence duplicate_activation_sha256 all_ordered_native_members_verified')
        need(b['verified'] and b['runtime_sha256'] == v['archive_sha256'] and (set(b['invalidated_packages']) == {'voicevox_core', 'voicevox_benchmark'}), 'Bundle proof')
        if mode == 'original':
            need(v['archive_sha256'] == PRISTINE_SHA, 'Original pristine XNN archive')
        need(b['activation_occurrence'] == 1 and b['all_ordered_native_members_verified'] == 1438, 'Indexed activation bundle')
    s = r['source_proof']
    keys(s, 'original_control candidate_scope rebuilt_dependency_count candidate_dependency_count all_element_segments_identical manifest_sha256 source_refs preparation_hashes')
    keys(s['original_control'], 'passed function_definition_count all_function_definitions_exact_IR_equal_without_normalization complete_IR_equal_after_declared_normalization normalized_IR_sha256 all_defined_symbols_equal original_object_sha256 rebuilt_object_sha256')
    keys(s['candidate_scope'], 'passed definition_count counts metadata_nodes_matched metadata_mapping_bijective metadata_edges_recursively_equal original_attribute_definitions_identical normalized_IR_sha256 defined_symbols_equal candidate_object_sha256')
    need(s['original_control']['passed'] and s['original_control']['function_definition_count'] == 372 and s['candidate_scope']['passed'] and (s['candidate_scope']['definition_count'] == 372) and s['all_element_segments_identical'], 'Source gate')
    need(s['candidate_scope']['counts'] == {'raw_IR_identical': 295, 'metadata_ID_renumbering_only': 73, 'diagnostic_line_only': 2, 'intended_LeakyRelu_exact_alias_change': 2}, 'Source scope')
    need(s['source_refs'] == SOURCE_REFS, 'Public source refs')
    need(set(s['preparation_hashes']) == {'preparer_sha256', 'builder_source_sha256', 'source_pins_sha256', 'source_proof_helper_sha256', 'callback_discovery_sha256'}, 'Preparation hash schema')
    for value in s['preparation_hashes'].values():
        digest(value)
    for obj, names in [(s['original_control'], ['original_object_sha256', 'rebuilt_object_sha256']), (s['candidate_scope'], ['candidate_object_sha256'])]:
        for name in names:
            digest(obj[name])
    need(s['original_control']['all_defined_symbols_equal'] and s['candidate_scope']['defined_symbols_equal'], 'Symbol proof')
    keys(r['environment'], 'os architecture cpu available_logical_cpus host_logical_cpus python core_commit onnxruntime_version onnxruntime_builder_commit uv rust emscripten affinity_logical_cpus cgroup_cpu_quota memory_gib')
    need(r['environment']['cpu'] and r['environment']['available_logical_cpus'] >= 1 and (r['environment']['affinity_logical_cpus'] >= 1), 'CPU environment')
    validate_host(r['initial_host'])
    for name, value in r['source_hashes'].items():
        need(Path(name).name == name, 'Source basename')
        digest(value)
    for value in r['worker_source_sha256'].values():
        digest(value)
    if 'distribution' in r:
        keys(r['distribution'], 'sha256 bytes')
        digest(r['distribution']['sha256'])
        need(r['distribution']['bytes'] > 0, 'Distribution size')
    hcs = set()
    identities = []

    def init(v, mode, gate=False):
        keys(v, 'ready runtime dispatch wasm_sha256 js_sha256 timing_instrumentation engine browser_pid browser_created launch_flags')
        need(v['ready'] and v['engine'] == engine and (v['launch_flags'] == FLAGS) and (v['wasm_sha256'] == p[mode]['wasm_sha256']) and (v['js_sha256'] == p[mode]['js_sha256']), 'Actual worker identity')
        need(v['timing_instrumentation'] == ('alias_gate_only' if gate else 'clock_only'), 'Instrumentation isolation')
        need(v['runtime'] == XNN_RUNTIME, 'XNN runtime')
        d = v['dispatch']
        keys(d, 'schema_version source context expected_dispatch actual_dispatch worker_hardware_concurrency probe_hardware_concurrency is_x86 relaxed_simd mr nr splat_pointers loadsplat_pointers checked_pointers probe_return_code cross_origin_isolated shared_memory verified')
        need(d['verified'] and d['actual_dispatch'] == 'splat' and (d['expected_dispatch'] == 'auto') and (d['loadsplat_pointers'] == 0) and (d['splat_pointers'] == 12) and (d['checked_pointers'] == 12) and (not d['relaxed_simd']) and d['is_x86'] and (d['worker_hardware_concurrency'] == 4) and (d['probe_hardware_concurrency'] == 4) and (d['probe_return_code'] == 0) and (d['mr'] == 4) and (d['nr'] == 8) and d['cross_origin_isolated'] and d['shared_memory'], 'Actual dispatcher')
        hcs.add(d['worker_hardware_concurrency'])

    def clean(c):
        keys(c, 'observed_processes remaining_processes confirmed')
        need(c['confirmed'] and c['observed_processes'] > 0 and (c['remaining_processes'] == 0), 'Cleanup')

    def output(x, with_alias=False):
        keys(x, 'mode phase iteration wav_bytes raw_bytes wav_sha256 raw_sha256 exact_bytes alias pcm_sha256 finite fp32_exact pcm_exact wav_format fp32_samples')
        need(x['finite'] and x['exact_bytes'] and (x['wav_bytes'] == 478252) and (x['raw_bytes'] == 956416), 'Output exactness')
        need(x['fp32_exact'] is True and x['pcm_exact'] is True and (x['fp32_samples'] == 239104) and (x['wav_format'] == {'channels': 1, 'sample_bytes': 2, 'sample_rate': 24000, 'frames': 239104}), 'FP32/PCM format and exactness')
        for field in ['raw_sha256', 'pcm_sha256', 'wav_sha256']:
            need(x[field] == reference[field], 'All outputs match same original ON hash')
        if with_alias:
            need(len(x['alias']) == 2, 'Raw/WAV alias pair')
            for item in x['alias']:
                alias(item, x['mode'], p[x['mode']])
        else:
            need('alias' not in x, 'Instrumented primary check')
    need(set(r['browser_gate']) == set(KEYS), 'Browser gate coverage')
    for mode, g in r['browser_gate'].items():
        keys(g, 'initialization checks cleanup')
        init(g['initialization'], mode, True)
        clean(g['cleanup'])
        need(len(g['checks']) == 2 and [x['iteration'] for x in g['checks']] == [1, 2], 'Browser repeat gate')
        for x in g['checks']:
            need(x['mode'] == mode and x['phase'] == 'gate', 'Gate labels')
            output(x, True)
    need(len(r['process_sets']) == 3 and [g['id'] for g in r['process_sets']] == [1, 2, 3], 'Fresh sets')
    for g in r['process_sets']:
        keys(g, 'id creation_order runtime warmups checks sentinels cleanup idle')
        need(g['creation_order'] == list(KEYS[g['id'] - 1:] + KEYS[:g['id'] - 1]), 'Rotated process creation order')
        need(set(g['runtime']) == set(KEYS) and set(g['cleanup']) == set(KEYS), 'Set conditions')
        for mode, v in g['runtime'].items():
            init(v, mode)
            identities.append((v['browser_pid'], v['browser_created']))
            clean(g['cleanup'][mode])
        need(len(g['warmups']) == 15 and {(x['mode'], x['iteration']) for x in g['warmups']} == {(m, i) for m in KEYS for i in range(1, 6)}, 'Warmups')
        for x in g['warmups']:
            keys(x, 'mode iteration elapsed_s wav_bytes')
            positive(x)
        need(len(g['sentinels']) == 2 and [x['position'] for x in g['sentinels']] == ['before', 'after'], 'Sentinels')
        for x in g['sentinels']:
            keys(x, 'position elapsed_s wav_bytes')
            positive(x)
        need(len(g['checks']) == 12 and {(x['mode'], x['phase'], x['iteration']) for x in g['checks']} == {(m, q, i) for m in KEYS for q in ['before', 'after'] for i in [1, 2]}, 'Pre/post matrix')
        for x in g['checks']:
            output(x)
        idle = g['idle']
        keys(idle, 'interval_s modes')
        need(math.isfinite(idle['interval_s']) and idle['interval_s'] >= 1 and (set(idle['modes']) == set(KEYS)), 'Idle observation')
        for x in idle['modes'].values():
            keys(x, 'before after cpu_seconds_delta cpu_percent unmatched_processes')
            validate_processes(x['before'])
            validate_processes(x['after'])
            need({k: x[k] for k in ['cpu_seconds_delta', 'cpu_percent', 'unmatched_processes']} == idle_cpu(x['before'], x['after'], idle['interval_s']), 'Idle deltas')
    need(len(set(identities)) == 9 and len(hcs) == 1, 'Process/hardware identity')
    expected = [(x['process_set'], x['pair'], mode) for x in schedule() for mode in x['order']]
    need(len(r['trials']) == 27 and [(x['process_set'], x['pair'], x['mode']) for x in r['trials']] == expected, 'All27 predeclared calls')
    for x in r['trials']:
        keys(x, 'process_set pair mode pair_order elapsed_s wav_bytes before after resource_observations')
        positive(x)
        block = next((z for z in schedule() if (z['process_set'], z['pair']) == (x['process_set'], x['pair'])))
        need(x['pair_order'] == block['order'], 'Order receipt')
        validate_snapshot(x['before'])
        validate_snapshot(x['after'])
        need(x['resource_observations'] == resource_observations(x['before'], x['after']), 'Resource derivation')
        need(min(x['before']['host']['available_bytes'], x['after']['host']['available_bytes']) >= 1024 ** 3, 'Low-memory stop should be incomplete')
    return True

def geometric_mean(values):
    return math.exp(statistics.mean((math.log(x) for x in values)))

def summarize(r):
    validate(r)
    fault_rounds = {(x['process_set'], x['pair']) for x in r['trials'] if x['resource_observations']['target_major_fault_delta'] > 0}
    effects = {}
    for numerator, denominator in [('rebuilt', 'original'), ('candidate', 'original'), ('candidate', 'rebuilt')]:
        pairs = []
        for block in schedule():
            rows = {x['mode']: x for x in r['trials'] if (x['process_set'], x['pair']) == (block['process_set'], block['pair'])}
            pairs.append({'process_set': block['process_set'], 'pair': block['pair'], 'ratio': rows[numerator]['elapsed_s'] / rows[denominator]['elapsed_s'], 'numerator_first': block['order'].index(numerator) < block['order'].index(denominator), 'fault_exposed': (block['process_set'], block['pair']) in fault_rounds})
        effects[numerator + '_vs_' + denominator] = {'pairs': pairs, 'median_ratio': statistics.median((x['ratio'] for x in pairs)), 'geometric_mean_ratio': geometric_mean([x['ratio'] for x in pairs]), 'per_process_set': [{'id': i, 'ratios': [x['ratio'] for x in pairs if x['process_set'] == i], 'geometric_mean_ratio': geometric_mean([x['ratio'] for x in pairs if x['process_set'] == i])} for i in [1, 2, 3]], 'order_sensitivity': [{'numerator_first': first, 'observations': sum((x['numerator_first'] == first for x in pairs)), 'geometric_mean_ratio': geometric_mean([x['ratio'] for x in pairs if x['numerator_first'] == first])} for first in [True, False]], 'fault_sensitivity': [{'fault_exposed': fault, 'observations': sum((x['fault_exposed'] == fault for x in pairs)), 'geometric_mean_ratio': geometric_mean([x['ratio'] for x in pairs if x['fault_exposed'] == fault]) if any((x['fault_exposed'] == fault for x in pairs)) else None} for fault in [False, True]], 'leave_one_process_set_out': [{'omitted_set': i, 'geometric_mean_ratio': geometric_mean([x['ratio'] for x in pairs if x['process_set'] != i])} for i in [1, 2, 3]]}
    result = {'passed': True, 'primary_calls': 27, 'observations_per_condition': 9, 'process_set_clusters': 3, 'design': design(), 'paired_effects': effects, 'sentinel_ratios': [g['sentinels'][1]['elapsed_s'] / g['sentinels'][0]['elapsed_s'] for g in r['process_sets']], 'fault_exposed_calls': sum((x['resource_observations']['fault_exposed'] for x in r['trials'])), 'source_outputs_exact_to_original_on': True, 'scope': 'One bounded ON screen. Three process-set clusters; all observations retained. Sensitivities are descriptive, not additional evidence or a license to select observations.'}

    quality = evaluate_quality(r)
    result['fault_sensitivity_policy'] = 'Common-round exclusion: any of three conditions having a major fault excludes that entire matched round from all secondary comparisons; all primary rows retained.'
    result['fault_sensitivity_excluded_rounds'] = [list(x) for x in sorted(fault_rounds)]
    result['fault_sensitivity_remaining_rounds'] = 9 - len(fault_rounds)
    result.update(passed=quality['passed'], structurally_valid=True, quality=quality, performance_claim_allowed=quality['performance_claim_allowed'], status=quality['status'])
    return result

def main():
    p = argparse.ArgumentParser()
    p.add_argument('input', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    r = json.loads(a.input.read_text())
    s = summarize(r)
    a.output.write_text(json.dumps(s, indent=2) + '\n')
    print('SOURCE_ON_SUMMARY ' + json.dumps(s, separators=(',', ':')))
    print('SOURCE_ON_META ' + json.dumps({k: v for k, v in r.items() if k not in ['trials', 'process_sets', 'browser_gate']}, separators=(',', ':')))
    for mode, value in r['browser_gate'].items():
        print('SOURCE_ON_GATE ' + json.dumps({'mode': mode, **value}, separators=(',', ':')))
    for g in r['process_sets']:
        print('SOURCE_ON_SET ' + json.dumps({k: v for k, v in g.items() if k not in ['warmups', 'checks']}, separators=(',', ':')))
        for x in g['warmups']:
            print('SOURCE_ON_WARMUP ' + json.dumps({'process_set': g['id'], **x}, separators=(',', ':')))
        for x in g['checks']:
            print('SOURCE_ON_CHECK ' + json.dumps({'process_set': g['id'], **x}, separators=(',', ':')))
    for x in r['trials']:
        print('SOURCE_ON_TRIAL ' + json.dumps(x, separators=(',', ':')))
if __name__ == '__main__':
    main()
