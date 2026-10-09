#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]
# ///
"""Source-built three-control browser confirmation: 27 unsampled ON primary calls."""
import argparse, hashlib, importlib.util, json, math, os, shutil, sys, tempfile, time
from pathlib import Path
import psutil
ROOT = Path(__file__).resolve().parent
BROWSER = ROOT.parent / 'browser'
sys.path.insert(0, str(BROWSER))
from source_manifest import KEYS, FLAGS, HARNESS_SHA, QUERY_SHA, MODEL_SHA, need, sha, verify_manifest
from source_resources import resource_observations, pressure_during_call, idle_cpu
from timing_validate import schedule, validate, design
from release_gate import release_gate, load_contract
ROOT = Path(__file__).parent

def host():
    m = psutil.virtual_memory()
    s = psutil.swap_memory()
    psi = {}
    try:
        for line in Path('/proc/pressure/memory').read_text().splitlines():
            name, *fields = line.split()
            psi[name] = {k: float(v) for k, v in (f.split('=') for f in fields)}
    except (OSError, ValueError):
        pass
    return {'memory_psi': psi, 'available_bytes': m.available, 'total_bytes': m.total, 'swap_used': s.used, 'swap_in': s.sin, 'swap_out': s.sout, 'loadavg': list(os.getloadavg())}

def usage(pid):
    out = []
    for q in [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]:
        try:
            stat = Path(f'/proc/{q.pid}/stat').read_text().rsplit(')', 1)[1].split()
            status = Path(f'/proc/{q.pid}/status').read_text()
            swap = next((int(x.split()[1]) * 1024 for x in status.splitlines() if x.startswith('VmSwap:')), None)
            out.append({'pid': q.pid, 'created': q.create_time(), 'rss': q.memory_info().rss, 'cpu': q.cpu_times()._asdict(), 'major_faults': int(stat[9]), 'minor_faults': int(stat[7]), 'swap_bytes': swap})
        except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError, PermissionError):
            pass
    return out

def alias_gate(x, mode, entry):
    need(x['variant'] == mode and x['table_slot'] == entry['callback_table']['slot'] and (x['function_index'] == entry['absolute_function_index']), 'Alias identity')
    need(x['restored'] and (not x['metadata_errors']) and (x['callbacks'] == 69) and (x['elements'] == 435901440), 'Alias coverage/restoration')
    need(x['classes'] == {'exact_inplace': {'calls': 41, 'elements': 256615424}, 'disjoint': {'calls': 28, 'elements': 179286016}}, 'Alias classes')
    need(x['length_histogram'] == {'492544': 1, '1970176': 17, '7880704': 51}, 'Alias shape histogram')
    expected = {'simd': {'calls': 69, 'elements': 435901440}, 'scalar': {'calls': 0, 'elements': 0}} if mode == 'candidate' else {'simd': {'calls': 28, 'elements': 179286016}, 'scalar': {'calls': 41, 'elements': 256615424}}
    need(x['predicted_branch'] == expected, 'Guard classification')

def main():
    p = argparse.ArgumentParser()
    
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--harness', type=Path, required=True)
    p.add_argument('--dispatch-helper', type=Path, required=True)
    p.add_argument('--distribution', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    a.capture_only = False
    load_contract()
    need(sha(a.harness.read_bytes()) == HARNESS_SHA, 'Pristine harness pin')
    need(sha(a.dispatch_helper.read_bytes()) == 'abe7453d3feeaf6bc7f489452055a8ba0d02d22c7c158481112c51e0ada10c2b', 'Dispatch helper pin')
    spec = importlib.util.spec_from_file_location('source_confirmation_harness', a.harness)
    h = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = h
    spec.loader.exec_module(h)
    entries, provenance, proof = verify_manifest(a.manifest)
    report = {'schema': 'inplace-leaky-on-reviewed-timing-v1', 'capture_only': a.capture_only, 'status': 'initializing', 'stage': 'initializing', 'schedule': schedule(), 'normal_tiering': True, 'browser_flags': FLAGS, 'warmups_per_browser': 5, 'provenance': provenance, 'source_proof': proof, 'environment': h.environment_info(), 'source_hashes': {x.name: sha(x.read_bytes()) for x in ROOT.iterdir() if x.is_file() and x.suffix in ['.py', '.js']}, 'model_sha256': MODEL_SHA, 'query_sha256': QUERY_SHA, 'browser_gate': {}, 'process_sets': [], 'trials': [], 'sampling_policy': 'No primary profiler or high-frequency sampler; pre/post process/host resources plus one untimed idle interval per set.', 'release_gate': {}, 'activation': {}, 'reference': None, 'gates_complete_before_timing': False, 'design': design(), 'initial_host': host()}
    if a.distribution:
        report['distribution'] = {'sha256': sha(a.distribution.read_bytes()), 'bytes': a.distribution.stat().st_size}
    work = None
    cleanup_safe = True
    reference = None
    expected_engine = None
    worker_hc = None

    def save():
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')

    def stage(name):
        report['stage'] = name
        save()
        print('SOURCE_ON_STAGE ' + json.dumps({'stage': name, 'completed_primary_calls': len(report['trials'])}, separators=(',', ':')), flush=True)
    h.progress = lambda message: print('SOURCE_ON_ACTIVITY ' + json.dumps({'stage': report['stage']}, separators=(',', ':')), flush=True)

    def memory():
        x = host()
        if x['available_bytes'] < 1024 ** 3:
            report['low_memory_stop'] = x
            save()
            raise RuntimeError('Available memory below1GiB')

    def close_browser(browser, pid):
        nonlocal cleanup_safe
        targets = []
        try:
            root = psutil.Process(pid)
            targets = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            pass
        closed = True
        try:
            browser.close()
        except Exception:
            closed = False
        _, pending = psutil.wait_procs(targets, timeout=10)
        alive = []
        for q in pending:
            try:
                if q.is_running() and q.status() != psutil.STATUS_ZOMBIE:
                    alive.append(q)
            except psutil.NoSuchProcess:
                pass
        good = closed and bool(targets) and (not alive)
        cleanup_safe = cleanup_safe and good
        return {'observed_processes': len(targets), 'remaining_processes': len(alive), 'confirmed': good}
    try:
        from playwright.sync_api import sync_playwright
        work = Path(tempfile.mkdtemp(prefix='inplace-source-confirm-private-'))
        query = json.dumps(h.prepared_query(10), separators=(',', ':'), ensure_ascii=False).encode()
        need(sha(query) == QUERY_SHA, 'Prepared query pin')
        (work / 'query.json').write_bytes(query)
        shutil.copyfile(entries['original']['model'], work / 'sample.vvm')
        (work / 'index.html').write_text(h.BROWSER_PAGE)
        (work / 'worker.js').write_text(h.BROWSER_WORKER)
        for key, e in entries.items():
            folder = work / key
            folder.mkdir()
            shutil.copyfile(e['binary'], folder / 'voicevox_benchmark.js')
            shutil.copyfile(e['binary'].with_suffix('.wasm'), folder / 'voicevox_benchmark.wasm')
            os.link(work / 'sample.vvm', folder / 'sample.vvm')
        (work / 'runtime_manifest.json').write_text(json.dumps({k: e['worker'] for k, e in entries.items()}))
        shutil.copyfile(a.dispatch_helper, work / 'core_dispatch_check.js')
        shutil.copyfile(BROWSER / 'source_alias_check.js', work / 'source_alias_check.js')
        hookfree = (BROWSER / 'source_worker.js').read_text()
        gateworker = hookfree.replace('/*GATE_IMPORT*/', "importScripts('/source_alias_check.js');").replace('/*GATE_BEFORE*/', 'const hook=installLeakyAliasCheck(Module,wasmTable,entry,variant);').replace('/*GATE_AFTER*/', 'hook.restore();alias.push(hook.result);').replace("timing_instrumentation:'clock_only'", "timing_instrumentation:'alias_gate_only'")
        report['worker_source_sha256'] = {'primary': sha(hookfree.encode()), 'untimed_alias': sha(gateworker.encode())}
        save()
        with h.serve_assets(work) as url, sync_playwright() as pw:
            stage('activation_gates')
            seen = {}
            for key in KEYS:
                identity = (provenance[key]['wasm_sha256'], provenance[key]['js_sha256'])
                need(identity not in seen, 'Each condition must identify a distinct source artifact')
                seen[identity] = key
                mode = h.Mode(key, 'Pristine XNN auto/splat source ' + key, 2, 'untimed ON activation', experimental=True, fixed_shape=True, spin_off=True, execution_provider='XNNPACK', revectorize=True)
                activation = h.verify_research_revectorization({'model_target': 'vocoder'}, mode, {'headless': True, 'args': FLAGS}, url, work, work / 'activation-private.json', 302, require_on=True)
                fields = ['product', 'js_version', 'transformed_groups', 'revectorizable_nodes', 'flag_rejected', 'launch_configuration_matches', 'trace_sha256']
                record = {name: {field: activation[name][field] for field in fields} for name in ['off', 'on']}
                record.update(wasm_sha256=identity[0], js_sha256=identity[1], verified=activation['on_verified'])
                report['activation'][key] = record
                save()
                need(record['verified'] and record['on']['transformed_groups'] > 0 and (record['on']['revectorizable_nodes'] > 0), 'Positive per-binary ON activation')
                engine = {field: record['on'][field] for field in ['product', 'js_version']}
                expected_engine = engine if expected_engine is None else expected_engine
                need(engine == expected_engine, 'Diagnostic engine changed')

            def open_browser(key):
                nonlocal worker_hc
                memory()
                browser = pw.chromium.launch(headless=True, args=FLAGS)
                pid = h.chromium_process_id(browser)
                try:
                    engine = h.browser_engine_info(browser)
                    need(engine == expected_engine, 'Engine changed')
                    page = browser.new_page()
                    page.goto(url)
                    info = page.evaluate('d=>request(d)', {'command': 'init', 'variant': key})
                    need(info['ready'] and info['wasm_sha256'] == entries[key]['worker']['wasm_sha256'] and (info['js_sha256'] == entries[key]['worker']['js_sha256']), 'Loaded artifact gate')
                    dispatch = info['dispatch']
                    need(dispatch['verified'] and dispatch['actual_dispatch'] == 'splat' and (dispatch['splat_pointers'] == 12) and (dispatch['loadsplat_pointers'] == 0) and (dispatch['checked_pointers'] == 12) and (dispatch['worker_hardware_concurrency'] == 4) and (dispatch['probe_hardware_concurrency'] == 4), 'Actual HC4/12splat')
                    worker_hc = 4
                    info.update(engine=engine, browser_pid=pid, browser_created=psutil.Process(pid).create_time(), launch_flags=FLAGS)
                    return (browser, page, pid, info)
                except BaseException:
                    close_browser(browser, pid)
                    raise

            def check(page, mode, container, phase, index, with_alias=False):
                nonlocal reference
                memory()
                v = page.evaluate('d=>request(d)', {'command': 'check'})
                wav = bytes(v['wav'])
                raw = bytes(v['raw'])
                row = {'mode': mode, 'phase': phase, 'iteration': index, 'wav_bytes': len(wav), 'raw_bytes': len(raw), 'wav_sha256': sha(wav), 'raw_sha256': sha(raw), 'exact_bytes': False}
                container.append(row)
                if with_alias:
                    row['alias'] = v['alias']
                else:
                    need(not v['alias'], 'Primary worker unexpectedly instrumented')
                save()
                if reference is None:
                    need(mode == 'original' and phase == 'gate' and (index == 1), 'Reference must be first original ON output')
                    reference = (wav, raw)
                metrics = h.waveform_comparison(*reference, wav, raw)
                row['pcm_sha256'] = metrics['pcm_sha256']
                row['finite'] = metrics['finite']
                row['exact_bytes'] = (wav, raw) == reference
                row['fp32_exact'] = metrics['fp32_exact']
                row['pcm_exact'] = metrics['pcm_exact']
                row['wav_format'] = metrics['wav_format']
                row['fp32_samples'] = metrics['fp32_samples']
                if report['reference'] is None:
                    report['reference'] = {'mode': 'original', 'phase': 'gate', 'iteration': 1, **{name: row[name] for name in ['wav_sha256', 'raw_sha256', 'pcm_sha256']}}
                save()
                need(row['finite'] and row['exact_bytes'] and row['fp32_exact'] and row['pcm_exact'], 'Every ON output must equal same original ON reference')
                if with_alias:
                    need(len(row['alias']) == 2, 'Raw/WAV alias coverage')
                    for x in row['alias']:
                        alias_gate(x, mode, entries[mode]['worker'])
            stage('alias_output_gates')
            (work / 'worker.js').write_text(gateworker)
            for mode in KEYS:
                browser, page, pid, info = open_browser(mode)
                record = {'initialization': info, 'checks': []}
                report['browser_gate'][mode] = record
                save()
                try:
                    for i in [1, 2]:
                        check(page, mode, record['checks'], 'gate', i, True)
                finally:
                    record['cleanup'] = close_browser(browser, pid)
                    save()
                    need(record['cleanup']['confirmed'], 'Gate browser cleanup')
            (work / 'worker.js').write_text(hookfree)
            stage('reviewed_release_gate')
            report['release_gate'] = release_gate(provenance, expected_engine, report['reference'])
            report['gates_complete_before_timing'] = True
            report['status'] = 'running'
            report['initial_host'] = host()
            save()
            for set_id in [1, 2, 3]:
                stage('primary_set_' + str(set_id))
                browsers = {}
                pages = {}
                pids = {}
                group = {'id': set_id, 'creation_order': list(KEYS[set_id - 1:] + KEYS[:set_id - 1]), 'runtime': {}, 'warmups': [], 'checks': [], 'sentinels': [], 'cleanup': {}}
                report['process_sets'].append(group)
                save()

                def call(mode):
                    return pages[mode].evaluate('d=>request(d)', {'command': 'synthesize'})
                try:
                    order = group['creation_order']
                    for mode in order:
                        browser, page, pid, info = open_browser(mode)
                        browsers[mode] = browser
                        pages[mode] = page
                        pids[mode] = pid
                        group['runtime'][mode] = info
                        save()
                        for iteration in range(1, 6):
                            memory()
                            group['warmups'].append({'mode': mode, 'iteration': iteration, **call(mode)})
                            save()
                        for i in [1, 2]:
                            check(page, mode, group['checks'], 'before', i)
                    start = time.monotonic()
                    before = {m: usage(pids[m]) for m in KEYS}
                    time.sleep(1)
                    after = {m: usage(pids[m]) for m in KEYS}
                    interval = time.monotonic() - start
                    group['idle'] = {'interval_s': interval, 'modes': {m: {'before': before[m], 'after': after[m], **idle_cpu(before[m], after[m], interval)} for m in KEYS}}
                    save()
                    memory()
                    group['sentinels'].append({'position': 'before', **call('original')})
                    save()
                    for block in [x for x in schedule() if x['process_set'] == set_id]:
                        for mode in block['order']:
                            memory()
                            before = {'host': host(), 'processes': usage(pids[mode])}
                            row = {'process_set': set_id, 'pair': block['pair'], 'mode': mode, 'pair_order': block['order'], **call(mode), 'before': before}
                            report['trials'].append(row)
                            save()
                            after = {'host': host(), 'processes': usage(pids[mode])}
                            row.update(after=after, resource_observations=resource_observations(before, after))
                            save()
                            need(math.isfinite(row['elapsed_s']) and row['elapsed_s'] > 0, 'Latency validity')
                            stop = pressure_during_call(before, after)
                            if stop:
                                raise RuntimeError(stop)
                    memory()
                    group['sentinels'].append({'position': 'after', **call('original')})
                    save()
                    for mode in KEYS:
                        for i in [1, 2]:
                            check(pages[mode], mode, group['checks'], 'after', i)
                finally:
                    for mode, browser in browsers.items():
                        group['cleanup'][mode] = close_browser(browser, pids[mode])
                        save()
                    need(cleanup_safe, 'Primary browser descendants remain')
            reference = None
            report['status'] = 'complete'
            report['stage'] = 'complete'
            save()
    except BaseException as e:
        report['status'] = 'failed'
        report['error_type'] = type(e).__name__
        save()
        print('SOURCE_ON_FAILURE ' + json.dumps({'stage': report['stage'], 'error_type': type(e).__name__, 'completed_primary_calls': len(report['trials']), 'gates_complete_before_timing': report['gates_complete_before_timing']}, separators=(',', ':')), flush=True)
        raise
    finally:
        reference = None
        report['private_native_dump_retained'] = False
        if work is not None and cleanup_safe:
            shutil.rmtree(work)
        report['private_temporary_directory_deleted'] = work is None or not work.exists()
        save()
    try:
        validate(report, capture_only=a.capture_only)
    except BaseException as e:
        report['status'] = 'failed'
        report['error_type'] = type(e).__name__
        save()
        raise
if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit(1) from None
