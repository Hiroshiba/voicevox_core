#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0", "onnx==1.19.1", "protobuf==6.32.1", "ml-dtypes==0.5.1"]
# ///
"""Proposed CI-only orchestration; NOT required by the distributed one-file script.

All runtime helpers, the canonical harness and dispatch checker come exclusively
from the supplied one-file candidate. This file adds only correctness-smoke
orchestration. It never shortens or relabels the 3x5 timing confirmation.

Source draft, not yet executed. Run only after explicit CI review/authorization.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def read_json(path):
    return json.loads(Path(path).read_text('utf-8'))


def write_json(candidate, path, value):
    candidate.kernel_check_report_privacy(value)
    candidate.atomic_write(path, json.dumps(value, indent=2, allow_nan=False).encode())


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--prepare-result', type=Path)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--log-results', action='store_true', help='print fixed-schema numeric CI evidence only')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--harness', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--manifest', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--validator', type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def verify_build(candidate, entry, archives):
    binary = Path(entry['binary'])
    receipt_path = binary.parent / 'complete.json'
    receipt = read_json(receipt_path)
    require(receipt['identity'] == entry['build_identity'], 'Build identity differs from manifest')
    require(receipt['identity']['runtime'] == archives[entry['key']], 'Unrecognized runtime archive')
    needed = {binary.name, binary.with_suffix('.wasm').name,
              'native-bundle-verification.json', 'cargo-link.log'}
    require(needed <= receipt['files'].keys(), 'Missing files/proofs in build receipt')
    for filename, expected in receipt['files'].items():
        require(digest(binary.parent / filename) == expected, 'Build/proof file hash mismatch')
    bundle = read_json(binary.parent / 'native-bundle-verification.json')
    require(bundle['verified'] is True and bundle['runtime_sha256'] == archives[entry['key']],
            'Native bundle verification mismatch')
    require(bundle['invalidated_packages'] == ['voicevox_core', 'voicevox_benchmark'],
            'Both bundling crates must be invalidated')
    require(entry['model_sha256'] == candidate.KERNEL_MODEL_SHA256
            and digest(entry['model']) == candidate.KERNEL_MODEL_SHA256, 'Original model pin mismatch')
    return {'receipt_sha256': digest(receipt_path), 'receipt': receipt, 'native_bundle': bundle,
            'model_sha256': entry['model_sha256']}


def browser_worker(args, candidate):
    """Sequential correctness only; all measured durations are discarded."""
    import psutil
    from playwright.sync_api import sync_playwright
    require(digest(args.harness) == candidate.KERNEL_BROWSER_SHA256, 'Browser harness source changed')
    harness = load_module(args.harness, 'standalone_smoke_harness')
    validator = load_module(args.validator, 'standalone_smoke_validator')
    manifest = read_json(args.manifest)
    entries = manifest['variants']
    require([entry['key'] for entry in entries] == list(candidate.KERNEL_MODES), 'Three controls required')
    require([entry['dispatch_expected'] for entry in entries] == ['auto', 'auto', 'loadsplat'],
            'Actual dispatcher expectations changed')
    for gate in ('require_exact_fp32_pcm_to_reference', 'require_actual_core_browser_dispatch',
                 'require_repeated_raw_fp32_and_pcm_checks'):
        require(manifest.get(gate) is True, 'Missing runtime correctness gate')
    for entry in entries:
        require(entry['provider'] == 'XNNPACK' and entry['threads'] == 2 and entry['fixed_shape']
                and entry['spin_off'] and entry['model_target'] == 'vocoder'
                and entry['revectorize'] is False, 'Smoke fixture differs from reviewed OFF setup')
    report = {'schema': 'voicevox-standalone-sequential-correctness-smoke-v1', 'status': 'initializing',
              'scope': 'untimed correctness only; not a timing confirmation or speedup measurement',
              'latency_measurements_retained': False, 'cpu_sampling_performed': False,
              'sequential_browser_processes': True, 'revectorization': 'off',
              'candidate_sha256': digest(args.candidate), 'browser_harness_sha256': digest(args.harness),
              'manifest_sha256': digest(args.manifest), 'provenance': {}, 'diagnostics': {}, 'conditions': {},
              'notes': ['Each reviewed V8 diagnostic performs three untimed syntheses.',
                        'Each correctness condition performs two synthesize/save calls and two raw captures.',
                        'Raw capture can execute inference; no captured duration is used as latency data.',
                        'Waveform/model bytes are private and never added to this report.']}
    output = args.results / 'worker-correctness.json'
    def save():
        write_json(candidate, output, report)
    def memory_gate():
        available = psutil.virtual_memory().available
        report['available_memory_before_latest_browser_bytes'] = available
        save()
        require(available >= 1024**3, 'Available memory below 1 GiB')
    def exact(metrics):
        return metrics['finite'] and metrics['pcm_exact'] and metrics['fp32_exact']
    private = None
    reference = None
    fixed_length = None
    worker_hc = None
    expected_engine = None
    save()
    try:
        with tempfile.TemporaryDirectory(prefix='sequential-correctness-private-') as directory:
            private = Path(directory)
            query = json.dumps(harness.prepared_query(10), separators=(',', ':'), ensure_ascii=False).encode()
            report['query_sha256'] = hashlib.sha256(query).hexdigest()
            require(report['query_sha256'] == manifest['fixture_query_sha256'], 'Query fixture changed')
            (private / 'query.json').write_bytes(query)
            (private / 'index.html').write_text(harness.BROWSER_PAGE)
            (private / 'worker.js').write_text(harness.BROWSER_WORKER)
            for entry in entries:
                report['provenance'][entry['key']] = verify_build(candidate, entry, validator.ARCHIVES)
                save()
                binary = Path(entry['binary'])
                folder = private / entry['key']
                folder.mkdir()
                for suffix in ('.js', '.wasm'):
                    source = binary.with_suffix(suffix)
                    destination = folder / ('voicevox_benchmark' + suffix)
                    shutil.copyfile(source, destination)
                    require(digest(destination) == digest(source), 'Staged browser asset changed')
                shutil.copyfile(entry['model'], folder / 'sample.vvm')
            with harness.serve_assets(private) as url, sync_playwright() as playwright:
                for entry in entries:
                    key = entry['key']
                    mode = harness.Mode(key, entry['label'], 2, 'untimed correctness smoke',
                                        experimental=key != 'original', fixed_shape=True, spin_off=True,
                                        execution_provider='XNNPACK', revectorize=False)
                    memory_gate()
                    diagnostic = harness.verify_research_revectorization(
                        entry, mode, {'headless': True}, url, private, output, 302, require_on=False)
                    report['diagnostics'][key] = diagnostic
                    save()
                    require(diagnostic.get('off_verified') is True and not diagnostic.get('on_verified'),
                            'Explicit OFF was not independently verified')
                    memory_gate()
                    browser = playwright.chromium.launch(headless=True, args=['--js-flags=--no-wasm-revectorize'])
                    runner = None
                    try:
                        engine = harness.browser_engine_info(browser)
                        matched = {field: diagnostic['off'][field] for field in ('product', 'js_version')}
                        require(engine == matched, 'Actual engine differs from OFF diagnostic')
                        if expected_engine is None:
                            expected_engine = engine
                        require(engine == expected_engine, 'Engine changed between sequential conditions')
                        runner = harness.BrowserRunner(
                            browser, url, '/' + key + '/voicevox_benchmark.js', 2, True, 302,
                            private / (key + '.wav'), model_url='/' + key + '/sample.vvm',
                            fixed_shape=True, spin_off=True, xnn_threads=2, model_target='vocoder')
                        condition = {'engine': engine, 'browser_pid': harness.chromium_process_id(browser),
                                     'shared_memory': runner.shared_memory, 'xnn_threads': runner.xnn_threads,
                                     'xnn_sessions': runner.xnn_sessions, 'fixed_length': runner.fixed_length,
                                     'fixed_matches': runner.fixed_matches, 'pthreads_after_init': runner.thread_state()}
                        report['conditions'][key] = condition
                        condition['dispatch'] = runner.page.evaluate(
                            'data => request(data)', {'command': 'dispatch', 'expected_dispatch': entry['dispatch_expected']})['dispatch']
                        save()  # Retain bounded actual-worker failure evidence before its gate.
                        proof = condition['dispatch']
                        require(proof.get('verified') is True and proof['checked_pointers'] == 12,
                                'Actual CORE dedicated-worker pointer verification failed')
                        if worker_hc is None:
                            worker_hc = proof['worker_hardware_concurrency']
                            fixed_length = runner.fixed_length
                        require(proof['worker_hardware_concurrency'] == worker_hc
                                and runner.fixed_length == fixed_length, 'Worker context or fixed shape changed')
                        first = None
                        for index in range(2):
                            runner.synthesize(save=True)  # Return durations deliberately discarded.
                            current = (runner.wav.read_bytes(), runner.raw_wave())
                            if first is None:
                                first = current
                                if reference is None:
                                    reference = current
                                metrics = harness.waveform_comparison(*reference, *current)
                                condition['versus_untouched_xnn'] = metrics
                            else:
                                metrics = harness.waveform_comparison(*first, *current)
                                condition['repeat'] = metrics
                            save()  # Numeric errors persist before assertion; no raw arrays are written.
                            require(exact(metrics), 'FP32/PCM equality or repeatability gate failed')
                        condition['pthreads_after_checks'] = runner.thread_state()
                        save()
                        require(condition['pthreads_after_checks'] == 1, 'Unexpected XNN2 pthread ownership')
                        first = current = None
                    finally:
                        if runner is not None:
                            runner.close()
                        browser.close()
                report['engine'] = expected_engine
                report['selector_noop_on_this_worker'] = all(
                    item['dispatch']['actual_dispatch'] == 'loadsplat' for item in report['conditions'].values())
        reference = None
        report['private_temporary_directory_deleted'] = not private.exists()
        require(report['private_temporary_directory_deleted'], 'Private smoke files were not removed')
        report['status'] = 'passed'
        save()
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__)
        save()
        raise
    finally:
        reference = None
        report['private_temporary_directory_deleted'] = private is None or not private.exists()
        save()


def controller(args, candidate):
    require(args.prepare_result is not None, '--prepare-result is required')
    prepared = read_json(args.prepare_result)
    require(prepared['status'] == 'prepared_only' and prepared['private_helpers_deleted'] is True,
            'Need successful one-file preparation-only result')
    require(prepared['standalone_sha256'] == args.expected_sha256
            and prepared['embedded_source_sha256'] == candidate.KERNEL_SOURCE_SHA256,
            'Preparation did not execute this exact one-file distribution')
    args.results.mkdir(parents=True, exist_ok=True)
    receipt = {'schema': 'voicevox-standalone-ci-correctness-v1', 'status': 'initializing',
               'candidate_sha256': args.expected_sha256, 'prepare_result_sha256': digest(args.prepare_result),
               'test_orchestrator_sha256': digest(Path(__file__)), 'timing_matrix_executed': False,
               'scope': 'one-file preparation plus sequential untimed actual-Worker correctness and CPU reference'}
    output = args.results / 'smoke-run.json'
    def save():
        write_json(candidate, output, receipt)
    environment = dict(os.environ)
    environment.pop('PYTHONOPTIMIZE', None)
    environment['PYTHONUNBUFFERED'] = '1'
    environment['PLAYWRIGHT_BROWSERS_PATH'] = str(args.cache.resolve() / 'playwright')
    private = None
    save()
    try:
        with candidate.kernel_private_directory() as directory:
            private = Path(directory)
            child_temp = private / 'temporary'
            child_temp.mkdir(mode=0o700)
            for key in ('TMPDIR', 'TMP', 'TEMP'):
                environment[key] = str(child_temp)
            sources = candidate.kernel_materialize_sources(private)
            browser = private / 'browser_runtime.py'
            candidate.kernel_run_checked(
                [sys.executable, str(sources['runtime/apply_browser_dispatch.py']),
                 str(sources['voicevox_webcpu_research.py']), '--output', str(browser),
                 '--helper', str(sources['runtime/core_dispatch_check.js'])],
                args.results / 'generate-browser.log', environment)
            require(digest(browser) == candidate.KERNEL_BROWSER_SHA256, 'Generated browser SHA mismatch')
            manifest = {key: value for key, value in prepared['manifest'].items() if key != 'variants'}
            rows = []
            for stored, name in zip(prepared['manifest']['variants'], ('original', 'auto-roundtrip', 'loadsplat')):
                entry = read_json(args.cache / 'vocoder-dispatch-v3' / ('prepared-' + name + '.json'))
                public = {key: value for key, value in entry.items() if key not in ('model', 'binary')}
                require(public == stored, 'Prepared runtime cache row differs from one-file proof')
                rows.append(entry)
            require(len(rows) == 3, 'Missing prepared runtime')
            manifest['variants'] = rows
            manifest_path = private / 'runtime.json'
            write_json(candidate, manifest_path, manifest)
            receipt['stage'] = 'sequential actual-Worker correctness'
            save()
            candidate.kernel_run_checked(
                [sys.executable, str(Path(__file__).resolve()), '--worker', '--candidate', str(args.candidate),
                 '--expected-sha256', args.expected_sha256, '--cache', str(args.cache), '--results', str(args.results),
                 '--harness', str(browser), '--manifest', str(manifest_path),
                 '--validator', str(sources['runtime/validate_runtime_confirmation.py'])],
                args.results / 'worker-correctness.log', environment)
            checks = read_json(args.results / 'worker-correctness.json')
            require(checks['status'] == 'passed' and checks['private_temporary_directory_deleted'],
                    'Worker correctness/cleanup failed')
            evidence = args.results / 'activation-evidence.json'
            write_json(candidate, evidence, checks['diagnostics'])
            assets = read_json(args.cache / 'research-assets.json')
            cpu = Path(assets['binaries']['mt'])
            receipt['stage'] = 'sequential same-run CPU reference'
            save()
            candidate.kernel_run_checked(
                [sys.executable, str(sources['runtime/browser_reference_and_profile.py']), '--harness', str(browser),
                 '--manifest', str(manifest_path), '--cpu-binary', str(cpu), '--cpu-receipt', str(cpu.parent / 'complete.json'),
                 '--activation-evidence', str(evidence), '--revec', 'off',
                 '--output', str(args.results / 'cpu-reference.json')],
                args.results / 'cpu-reference.log', environment)
            quality = read_json(args.results / 'cpu-reference.json')
            require(quality['passed'] is True and quality['private_temporary_directory_deleted'] is True,
                    'Same-run CPU reference/cleanup failed')
            require(quality['engine'] == checks['engine'], 'CPU reference used a different browser engine')
            require(quality['references']['xnn']['dispatch']['worker_hardware_concurrency']
                    == checks['conditions']['original']['dispatch']['worker_hardware_concurrency'],
                    'Worker context changed for CPU reference')
            receipt['xnn_vs_cpu'] = quality['xnn_vs_cpu']
            receipt['actual_dispatch'] = {key: value['dispatch'] for key, value in checks['conditions'].items()}
            receipt['selector_noop_on_this_worker'] = checks['selector_noop_on_this_worker']
            receipt['status'] = 'validated'
            save()
        receipt['private_helpers_deleted'] = not private.exists()
        receipt['stage'] = 'artifact privacy verification'
        save()
        candidate.kernel_check_artifacts(args.results)
        receipt.update(status='passed', stage='finished')
        save()
    except BaseException as error:
        receipt.update(status='failed', error_type=type(error).__name__)
        save()
        raise
    finally:
        receipt['private_helpers_deleted'] = private is None or not private.exists()
        save()


def log_numeric_results(args, candidate):
    """Fixed-schema console evidence; never dump arbitrary JSON, logs or arrays."""
    import math
    import re
    def sha(value):
        require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value), 'Invalid SHA field')
        return value
    def number(value, integer=False):
        require(type(value) is int if integer else type(value) in (int, float), 'Invalid number type')
        require(math.isfinite(value) and value >= 0, 'Invalid numeric metric')
        return value
    def boolean(value):
        require(type(value) is bool, 'Invalid boolean metric')
        return value
    def metrics(value):
        result = {}
        integer_fields = {'fp32_samples', 'pcm_changed_samples', 'pcm_max_abs_lsb', 'fp32_changed_samples'}
        float_fields = {'pcm_rmse_lsb', 'fp32_max_abs', 'fp32_rmse', 'fp32_relative_rmse', 'relative_rms_floor'}
        for key in ('finite', 'pcm_exact', 'fp32_exact'):
            if key in value: result[key] = boolean(value[key])
        for key in integer_fields | float_fields:
            if key in value: result[key] = number(value[key], key in integer_fields)
        for key in ('pcm_sha256', 'fp32_sha256'):
            if key in value: result[key] = sha(value[key])
        if 'wav_format' in value:
            result['wav_format'] = {key: number(value['wav_format'][key], True)
                                    for key in ('channels', 'sample_bytes', 'sample_rate', 'frames')}
        return result
    def dispatch(value):
        result = {'verified': value.get('verified') is True}
        for key in ('source', 'context'):
            expected = 'actual_CORE_module' if key == 'source' else 'dedicated_worker'
            require(value.get(key) == expected, 'Unexpected dispatch provenance')
            result[key] = expected
        for key in ('expected_dispatch', 'actual_dispatch'):
            if key in value:
                require(value[key] in ('auto', 'splat', 'loadsplat'), 'Unexpected pointer family')
                result[key] = value[key]
        for key in ('worker_hardware_concurrency', 'probe_hardware_concurrency', 'checked_pointers',
                    'splat_pointers', 'loadsplat_pointers', 'mr', 'nr', 'probe_return_code'):
            if value.get(key) is not None: result[key] = number(value[key], True)
        for key in ('is_x86', 'relaxed_simd', 'cross_origin_isolated', 'shared_memory'):
            if key in value: result[key] = boolean(value[key])
        return result
    def engine(value):
        require(re.fullmatch(r'[A-Za-z]+/[0-9]+(?:\.[0-9]+){1,5}', value['product']), 'Invalid browser version')
        require(re.fullmatch(r'[0-9][0-9A-Za-z.+_-]{0,64}', value['js_version']), 'Invalid V8 version')
        return {key: value[key] for key in ('product', 'js_version')}
    def status(value):
        require(value in ('initializing', 'prepared_only', 'validated', 'passed', 'failed'), 'Unknown status')
        return value
    candidate.kernel_check_artifacts(args.results)
    result = {'schema': 'standalone-ci-smoke-numeric-v1', 'candidate_sha256': sha(args.expected_sha256),
              'timing_matrix_executed': False, 'cpu_sampling_performed': False}
    prepared_path = args.results / 'prepared.json'
    if prepared_path.exists():
        prepared = read_json(prepared_path)
        result['preparation'] = {'status': status(prepared['status']), 'receipt_sha256': digest(prepared_path),
                                 'private_helpers_deleted': boolean(prepared['private_helpers_deleted']), 'variants': []}
        for row in prepared['manifest']['variants']:
            require(row['key'] in candidate.KERNEL_MODES, 'Unknown prepared runtime')
            bundle = row['native_bundle']
            require(bundle['invalidated_packages'] == ['voicevox_core', 'voicevox_benchmark'], 'Invalid invalidation proof')
            result['preparation']['variants'].append({
                'mode': row['key'], 'runtime_archive_sha256': sha(row['runtime_archive_sha256']),
                'build_adapter_sha256': sha(row['build_identity']['build_adapter_sha256']),
                'wrapper_sha256': sha(row['build_identity']['wrapper']),
                'dispatcher_sha256': sha(bundle['dispatcher_sha256']),
                'native_bundle_verified': boolean(bundle['verified']),
                'invalidated_packages': ['voicevox_core', 'voicevox_benchmark']})
    checks_path = args.results / 'worker-correctness.json'
    if checks_path.exists():
        checks = read_json(checks_path)
        output = {'status': status(checks['status']), 'report_sha256': digest(checks_path),
                  'private_temporary_directory_deleted': checks.get('private_temporary_directory_deleted') is True,
                  'conditions': {}, 'builds': {}}
        if 'engine' in checks: output['engine'] = engine(checks['engine'])
        for mode, proof in checks['provenance'].items():
            require(mode in candidate.KERNEL_MODES, 'Unknown build mode')
            safe_files = {}
            for filename, digest_value in proof['receipt']['files'].items():
                require(re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', filename), 'Invalid receipt filename')
                safe_files[filename] = sha(digest_value)
            output['builds'][mode] = {'receipt_sha256': sha(proof['receipt_sha256']), 'files': safe_files,
                                     'model_sha256': sha(proof['model_sha256'])}
        for mode, condition in checks['conditions'].items():
            require(mode in candidate.KERNEL_MODES, 'Unknown condition')
            safe = {'engine': engine(condition['engine'])}
            for key in ('fixed_length', 'fixed_matches', 'xnn_threads', 'xnn_sessions',
                        'pthreads_after_init', 'pthreads_after_checks'):
                if key in condition: safe[key] = number(condition[key], True)
            if 'dispatch' in condition: safe['dispatch'] = dispatch(condition['dispatch'])
            for key in ('versus_untouched_xnn', 'repeat'):
                if key in condition: safe[key] = metrics(condition[key])
            output['conditions'][mode] = safe
        result['worker_correctness'] = output
    quality_path = args.results / 'cpu-reference.json'
    if quality_path.exists():
        quality = read_json(quality_path)
        safe = {'passed': quality.get('passed') is True, 'report_sha256': digest(quality_path),
                'private_temporary_directory_deleted': quality.get('private_temporary_directory_deleted') is True,
                'references': {}}
        if quality.get('engine'): safe['engine'] = engine(quality['engine'])
        if 'xnn_vs_cpu' in quality: safe['xnn_vs_cpu'] = metrics(quality['xnn_vs_cpu'])
        for label in ('cpu', 'xnn'):
            if label not in quality['references']: continue
            reference = quality['references'][label]
            safe['references'][label] = {key: number(reference[key], True)
                                         for key in ('threads', 'configured_ort_global_threads', 'configured_xnn_threads',
                                                     'fixed_length', 'fixed_matches', 'xnn_sessions') if key in reference}
            if 'repeat_comparison' in reference: safe['references'][label]['repeat'] = metrics(reference['repeat_comparison'])
            if 'dispatch' in reference: safe['references'][label]['dispatch'] = dispatch(reference['dispatch'])
        result['cpu_reference'] = safe
    run_path = args.results / 'smoke-run.json'
    if run_path.exists():
        run = read_json(run_path)
        result['smoke'] = {'status': status(run['status']), 'receipt_sha256': digest(run_path),
                           'private_helpers_deleted': run.get('private_helpers_deleted') is True,
                           'test_orchestrator_sha256': sha(run['test_orchestrator_sha256'])}
    print('STANDALONE_SMOKE_NUMERIC_BEGIN')
    print(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False))
    print('STANDALONE_SMOKE_NUMERIC_END')


def main():
    args = parse_args()
    args.candidate = args.candidate.resolve()
    args.cache = args.cache.resolve()
    args.results = args.results.resolve()
    require(__debug__, 'Run without Python -O/PYTHONOPTIMIZE')
    require(digest(args.candidate) == args.expected_sha256, 'Unexpected candidate source SHA')
    candidate = load_module(args.candidate, 'standalone_ci_candidate')
    if args.log_results:
        log_numeric_results(args, candidate)
    elif args.worker:
        require(args.harness and args.manifest and args.validator, 'Worker inputs missing')
        browser_worker(args, candidate)
    else:
        controller(args, candidate)


if __name__ == '__main__':
    main()
