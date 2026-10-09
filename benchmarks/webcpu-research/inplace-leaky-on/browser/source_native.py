"""Separate normal-tier browser code-dump gate. Never used by a timed worker.

All browser text, machine addresses, waveforms and model inputs stay in the private
work directory. Only hashes, bounded runtime metadata and the parser's CFG proof
enter the public receipt. Unsupported lowering aborts before any primary call.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import psutil

from source_manifest import KEYS, HARNESS_SHA, need, sha

PROOF_DIRECTORY = Path(__file__).resolve().parent.parent / 'local-gates'
NATIVE_PROOF_SHA256 = '8f87d0b1a829edc3c381f8bf7ad50c5a1aa1cb5468a2b49d07f97ab3f05ae783'
MAX_NATIVE_TRACE_BYTES = 64 * 1024 * 1024


def proof_module():
    sys.path.append(str(PROOF_DIRECTORY))
    import source_native_proof
    need(sha(Path(source_native_proof.__file__).read_bytes()) == NATIVE_PROOF_SHA256, 'Reviewed native proof helper pin')
    return source_native_proof


def diagnostic_flags(index):
    need(type(index) is int and index >= 0, 'Native function index')
    return [f'--js-flags=--wasm-revectorize,--print-wasm-code-function-index={index}']


def check_dispatch(record):
    need(record['verified'] is True and record['context'] == 'dedicated_worker'
         and record['source'] == 'actual_CORE_module'
         and record['expected_dispatch'] == 'auto'
         and record['actual_dispatch'] == 'splat'
         and record['worker_hardware_concurrency'] == 4
         and record['probe_hardware_concurrency'] == 4
         and record['splat_pointers'] == record['checked_pointers'] == 12
         and record['loadsplat_pointers'] == 0
         and record['probe_return_code'] == 0
         and record['is_x86'] is True and record['relaxed_simd'] is False
         and record['mr'] == 4 and record['nr'] == 8
         and record['cross_origin_isolated'] is True
         and record['shared_memory'] is True, 'Native actual HC4/12splat')


def child(config_path):
    # The parent serves the same immutable verified JS/Wasm as the primary run.
    from playwright.sync_api import sync_playwright
    config = json.loads(config_path.read_text())
    harness = Path(config['harness'])
    need(sha(harness.read_bytes()) == HARNESS_SHA, 'Native harness pin')
    spec = importlib.util.spec_from_file_location('native_harness', harness)
    h = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = h
    spec.loader.exec_module(h)
    result = {'initialization': None, 'engine': None, 'launch_flags': diagnostic_flags(config['function_index']),
              'diagnostic_calls': [], 'checks': [], 'cleanup': None}
    result_path = Path(config['result'])

    def save():
        result_path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=result['launch_flags'])
        pid = h.chromium_process_id(browser)
        targets = [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]
        try:
            result['engine'] = h.browser_engine_info(browser)
            page = browser.new_page()
            page.goto(config['url'])
            result['initialization'] = page.evaluate('d=>request(d)', {'command': 'init', 'variant': config['variant']})
            info = result['initialization']
            need(info['ready'] is True and info['timing_instrumentation'] == 'clock_only', 'Native hook-free worker')
            need(info['wasm_sha256'] == config['wasm_sha256'] and info['js_sha256'] == config['js_sha256'], 'Native loaded binary identity')
            check_dispatch(info['dispatch'])
            save()
            for index in range(1, 4):
                value = page.evaluate('d=>request(d)', {'command': 'synthesize'})
                result['diagnostic_calls'].append({'iteration': index, **value})
                save()
            for index in [1, 2]:
                value = page.evaluate('d=>request(d)', {'command': 'check'})
                need(not value['alias'], 'Native worker unexpectedly instrumented')
                wav, raw = bytes(value['wav']), bytes(value['raw'])
                metrics = h.waveform_comparison(wav, raw, wav, raw)
                row = {'iteration': index, 'raw_sha256': sha(raw), 'wav_sha256': sha(wav),
                       'pcm_sha256': metrics['pcm_sha256'], 'finite': metrics['finite'],
                       'wav_bytes': len(wav), 'raw_bytes': len(raw),
                       'fp32_samples': metrics['fp32_samples'], 'wav_format': metrics['wav_format']}
                result['checks'].append(row)
                save()
                need(all(row[name] == config['reference'][name] for name in ['raw_sha256', 'wav_sha256', 'pcm_sha256']),
                     'Native output differs from same original ON reference')
        finally:
            # Refresh the descendant list after worker creation and inference.
            try:
                targets = [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]
            except psutil.NoSuchProcess:
                pass
            closed = True
            try:
                browser.close()
            except Exception:
                closed = False
            _, pending = psutil.wait_procs(targets, timeout=10)
            alive = []
            for process in pending:
                try:
                    if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                        alive.append(process)
                except psutil.NoSuchProcess:
                    pass
            result['cleanup'] = {'observed_processes': len(targets), 'remaining_processes': len(alive),
                                 'confirmed': closed and bool(targets) and not alive}
            save()
            need(result['cleanup']['confirmed'], 'Native browser cleanup')


class NativeTraceLimitError(RuntimeError):
    pass


def check_trace_size(stdout, stderr):
    def size(value):
        if value is None:
            return 0
        return len(value.encode('utf-8') if isinstance(value, str) else value)
    if size(stdout) + size(stderr) + 1 > MAX_NATIVE_TRACE_BYTES:
        raise NativeTraceLimitError('Targeted native trace exceeded 64 MiB')


class NativeCaptureError(RuntimeError):
    def __init__(self, category, error_type, captured, cleanup, returncode):
        super().__init__('Native diagnostic capture failed')
        self.category = category
        self.error_type = error_type
        self.captured = captured
        self.cleanup = cleanup
        self.returncode = returncode


def capture_child(command, env):
    """Observe owned descendants and clean them on every exceptional exit.

    Polling here tracks process identities for cleanup only, in the separate
    untimed diagnostic. It does not sample resources or touch primary workers.
    """
    process = None
    observed = {}
    stdout = stderr = ''
    failure = None
    category = None
    cleanup = {'observed_processes': 0, 'remaining_processes': 0, 'confirmed': False}
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='replace', env=env)
        root = psutil.Process(process.pid)
        observed[(root.pid, root.create_time())] = root
        deadline = time.monotonic() + 1800
        while True:
            try:
                for target in [root, *root.children(recursive=True)]:
                    observed[(target.pid, target.create_time())] = target
            except psutil.NoSuchProcess:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 1800)
            try:
                stdout, stderr = process.communicate(timeout=min(1, remaining))
                check_trace_size(stdout, stderr)
                break
            except subprocess.TimeoutExpired as error:
                check_trace_size(error.output, error.stderr)
                continue
    except BaseException as error:
        failure = error
        category = ('diagnostic_trace_limit' if isinstance(error, NativeTraceLimitError)
                    else 'diagnostic_timeout' if isinstance(error, subprocess.TimeoutExpired)
                    else 'diagnostic_interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit))
                    else 'diagnostic_launch_failed' if process is None
                    else 'diagnostic_communication_failure')
    finally:
        if process is not None:
            try:
                root = psutil.Process(process.pid)
                for target in [root, *root.children(recursive=True)]:
                    observed[(target.pid, target.create_time())] = target
            except psutil.NoSuchProcess:
                pass
        alive = []
        for target in observed.values():
            try:
                if target.is_running() and target.status() != psutil.STATUS_ZOMBIE:
                    alive.append(target)
            except psutil.NoSuchProcess:
                pass
        for target in reversed(alive):
            try:
                target.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, pending = psutil.wait_procs(alive, timeout=5)
        for target in pending:
            try:
                target.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, pending = psutil.wait_procs(pending, timeout=5)
        remaining = []
        for target in pending:
            try:
                if target.is_running() and target.status() != psutil.STATUS_ZOMBIE:
                    remaining.append(target)
            except psutil.NoSuchProcess:
                pass
        cleanup = {'observed_processes': len(observed), 'remaining_processes': len(remaining),
                   'confirmed': not remaining}
        if process is not None:
            try:
                stdout, stderr = process.communicate(timeout=5)
            except BaseException as error:
                cleanup['confirmed'] = False
                if failure is None:
                    failure = error
                    category = 'diagnostic_communication_failure'
    if failure is None:
        try:
            check_trace_size(stdout, stderr)
        except NativeTraceLimitError as error:
            failure = error
            category = 'diagnostic_trace_limit'
    if failure is not None:
        raise NativeCaptureError(category, type(failure).__name__, stdout + '\n' + stderr,
                                 cleanup, process.returncode if process is not None else None) from None
    need(cleanup['confirmed'], 'Native capture descendants remain')
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr), cleanup


SAFE_MNEMONICS = frozenset((
    'mov movl movq movb movw movzxbl movzxwl movsxlq movzxbq movzxwq '
    'leal leaq addl addq subl subq andl andq orl orq xorl xorq imull imulq '
    'shll shlq sall salq shrl shrq sarl sarq cmpl cmpq cmpb testl testq '
    'sete setne setl setle setg setge seta setae setb setbe setz setnz '
    'jmp jmpq je jne jz jnz ja jae jb jbe jc jnc jl jle jg jge js jns jo jno jp jnp '
    'vmovups vmovaps vmovdqu vmovdqa movups movaps movdqu movdqa vmovss movss vmovd movd '
    'vmulps mulps vmulss mulss vcmpps cmpps vcmpleps vcmpltps vcmpgeps '
    'vxorps vpxor xorps pxor vbroadcastss vpbroadcastd vshufps vpshufd shufps pshufd '
    'vpandn vandnps pandn andnps vpand vandps pand andps vpor vorps por orps vblendvps '
    'nop nopl nopw push pushq pop popq call callq ret retq ud2 int3 vzeroupper '
    'ucomiss vucomiss unsupported'
).split())


def native_observations(parser, captured, function_index):
    """Diagnostic counts only, useful on failure without exporting machine text."""
    try:
        blocks = parser.parse_blocks(captured, function_index)
    except Exception:
        return {'parsed': False, 'blocks': [], 'omitted_blocks': 0, 'instruction_context': []}
    result = {'parsed': True, 'blocks': [], 'omitted_blocks': max(0, len(blocks) - 8), 'instruction_context': []}
    for block in blocks[:8]:
        operations = [row['mnemonic'] for row in block['instructions']]
        result['blocks'].append({'tier': block['compiler'], 'instruction_bytes': block['instruction_bytes'],
                                 'instruction_count': len(operations),
                                 'packed_multiply_count': sum(x in ['mulps', 'vmulps'] for x in operations),
                                 'scalar_multiply_count': sum(x in ['mulss', 'vmulss'] for x in operations),
                                 'conditional_branch_count': sum(x.startswith('j') and x not in ['jmp', 'jmpq'] for x in operations)})
    optimized = next((b for b in blocks if b['compiler'] == 'TurboFan'), None)
    if optimized is not None:
        instructions = optimized['instructions']
        selected = set(range(min(16, len(instructions))))
        for predicate in [lambda row: row['mnemonic'] in ['vmulps', 'mulps'],
                          lambda row: row['mnemonic'].startswith('cmp') and any(x in ['0x10', '16'] for x in row['operands'])]:
            index = next((i for i, row in enumerate(instructions) if predicate(row)), None)
            if index is not None:
                selected.update(range(max(0, index - 3), min(len(instructions), index + 5)))
        for index in sorted(selected)[:32]:
            row = instructions[index]
            mnemonic = row['mnemonic'] if row['mnemonic'] in SAFE_MNEMONICS else 'unsupported'
            target = row['target'] if type(row['target']) is int and 0 <= row['target'] <= 1000000 else None
            result['instruction_context'].append({'offset': row['offset'], 'mnemonic': mnemonic,
                'operands': [safe_operand(x) for x in row['operands'][:4]], 'target': target})
    validate_native_observations(result)
    return result


def safe_operand(operand):
    """Keep register/guard context; suppress absolute addresses and unknown text."""
    if not isinstance(operand, str) or len(operand) > 128:
        return '<unsupported>'
    if operand == '<external>':
        return operand
    allowed_numbers = {0, 1, 2, 3, 4, 8, 12, 15, 16, 24, 31, 32, 63, 64, 127, 128, 255, 0xffffffff, 0xfffffffc}
    def number(match):
        value = int(match[0], 16 if match[0].startswith('0x') else 10)
        return match[0] if value in allowed_numbers else 'IMM'
    cleaned = re.sub(r'0x[0-9a-fA-F]+|(?<![A-Za-z0-9_])\d+', number, operand)
    registers = {'rax', 'rbx', 'rcx', 'rdx', 'rsi', 'rdi', 'rsp', 'rbp', 'rip',
                 'eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'esp', 'ebp',
                 'al', 'bl', 'cl', 'dl', 'ah', 'bh', 'ch', 'dh', 'sil', 'dil',
                 'spl', 'bpl', 'ax', 'bx', 'cx', 'dx', 'qword', 'dword', 'word', 'byte', 'ptr', 'IMM'}
    registers.update(f'r{i}{suffix}' for i in range(8, 16) for suffix in ['', 'd', 'b', 'w'])
    registers.update(f'{prefix}{i}' for prefix in ['xmm', 'ymm'] for i in range(32))
    without_numbers = re.sub(r'0x[0-9a-fA-F]+|(?<![A-Za-z0-9_])\d+', '', cleaned)
    if any(token not in registers for token in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', without_numbers)):
        return '<unsupported>'
    if not re.fullmatch(r'[A-Za-z0-9_\[\]+* .:-]+', cleaned) or len(cleaned) > 64:
        return '<unsupported>'
    return cleaned


def validate_native_observations(value):
    need(set(value) == {'parsed', 'blocks', 'omitted_blocks', 'instruction_context'} and type(value['parsed']) is bool,
         'Native observation schema')
    need(type(value['omitted_blocks']) is int and value['omitted_blocks'] >= 0
         and isinstance(value['blocks'], list) and len(value['blocks']) <= 8, 'Native observation bound')
    if value['parsed']:
        need(bool(value['blocks']), 'Parsed native observations require a block')
    if not value['parsed']:
        need(value['blocks'] == [] and value['omitted_blocks'] == 0 and value['instruction_context'] == [], 'Unparsed native observations')
    for block in value['blocks']:
        need(set(block) == {'tier', 'instruction_bytes', 'instruction_count', 'packed_multiply_count',
                            'scalar_multiply_count', 'conditional_branch_count'}
             and block['tier'] in {'Liftoff', 'TurboFan'}, 'Native observation fields')
        for name, number in block.items():
            if name != 'tier':
                need(type(number) is int and 0 <= number <= 1000000, 'Native observation number')
    need(isinstance(value['instruction_context'], list) and len(value['instruction_context']) <= 32, 'Native context bound')
    for row in value['instruction_context']:
        need(set(row) == {'offset', 'mnemonic', 'operands', 'target'}, 'Native context fields')
        need(type(row['offset']) is int and 0 <= row['offset'] <= 1000000
             and row['mnemonic'] in SAFE_MNEMONICS, 'Native context identity')
        need(row['target'] is None or type(row['target']) is int and 0 <= row['target'] <= 1000000, 'Native context target')
        need(isinstance(row['operands'], list) and len(row['operands']) <= 4, 'Native context operands')
        for operand in row['operands']:
            need(operand in ['<unsupported>', '<external>'] or safe_operand(operand) == operand, 'Unsafe native operand')


def failure_receipt(record, stage, category, error_type):
    """Export fixed, validated fields only; never captured text or a traceback."""
    need(stage in {'native_capture', 'native_cfg_proof'}, 'Native failure stage')
    categories = {'diagnostic_trace_limit', 'diagnostic_timeout', 'diagnostic_interrupted', 'diagnostic_communication_failure',
                  'diagnostic_launch_failed', 'diagnostic_child_failed', 'flag_rejected',
                  'engine_mismatch', 'unsupported_lowering', 'native_gate_contract'}
    need(category in categories, 'Native failure category')
    need(re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,47}', error_type) is not None, 'Native failure type')
    result = {'stage': stage, 'category': category, 'error_type': error_type, 'variant': record['variant']}
    need(result['variant'] in KEYS, 'Native failure variant')
    for name in ['wasm_sha256', 'js_sha256', 'callback_body_sha256', 'dump_sha256']:
        need(re.fullmatch('[a-f0-9]{64}', record[name]) is not None, 'Native failure digest')
        result[name] = record[name]
    for name in ['callback_function_index', 'callback_table_slot', 'dump_bytes']:
        need(type(record[name]) is int and record[name] >= 0, 'Native failure integer')
        result[name] = record[name]
    if 'native_failure' in record:
        proof_module().validate_native_failure_evidence(record['native_failure'])
        result['native_failure'] = record['native_failure']
    if 'native_observations' in record:
        validate_native_observations(record['native_observations'])
        result['native_observations'] = record['native_observations']
    # Parser-specific arbitrary attributes are deliberately excluded here.
    return result


def emit_failure(record, stage, category, error_type):
    bounded = failure_receipt(record, stage, category, error_type)
    record['failure'] = bounded
    print('SOURCE_ON_NATIVE_FAILURE ' + json.dumps(bounded, separators=(',', ':')), flush=True)


def run_native_gates(h, entries, provenance, url, work, engine, save, report, *, capture_only=False):
    if capture_only:
        from source_capture import extraction_module
        parser = extraction_module()
    else:
        parser = proof_module()
    records = report['native_gate']
    for mode in KEYS:
        pin = provenance[mode]
        need(psutil.virtual_memory().available >= 1024 ** 3, 'Native gate available memory below 1 GiB')
        config = {'harness': str(Path(h.__file__).resolve()), 'url': url, 'variant': mode,
                  'function_index': pin['callback_function_index'], 'wasm_sha256': pin['wasm_sha256'],
                  'js_sha256': pin['js_sha256'], 'reference': report['reference'],
                  'result': str(work / (mode + '-native-result.json'))}
        config_path = work / (mode + '-native-config.json')
        config_path.write_text(json.dumps(config))
        record = {name: pin[name] for name in ['wasm_sha256', 'js_sha256', 'callback_body_sha256',
                                               'callback_function_index', 'callback_table_slot']}
        record.update(schema='inplace-leaky-browser-native-v1', normal_tiering=True, variant=mode,
                      launch_flags=diagnostic_flags(pin['callback_function_index']), verified=False,
                      native_proof_helper_sha256=sha(Path(parser.__file__).read_bytes()),
                      cleanup={'observed_processes': 0, 'remaining_processes': 0, 'confirmed': False})
        if capture_only:
            record['schema'] = 'inplace-leaky-browser-native-capture-v1'
            record['native_extraction_helper_sha256'] = record.pop('native_proof_helper_sha256')
            record.update(capture_verified=False, semantic_verified=False)
        # Insert before launch: any exception makes outer cleanup retain private data
        # unless capture cleanup has explicitly established that descendants are gone.
        records[mode] = record
        save()
        env = dict(os.environ)
        env['DEBUG'] = 'pw:browser'
        try:
            process, capture_cleanup = capture_child([sys.executable, str(Path(__file__).resolve()), '--child', str(config_path)], env)
        except NativeCaptureError as error:
            captured = error.captured
            (work / (mode + '-native-private.log')).write_text(captured)
            record.update(dump_sha256=sha(captured.encode()), dump_bytes=len(captured.encode()),
                          diagnostic_exit_code=error.returncode, cleanup=error.cleanup)
            emit_failure(record, 'native_capture', error.category, error.error_type)
            save()
            raise RuntimeError('Native capture failed; no primary calls allowed') from None
        captured = process.stdout + '\n' + process.stderr
        (work / (mode + '-native-private.log')).write_text(captured)
        record.update(dump_sha256=sha(captured.encode()), dump_bytes=len(captured.encode()),
                      diagnostic_exit_code=process.returncode, cleanup=capture_cleanup)
        if Path(config['result']).is_file():
            details = json.loads(Path(config['result']).read_text())
            record.update(details)
            if not capture_cleanup['confirmed']:
                record['cleanup']['confirmed'] = False
        save()
        if process.returncode != 0:
            emit_failure(record, 'native_capture', 'diagnostic_child_failed', 'RuntimeError')
            save()
            raise RuntimeError('Native diagnostic child failed; no primary calls allowed')
        if re.search(r'(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?', captured, re.I):
            emit_failure(record, 'native_capture', 'flag_rejected', 'RuntimeError')
            save()
            raise RuntimeError('Native diagnostic flag rejected')
        if record['engine'] != engine:
            emit_failure(record, 'native_capture', 'engine_mismatch', 'ValueError')
            save()
            raise ValueError('Native/primary engine mismatch')
        try:
            if capture_only:
                from source_capture import capture_target_code
                record['code_evidence'] = capture_target_code(captured, pin['callback_function_index'], mode)
            else:
                record['proof'] = parser.parse_native_dump(captured, pin['callback_function_index'], mode)
                parser.validate_native_proof(record['proof'], mode, pin['callback_function_index'])
        except Exception as error:
            record['native_failure'] = parser.native_failure_evidence(error)
            parser.validate_native_failure_evidence(record['native_failure'])
            record['native_observations'] = native_observations(parser, captured, pin['callback_function_index'])
            emit_failure(record, 'native_capture' if capture_only else 'native_cfg_proof', 'unsupported_lowering', type(error).__name__)
            save()
            raise RuntimeError('Unsupported native lowering; no primary calls allowed') from None
        if capture_only:
            record['capture_verified'] = True
        else:
            record['verified'] = True
        save()
    validate_native_gates(records, provenance, engine, report['reference'], capture_only=capture_only)
    return records


def validate_native_gates(records, provenance, engine, reference=None, *, capture_only=False):
    if capture_only:
        from source_capture import extraction_module, validate_target_code
        parser = extraction_module()
    else:
        parser = proof_module()
    need(set(records) == set(KEYS), 'Native gate coverage')
    allowed = {'schema', 'variant', 'normal_tiering', 'launch_flags', 'dump_sha256', 'dump_bytes', 'diagnostic_exit_code',
               'verified', 'wasm_sha256', 'js_sha256', 'callback_body_sha256', 'callback_function_index',
               'callback_table_slot', 'initialization', 'engine', 'diagnostic_calls', 'checks', 'cleanup', 'proof', 'native_proof_helper_sha256'}
    if capture_only:
        allowed = (allowed - {'proof', 'native_proof_helper_sha256'}) | {'capture_verified', 'semantic_verified', 'code_evidence', 'native_extraction_helper_sha256'}
    for mode, record in records.items():
        need(set(record) == allowed, 'Native receipt schema')
        pin = provenance[mode]
        helper_field = 'native_extraction_helper_sha256' if capture_only else 'native_proof_helper_sha256'
        need(record[helper_field] == sha(Path(parser.__file__).read_bytes()), 'Native helper hash')
        if capture_only:
            need(record['capture_verified'] is True and record['semantic_verified'] is False, 'Capture cannot assert semantic acceptance')
        need(record['schema'] == ('inplace-leaky-browser-native-capture-v1' if capture_only else 'inplace-leaky-browser-native-v1') and record['variant'] == mode
             and record['verified'] is (False if capture_only else True) and record['normal_tiering'] is True
             and record['diagnostic_exit_code'] == 0 and record['engine'] == engine,
             'Native completion/engine')
        need(record['launch_flags'] == diagnostic_flags(pin['callback_function_index']), 'Native isolated flags')
        need(re.fullmatch('[a-f0-9]{64}', record['dump_sha256']) is not None and type(record['dump_bytes']) is int and 0 < record['dump_bytes'] <= MAX_NATIVE_TRACE_BYTES, 'Native dump digest/size')
        for name in ['wasm_sha256', 'js_sha256', 'callback_body_sha256', 'callback_function_index', 'callback_table_slot']:
            need(record[name] == pin[name], 'Native module/body/index binding')
        info = record['initialization']
        need(set(info) == {'ready', 'runtime', 'dispatch', 'wasm_sha256', 'js_sha256', 'timing_instrumentation'},
             'Native initialization schema')
        need(info['ready'] is True and info['timing_instrumentation'] == 'clock_only'
             and info['wasm_sha256'] == pin['wasm_sha256'] and info['js_sha256'] == pin['js_sha256'], 'Native actual loaded binary')
        need(info['runtime'] == {'shared_memory': True, 'pthreads': 1, 'spin_off': True,
             'fixed_length': 962, 'fixed_matches': 1, 'xnn_threads': 2, 'xnn_sessions': 1,
             'configured_ort_global_threads': 1}, 'Native actual runtime')
        check_dispatch(info['dispatch'])
        clean = record['cleanup']
        need(set(clean) == {'observed_processes', 'remaining_processes', 'confirmed'}
             and clean['confirmed'] is True and clean['observed_processes'] > 0 and clean['remaining_processes'] == 0,
             'Native cleanup')
        from source_validate import positive
        need(len(record['diagnostic_calls']) == 3, 'Native normal-tier exercise count')
        for index, row in enumerate(record['diagnostic_calls'], 1):
            need(set(row) == {'iteration', 'elapsed_s', 'wav_bytes'} and row['iteration'] == index, 'Native exercise schema')
            positive(row)
        need(len(record['checks']) == 2, 'Native output repeats')
        for index, row in enumerate(record['checks'], 1):
            need(set(row) == {'iteration', 'raw_sha256', 'wav_sha256', 'pcm_sha256', 'finite',
                              'wav_bytes', 'raw_bytes', 'fp32_samples', 'wav_format'}, 'Native output schema')
            need(row['iteration'] == index and row['finite'] is True and row['wav_bytes'] == 478252
                 and row['raw_bytes'] == 956416 and row['fp32_samples'] == 239104
                 and row['wav_format'] == {'channels': 1, 'sample_bytes': 2, 'sample_rate': 24000, 'frames': 239104},
                 'Native output format')
            for name in ['raw_sha256', 'pcm_sha256', 'wav_sha256']:
                need(re.fullmatch('[a-f0-9]{64}', row[name]) is not None, 'Native output digest')
                if reference is not None:
                    need(row[name] == reference[name], 'Native original ON output equality')
        if capture_only:
            validate_target_code(record['code_evidence'], pin['callback_function_index'], mode)
        else:
            parser.validate_native_proof(record['proof'], mode, pin['callback_function_index'])


if __name__ == '__main__':
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument('--child', type=Path, required=True)
    child(argument_parser.parse_args().child)
