#!/usr/bin/env python3
"""Synthetic-only Chromium native-print capability probe, never a timing gate.

Two separate fresh browsers exercise the same 41-byte f32 multiply function at
index zero. The normal-tier case preserves the production capture flags. The
second case adds --no-liftoff only as a synthetic diagnostic. No model, runtime
archive, user text, audio, or actual research binary is opened.

Only validated counts, booleans, fixed enums and bounded engine identifiers leave
the child process. Captured stdout/stderr and native instructions are not saved
or emitted. The existing frozen structural parser is used without modification.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'browser'))
from source_capture import EXTRACTION_SHA256, capture_target_code, extraction_module
from source_native import diagnostic_flags

WASM = bytes.fromhex('0061736d0100000001060160017d017d03020100070501016600000a0c010a002000430000003f940b')
WASM_SHA = 'a40399d0fae7e3b624afe96a864849ffbc921d31fd6c70792bb2191695899670'
EXPECTED_BROWSER = '153.0.8010.12'
MODES = ('normal', 'forced_turbofan_diagnostic')
CALLS = 2000000
MAX_CAPTURE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 90
RESULT_PREFIX = 'NATIVE_CANARY_CHILD '
PARSER_CATEGORIES = {'accepted', 'missing_complete_block', 'function_binding',
                     'missing_instruction_section', 'instruction_coverage',
                     'unsupported_instruction_text', 'other_parser_rejection'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def flags(mode):
    require(mode in MODES, 'Canary mode')
    normal = diagnostic_flags(0)
    if mode == 'normal':
        return normal
    return [normal[0] + ',--no-liftoff']


def engine_identity(value):
    require(isinstance(value, dict), 'Engine object')
    product, version = value.get('product'), value.get('jsVersion')
    require(isinstance(product, str) and len(product) <= 80
            and re.fullmatch(r'(?:HeadlessChrome|Chrome)/[0-9.]+', product), 'Engine product')
    require(isinstance(version, str) and len(version) <= 80
            and re.fullmatch(r'[0-9A-Za-z._-]+', version), 'Engine version')
    return {'product': product, 'js_version': version}


def child(mode):
    from playwright.sync_api import sync_playwright
    data = {'engine': None, 'completed_calls': 0, 'result_verified': False,
            'browser_version_matches_pin': False, 'child_error': False}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=flags(mode))
            try:
                session = browser.new_browser_cdp_session()
                try:
                    data['engine'] = engine_identity(session.send('Browser.getVersion'))
                finally:
                    session.detach()
                data['browser_version_matches_pin'] = browser.version == EXPECTED_BROWSER
                page = browser.new_page()
                # No navigation or network assets. Every call varies its input.
                # Wait briefly for ordinary asynchronous tier-up, then exercise
                # the same function again without forcing the normal-tier case.
                answer = page.evaluate('''async ({bytes, calls}) => {
                  const module = new WebAssembly.Module(new Uint8Array(bytes));
                  const instance = new WebAssembly.Instance(module);
                  let valid = true;
                  for (let i = 0; i < calls; ++i) {
                    const x = (i % 257) - 128;
                    const actual = instance.exports.f(x);
                    valid = (actual === x * 0.5) && valid;
                  }
                  await new Promise(resolve => setTimeout(resolve, 250));
                  const final = instance.exports.f(3);
                  valid = (final === 1.5) && valid;
                  return {valid, completed: calls + 1};
                }''', {'bytes': list(WASM), 'calls': CALLS})
                require(set(answer) == {'valid', 'completed'} and type(answer['valid']) is bool
                        and answer['completed'] == CALLS + 1, 'Synthetic result')
                data['completed_calls'] = answer['completed']
                data['result_verified'] = answer['valid']
            finally:
                browser.close()
    except Exception:
        # Browser diagnostics remain only in captured pipes. Never serialize an
        # exception message, stack, arbitrary browser log, or local pathname.
        data['child_error'] = True
    print(RESULT_PREFIX + json.dumps(data, separators=(',', ':')), flush=True)
    return 1 if data['child_error'] else 0


def parser_category(error):
    message = str(error)
    if message.startswith('No complete V8'):
        return 'missing_complete_block'
    if 'binding' in message or 'compiler' in message:
        return 'function_binding'
    if 'Missing native instruction section' in message:
        return 'missing_instruction_section'
    if 'coverage' in message or 'address/offset' in message:
        return 'instruction_coverage'
    if 'instruction' in message or 'annotation' in message:
        return 'unsupported_instruction_text'
    return 'other_parser_rejection'


def summarize_capture(stdout, stderr):
    text = stdout + '\n' + stderr
    headers = re.findall(r'--- WebAssembly code ---\s*(.*?)--- End code ---', text, re.S)
    result = {
        'stdout_bytes': len(stdout.encode()), 'stderr_bytes': len(stderr.encode()),
        'begin_blocks': text.count('--- WebAssembly code ---'),
        'end_blocks': text.count('--- End code ---'), 'complete_blocks': len(headers),
        'target_index_headers': len(re.findall(r'\bindex:\s*0\s*(?:\n|$)', text)),
        'liftoff_observed': bool(re.search(r'\bcompiler:\s*Liftoff\b', text)),
        'turbofan_observed': bool(re.search(r'\bcompiler:\s*TurboFan\b', text)),
        'unknown_flag_errors': len(re.findall(r'(?:unrecognized|unknown|unrecognised) (?:command[- ]line )?flags?', text, re.I)),
        'illegal_flag_value_errors': len(re.findall(r'(?:illegal|invalid) value for flag', text, re.I)),
        'contradictory_flag_errors': len(re.findall(r'contradictory (?:command[- ]line )?flags?', text, re.I)),
        'strict_parser_accepted': False, 'parser_category': 'missing_complete_block',
        'parsed_blocks': 0, 'parsed_instruction_bytes': 0,
    }
    try:
        evidence = capture_target_code(text, 0, 'original')
        result.update(strict_parser_accepted=True, parser_category='accepted',
                      parsed_blocks=len(evidence['blocks']),
                      parsed_instruction_bytes=sum(b['instruction_bytes'] for b in evidence['blocks']))
    except Exception as error:
        result['parser_category'] = parser_category(error)
    return result


def child_result(stdout):
    records = [line[len(RESULT_PREFIX):] for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    if len(records) != 1:
        return None
    try:
        value = json.loads(records[0])
        require(set(value) == {'engine', 'completed_calls', 'result_verified',
                              'browser_version_matches_pin', 'child_error'}, 'Child fields')
        if value['engine'] is not None:
            require(set(value['engine']) == {'product', 'js_version'}, 'Engine fields')
            require(engine_identity({'product': value['engine']['product'],
                                     'jsVersion': value['engine']['js_version']}) == value['engine'], 'Engine identity')
        require(type(value['completed_calls']) is int and value['completed_calls'] in (0, CALLS + 1), 'Call count')
        require(all(type(value[k]) is bool for k in ('result_verified', 'browser_version_matches_pin', 'child_error')), 'Child booleans')
        return value
    except Exception:
        return None


def run_case(mode):
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', mode],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                               env=dict(os.environ, DEBUG='pw:browser'))
    stdout = stderr = b''
    timed_out = oversized = False
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=min(1, max(0.01, deadline - time.monotonic())))
                oversized = len(stdout) + len(stderr) > MAX_CAPTURE_BYTES
                break
            except subprocess.TimeoutExpired as error:
                stdout, stderr = error.output or b'', error.stderr or b''
                oversized = len(stdout) + len(stderr) > MAX_CAPTURE_BYTES
                timed_out = time.monotonic() >= deadline
                if oversized or timed_out:
                    break
    finally:
        if process.poll() is None:
            # This is the exact process group created by this synthetic child.
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=10)
    captured_sizes = (len(stdout), len(stderr))
    # Do not parse or export text after a capture bound was exceeded.
    if oversized:
        stdout = stderr = b''
    out, err = stdout.decode('utf-8', 'replace'), stderr.decode('utf-8', 'replace')
    capture = summarize_capture(out, err)
    capture['stdout_bytes'], capture['stderr_bytes'] = captured_sizes
    return {'mode': mode, 'normal_tiering': mode == 'normal', 'flags': flags(mode),
            'child_exit_code': process.returncode, 'timed_out': timed_out, 'capture_limit_exceeded': oversized,
            'child_result': child_result(out), 'capture': capture}


def normal_ready(case):
    receipt, capture = case['child_result'], case['capture']
    return bool(case['child_exit_code'] == 0 and not case['timed_out'] and not case['capture_limit_exceeded']
                and receipt is not None and receipt['result_verified'] and receipt['browser_version_matches_pin']
                and not receipt['child_error'] and capture['strict_parser_accepted']
                and capture['unknown_flag_errors'] == capture['illegal_flag_value_errors'] == capture['contradictory_flag_errors'] == 0
                and capture['begin_blocks'] == capture['end_blocks'] == capture['complete_blocks'])


def validate_report(report):
    require(set(report) == {'schema', 'synthetic_only', 'models_loaded', 'completed_primary_calls',
                           'semantic_verified', 'full_capture_released', 'wasm_sha256', 'function_index',
                           'extraction_helper_sha256', 'playwright_version', 'expected_browser_version',
                           'status', 'cases'}, 'Report fields')
    require(report['schema'] == 'inplace-native-capability-canary-v1' and report['synthetic_only'] is True
            and report['models_loaded'] is False and type(report['completed_primary_calls']) is int
            and report['completed_primary_calls'] == 0 and report['semantic_verified'] is False
            and report['full_capture_released'] is False, 'Canary scope')
    require(report['wasm_sha256'] == WASM_SHA and report['function_index'] == 0
            and report['extraction_helper_sha256'] == EXTRACTION_SHA256
            and report['playwright_version'] == '1.63.0'
            and report['expected_browser_version'] == EXPECTED_BROWSER, 'Canary pins')
    require(isinstance(report['cases'], list) and [r['mode'] for r in report['cases']] == list(MODES), 'Case coverage')
    count_names = set(summarize_capture('', '')) - {'liftoff_observed', 'turbofan_observed', 'strict_parser_accepted', 'parser_category'}
    for case in report['cases']:
        require(set(case) == {'mode', 'normal_tiering', 'flags', 'child_exit_code', 'timed_out',
                              'capture_limit_exceeded', 'child_result', 'capture'}, 'Case fields')
        require(case['flags'] == flags(case['mode']) and case['normal_tiering'] is (case['mode'] == 'normal'), 'Case flags')
        require(type(case['child_exit_code']) is int and -128 <= case['child_exit_code'] <= 255, 'Exit code')
        require(type(case['timed_out']) is bool and type(case['capture_limit_exceeded']) is bool, 'Capture bounds')
        if case['child_result'] is not None:
            require(child_result(RESULT_PREFIX + json.dumps(case['child_result'])) == case['child_result'], 'Child receipt')
        c = case['capture']
        require(set(c) == set(summarize_capture('', '')), 'Capture fields')
        require(all(type(c[k]) is int and 0 <= c[k] <= MAX_CAPTURE_BYTES * 3 for k in count_names), 'Capture counts')
        require(all(type(c[k]) is bool for k in ('liftoff_observed', 'turbofan_observed', 'strict_parser_accepted'))
                and c['parser_category'] in PARSER_CATEGORIES, 'Capture classifications')
        require(c['strict_parser_accepted'] is (c['parser_category'] == 'accepted'), 'Parser result consistency')
        if c['strict_parser_accepted']:
            require(c['parsed_blocks'] > 0 and c['parsed_instruction_bytes'] > 0, 'Parsed native evidence counts')
    ready = normal_ready(report['cases'][0])
    require(report['status'] == ('normal_capture_capability_confirmed' if ready else 'normal_capture_capability_unconfirmed'), 'Canary status')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=MODES)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--validate', type=Path)
    args = parser.parse_args()
    require(hashlib.sha256(WASM).hexdigest() == WASM_SHA, 'Synthetic Wasm pin')
    extraction_module()  # Verify unchanged frozen parser before running anything.
    if args.child:
        return child(args.child)
    if args.validate:
        validate_report(json.loads(args.validate.read_text()))
        print('Native canary report schema/privacy validated; no full capture or timing is released.')
        return 0
    require(args.output is not None, 'Output required')
    require(importlib.metadata.version('playwright') == '1.63.0', 'Pinned Playwright required')
    cases = [run_case(mode) for mode in MODES]
    ready = normal_ready(cases[0])
    report = {'schema': 'inplace-native-capability-canary-v1', 'synthetic_only': True,
              'models_loaded': False, 'completed_primary_calls': 0, 'semantic_verified': False,
              'full_capture_released': False, 'wasm_sha256': WASM_SHA, 'function_index': 0,
              'extraction_helper_sha256': EXTRACTION_SHA256, 'playwright_version': '1.63.0',
              'expected_browser_version': EXPECTED_BROWSER,
              'status': 'normal_capture_capability_confirmed' if ready else 'normal_capture_capability_unconfirmed',
              'cases': cases}
    validate_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print('SOURCE_ON_NATIVE_CANARY ' + json.dumps(report, separators=(',', ':')), flush=True)
    return 0  # Diagnostic completion is distinct from capability confirmation.


if __name__ == '__main__':
    raise SystemExit(main())
