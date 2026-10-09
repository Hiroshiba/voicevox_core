"""Synthetic binary fixtures only; no browser, model or private file access."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import native_capture_canary as base
import perf_native_canary as p

CODE = bytes.fromhex('554889e5f30f59c05dc3')
SYMBOL = b'JS:wasm-function[0]-0-turbofan'
PID = 12345


def load_record(code=CODE, name=SYMBOL, ident=0, pid=PID):
    size = 56 + len(name) + 1 + len(code)
    return struct.pack('<IIQII4Q', 0, size, 1, pid, pid, 0x10000000, 0x10000000, len(code), ident) + name + b'\0' + code


def jit(*records):
    return struct.pack('<6I2Q', 0x4A695444, 1, 40, 62, 0xDEADBEEF, PID, 0, 0) + b''.join(records)


def receipt():
    return {'engine': {'product': 'HeadlessChrome/153.0.8010.12', 'js_version': '15.3.76.4'},
            'completed_calls': base.CALLS + 1, 'result_verified': True,
            'browser_version_matches_pin': True, 'child_error': False,
            'synthetic_js_drain': {'functions': p.DRAIN_FUNCTIONS,
                'calls': p.DRAIN_FUNCTIONS * p.DRAIN_CALLS_PER_FUNCTION, 'result_verified': True}}


def evidence():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d); folder = root / 'jit'; folder.mkdir()
        (folder / f'jit-{PID}.dump').write_bytes(jit(load_record()))
        return p.summarize_jit(folder, root)


def report():
    cases = [{'mode': mode, 'normal_tiering': mode == 'normal', 'diagnostic_profiling_enabled': True,
              'flags_without_private_output_path': p.flags(mode), 'child_exit_code': 0,
              'timed_out': False, 'capture_limit_exceeded': False, 'jit_limit_exceeded': False,
              'skipped_after_permission_denial': False, 'child_result': receipt(),
              'capture': p.capture_summary('', ''), 'jit': evidence()} for mode in p.MODES]
    return {'schema': 'inplace-perf-native-canary-v2', 'synthetic_only': True, 'models_loaded': False,
            'completed_primary_calls': 0, 'semantic_verified': False, 'full_capture_released': False,
            'wasm_sha256': base.WASM_SHA, 'source_revision': p.SOURCE_REV, 'playwright_version': '1.63.0',
            'expected_browser_version': base.EXPECTED_BROWSER,
            'status': 'normal_tier_profiled_native_bytes_confirmed', 'cases': cases}


class PerfCanaryTests(unittest.TestCase):
    def test_flags_have_no_kernel_sandbox_or_tiering_override_in_normal(self):
        self.assertEqual(p.flags('normal'), [base.flags('normal')[0] + ',--perf-prof'])
        self.assertNotIn('--no-liftoff', p.flags('normal')[0])
        self.assertEqual(p.flags_for_child('normal', '/tmp/abc'), [p.flags('normal')[0] + ',--perf-prof-path=/tmp/abc'])
        with self.assertRaises(p.ProbeError):
            p.flags_for_child('normal', '/tmp/x,--unsafe')

    def test_exact_code_payload_not_address_header(self):
        parsed = p.parse_jit(jit(load_record()), PID)
        self.assertEqual(parsed['targets'], [{'tier': 'turbofan', 'code': CODE}])
        self.assertEqual(parsed['loads'], 1)
        self.assertNotEqual(parsed['targets'][0]['code'], struct.pack('<Q', 0x10000000))

    def test_actual_objdump_complete_bytes_and_hash(self):
        result = evidence()
        self.assertEqual(result['category'], 'accepted')
        t = result['targets'][0]
        self.assertEqual(t['code_size'], len(CODE))
        self.assertEqual(t['readable_code_bytes'], len(CODE))
        self.assertEqual(t['decoded_bytes'], len(CODE))
        self.assertEqual(t['code_sha256'], hashlib.sha256(CODE).hexdigest())
        self.assertEqual(t['code_sha256'], t['decoder_bytes_sha256'])
        self.assertEqual(t['prefix16_hex'], CODE.hex())
        self.assertEqual(t['undecodable_instruction_count'], 0)

    def test_only_exact_wasm_name_index_tier_matches(self):
        for name in (b'JS:myFunction', b'JS:wasm-function[0]-1-turbofan', b'JS:wasm-function[1]-1-turbofan',
                     b'JS:wasm-function[0]-0-magic', b'Stub:wasm-function[0]-0-turbofan', SYMBOL + b' trailing'):
            self.assertEqual(p.parse_jit(jit(load_record(name=name)), PID)['targets'], [])
        both = p.parse_jit(jit(load_record(name=b'JS:wasm-function[0]-0-liftoff'), load_record(ident=1)), PID)
        self.assertEqual([x['tier'] for x in both['targets']], ['liftoff', 'turbofan'])

    def test_truncated_target_never_claims_complete_code(self):
        full = jit(load_record())
        for cut in (0, 20, 39):
            with self.assertRaises(p.ProbeError): p.parse_jit(full[:cut], PID)
        for cut in (41, len(full)-1):
            parsed = p.parse_jit(full[:cut], PID)
            self.assertFalse(parsed['file_complete'])
            self.assertEqual(parsed['targets'], [])
            self.assertEqual(parsed['records'], 0)
            self.assertIn(parsed['first_failure']['kind'], p.TAIL_KINDS)

    def test_complete_target_before_partial_suffix_retains_exact_evidence(self):
        complete = jit(load_record())
        for suffix in (b'abc', load_record(name=b'JS:unrelated', ident=1)[:-1]):
            parsed = p.parse_jit(complete + suffix, PID)
            self.assertEqual(parsed['targets'], [{'tier': 'turbofan', 'code': CODE}])
            self.assertFalse(parsed['file_complete'])
            self.assertEqual(parsed['validated_prefix_bytes'], len(complete))
            self.assertEqual(parsed['trailing_bytes'], len(suffix))
            self.assertEqual(parsed['first_failure']['complete_records_before_failure'], 1)
            with tempfile.TemporaryDirectory() as d:
                root = Path(d); folder = root/'jit'; folder.mkdir()
                (folder/f'jit-{PID}.dump').write_bytes(complete + suffix)
                proof = p.summarize_jit(folder, root)
            self.assertEqual(proof['category'], 'accepted_complete_targets_with_partial_tail')
            self.assertEqual(proof['targets'][0]['code_sha256'], hashlib.sha256(CODE).hexdigest())
            r = report(); r['cases'][0]['jit'] = proof
            self.assertTrue(p.validate(r)); self.assertTrue(p.ready(r['cases'][0]))
            proof['all_files_complete'] = True
            with self.assertRaises(p.ProbeError): p.validate(r)

    def test_malformed_suffix_is_not_silently_accepted_or_resynchronized(self):
        bad = jit(load_record()) + struct.pack('<IIQ', 99, 16, 0) + load_record(ident=1)
        with self.assertRaises(p.ProbeError) as caught: p.parse_jit(bad, PID)
        self.assertEqual(caught.exception.details['kind'], 'record_event_invalid')
        self.assertEqual(caught.exception.details['complete_records_before_failure'], 1)
        self.assertNotIn('code', caught.exception.details)

    def test_rejects_bad_machine_magic_pid_record_binding(self):
        for off, value in ((0, 0), (4, 2), (8, 48), (12, 183), (20, 1), (40+16, 99)):
            raw = bytearray(jit(load_record())); struct.pack_into('<I', raw, off, value)
            with self.assertRaises(p.ProbeError):
                p.parse_jit(bytes(raw), PID)
        with self.assertRaises(p.ProbeError):
            p.parse_jit(jit(load_record(), load_record()), PID)

    def test_record_size_name_terminator_and_code_size(self):
        raw = bytearray(jit(load_record()))
        for off, form, val in ((44, '<I', 0), (40+40, '<Q', len(CODE)+1)):
            bad = raw.copy(); struct.pack_into(form, bad, off, val)
            with self.assertRaises(p.ProbeError): p.parse_jit(bad, PID)
        bad = raw.copy(); bad[40+56+len(SYMBOL)] = 1
        with self.assertRaises(p.ProbeError): p.parse_jit(bad, PID)

    def test_unknown_and_nonload_records(self):
        close = struct.pack('<IIQ', 3, 16, 0)
        self.assertEqual(p.parse_jit(jit(load_record(), close), PID)['records'], 2)
        with self.assertRaises(p.ProbeError): p.parse_jit(jit(struct.pack('<IIQ', 9, 16, 0)), PID)

    def test_no_target_or_multiple_target_files_not_confirmed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); folder = root/'jit'; folder.mkdir()
            self.assertEqual(p.summarize_jit(folder, root)['category'], 'missing_jit_files')
            (folder/f'jit-{PID}.dump').write_bytes(jit(load_record(name=b'JS:ordinary')))
            self.assertEqual(p.summarize_jit(folder, root)['category'], 'target_missing')
            (folder/f'jit-{PID}.dump').write_bytes(jit(load_record(), load_record(name=b'JS:wasm-function[1]-1-liftoff', ident=1)))
            self.assertEqual(p.summarize_jit(folder, root)['category'], 'ambiguous_binding')

    def test_symlink_and_file_count_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); outside = root/'target'; outside.write_text('PRIVATE')
            folder = root/'jit'; folder.mkdir(); link = folder/'jit-1.dump'; link.symlink_to(outside)
            with self.assertRaises(p.ProbeError): p.inventory(folder)
            link.unlink()
            for i in range(p.MAX_JIT_FILES+1): (folder/f'jit-{i}.dump').write_bytes(b'')
            with self.assertRaises(p.ProbeError): p.inventory(folder)

    def test_header_address_only_classified_without_leak(self):
        raw = 'Instructions (size = 128, 0x12340000-0x12340080)\nInstructions (size = 7)\n'
        c = p.capture_summary('', raw)
        self.assertEqual(c['address_range_only_headers'], 1)
        self.assertEqual(c['decoded_instruction_headers'], 1)
        self.assertNotIn('0x1234', json.dumps(c))

    def test_decoder_gap_wrong_bytes_and_invalid_opcode(self):
        for text in ('0: c3 ret\n2: c3 ret', '0: c2 ret', '0: c3 ret\n0: c3 ret'):
            with self.assertRaises(p.ProbeError): p.decode_output(text, b'\xc3\xc3')
        bad = p.decode_output('0: 0f .byte 0x0f', b'\x0f')
        self.assertEqual(bad['undecodable_instruction_count'], 1)

    def test_schema_scope_and_privacy(self):
        r = report(); self.assertTrue(p.validate(r)); self.assertTrue(p.ready(r['cases'][0]))
        for location, key, value in [('root', 'raw_jit', 'PRIVATE'), ('root', 'models_loaded', True),
                    ('root', 'completed_primary_calls', 1), ('root', 'semantic_verified', True),
                    ('target', 'symbol', 'PRIVATE'), ('target', 'address', 12345), ('case', 'private_path', '/private')]:
            bad = copy.deepcopy(r)
            where = bad if location == 'root' else bad['cases'][0] if location == 'case' else bad['cases'][0]['jit']['targets'][0]
            where[key] = value
            with self.assertRaises(p.ProbeError): p.validate(bad)

    def test_normal_confirmation_requires_turbofan_and_clean_decoder(self):
        for change in ('tier', 'permission', 'decoder', 'engine'):
            c = report()['cases'][0]
            if change == 'tier': c['jit']['targets'][0]['tier'] = 'liftoff'
            if change == 'permission': c['capture']['permission_denied_errors'] = 1
            if change == 'decoder': c['jit']['targets'][0]['undecodable_instruction_count'] = 1
            if change == 'engine': c['child_result']['browser_version_matches_pin'] = False
            self.assertFalse(p.ready(c))

    def test_permission_denial_stops_before_second_browser(self):
        first = report()['cases'][0]
        first['capture']['permission_denied_errors'] = 1
        with patch.object(p, 'run_case', return_value=first) as run:
            cases = p.run_cases()
        run.assert_called_once_with('normal')
        self.assertTrue(cases[1]['skipped_after_permission_denial'])
        self.assertIsNone(cases[1]['child_result'])

    def test_timeout_kills_owned_group_only(self):
        process = Mock(pid=12345, returncode=-9)
        process.communicate.side_effect = [p.subprocess.TimeoutExpired('child', 1, output=b'', stderr=b''), (b'', b'')]
        process.poll.return_value = None
        with patch.object(p.subprocess, 'Popen', return_value=process) as start, \
             patch.object(p.time, 'monotonic', side_effect=[0, 0, 100]), patch.object(p.os, 'killpg') as kill:
            c = p.run_case('normal')
        self.assertTrue(c['timed_out']); self.assertFalse(p.ready(c))
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        kill.assert_called_once_with(12345, p.signal.SIGKILL)

    def test_oversized_stdout_never_leaks(self):
        process = Mock(pid=12345, returncode=0)
        process.communicate.return_value = (b'PRIVATE' * (base.MAX_CAPTURE_BYTES // 7 + 1), b'')
        process.poll.return_value = 0
        with patch.object(p.subprocess, 'Popen', return_value=process): c = p.run_case('normal')
        self.assertTrue(c['capture_limit_exceeded']); self.assertFalse(p.ready(c))
        self.assertNotIn('PRIVATE', json.dumps(c))


if __name__ == '__main__': unittest.main()
