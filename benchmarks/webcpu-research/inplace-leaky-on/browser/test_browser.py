"""Synthetic contract tests only: no builds, inference, browser or network access."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch, Mock

import source_native
from source_manifest import KEYS, FLAGS, QUERY_SHA, MODEL_SHA, PRISTINE_SHA, ORIGINAL_BODY, CANDIDATE_BODY, SOURCE_REFS
from source_resources import idle_cpu, resource_observations
from source_validate import design, schedule, summarize, validate

H = 'a' * 64
ENGINE = {'product': 'synthetic Chromium', 'js_version': 'synthetic V8'}
RUNTIME = {'shared_memory': True, 'pthreads': 1, 'spin_off': True, 'fixed_length': 962,
           'fixed_matches': 1, 'xnn_threads': 2, 'xnn_sessions': 1, 'configured_ort_global_threads': 1}
FORMAT = {'channels': 1, 'sample_bytes': 2, 'sample_rate': 24000, 'frames': 239104}


def dispatch():
    return {'schema_version': 1, 'source': 'actual_CORE_module', 'context': 'dedicated_worker',
            'expected_dispatch': 'auto', 'actual_dispatch': 'splat', 'worker_hardware_concurrency': 4,
            'probe_hardware_concurrency': 4, 'is_x86': True, 'relaxed_simd': False, 'mr': 4, 'nr': 8,
            'splat_pointers': 12, 'loadsplat_pointers': 0, 'checked_pointers': 12, 'probe_return_code': 0,
            'cross_origin_isolated': True, 'shared_memory': True, 'verified': True}


def host():
    return {'memory_psi': {}, 'available_bytes': 4 * 1024 ** 3, 'total_bytes': 8 * 1024 ** 3,
            'swap_used': 0, 'swap_in': 0, 'swap_out': 0, 'loadavg': [0, 0, 0]}


def processes():
    return [{'pid': 100, 'created': 1, 'rss': 100, 'cpu': {'user': 1, 'system': 1},
             'major_faults': 0, 'minor_faults': 0, 'swap_bytes': 0}]


def cleanup():
    return {'observed_processes': 3, 'remaining_processes': 0, 'confirmed': True}


def alias(mode, pin):
    return {'variant': mode, 'table_slot': pin['callback_table_slot'], 'function_index': pin['callback_function_index'],
            'callbacks': 69, 'elements': 435901440,
            'classes': {'exact_inplace': {'calls': 41, 'elements': 256615424}, 'disjoint': {'calls': 28, 'elements': 179286016}},
            'predicted_branch': {'simd': {'calls': 69 if mode == 'candidate' else 28, 'elements': 435901440 if mode == 'candidate' else 179286016},
                                 'scalar': {'calls': 0 if mode == 'candidate' else 41, 'elements': 0 if mode == 'candidate' else 256615424}},
            'length_histogram': {'492544': 1, '1970176': 17, '7880704': 51}, 'restored': True, 'metadata_errors': 0,
            'scope': 'Synthetic fixture for validator coverage; not execution evidence.'}


def output(mode, phase, iteration, pin, gate=False):
    row = {'mode': mode, 'phase': phase, 'iteration': iteration, 'wav_bytes': 478252, 'raw_bytes': 956416,
           'wav_sha256': H, 'raw_sha256': H, 'pcm_sha256': H, 'exact_bytes': True, 'finite': True,
           'fp32_exact': True, 'pcm_exact': True, 'fp32_samples': 239104, 'wav_format': FORMAT.copy()}
    if gate:
        row['alias'] = [alias(mode, pin), alias(mode, pin)]
    return row


def initialization(pin, pid, gate=False):
    return {'ready': True, 'runtime': RUNTIME.copy(), 'dispatch': dispatch(),
            'wasm_sha256': pin['wasm_sha256'], 'js_sha256': pin['js_sha256'],
            'timing_instrumentation': 'alias_gate_only' if gate else 'clock_only',
            'engine': ENGINE.copy(), 'browser_pid': pid, 'browser_created': pid + 0.1, 'launch_flags': FLAGS.copy()}


def native_receipt(mode, pin):
    info = initialization(pin, 10)
    for name in ['engine', 'browser_pid', 'browser_created', 'launch_flags']:
        del info[name]
    checks = []
    for index in [1, 2]:
        checks.append({'iteration': index, 'raw_sha256': H, 'wav_sha256': H, 'pcm_sha256': H,
                       'finite': True, 'wav_bytes': 478252, 'raw_bytes': 956416, 'fp32_samples': 239104, 'wav_format': FORMAT.copy()})
    return {'schema': 'inplace-leaky-browser-native-v1', 'variant': mode, 'normal_tiering': True,
            'launch_flags': source_native.diagnostic_flags(pin['callback_function_index']), 'dump_sha256': H, 'dump_bytes': 100,
            'diagnostic_exit_code': 0, 'verified': True,
            **{name: pin[name] for name in ['wasm_sha256', 'js_sha256', 'callback_body_sha256', 'callback_function_index', 'callback_table_slot']},
            'initialization': info, 'engine': ENGINE.copy(), 'cleanup': cleanup(),
            'diagnostic_calls': [{'iteration': i, 'elapsed_s': 1, 'wav_bytes': 478252} for i in [1, 2, 3]],
            'checks': checks, 'proof': {'synthetic_native_proof': True}, 'native_proof_helper_sha256': source_native.sha(Path(source_native.proof_module().__file__).read_bytes())}


def fixture():
    provenance = {}
    for index, mode in enumerate(KEYS, 1):
        archive = PRISTINE_SHA if mode == 'original' else str(index) * 64
        provenance[mode] = {'receipt_sha256': H, 'archive_sha256': archive, 'wasm_sha256': str(index) * 64, 'js_sha256': H,
                            'callback_body_sha256': CANDIDATE_BODY if mode == 'candidate' else ORIGINAL_BODY,
                            'callback_table_slot': 100, 'callback_function_index': 101,
                            'build_toolchain': {'core': '9b539761f3e152b966e08c2de0784129fe8cf68d', 'rust': '1.96.0', 'emscripten': '4.0.8', 'optimization': 'z', 'graph_level': 1},
                            'native_bundle': {'verified': True, 'runtime_sha256': archive, 'dispatcher_sha256': H,
                                              'invalidated_packages': ['voicevox_core', 'voicevox_benchmark'], 'activation_occurrence': 1,
                                              'all_ordered_native_members_verified': 1438, 'activation_member_sha256': H, 'duplicate_activation_sha256': H}}
    source_proof = {'source_refs': SOURCE_REFS.copy(), 'preparation_hashes': {k: H for k in ['preparer_sha256', 'builder_source_sha256', 'source_pins_sha256', 'source_proof_helper_sha256', 'callback_discovery_sha256']},
                    'original_control': {'passed': True, 'function_definition_count': 372, 'all_function_definitions_exact_IR_equal_without_normalization': True,
                                         'complete_IR_equal_after_declared_normalization': True, 'normalized_IR_sha256': '1ddac01abf505708e17ad0f22cafcbc178e428cf1c170b9bb89ffff4110f3fcc',
                                         'all_defined_symbols_equal': True, 'original_object_sha256': H, 'rebuilt_object_sha256': H},
                    'candidate_scope': {'passed': True, 'definition_count': 372,
                                        'counts': {'raw_IR_identical': 295, 'metadata_ID_renumbering_only': 73, 'diagnostic_line_only': 2, 'intended_LeakyRelu_exact_alias_change': 2},
                                        'metadata_nodes_matched': 100, 'metadata_mapping_bijective': True, 'metadata_edges_recursively_equal': True,
                                        'original_attribute_definitions_identical': True, 'normalized_IR_sha256': 'e3679458624f93875f2eb22c555f73a7b93634f67c1bbdca40c94bd4224abca7',
                                        'defined_symbols_equal': True, 'candidate_object_sha256': H},
                    'rebuilt_dependency_count': 1291, 'candidate_dependency_count': 1292, 'all_element_segments_identical': True, 'manifest_sha256': H}
    report = {'schema': 'inplace-leaky-on-confirmation-v1', 'status': 'complete', 'capture_only': False, 'stage': 'complete', 'normal_tiering': True,
              'browser_flags': FLAGS.copy(), 'private_temporary_directory_deleted': True, 'query_sha256': QUERY_SHA, 'model_sha256': MODEL_SHA,
              'schedule': schedule(), 'warmups_per_browser': 5, 'design': design(), 'gates_complete_before_timing': True,
              'activation': {}, 'provenance': provenance, 'native_gate': {},
              'reference': {'mode': 'original', 'phase': 'gate', 'iteration': 1, 'raw_sha256': H, 'pcm_sha256': H, 'wav_sha256': H},
              'source_proof': source_proof, 'environment': {'cpu': 'synthetic', 'available_logical_cpus': 4, 'affinity_logical_cpus': 4},
              'initial_host': host(), 'source_hashes': {'source_worker.js': H}, 'worker_source_sha256': {'primary': H, 'untimed_alias': 'b' * 64},
              'browser_gate': {}, 'process_sets': [], 'trials': [], 'sampling_policy': 'synthetic fixture'}
    for mode, pin in provenance.items():
        report['activation'][mode] = {kind: {**ENGINE, 'transformed_groups': 1 if kind == 'on' else 0, 'revectorizable_nodes': 1 if kind == 'on' else 0,
                                           'flag_rejected': False, 'launch_configuration_matches': True, 'trace_sha256': H} for kind in ['off', 'on']}
        report['activation'][mode].update(wasm_sha256=pin['wasm_sha256'], js_sha256=pin['js_sha256'], verified=True)
        report['native_gate'][mode] = native_receipt(mode, pin)
        report['browser_gate'][mode] = {'initialization': initialization(pin, 10, True), 'cleanup': cleanup(),
                                        'checks': [output(mode, 'gate', i, pin, True) for i in [1, 2]]}
    for set_id in [1, 2, 3]:
        order = list(KEYS[set_id - 1:] + KEYS[:set_id - 1])
        group = {'id': set_id, 'creation_order': order,
                 'runtime': {m: initialization(provenance[m], 100 * set_id + i) for i, m in enumerate(KEYS)},
                 'cleanup': {m: cleanup() for m in KEYS},
                 'warmups': [{'mode': m, 'iteration': i, 'elapsed_s': 1, 'wav_bytes': 478252} for m in order for i in range(1, 6)],
                 'sentinels': [{'position': p, 'elapsed_s': 1, 'wav_bytes': 478252} for p in ['before', 'after']],
                 'checks': [output(m, p, i, provenance[m]) for p in ['before', 'after'] for m in KEYS for i in [1, 2]],
                 'idle': {'interval_s': 1, 'modes': {m: {'before': processes(), 'after': processes(), **idle_cpu(processes(), processes(), 1)} for m in KEYS}}}
        report['process_sets'].append(group)
    for block in schedule():
        for mode in block['order']:
            snapshot = {'host': host(), 'processes': processes()}
            report['trials'].append({'process_set': block['process_set'], 'pair': block['pair'], 'mode': mode, 'pair_order': block['order'].copy(),
                                     'elapsed_s': {'original': 10, 'rebuilt': 11, 'candidate': 9}[mode], 'wav_bytes': 478252,
                                     'before': copy.deepcopy(snapshot), 'after': copy.deepcopy(snapshot), 'resource_observations': resource_observations(snapshot, snapshot)})
    return report


class SyntheticProofParser:
    __file__ = __file__
    """Parser unit tests live beside the real parser; these tests cover its caller."""
    @staticmethod
    def validate_native_proof(proof, mode, index):
        if proof != {'synthetic_native_proof': True}:
            raise ValueError('Synthetic native proof mismatch')


class BrowserContracts(unittest.TestCase):
    def setUp(self):
        self.parser_patch = patch.object(source_native, 'proof_module', return_value=SyntheticProofParser)
        self.parser_patch.start()
        self.addCleanup(self.parser_patch.stop)

    def test_complete_fixture_and_paired_summaries(self):
        report = fixture()
        self.assertTrue(validate(report))
        result = summarize(report)
        self.assertEqual(result['primary_calls'], 27)
        self.assertEqual(result['observations_per_condition'], 9)
        self.assertAlmostEqual(result['paired_effects']['candidate_vs_original']['geometric_mean_ratio'], .9)
        self.assertAlmostEqual(result['paired_effects']['candidate_vs_rebuilt']['geometric_mean_ratio'], 9 / 11)
        self.assertEqual(len(result['paired_effects']['candidate_vs_rebuilt']['pairs']), 9)

    def test_schedule_position_and_pair_order_balance(self):
        expected = [['rebuilt', 'original', 'candidate'], ['rebuilt', 'candidate', 'original'], ['candidate', 'rebuilt', 'original'],
                    ['candidate', 'original', 'rebuilt'], ['original', 'candidate', 'rebuilt'], ['rebuilt', 'candidate', 'original'],
                    ['candidate', 'original', 'rebuilt'], ['original', 'rebuilt', 'candidate'], ['original', 'rebuilt', 'candidate']]
        self.assertEqual([x['order'] for x in schedule()], expected)
        self.assertTrue(all(x == [3, 3, 3] for x in design()['position_counts'].values()))
        self.assertEqual(set(design()['pair_order_counts'].values()), {4, 5})

    def test_mutations_are_rejected(self):
        def set_path(report, path, value):
            current = report
            for key in path[:-1]:
                current = current[key]
            current[path[-1]] = value
        cases = [
            (['status'], 'running'), (['browser_flags'], ['--js-flags=--no-liftoff,--wasm-revectorize']),
            (['gates_complete_before_timing'], False), (['reference', 'mode'], 'candidate'),
            (['activation', 'candidate', 'on', 'transformed_groups'], 0),
            (['activation', 'rebuilt', 'on', 'revectorizable_nodes'], 0),
            (['activation', 'original', 'off', 'transformed_groups'], 1),
            (['activation', 'candidate', 'wasm_sha256'], H),
            (['activation', 'rebuilt', 'on', 'js_version'], 'different'),
            (['native_gate', 'candidate', 'callback_function_index'], 555),
            (['native_gate', 'candidate', 'callback_body_sha256'], ORIGINAL_BODY),
            (['native_gate', 'candidate', 'wasm_sha256'], H),
            (['native_gate', 'candidate', 'proof'], {}),
            (['native_gate', 'rebuilt', 'checks', 1, 'raw_sha256'], 'b' * 64),
            (['native_gate', 'original', 'normal_tiering'], False),
            (['native_gate', 'original', 'launch_flags'], ['--js-flags=--wasm-revectorize,--print-wasm-code,--print-wasm-code-function-index=101']),
            (['provenance', 'original', 'archive_sha256'], H),
            (['provenance', 'original', 'native_bundle', 'all_ordered_native_members_verified'], 1),
            (['browser_gate', 'original', 'initialization', 'dispatch', 'worker_hardware_concurrency'], 8),
            (['browser_gate', 'candidate', 'checks', 0, 'alias', 0, 'callbacks'], 68),
            (['browser_gate', 'candidate', 'checks', 0, 'alias', 0, 'restored'], False),
            (['browser_gate', 'candidate', 'checks', 1, 'pcm_sha256'], 'b' * 64),
            (['process_sets', 0, 'runtime', 'candidate', 'timing_instrumentation'], 'alias_gate_only'),
            (['process_sets', 0, 'runtime', 'rebuilt', 'dispatch', 'splat_pointers'], 11),
            (['process_sets', 0, 'checks', 0, 'raw_sha256'], 'b' * 64),
            (['process_sets', 0, 'checks', 0, 'fp32_exact'], False),
            (['process_sets', 0, 'sentinels', 0, 'elapsed_s'], 0),
            (['process_sets', 1, 'creation_order'], list(KEYS)),
            (['process_sets', 0, 'idle', 'interval_s'], .5),
            (['trials', 0, 'elapsed_s'], float('nan')),
            (['trials', 0, 'before', 'host', 'available_bytes'], 100),
            (['trials', 0, 'resource_observations', 'target_major_fault_delta'], 1),
            (['private_temporary_directory_deleted'], False),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                report = fixture()
                set_path(report, path, value)
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    validate(report)
        for remove in ['trials', 'native_gate', 'activation']:
            with self.subTest(remove=remove):
                report = fixture()
                if remove == 'trials': report[remove].pop()
                else: del report[remove]['candidate']
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    validate(report)

    def test_fault_rows_retained_and_described(self):
        report = fixture()
        row = report['trials'][0]
        row['after']['processes'][0]['major_faults'] = 1
        row['resource_observations'] = resource_observations(row['before'], row['after'])
        result = summarize(report)
        self.assertEqual(result['primary_calls'], 27)
        self.assertEqual(result['fault_exposed_calls'], 1)
        self.assertEqual(sum(x['observations'] for x in result['paired_effects']['candidate_vs_rebuilt']['fault_sensitivity']), 9)

    def test_native_dump_flag_targets_only_rediscovered_function(self):
        self.assertEqual(source_native.diagnostic_flags(8287),
                         ['--js-flags=--wasm-revectorize,--print-wasm-code-function-index=8287'])
        self.assertNotIn('--print-wasm-code', source_native.diagnostic_flags(8287)[0].split(','))
        self.assertEqual(source_native.MAX_NATIVE_TRACE_BYTES, 64 * 1024 * 1024)

    def test_trace_limit_checks_partial_and_final_output_and_cleans_up(self):
        for first in [('a' * 64, ''), source_native.subprocess.TimeoutExpired(['synthetic-command'], 1, output=b'a' * 64, stderr=b'')]:
            with self.subTest(partial=isinstance(first, BaseException)):
                process = Mock()
                process.pid = 702
                process.returncode = -15
                process.communicate.side_effect = [first, ('a' * 64, '')]
                root = Mock()
                root.pid = 702
                root.create_time.return_value = 1
                root.children.return_value = []
                root.is_running.return_value = True
                root.status.return_value = 'running'
                with patch.object(source_native, 'MAX_NATIVE_TRACE_BYTES', 64), \
                     patch.object(source_native.subprocess, 'Popen', return_value=process), \
                     patch.object(source_native.psutil, 'Process', return_value=root), \
                     patch.object(source_native.psutil, 'wait_procs', return_value=([], [])):
                    with self.assertRaises(source_native.NativeCaptureError) as raised:
                        source_native.capture_child(['synthetic-command'], {})
                self.assertEqual(raised.exception.category, 'diagnostic_trace_limit')
                self.assertTrue(raised.exception.cleanup['confirmed'])
                self.assertEqual(raised.exception.captured, 'a' * 64 + '\n')
                root.terminate.assert_called_once()

    def test_capture_interruption_cleans_observed_tree(self):
        process = Mock()
        process.pid = 700
        process.returncode = -15
        process.communicate.side_effect = [KeyboardInterrupt(), ('private stdout', 'private stderr')]
        root = Mock()
        root.pid = 700
        root.create_time.return_value = 1
        root.children.return_value = []
        root.is_running.return_value = True
        root.status.return_value = 'running'
        with patch.object(source_native.subprocess, 'Popen', return_value=process), \
             patch.object(source_native.psutil, 'Process', return_value=root), \
             patch.object(source_native.psutil, 'wait_procs', return_value=([], [])):
            with self.assertRaises(source_native.NativeCaptureError) as raised:
                source_native.capture_child(['synthetic-command'], {})
        error = raised.exception
        self.assertEqual(error.category, 'diagnostic_interrupted')
        self.assertTrue(error.cleanup['confirmed'])
        root.terminate.assert_called_once()
        self.assertIn('private stdout', error.captured)

    def test_capture_other_exception_cleans_observed_tree(self):
        process = Mock()
        process.pid = 701
        process.returncode = -15
        process.communicate.side_effect = [OSError('private exception payload'), ('', '')]
        root = Mock()
        root.pid = 701
        root.create_time.return_value = 1
        root.children.return_value = []
        root.is_running.return_value = True
        root.status.return_value = 'running'
        with patch.object(source_native.subprocess, 'Popen', return_value=process), \
             patch.object(source_native.psutil, 'Process', return_value=root), \
             patch.object(source_native.psutil, 'wait_procs', return_value=([], [])):
            with self.assertRaises(source_native.NativeCaptureError) as raised:
                source_native.capture_child(['synthetic-command'], {})
        self.assertEqual(raised.exception.category, 'diagnostic_communication_failure')
        self.assertTrue(raised.exception.cleanup['confirmed'])
        self.assertNotIn('private exception payload', str(raised.exception))
        root.terminate.assert_called_once()

    def test_failure_receipt_is_bounded(self):
        report = fixture()
        record = report['native_gate']['candidate']
        record['untrusted_dump'] = 'private payload'
        record['incomplete_proof'] = {'untrusted': 'private payload'}
        bounded = source_native.failure_receipt(record, 'native_cfg_proof', 'unsupported_lowering', 'ValueError')
        self.assertNotIn('private payload', json.dumps(bounded))
        self.assertEqual(set(bounded), {'stage', 'category', 'error_type', 'variant', 'wasm_sha256', 'js_sha256',
                                      'callback_body_sha256', 'dump_sha256', 'callback_function_index', 'callback_table_slot', 'dump_bytes'})
        for field, value in [('variant', 'secret mode'), ('wasm_sha256', 'private payload'), ('dump_bytes', -1)]:
            corrupt = copy.deepcopy(record)
            corrupt[field] = value
            with self.assertRaises(ValueError):
                source_native.failure_receipt(corrupt, 'native_cfg_proof', 'unsupported_lowering', 'ValueError')
        with self.assertRaises(ValueError):
            source_native.failure_receipt(record, 'native_cfg_proof', 'private payload', 'ValueError')
        with self.assertRaises(ValueError):
            source_native.failure_receipt(record, 'native_cfg_proof', 'unsupported_lowering', 'Exception: private payload')

    def test_native_failure_operand_redaction(self):
        self.assertEqual(source_native.safe_operand('[r15+rax*1+0x8]'), '[r15+rax*1+0x8]')
        self.assertEqual(source_native.safe_operand('0x7fffffff1234'), 'IMM')
        self.assertEqual(source_native.safe_operand('private_customer_name'), '<unsupported>')
        self.assertEqual(source_native.safe_operand('/private/path'), '<unsupported>')
        self.assertEqual(source_native.safe_operand('<external>'), '<external>')
        invalid = {'parsed': True, 'blocks': [], 'omitted_blocks': 0,
                   'instruction_context': [{'offset': 0, 'mnemonic': 'movq', 'operands': ['private_customer_name'], 'target': None}]}
        with self.assertRaises(ValueError):
            source_native.validate_native_observations(invalid)

    def test_real_native_parser_receipt_integration(self):
        self.parser_patch.stop()
        parser = source_native.proof_module()
        import test_source_native_proof as native_fixture
        report = fixture()
        for mode in KEYS:
            pin = report['provenance'][mode]
            dump = native_fixture.dump(native_fixture.fixture(mode), index=pin['callback_function_index'])
            report['native_gate'][mode]['proof'] = parser.parse_native_dump(dump, pin['callback_function_index'], mode)
        self.assertTrue(validate(json.loads(json.dumps(report))))
        observations = source_native.native_observations(parser, dump, pin['callback_function_index'])
        self.assertTrue(observations['parsed'])
        self.assertEqual(observations['blocks'][0]['packed_multiply_count'], 1)
        record = report['native_gate']['candidate']
        record['native_failure'] = parser.native_failure_evidence(parser.NativeProofError('Normal-tier trace lacks optimized callback'))
        record['native_observations'] = observations
        bounded = source_native.failure_receipt(record, 'native_cfg_proof', 'unsupported_lowering', 'NativeProofError')
        self.assertEqual(bounded['native_failure']['category'], 'optimized_tier_not_observed')
        self.assertNotIn('instructions', json.dumps(bounded))

    def test_primary_worker_contains_no_injected_alias_hooks(self):
        worker = Path(__file__).with_name('source_worker.js').read_text()
        self.assertNotIn('installLeakyAliasCheck(', worker)
        self.assertNotIn("importScripts('/source_alias_check.js')", worker)
        self.assertNotIn('no-liftoff', worker)
        self.assertNotIn('loadsplat\');', worker)
        self.assertIn("verifyActualCoreDispatch(Module,stdout,'auto')", worker)


if __name__ == '__main__':
    unittest.main()
