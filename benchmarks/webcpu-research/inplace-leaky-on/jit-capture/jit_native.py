"""Profiled, module-audited Chrome native capture. Zero primary timing calls."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import psutil

ROOT = Path(__file__).resolve().parent
BROWSER = ROOT.parent / 'browser'
sys.path.insert(0, str(BROWSER))
from source_manifest import KEYS, HARNESS_SHA, need, sha
from source_native import check_dispatch
from jit_transport import MAX_JIT_BYTES, ProbeError, make_evidence, module_name, validate_evidence
MAX_NATIVE_TRACE_BYTES = 8 * 1024 * 1024


def diagnostic_flags(index, directory=None):
    need(type(index) is int and index >= 0, 'Native function index')
    value = f'--js-flags=--wasm-revectorize,--print-wasm-code-function-index={index},--perf-prof,--trace-baseline'
    if directory is not None:
        need(re.fullmatch(r'[A-Za-z0-9_/.-]+', str(directory)) is not None, 'Private JIT path')
        value += ',--perf-prof-path=' + str(directory)
    return [value]


def validate_binding(receipt, wasm_sha, wasm_bytes):
    need(set(receipt) == {'schema', 'wasm_sha256', 'byte_length', 'compilation_calls', 'instantiation_calls',
        'tracked_modules', 'workers_created', 'known_module_transfers', 'violations',
        'all_compilation_bytes_exact', 'all_worker_urls_exact', 'module_transfer_verified'}, 'Module binding fields')
    need(receipt['schema'] == 'inplace-jit-module-binding-v1' and receipt['wasm_sha256'] == wasm_sha
         and receipt['byte_length'] == wasm_bytes and receipt['compilation_calls'] == 1
         and receipt['tracked_modules'] == 1 and receipt['instantiation_calls'] == 1
         and receipt['workers_created'] == receipt['known_module_transfers'] == 2
         and receipt['violations'] == 0 and receipt['all_compilation_bytes_exact'] is True
         and receipt['all_worker_urls_exact'] is True and receipt['module_transfer_verified'] is True,
         'Exactly one pinned compilation and two known pthread module transfers')
    for key in ('byte_length', 'compilation_calls', 'instantiation_calls', 'tracked_modules',
                'workers_created', 'known_module_transfers', 'violations'):
        need(type(receipt[key]) is int, 'Binding count type')


def audit_worker(source):
    need(sha(source.encode()) == '3be1e45a9ea9522f950c82a91cb553e6103d8f04df36778366d464d560f1b64b', 'Frozen worker input')
    patches = [
      ("importScripts('/core_dispatch_check.js');", "importScripts('/core_dispatch_check.js');\nimportScripts('/jit_binding.js');"),
      ('let ready=false,stdout=[],variant,entry;', 'let ready=false,stdout=[],variant,entry,nativeBinding;'),
      ('  globalThis.Module={', '  nativeBinding=installJitModuleBinding(wasm,wasmSha,new URL(moduleUrl,self.location).href);\n  globalThis.Module={'),
      ("timing_instrumentation:'clock_only'", "timing_instrumentation:'native_module_audit',native_module_binding:nativeBinding.snapshot()"),
      (" }else throw Error('Unsupported command');", """ }else if(data.command==='native_drain'){
  if(!ready)throw Error('Not ready');
  const functions=[];for(let i=0;i<256;++i)functions.push(new Function('x','return ((x + '+i+')|0)^12345;'));
  let correct=true;for(let r=0;r<1024;++r)for(let i=0;i<256;++i)correct=(functions[i](r)===(((r+i)|0)^12345))&&correct;
  await new Promise(resolve=>setTimeout(resolve,250));
  postMessage({functions:256,calls:262144,result_verified:correct,module_binding:nativeBinding.snapshot()});
 }else throw Error('Unsupported command');""")]
    for before, after in patches:
        need(source.count(before) == 1, 'Diagnostic worker transform scope')
        source = source.replace(before, after)
    return source


def private_bounds(root):
    files = list(root.rglob('*'))
    need(len(files) <= 64, 'Private capture file count')
    total = 0
    for path in files:
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), 'Private capture regular files only')
        if stat.S_ISREG(info.st_mode): total += info.st_size
    need(total <= MAX_JIT_BYTES, 'Private capture file byte limit')


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
              'diagnostic_calls': [], 'checks': [], 'cleanup': None, 'drain': None}
    result_path = Path(config['result'])

    def save():
        result_path.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=diagnostic_flags(config['function_index'], config['jit_directory']))
        pid = h.chromium_process_id(browser)
        targets = [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]
        try:
            result['engine'] = h.browser_engine_info(browser)
            page = browser.new_page()
            page.goto(config['url'])
            result['initialization'] = page.evaluate('d=>request(d)', {'command': 'init', 'variant': config['variant']})
            info = result['initialization']
            need(info['ready'] is True and info['timing_instrumentation'] == 'native_module_audit', 'Native audited worker')
            need(info['wasm_sha256'] == config['wasm_sha256'] and info['js_sha256'] == config['js_sha256'], 'Native loaded binary identity')
            check_dispatch(info['dispatch'])
            validate_binding(info['native_module_binding'], config['wasm_sha256'], config['wasm_bytes'])
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
            result['drain'] = page.evaluate('d=>request(d)', {'command': 'native_drain'})
            need(result['drain']['functions'] == 256 and result['drain']['calls'] == 262144
                 and result['drain']['result_verified'] is True, 'Synthetic JS drain')
            validate_binding(result['drain']['module_binding'], config['wasm_sha256'], config['wasm_bytes'])
            save()
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
        raise NativeTraceLimitError('Targeted native trace exceeded 8 MiB')


class NativeCaptureError(RuntimeError):
    def __init__(self, category, error_type, captured, cleanup, returncode):
        super().__init__('Native diagnostic capture failed')
        self.category = category
        self.error_type = error_type
        self.captured = captured
        self.cleanup = cleanup
        self.returncode = returncode


def capture_child(command, env, private_root):
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
                                   text=True, encoding='utf-8', errors='replace', env=env, cwd=private_root)
        root = psutil.Process(process.pid)
        observed[(root.pid, root.create_time())] = root
        deadline = time.monotonic() + 1800
        while True:
            private_bounds(private_root)
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
            private_bounds(private_root)
        except (NativeTraceLimitError, ValueError) as error:
            failure = error
            category = 'diagnostic_trace_limit'
    if failure is not None:
        raise NativeCaptureError(category, type(failure).__name__, stdout + '\n' + stderr,
                                 cleanup, process.returncode if process is not None else None) from None
    need(cleanup['confirmed'], 'Native capture descendants remain')
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr), cleanup


def source_pins():
    return {name: sha((ROOT / name).read_bytes()) for name in ('jit_native.py', 'jit_transport.py', 'jit_binding.js')}


def run_native_gates(h, entries, provenance, url, work, engine, save, report, *, capture_only=True):
    need(capture_only is True, 'JIT backend is capture-only')
    worker = audit_worker((BROWSER / 'source_worker.js').read_text())
    (work / 'worker.js').write_text(worker)
    shutil.copyfile(ROOT / 'jit_binding.js', work / 'jit_binding.js')
    report['worker_source_sha256']['native_module_audit'] = sha(worker.encode())
    records = report['native_gate']
    for variant in KEYS:
        pin = provenance[variant]
        wasm = entries[variant]['binary'].with_suffix('.wasm').read_bytes()
        need(sha(wasm) == pin['wasm_sha256'], 'JIT pinned input module')
        name = module_name(wasm, pin['callback_function_index'])
        private = Path(tempfile.mkdtemp(prefix=variant + '-jit-private-', dir=work))
        jit = private / 'jit'; jit.mkdir()
        config = {'harness': str(Path(h.__file__).resolve()), 'url': url, 'variant': variant,
            'function_index': pin['callback_function_index'], 'wasm_sha256': pin['wasm_sha256'],
            'wasm_bytes': len(wasm), 'js_sha256': pin['js_sha256'], 'reference': report['reference'],
            'result': str(private / 'result.json'), 'jit_directory': str(jit)}
        config_path = private / 'config.json'; config_path.write_text(json.dumps(config))
        record = {key: pin[key] for key in ('wasm_sha256', 'js_sha256', 'callback_body_sha256',
                                          'callback_function_index', 'callback_table_slot')}
        record.update(schema='inplace-leaky-browser-jit-capture-v1', variant=variant, normal_tiering=True,
            diagnostic_profiled=True, uninstrumented_code_identity_claimed=False, wasm_bytes=len(wasm),
            capture_verified=False, semantic_verified=False, verified=False, source_pins=source_pins(),
            private_jit_deleted=False, cleanup={'observed_processes': 0, 'remaining_processes': 0, 'confirmed': False})
        records[variant] = record; save()
        try:
            process, cleanup = capture_child([sys.executable, str(Path(__file__).resolve()), '--child', str(config_path)],
                                              dict(os.environ, DEBUG='pw:browser'), private)
            record['cleanup'] = cleanup
            captured = process.stdout + '\n' + process.stderr
            record['stream_diagnostics'] = {'stdout_bytes': len(process.stdout.encode()),
                'stderr_bytes': len(process.stderr.encode()), 'sha256': sha(captured.encode()),
                'flag_errors': len(re.findall(r'(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|(?:illegal|invalid) value for flag', captured, re.I)),
                'permission_errors': len(re.findall(r'permission denied|operation not permitted', captured, re.I))}
            record['diagnostic_exit_code'] = process.returncode
            need(process.returncode == 0, 'JIT child failed')
            need(record['stream_diagnostics']['flag_errors'] == record['stream_diagnostics']['permission_errors'] == 0,
                 'JIT flag or permission blocker')
            details = json.loads(Path(config['result']).read_text())
            need(details['cleanup']['confirmed'] and cleanup['confirmed'], 'JIT child cleanup')
            record.update(details)
            need(record['engine'] == engine, 'JIT engine mismatch')
            record['code_evidence'] = make_evidence(jit, private, pin, variant, name, captured)
            record['capture_verified'] = True
        except BaseException as error:
            if isinstance(error, NativeCaptureError): record['cleanup'] = error.cleanup
            failure = {'variant': variant, 'function_index': pin['callback_function_index'],
                'category': str(error) if isinstance(error, ProbeError) else error.category if isinstance(error, NativeCaptureError) else 'diagnostic_failure',
                'error_type': type(error).__name__, 'completed_primary_calls': 0}
            need(re.fullmatch(r'[a-z_]{1,64}', failure['category']) is not None, 'Bounded failure category')
            if isinstance(error, ProbeError) and error.details is not None:
                failure['record_failure'] = error.details
            print('SOURCE_ON_JIT_FAILURE ' + json.dumps(failure, separators=(',', ':')), flush=True)
            record['failure'] = failure
            raise RuntimeError('Source-bound JIT capture failed; timing remains disabled') from None
        finally:
            # Owned browser descendants are confirmed gone before deleting files.
            if record['cleanup'].get('confirmed') is True:
                shutil.rmtree(private)
            record['private_jit_deleted'] = not private.exists()
            save()
    validate_native_gates(records, provenance, engine, report['reference'])
    return records


def validate_native_gates(records, provenance, engine, reference, *, capture_only=True):
    need(capture_only is True and set(records) == set(KEYS), 'JIT capture coverage')
    fields = {'schema', 'variant', 'normal_tiering', 'diagnostic_profiled', 'uninstrumented_code_identity_claimed',
        'wasm_bytes', 'capture_verified', 'semantic_verified', 'verified', 'source_pins', 'private_jit_deleted',
        'cleanup', 'wasm_sha256', 'js_sha256', 'callback_body_sha256', 'callback_function_index',
        'callback_table_slot', 'stream_diagnostics', 'diagnostic_exit_code', 'initialization', 'engine',
        'launch_flags', 'diagnostic_calls', 'checks', 'drain', 'code_evidence'}
    for variant, record in records.items():
        need(set(record) == fields, 'JIT receipt schema')
        pin = provenance[variant]
        need(record['schema'] == 'inplace-leaky-browser-jit-capture-v1' and record['variant'] == variant
             and record['normal_tiering'] is True and record['diagnostic_profiled'] is True
             and record['uninstrumented_code_identity_claimed'] is False and record['capture_verified'] is True
             and record['semantic_verified'] is False and record['verified'] is False
             and record['diagnostic_exit_code'] == 0 and record['engine'] == engine
             and record['source_pins'] == source_pins() and record['private_jit_deleted'] is True, 'JIT receipt scope')
        for field in ('wasm_sha256', 'js_sha256', 'callback_body_sha256', 'callback_function_index', 'callback_table_slot'):
            need(record[field] == pin[field], 'JIT source identity')
        need(type(record['wasm_bytes']) is int and 0 < record['wasm_bytes'] < 256 * 1024 * 1024, 'JIT Wasm size')
        need(record['launch_flags'] == diagnostic_flags(pin['callback_function_index']), 'JIT diagnostic flags')
        stream = record['stream_diagnostics']
        need(set(stream) == {'stdout_bytes', 'stderr_bytes', 'sha256', 'flag_errors', 'permission_errors'}, 'JIT stream fields')
        need(stream['flag_errors'] == stream['permission_errors'] == 0
             and all(type(stream[k]) is int and 0 <= stream[k] <= MAX_NATIVE_TRACE_BYTES for k in ('stdout_bytes', 'stderr_bytes'))
             and re.fullmatch(r'[0-9a-f]{64}', stream['sha256']), 'JIT stream safety')
        info = record['initialization']
        need(set(info) == {'ready', 'runtime', 'dispatch', 'wasm_sha256', 'js_sha256', 'timing_instrumentation', 'native_module_binding'}, 'JIT runtime fields')
        need(info['ready'] is True and info['wasm_sha256'] == pin['wasm_sha256'] and info['js_sha256'] == pin['js_sha256']
             and info['timing_instrumentation'] == 'native_module_audit', 'JIT runtime identity')
        need(info['runtime'] == {'shared_memory': True, 'pthreads': 1, 'spin_off': True, 'fixed_length': 962,
            'fixed_matches': 1, 'xnn_threads': 2, 'xnn_sessions': 1, 'configured_ort_global_threads': 1}, 'JIT pristine XNN runtime')
        check_dispatch(info['dispatch'])
        validate_binding(info['native_module_binding'], pin['wasm_sha256'], record['wasm_bytes'])
        drain = record['drain']
        need(set(drain) == {'functions', 'calls', 'result_verified', 'module_binding'} and drain['functions'] == 256
             and drain['calls'] == 262144 and drain['result_verified'] is True, 'JIT bounded drain')
        validate_binding(drain['module_binding'], pin['wasm_sha256'], record['wasm_bytes'])
        need(drain['module_binding'] == info['native_module_binding'], 'No later/foreign module compilation')
        clean = record['cleanup']
        need(set(clean) == {'observed_processes', 'remaining_processes', 'confirmed'} and clean['confirmed'] is True
             and type(clean['observed_processes']) is int and clean['observed_processes'] > 0
             and clean['remaining_processes'] == 0, 'JIT cleanup')
        need(len(record['diagnostic_calls']) == 3, 'JIT diagnostic exercise coverage')
        from source_validate import positive
        for i, row in enumerate(record['diagnostic_calls'], 1):
            need(set(row) == {'iteration', 'elapsed_s', 'wav_bytes'} and row['iteration'] == i, 'JIT exercise fields')
            positive(row)
        need(len(record['checks']) == 2, 'JIT output repeats')
        for i, row in enumerate(record['checks'], 1):
            need(set(row) == {'iteration', 'raw_sha256', 'wav_sha256', 'pcm_sha256', 'finite', 'wav_bytes',
                  'raw_bytes', 'fp32_samples', 'wav_format'}, 'JIT output fields')
            need(row['iteration'] == i and row['finite'] is True and row['wav_bytes'] == 478252
                 and row['raw_bytes'] == 956416 and row['fp32_samples'] == 239104 and row['wav_format'] ==
                 {'channels': 1, 'sample_bytes': 2, 'sample_rate': 24000, 'frames': 239104}, 'JIT exact output format')
            need(all(row[k] == reference[k] for k in ('raw_sha256', 'wav_sha256', 'pcm_sha256')), 'JIT original ON equality')
        validate_evidence(record['code_evidence'], pin, variant)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', type=Path, required=True)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_JIT_BYTES, MAX_JIT_BYTES))
    try:
        child(args.child)
    except Exception:
        raise SystemExit(1) from None
