"""Synthetic fixture tests; these do not launch a browser or run the experiment."""
import copy
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

import native_capture_canary as c

BLOCK = '''--- WebAssembly code ---
name: wasm-function[0]
index: 0
kind: wasm function
compiler: Liftoff
Instructions (size = 1)
0x12340000 0 c3 ret
--- End code ---
'''


def child():
    return {'engine': {'product': 'HeadlessChrome/153.0.8010.12', 'js_version': '15.3.201.2'},
            'completed_calls': c.CALLS + 1, 'result_verified': True,
            'browser_version_matches_pin': True, 'child_error': False}


def report():
    cases = [{'mode': mode, 'normal_tiering': mode == 'normal', 'flags': c.flags(mode),
              'child_exit_code': 0, 'timed_out': False, 'capture_limit_exceeded': False,
              'child_result': child(), 'capture': c.summarize_capture(BLOCK, '')} for mode in c.MODES]
    return {'schema': 'inplace-native-capability-canary-v1', 'synthetic_only': True, 'models_loaded': False,
            'completed_primary_calls': 0, 'semantic_verified': False, 'full_capture_released': False,
            'wasm_sha256': c.WASM_SHA, 'function_index': 0, 'extraction_helper_sha256': c.EXTRACTION_SHA256,
            'playwright_version': '1.63.0', 'expected_browser_version': c.EXPECTED_BROWSER,
            'status': 'normal_capture_capability_confirmed', 'cases': cases}


class CanaryTests(unittest.TestCase):
    def test_synthetic_pin_and_flags(self):
        self.assertEqual(len(c.WASM), 41)
        self.assertEqual(hashlib.sha256(c.WASM).hexdigest(), c.WASM_SHA)
        self.assertEqual(c.flags('normal'), c.diagnostic_flags(0))
        self.assertNotIn('--no-liftoff', c.flags('normal')[0])
        self.assertIn('--no-liftoff', c.flags('forced_turbofan_diagnostic')[0])
        for mode in c.MODES:
            self.assertNotIn('--print-wasm-code,', c.flags(mode)[0])
            self.assertIn('--print-wasm-code-function-index=0', c.flags(mode)[0])

    def test_current_frozen_parser_accepts_complete_fixture(self):
        r = c.summarize_capture(BLOCK, '')
        self.assertTrue(r['strict_parser_accepted'])
        self.assertTrue(r['liftoff_observed'])
        self.assertFalse(r['turbofan_observed'])
        self.assertEqual(r['parsed_instruction_bytes'], 1)
        self.assertTrue(c.validate_report(report()))

    def test_playwright_prefix_supported_without_relaxation(self):
        text = '\n'.join('2026-10-09T20:00:00.000Z pw:browser [pid=123][out] ' + line
                         for line in BLOCK.splitlines())
        r = c.summarize_capture('', text)
        self.assertTrue(r['strict_parser_accepted'])
        self.assertEqual(r['target_index_headers'], 1)

    def test_missing_dump_is_not_confirmed(self):
        r = report()
        r['cases'][0]['capture'] = c.summarize_capture('', 'private diagnostic text')
        r['status'] = 'normal_capture_capability_unconfirmed'
        self.assertTrue(c.validate_report(r))
        self.assertEqual(r['cases'][0]['capture']['parser_category'], 'missing_complete_block')
        self.assertNotIn('private diagnostic text', json.dumps(r))

    def test_flag_error_classes_are_bounded(self):
        r = c.summarize_capture('', 'Error: unrecognized flag SECRET\nError: illegal value for flag SECRET')
        self.assertEqual(r['unknown_flag_errors'], 1)
        self.assertEqual(r['illegal_flag_value_errors'], 1)
        self.assertNotIn('SECRET', json.dumps(r))

    def test_wrong_binding_and_incomplete_coverage_rejected(self):
        wrong = c.summarize_capture(BLOCK.replace('index: 0', 'index: 1'), '')
        self.assertEqual(wrong['parser_category'], 'function_binding')
        missing = c.summarize_capture(BLOCK.replace('size = 1', 'size = 2'), '')
        self.assertEqual(missing['parser_category'], 'instruction_coverage')
        incomplete = report()['cases'][0]
        incomplete['capture'] = c.summarize_capture(BLOCK + '--- WebAssembly code ---', '')
        self.assertFalse(c.normal_ready(incomplete))

    def test_schema_rejects_scope_and_raw_text_expansion(self):
        for key, value in [('models_loaded', True), ('completed_primary_calls', 1),
                           ('semantic_verified', True), ('full_capture_released', True),
                           ('raw_stderr', 'PRIVATE')]:
            r = report(); r[key] = value
            with self.assertRaises(ValueError):
                c.validate_report(r)
        r = report(); r['cases'][0]['capture']['raw_text'] = 'PRIVATE'
        with self.assertRaises(ValueError):
            c.validate_report(r)

    def test_child_receipt_whitelist(self):
        valid = c.RESULT_PREFIX + json.dumps(child())
        self.assertEqual(c.child_result(valid), child())
        self.assertIsNone(c.child_result(valid + '\n' + valid))
        r = child(); r['path'] = '/private/model'
        self.assertIsNone(c.child_result(c.RESULT_PREFIX + json.dumps(r)))
        r = child(); r['engine']['product'] = 'PRIVATE'
        self.assertIsNone(c.child_result(c.RESULT_PREFIX + json.dumps(r)))

    def test_no_capability_on_flag_error_or_wrong_engine(self):
        for kind in ('flag', 'engine', 'timeout'):
            r = report()
            case = r['cases'][0]
            if kind == 'flag':
                case['capture']['illegal_flag_value_errors'] = 1
            elif kind == 'engine':
                case['child_result']['browser_version_matches_pin'] = False
            else:
                case['timed_out'] = True
            self.assertFalse(c.normal_ready(case))
            r['status'] = 'normal_capture_capability_unconfirmed'
            self.assertTrue(c.validate_report(r))

    def test_timeout_kills_only_owned_child_group_and_does_not_accept(self):
        process = Mock(pid=12345, returncode=-9)
        process.communicate.side_effect = [c.subprocess.TimeoutExpired('child', 1, output=b'', stderr=b''), (b'', b'')]
        process.poll.return_value = None
        with patch.object(c.subprocess, 'Popen', return_value=process) as start, \
             patch.object(c.time, 'monotonic', side_effect=[0, 0, 100]), \
             patch.object(c.os, 'killpg') as kill:
            case = c.run_case('normal')
        self.assertTrue(case['timed_out'])
        self.assertFalse(c.normal_ready(case))
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        kill.assert_called_once_with(12345, c.signal.SIGKILL)

    def test_oversized_capture_is_counted_without_parsing_or_leaking(self):
        private = b'PRIVATE' * (c.MAX_CAPTURE_BYTES // 7 + 1)
        process = Mock(pid=12345, returncode=0)
        process.communicate.return_value = (private, b'')
        process.poll.return_value = 0
        with patch.object(c.subprocess, 'Popen', return_value=process):
            case = c.run_case('normal')
        self.assertTrue(case['capture_limit_exceeded'])
        self.assertEqual(case['capture']['stdout_bytes'], len(private))
        self.assertFalse(c.normal_ready(case))
        self.assertNotIn('PRIVATE', json.dumps(case))


if __name__ == '__main__':
    unittest.main()
