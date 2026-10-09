#!/usr/bin/env python3
"""Bounded synthetic JITdump capability probe, never a timing/semantic gate.

Pinned source contract (V8 0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7):
  src/diagnostics/perf-jit.cc: 40-byte PerfJitHeader, 16-byte record header,
    56-byte JIT_CODE_LOAD prefix, NUL symbol, then exactly code_size native bytes.
  src/logging/code-events.h + log.cc: Wasm symbols ALSO use the JS tag, followed
    by the Wasm DebugName, absolute function index, and execution tier.
  src/wasm/wasm-code-manager.cc + wasm-tier.h: this unnamed single-function module
    binds exactly to JS:wasm-function[0]-0-liftoff or ...-turbofan.

No perf recorder, kernel setting, sandbox switch or access-permission change is
used. Private JIT files are bounded, parsed after browser exit, and removed.
Only synthetic target sizes, hashes, a 16-byte prefix and decoder coverage leave
this process; other native code, symbols, addresses, paths and stderr never do.
--perf-prof implies profiling/code-layout changes, so even the normal-tier case
is diagnostic configuration, not the eventual uninstrumented timing setup.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

import native_capture_canary as base

SOURCE_REV = '0b60d2b01800d7ba2c6eeb5e51ecd95f6dab44c7'
MODES = base.MODES
MAX_JIT_BYTES = 64 * 1024 * 1024
MAX_JIT_FILES = 8
MAX_TARGET_BYTES = 64 * 1024
MAX_TARGET_RECORDS = 8
TIMEOUT = 90
TARGET = re.compile(rb'JS:wasm-function\[0\]-0-(liftoff|turbofan)')
WASM_SYMBOL = re.compile(rb'JS:wasm-function\[[0-9]+\]-[0-9]+-(?:liftoff|turbofan)')
CATEGORIES = {'accepted', 'accepted_complete_targets_with_partial_tail', 'missing_jit_files', 'file_limit', 'file_shape', 'jit_header',
              'record_shape', 'target_limit', 'target_missing', 'ambiguous_binding',
              'decoder_unavailable', 'decoder_rejection', 'private_io_failure', 'not_attempted'}


class ProbeError(ValueError):
    def __init__(self, category, details=None):
        super().__init__(category)
        self.details = details


TAIL_KINDS = {'truncated_record_header', 'truncated_record_body'}
FAILURE_KINDS = TAIL_KINDS | {'record_event_invalid', 'record_length_invalid', 'load_prefix_short',
    'load_binding_invalid', 'load_duplicate_id', 'load_name_terminator', 'load_code_length'}
RECORD_KINDS = ('code_load', 'code_move', 'debug_info', 'close', 'unwinding_info')
DRAIN_FUNCTIONS = 256
DRAIN_CALLS_PER_FUNCTION = 1024


def check(condition, category):
    if not condition:
        raise ProbeError(category)


def flags(mode, directory=None):
    value = base.flags(mode)[0] + ',--perf-prof'
    if directory is not None:
        check(re.fullmatch(r'[A-Za-z0-9_/.-]+', str(directory)) is not None, 'file_shape')
        value += ',--perf-prof-path=' + str(directory)
    return [value]


def capture_summary(out, err):
    data = base.summarize_capture(out, err)
    text = out + '\n' + err
    data['address_range_only_headers'] = len(re.findall(
        r'Instructions \(size = [0-9]+, 0x[0-9a-fA-F]+-0x[0-9a-fA-F]+\)', text))
    data['decoded_instruction_headers'] = len(re.findall(r'Instructions \(size = [0-9]+\)', text))
    data['permission_denied_errors'] = len(re.findall(r'permission denied|operation not permitted', text, re.I))
    return data


def inventory(directory):
    paths = sorted(directory.iterdir())
    check(len(paths) <= MAX_JIT_FILES, 'file_limit')
    total = 0
    for path in paths:
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and re.fullmatch(r'jit-[0-9]+\.dump', path.name), 'file_shape')
        total += info.st_size
        check(total <= MAX_JIT_BYTES, 'file_limit')
    return paths, total


def parse_jit(data, expected_pid):
    """Parse complete records in order; never resynchronize after an invalid tail.

    A partial final record is separately reported. Earlier full CODE_LOAD bytes
    retain their own complete size/symbol binding. No claim is made about absent
    later records, the entire file, or which tier executed the final call.
    """
    check(40 <= len(data) <= MAX_JIT_BYTES, 'jit_header')
    magic, version, size, machine, reserved, pid, timestamp, flags_value = struct.unpack_from('<6I2Q', data)
    check((magic, version, size, machine, reserved, pid, flags_value) ==
          (0x4A695444, 1, 40, 62, 0xDEADBEEF, expected_pid, 0), 'jit_header')
    offset, records, loads, other_wasm = 40, 0, 0, 0
    targets, ids = [], set()
    event, length = -1, 0

    def details(kind):
        return {'kind': kind, 'offset_bytes': offset, 'available_bytes': len(data) - offset,
                'declared_record_bytes': min(length, MAX_JIT_BYTES),
                'declared_record_exceeds_limit': length > MAX_JIT_BYTES,
                'record_kind': RECORD_KINDS[event] if 0 <= event < len(RECORD_KINDS) else 'unknown',
                'complete_records_before_failure': records}

    def invalid(condition, kind):
        if not condition:
            raise ProbeError('record_shape', details(kind))

    def finish(tail=None):
        return {'records': records, 'loads': loads, 'other_wasm': other_wasm, 'targets': targets,
                'validated_prefix_bytes': offset, 'trailing_bytes': len(data) - offset,
                'file_complete': tail is None, 'first_failure': tail}

    while offset < len(data):
        event, length = -1, 0
        if len(data) - offset < 16:
            return finish(details('truncated_record_header'))
        event, length, stamp = struct.unpack_from('<IIQ', data, offset)
        invalid(event in (0, 1, 2, 3, 4), 'record_event_invalid')
        invalid(16 <= length <= MAX_JIT_BYTES, 'record_length_invalid')
        if length > len(data) - offset:
            return finish(details('truncated_record_body'))
        record = data[offset:offset + length]
        if event == 0:
            invalid(length >= 58, 'load_prefix_short')
            rpid, tid, vma, address, code_size, code_id = struct.unpack_from('<II4Q', record, 16)
            invalid(rpid == pid and vma == address and address > 0 and code_size > 0, 'load_binding_invalid')
            invalid(code_id not in ids, 'load_duplicate_id')
            ids.add(code_id)
            end = record.find(b'\0', 56, min(length, 56 + 4097))
            invalid(end >= 56, 'load_name_terminator')
            invalid(length - end - 1 == code_size, 'load_code_length')
            name, code = record[56:end], record[end + 1:]
            match = TARGET.fullmatch(name)
            if match:
                check(code_size <= MAX_TARGET_BYTES and len(targets) < MAX_TARGET_RECORDS, 'target_limit')
                targets.append({'tier': match.group(1).decode('ascii'), 'code': code})
            elif WASM_SYMBOL.fullmatch(name):
                other_wasm += 1
            loads += 1
        offset += length
        records += 1
    return finish()


def decode_output(text, code):
    """Require objdump's full contiguous displayed bytes to equal native bytes."""
    reconstructed = bytearray()
    instructions = invalid = 0
    for line in text.splitlines():
        match = re.match(r'^\s*([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s+)+)\s*(.*?)\s*$', line)
        if not match:
            continue
        offset, shown, assembly = match.groups()
        check(int(offset, 16) == len(reconstructed), 'decoder_rejection')
        raw = bytes.fromhex(shown)
        check(0 < len(raw) <= 15 and bool(assembly), 'decoder_rejection')
        reconstructed.extend(raw)
        instructions += 1
        invalid += int('(bad)' in assembly or assembly.startswith('.byte'))
    check(bytes(reconstructed) == code and instructions > 0, 'decoder_rejection')
    return {'decoded_bytes': len(reconstructed), 'decoded_instruction_count': instructions,
            'undecodable_instruction_count': invalid, 'decoder_complete_byte_coverage': True,
            'decoder_bytes_sha256': hashlib.sha256(reconstructed).hexdigest()}


def decode(code, directory):
    path = directory / 'target-native.bin'
    path.write_bytes(code)
    try:
        try:
            proc = subprocess.run(['objdump', '-D', '-z', '-w', '--insn-width=16', '-b', 'binary',
                                   '-m', 'i386:x86-64', str(path)], capture_output=True, timeout=10,
                                  env=dict(os.environ, LC_ALL='C'))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            raise ProbeError('decoder_unavailable') from None
        check(proc.returncode == 0 and len(proc.stdout) <= 4 * 1024 * 1024, 'decoder_rejection')
        return decode_output(proc.stdout.decode('ascii', 'replace'), code)
    finally:
        path.unlink(missing_ok=True)


def empty_jit(category):
    return {'category': category, 'files': 0, 'readable_bytes': 0, 'records': 0, 'code_load_records': 0,
            'other_wasm_records': 0, 'target_files': 0, 'targets': [], 'validated_prefix_bytes': 0,
            'trailing_bytes': 0, 'partial_files': 0, 'all_files_complete': False, 'first_failure': None}


def summarize_jit(directory, scratch):
    result = empty_jit('not_attempted')
    try:
        paths, total = inventory(directory)
        result.update(files=len(paths), readable_bytes=total)
        check(bool(paths), 'missing_jit_files')
        result['all_files_complete'] = True
        for path in paths:
            data = path.read_bytes()
            check(len(data) == path.stat().st_size, 'file_shape')
            parsed = parse_jit(data, int(path.stem.split('-')[1]))
            result['records'] += parsed['records']
            result['validated_prefix_bytes'] += parsed['validated_prefix_bytes']
            result['trailing_bytes'] += parsed['trailing_bytes']
            result['partial_files'] += int(not parsed['file_complete'])
            result['all_files_complete'] &= parsed['file_complete']
            if result['first_failure'] is None:
                result['first_failure'] = parsed['first_failure']
            result['code_load_records'] += parsed['loads']
            result['other_wasm_records'] += parsed['other_wasm']
            result['target_files'] += int(bool(parsed['targets']))
            for target in parsed['targets']:
                code = target['code']
                check(len(result['targets']) < MAX_TARGET_RECORDS, 'target_limit')
                decoded = decode(code, scratch)
                result['targets'].append({'function_index': 0, 'tier': target['tier'],
                    'exact_wasm_symbol_binding': True, 'code_size': len(code), 'readable_code_bytes': len(code),
                    'code_sha256': hashlib.sha256(code).hexdigest(), 'prefix16_hex': code[:16].hex(), **decoded})
        check(bool(result['targets']), 'target_missing')
        check(result['target_files'] == 1 and result['other_wasm_records'] == 0, 'ambiguous_binding')
        result['category'] = 'accepted' if result['all_files_complete'] else 'accepted_complete_targets_with_partial_tail'
    except ProbeError as error:
        result['category'] = str(error)
        if result['validated_prefix_bytes'] + result['trailing_bytes'] != result['readable_bytes']:
            result['all_files_complete'] = False
        if result['first_failure'] is None:
            result['first_failure'] = error.details
    except OSError:
        result['category'] = 'private_io_failure'
        result['all_files_complete'] = False
    return result


def storage_bounds(directory):
    paths, total = inventory(directory)
    # V8's implied --log may also create ordinary isolate logs in this private cwd.
    extras = [p for p in directory.parent.iterdir() if p != directory]
    check(len(extras) <= 32, 'file_limit')
    for path in extras:
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode), 'file_shape')
        total += info.st_size
    check(total <= MAX_JIT_BYTES, 'file_limit')


def run_case(mode):
    with tempfile.TemporaryDirectory(prefix='v8-perf-canary-') as temp:
        root = Path(temp)
        directory = root / 'jit'
        directory.mkdir()
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', mode,
                                    '--private-directory', str(directory)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   env=dict(os.environ, DEBUG='pw:browser'), cwd=root)
        out = err = b''
        timed_out = stream_limit = file_limit = False
        deadline = time.monotonic() + TIMEOUT
        try:
            while True:
                try:
                    out, err = process.communicate(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired as error:
                    out, err = error.output or b'', error.stderr or b''
                    stream_limit = len(out) + len(err) > base.MAX_CAPTURE_BYTES
                    timed_out = time.monotonic() >= deadline
                    try:
                        storage_bounds(directory)
                    except (ProbeError, OSError):
                        file_limit = True
                    if stream_limit or timed_out or file_limit:
                        break
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)  # Exact group created above.
            out, err = process.communicate(timeout=10)
        stream_limit |= len(out) + len(err) > base.MAX_CAPTURE_BYTES
        try:
            storage_bounds(directory)
        except (ProbeError, OSError):
            file_limit = True
        sizes = len(out), len(err)
        if stream_limit:
            out = err = b''
        stdout, stderr = out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')
        capture = capture_summary(stdout, stderr)
        capture['stdout_bytes'], capture['stderr_bytes'] = sizes
        jit = empty_jit('not_attempted') if (timed_out or stream_limit or file_limit) else summarize_jit(directory, root)
        return {'mode': mode, 'normal_tiering': mode == 'normal', 'diagnostic_profiling_enabled': True,
                'flags_without_private_output_path': flags(mode), 'child_exit_code': process.returncode,
                'timed_out': timed_out, 'capture_limit_exceeded': stream_limit, 'jit_limit_exceeded': file_limit, 'skipped_after_permission_denial': False,
                'child_result': child_result(stdout), 'capture': capture, 'jit': jit}


def ready(case):
    receipt, cap, jit = case['child_result'], case['capture'], case['jit']
    return bool(case['child_exit_code'] == 0 and not case['timed_out'] and not case['capture_limit_exceeded']
        and not case['jit_limit_exceeded'] and receipt and receipt['result_verified']
        and receipt['browser_version_matches_pin'] and not receipt['child_error']
        and receipt['synthetic_js_drain'] == {'functions': DRAIN_FUNCTIONS,
            'calls': DRAIN_FUNCTIONS * DRAIN_CALLS_PER_FUNCTION, 'result_verified': True}
        and all(cap[k] == 0 for k in ('unknown_flag_errors', 'illegal_flag_value_errors',
                                      'contradictory_flag_errors', 'permission_denied_errors'))
        and jit['category'] in ('accepted', 'accepted_complete_targets_with_partial_tail') and any(t['tier'] == 'turbofan' for t in jit['targets'])
        and all(t['decoder_complete_byte_coverage'] and t['undecodable_instruction_count'] == 0 for t in jit['targets']))


def validate(report):
    check(set(report) == {'schema', 'synthetic_only', 'models_loaded', 'completed_primary_calls',
          'semantic_verified', 'full_capture_released', 'wasm_sha256', 'source_revision',
          'playwright_version', 'expected_browser_version', 'status', 'cases'}, 'record_shape')
    check(report['schema'] == 'inplace-perf-native-canary-v2' and report['synthetic_only'] is True
          and report['models_loaded'] is False and report['completed_primary_calls'] == 0
          and type(report['completed_primary_calls']) is int and report['semantic_verified'] is False
          and report['full_capture_released'] is False and report['wasm_sha256'] == base.WASM_SHA
          and report['source_revision'] == SOURCE_REV and report['playwright_version'] == '1.63.0'
          and report['expected_browser_version'] == base.EXPECTED_BROWSER, 'record_shape')
    check(isinstance(report['cases'], list) and [c['mode'] for c in report['cases']] == list(MODES), 'record_shape')
    for case in report['cases']:
        check(set(case) == {'mode', 'normal_tiering', 'diagnostic_profiling_enabled', 'flags_without_private_output_path',
              'child_exit_code', 'timed_out', 'capture_limit_exceeded', 'jit_limit_exceeded', 'skipped_after_permission_denial', 'child_result', 'capture', 'jit'}, 'record_shape')
        check(case['normal_tiering'] is (case['mode'] == 'normal') and case['diagnostic_profiling_enabled'] is True
              and case['flags_without_private_output_path'] == flags(case['mode']), 'record_shape')
        check(type(case['child_exit_code']) is int and -128 <= case['child_exit_code'] <= 255, 'record_shape')
        check(all(type(case[k]) is bool for k in ('timed_out', 'capture_limit_exceeded', 'jit_limit_exceeded', 'skipped_after_permission_denial')), 'record_shape')
        if case['child_result'] is not None:
            check(child_result(base.RESULT_PREFIX + json.dumps(case['child_result'])) == case['child_result'], 'record_shape')
        cap = case['capture']
        check(set(cap) == set(capture_summary('', '')), 'record_shape')
        for key, value in cap.items():
            if key == 'parser_category':
                check(value in base.PARSER_CATEGORIES, 'record_shape')
            elif key in ('liftoff_observed', 'turbofan_observed', 'strict_parser_accepted'):
                check(type(value) is bool, 'record_shape')
            else:
                check(type(value) is int and 0 <= value <= MAX_JIT_BYTES, 'record_shape')
        jit = case['jit']
        check(set(jit) == set(empty_jit('accepted')) and jit['category'] in CATEGORIES, 'record_shape')
        for key in ('files', 'readable_bytes', 'records', 'code_load_records', 'other_wasm_records', 'target_files',
                    'validated_prefix_bytes', 'trailing_bytes', 'partial_files'):
            check(type(jit[key]) is int and 0 <= jit[key] <= MAX_JIT_BYTES, 'record_shape')
        check(jit['files'] <= MAX_JIT_FILES and isinstance(jit['targets'], list)
              and len(jit['targets']) <= MAX_TARGET_RECORDS, 'record_shape')
        check(type(jit['all_files_complete']) is bool and jit['partial_files'] <= jit['files'], 'record_shape')
        failure = jit['first_failure']
        if failure is not None:
            check(set(failure) == {'kind', 'offset_bytes', 'available_bytes', 'declared_record_bytes',
                  'declared_record_exceeds_limit', 'record_kind', 'complete_records_before_failure'}, 'record_shape')
            check(failure['kind'] in FAILURE_KINDS and failure['record_kind'] in (*RECORD_KINDS, 'unknown')
                  and type(failure['declared_record_exceeds_limit']) is bool, 'record_shape')
            for key in ('offset_bytes', 'available_bytes', 'declared_record_bytes', 'complete_records_before_failure'):
                check(type(failure[key]) is int and 0 <= failure[key] <= MAX_JIT_BYTES, 'record_shape')
        for target in jit['targets']:
            check(set(target) == {'function_index', 'tier', 'exact_wasm_symbol_binding', 'code_size', 'readable_code_bytes',
                  'code_sha256', 'prefix16_hex', 'decoded_bytes', 'decoded_instruction_count',
                  'undecodable_instruction_count', 'decoder_complete_byte_coverage', 'decoder_bytes_sha256'}, 'record_shape')
            check(target['function_index'] == 0 and type(target['function_index']) is int
                  and target['tier'] in ('liftoff', 'turbofan') and target['exact_wasm_symbol_binding'] is True
                  and target['decoder_complete_byte_coverage'] is True, 'record_shape')
            for key in ('code_size', 'readable_code_bytes', 'decoded_bytes', 'decoded_instruction_count'):
                check(type(target[key]) is int and 0 < target[key] <= MAX_TARGET_BYTES, 'record_shape')
            check(target['code_size'] == target['readable_code_bytes'] == target['decoded_bytes'], 'record_shape')
            check(type(target['undecodable_instruction_count']) is int
                  and 0 <= target['undecodable_instruction_count'] <= target['decoded_instruction_count'], 'record_shape')
            check(re.fullmatch(r'[0-9a-f]{64}', target['code_sha256']) is not None
                  and target['code_sha256'] == target['decoder_bytes_sha256'], 'record_shape')
            check(re.fullmatch(r'[0-9a-f]+', target['prefix16_hex']) is not None
                  and len(target['prefix16_hex']) == 2 * min(16, target['code_size']), 'record_shape')
        if jit['category'] in ('accepted', 'accepted_complete_targets_with_partial_tail'):
            check(jit['files'] > 0 and jit['target_files'] == 1 and jit['other_wasm_records'] == 0
                  and len(jit['targets']) > 0, 'record_shape')
            check(jit['validated_prefix_bytes'] + jit['trailing_bytes'] == jit['readable_bytes'], 'record_shape')
            check(sum(t['code_size'] for t in jit['targets']) <= jit['validated_prefix_bytes'], 'record_shape')
            if jit['category'] == 'accepted':
                check(jit['all_files_complete'] and jit['partial_files'] == jit['trailing_bytes'] == 0
                      and failure is None, 'record_shape')
            else:
                check(not jit['all_files_complete'] and jit['partial_files'] > 0 and jit['trailing_bytes'] > 0
                      and failure is not None and failure['kind'] in TAIL_KINDS, 'record_shape')
    check(report['status'] == ('normal_tier_profiled_native_bytes_confirmed' if ready(report['cases'][0])
                              else 'normal_tier_profiled_native_bytes_unconfirmed'), 'record_shape')
    return True


def child_result(stdout):
    records = [line[len(base.RESULT_PREFIX):] for line in stdout.splitlines() if line.startswith(base.RESULT_PREFIX)]
    if len(records) != 1:
        return None
    try:
        receipt = json.loads(records[0])
        drain = receipt.pop('synthetic_js_drain')
        check(set(drain) == {'functions', 'calls', 'result_verified'}, 'record_shape')
        check(type(drain['functions']) is int and drain['functions'] in (0, DRAIN_FUNCTIONS)
              and type(drain['calls']) is int and drain['calls'] in (0, DRAIN_FUNCTIONS * DRAIN_CALLS_PER_FUNCTION)
              and type(drain['result_verified']) is bool, 'record_shape')
        check(base.child_result(base.RESULT_PREFIX + json.dumps(receipt)) == receipt, 'record_shape')
        receipt['synthetic_js_drain'] = drain
        return receipt
    except Exception:
        return None


def child(mode, directory):
    from playwright.sync_api import sync_playwright
    data = {'engine': None, 'completed_calls': 0, 'result_verified': False,
            'browser_version_matches_pin': False, 'child_error': False,
            'synthetic_js_drain': {'functions': 0, 'calls': 0, 'result_verified': False}}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=flags_for_child(mode, directory))
            try:
                session = browser.new_browser_cdp_session()
                try:
                    data['engine'] = base.engine_identity(session.send('Browser.getVersion'))
                finally:
                    session.detach()
                data['browser_version_matches_pin'] = browser.version == base.EXPECTED_BROWSER
                page = browser.new_page()
                result = page.evaluate("""async ({bytes, calls, functions, rounds}) => {
                  const module = new WebAssembly.Module(new Uint8Array(bytes));
                  const instance = new WebAssembly.Instance(module);
                  let valid = true;
                  for (let i = 0; i < calls; ++i) {
                    const x = (i % 257) - 128;
                    valid = (instance.exports.f(x) === x * 0.5) && valid;
                  }
                  await new Promise(resolve => setTimeout(resolve, 250));
                  valid = (instance.exports.f(3) === 1.5) && valid;
                  // Browser renderer shutdown uses _exit, which may discard a
                  // buffered JITdump suffix. Generate bounded, distinct synthetic
                  // JS code records AFTER the Wasm target, without forcing tiers,
                  // to move its complete records into the readable file prefix.
                  const fs = [];
                  for (let i = 0; i < functions; ++i)
                    fs.push(new Function('x', 'return ((x + ' + i + ') | 0) ^ 12345;'));
                  let drainValid = true;
                  for (let round = 0; round < rounds; ++round)
                    for (let i = 0; i < functions; ++i)
                      drainValid = (fs[i](round) === (((round + i) | 0) ^ 12345)) && drainValid;
                  await new Promise(resolve => setTimeout(resolve, 250));
                  return {valid, completed: calls + 1, drainValid, functions, drainCalls: functions * rounds};
                }""", {'bytes': list(base.WASM), 'calls': base.CALLS,
                         'functions': DRAIN_FUNCTIONS, 'rounds': DRAIN_CALLS_PER_FUNCTION})
                check(set(result) == {'valid', 'completed', 'drainValid', 'functions', 'drainCalls'}
                      and type(result['valid']) is bool and type(result['drainValid']) is bool
                      and result['completed'] == base.CALLS + 1 and result['functions'] == DRAIN_FUNCTIONS
                      and result['drainCalls'] == DRAIN_FUNCTIONS * DRAIN_CALLS_PER_FUNCTION, 'record_shape')
                data.update(completed_calls=result['completed'], result_verified=result['valid'])
                data['synthetic_js_drain'] = {'functions': result['functions'], 'calls': result['drainCalls'],
                                               'result_verified': result['drainValid']}
            finally:
                browser.close()
    except Exception:
        data['child_error'] = True
    print(base.RESULT_PREFIX + json.dumps(data, separators=(',', ':')), flush=True)
    return 1 if data['child_error'] else 0


def run_cases():
    first = run_case(MODES[0])
    if first['capture']['permission_denied_errors']:
        second = {'mode': MODES[1], 'normal_tiering': False, 'diagnostic_profiling_enabled': True,
                  'flags_without_private_output_path': flags(MODES[1]), 'child_exit_code': -125,
                  'timed_out': False, 'capture_limit_exceeded': False, 'jit_limit_exceeded': False,
                  'skipped_after_permission_denial': True, 'child_result': None,
                  'capture': capture_summary('', ''), 'jit': empty_jit('not_attempted')}
    else:
        second = run_case(MODES[1])
    return [first, second]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=MODES)
    parser.add_argument('--private-directory', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    check(hashlib.sha256(base.WASM).hexdigest() == base.WASM_SHA, 'record_shape')
    base.extraction_module()
    if args.child:
        check(args.private_directory is not None and args.private_directory.is_dir(), 'file_shape')
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_JIT_BYTES, MAX_JIT_BYTES))
        return child(args.child, args.private_directory)
    if args.validate:
        validate(json.loads(args.validate.read_text()))
        print('Synthetic perf report schema/privacy validated; timing remains unreleased.')
        return 0
    check(args.output is not None and importlib.metadata.version('playwright') == '1.63.0', 'record_shape')
    cases = run_cases()
    report = {'schema': 'inplace-perf-native-canary-v2', 'synthetic_only': True, 'models_loaded': False,
              'completed_primary_calls': 0, 'semantic_verified': False, 'full_capture_released': False,
              'wasm_sha256': base.WASM_SHA, 'source_revision': SOURCE_REV, 'playwright_version': '1.63.0',
              'expected_browser_version': base.EXPECTED_BROWSER,
              'status': 'normal_tier_profiled_native_bytes_confirmed' if ready(cases[0]) else 'normal_tier_profiled_native_bytes_unconfirmed',
              'cases': cases}
    validate(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('SOURCE_ON_PERF_CANARY ' + json.dumps(report, separators=(',', ':')), flush=True)
    return 0


# Preserve the unmodified base flags before the child-only injection above.
_BASE_FLAGS = base.flags

def flags_for_child(mode, directory):
    value = _BASE_FLAGS(mode)[0] + ',--perf-prof,--perf-prof-path=' + str(directory)
    check(re.fullmatch(r'[A-Za-z0-9_/.-]+', str(directory)) is not None, 'file_shape')
    return [value]


if __name__ == '__main__':
    raise SystemExit(main())
