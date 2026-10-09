#!/usr/bin/env python3
"""Validate profiled JIT capture evidence; never authorize timing or semantics."""
import argparse
import json
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
BROWSER = ROOT.parent / 'browser'
sys.path.insert(0, str(BROWSER))
from source_manifest import KEYS, FLAGS, QUERY_SHA, MODEL_SHA, PRISTINE_SHA, ORIGINAL_BODY, CANDIDATE_BODY, SOURCE_REFS, need, sha
from source_resources import validate_host
from source_validate import keys, digest, schedule, design, alias, XNN_RUNTIME
from source_capture import canonical_hash

def validate(r, *, capture_only=True):
    need(capture_only is True, 'JIT validation is capture-only')
    keys(r, 'schema capture_only status stage schedule normal_tiering browser_flags warmups_per_browser provenance source_proof environment source_hashes model_sha256 query_sha256 browser_gate process_sets trials sampling_policy distribution worker_source_sha256 activation initial_host native_gate reference gates_complete_before_timing design private_temporary_directory_deleted private_native_dump_retained native_capture_backend jit_capture_source_hashes')
    need(r['capture_only'] is capture_only and r['schema'] == 'inplace-leaky-on-jit-capture-v1' and r['status'] == 'awaiting_manual_review' and r['stage'] == r['status'] and r['normal_tiering'] and r['browser_flags'] == FLAGS, 'Completion/configuration')
    need(r['private_native_dump_retained'] is False and r['private_temporary_directory_deleted'] is True,
         'Private JIT and model work directories deleted before publication')
    need(r['native_capture_backend'] == 'profiled_jitdump_module_audit_v1', 'Explicit versioned backend')
    current = {x.name: sha(x.read_bytes()) for x in ROOT.iterdir() if x.is_file() and x.suffix in ['.py', '.js']}
    need(r['jit_capture_source_hashes'] == current, 'Reviewed JIT source identities')
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
    need(engine['product'] in ('HeadlessChrome/153.0.8010.12', 'Chrome/153.0.8010.12')
         and engine['js_version'] == '15.3.76.4', 'Canary-confirmed pinned Chrome/V8 engine')
    p = r['provenance']
    need(set(p) == set(KEYS), 'Three provenance records')
    need(len({(v['wasm_sha256'], v['js_sha256']) for v in p.values()}) == 3, 'Distinct source binaries')
    for mode in KEYS:
        need(a[mode]['wasm_sha256'] == p[mode]['wasm_sha256'] and a[mode]['js_sha256'] == p[mode]['js_sha256'], 'Per-binary activation identity')
    from jit_native import validate_native_gates
    reference = r['reference']
    keys(reference, 'mode phase iteration raw_sha256 pcm_sha256 wav_sha256')
    need(reference['mode'] == 'original' and reference['phase'] == 'gate' and (reference['iteration'] == 1), 'Original ON reference')
    for field in ['raw_sha256', 'pcm_sha256', 'wav_sha256']:
        digest(reference[field])
    validate_native_gates(r['native_gate'], p, engine, reference, capture_only=capture_only)
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
    need(r['trials'] == [] and r['process_sets'] == [], 'Capture-only phase cannot contain primary calls or process sets')
    return True


def emit_evidence(evidence):
    from jit_transport import validate_evidence
    validate_evidence(evidence, {'callback_function_index': evidence['function_index'],
        'wasm_sha256': evidence['wasm_sha256'], 'callback_body_sha256': evidence['callback_body_sha256'],
        'callback_table_slot': evidence['callback_table_slot']}, evidence['variant'])
    identity = canonical_hash(evidence)
    header = {k: v for k, v in evidence.items() if k != 'blocks'}
    header.update(evidence_sha256=identity, blocks=[{k: v for k, v in b.items() if k != 'instructions'} for b in evidence['blocks']])
    print('SOURCE_ON_JIT_NATIVE_META ' + json.dumps(header, separators=(',', ':')), flush=True)
    for block_id, block in enumerate(evidence['blocks']):
        rows = block['instructions']; chunks = [rows[i:i + 8] for i in range(0, len(rows), 8)]
        for sequence, chunk in enumerate(chunks, 1):
            item = {'variant': evidence['variant'], 'function_index': evidence['function_index'],
                'evidence_sha256': identity, 'block': block_id, 'sequence': sequence,
                'total_chunks': len(chunks), 'instructions': chunk}
            text = json.dumps(item, separators=(',', ':'))
            need(len(text.encode()) <= 8192, 'Bounded evidence log chunks')
            print('SOURCE_ON_JIT_NATIVE_CODE ' + text, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.input.read_text()); validate(report)
    summary = {'schema': 'inplace-leaky-on-jit-capture-summary-v1', 'status': 'awaiting_manual_review',
        'capture_complete': True, 'semantic_verified': False, 'primary_calls': 0, 'process_sets': 0,
        'future_primary_schedule_active': False, 'diagnostic_profiled': True,
        'uninstrumented_code_identity_claimed': False, 'private_jit_deleted': True,
        'capture_report_sha256': sha(args.input.read_bytes()),
        'native_evidence_sha256': {k: canonical_hash(v['code_evidence']) for k, v in report['native_gate'].items()},
        'optimized_target_observed': {k: v['code_evidence']['optimized_target_observed'] for k, v in report['native_gate'].items()}}
    args.output.write_text(json.dumps(summary, indent=2) + '\n')
    print('SOURCE_ON_JIT_CAPTURE_SUMMARY ' + json.dumps(summary, separators=(',', ':')), flush=True)
    print('SOURCE_ON_JIT_CAPTURE_META ' + json.dumps({k: v for k, v in report.items() if k not in ('native_gate', 'browser_gate')}, separators=(',', ':')), flush=True)
    for variant, row in report['browser_gate'].items():
        print('SOURCE_ON_JIT_CAPTURE_ALIAS ' + json.dumps({'variant': variant, **row}, separators=(',', ':')), flush=True)
    for variant, row in report['native_gate'].items():
        print('SOURCE_ON_JIT_CAPTURE_BINDING ' + json.dumps({k: v for k, v in row.items() if k != 'code_evidence'}, separators=(',', ':')), flush=True)
        emit_evidence(row['code_evidence'])


if __name__ == '__main__': main()
