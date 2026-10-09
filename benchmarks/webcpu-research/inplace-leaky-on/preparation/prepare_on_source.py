#!/usr/bin/env python3
"""Prepare the pinned source-built exact-inplace LeakyRelu study. No inference.

Requires the canonical harness prepare-only assets and prepared pristine original runtime.
This helper downloads pinned sources, configures ORT, compiles only activations.cc,
then builds two CORE consumers. It does not build the whole ORT archive, execute
models, change precision, run timing variants, or publish/upload anything.
"""
from __future__ import annotations
import argparse
import contextlib
from collections import Counter
import copy
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from source_proof import (archive_entries, archive_manifest, compare_candidate,
    compare_control, file_sha, replace_activation, require, sha,
    verify_archive_replacement)
from callback_discovery import discover

ROOT = Path(__file__).resolve().parent
PINS = json.loads((ROOT / 'source_pins.json').read_text())
FLAGS = '-fwasm-exceptions -fno-fast-math -ffp-contract=off'
KEYS = ('original', 'rebuilt', 'candidate')

STAGE_PHASES = frozenset({
    'verify_inputs', 'configure', 'generate_headers', 'audit_toolchain',
    'compile_control', 'audit_control_dependencies', 'verify_control_ir',
    'apply_source_patch', 'compile_candidate', 'audit_candidate_dependencies',
    'verify_candidate_ir', 'replace_archives', 'link_rebuilt_core',
    'link_candidate_core', 'link_original_core', 'discover_callbacks', 'write_manifest',
})
STAGE_EXCEPTION_CLASSES = frozenset({
    'ValueError', 'FileNotFoundError', 'PermissionError', 'OSError',
    'RuntimeError', 'CalledProcessError', 'TimeoutExpired',
    'KeyboardInterrupt', 'SystemExit', 'OtherError',
})


def emit_stage(phase, status, exception_class=None):
    require(phase in STAGE_PHASES and status in {'begin', 'pass', 'fail'}, 'Invalid preparation stage event')
    value = {'schema': 'inplace-source-prep-stage-v1', 'phase': phase, 'status': status}
    if exception_class is not None:
        require(status == 'fail' and exception_class in STAGE_EXCEPTION_CLASSES, 'Invalid preparation stage exception class')
        value['exception_class'] = exception_class
    print('SOURCE_PREP_STAGE ' + json.dumps(value, sort_keys=True), flush=True)


@contextlib.contextmanager
def stage(phase):
    emit_stage(phase, 'begin')
    try:
        yield
    except BaseException as error:
        name = type(error).__name__
        emit_stage(phase, 'fail', name if name in STAGE_EXCEPTION_CLASSES else 'OtherError')
        raise
    else:
        emit_stage(phase, 'pass')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def check_payloads(args):
    for name, digest in [('inplace_leaky.patch', PINS['source_patch_sha256']),
                         ('xnnpack-static-dependencies.patch', PINS['release_patch_sha256'])]:
        require(file_sha(ROOT / name) == digest, 'Source payload pin mismatch: ' + name)
    require(file_sha(args.harness) == PINS['harness_sha256'], 'Canonical harness pin mismatch')
    require(file_sha(args.runtime_preparer) == PINS['runtime_preparer_sha256'], 'Runtime preparer pin mismatch')


def dry_run_plan(args):
    check_payloads(args)
    return {'schema': 'inplace-leaky-on-source-plan-v1', 'dry_run': True,
        'performed_builds': False, 'performed_inference': False, 'performed_downloads': False,
        'manifest_schema': 'inplace-leaky-on-source-manifest-v1', 'variant_keys': list(KEYS),
        'required_existing_cache': ['research-assets.json', 'vocoder-dispatch-v3/prepared-original.json', 'research-assets.json:runtimes.browser_xnnpack pristine archive'],
        'source_refs': source_refs(),
        'steps': ['Verify canonical helper sources and pristine original complete receipt',
                  'Download/hash pinned ORT tarball; apply pinned release-only dependency patch',
                  'Configure ORT with strict FP32 SIMD/pthread/XNN flags; generate three ONNX proto header targets',
                  'Check exact activation compile arguments, includes, compiler commit, and 1309 dependency hashes',
                  'Compile original activations.cc and prove all 372 definitions exact against archive occurrence1',
                  'Apply only pinned exact-inplace source patch; compile same TU and prove complete candidate IR pin and scoped metadata graph',
                  'Replace only member 514 in 1438-member pristine archive; preserve occurrence 2; regenerate archive symbol index',
                  'Build rebuilt and candidate CORE with both packages invalidated and all ordered native payloads verified',
                  'Rediscover exact callback body, ABI and table membership in all three final modules',
                  'Emit manifest requiring untimed correctness, actual-worker dispatch and source/alias gates before measurement'],
        'normalization_policy': 'Only ModuleID comment, source_filename prefix, and seven exact diagnostic filename strings with validated byte lengths. Entire normalized IR is pinned. No instruction, symbol, attribute or metadata normalization for the original control.',
        'not_evidence_of': ['successful fresh-machine build', 'browser execution', 'inference correctness', 'native lowering', 'performance']}


def source_refs():
    return {k: PINS[k] for k in ('source_commit', 'source_tar_sha256', 'archive_builder_commit',
        'generic_harness_builder_label', 'lineage', 'emsdk_version', 'emsdk_commit', 'llvm_commit',
        'pristine_runtime_archive_sha256', 'loadsplat_runtime_archive_sha256', 'source_patch_sha256',
        'release_patch_sha256', 'harness_sha256', 'runtime_preparer_sha256')}


def run(command, *, cwd=None, env=None, log=None):
    result = subprocess.run([str(x) for x in command], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if log:
        Path(log).write_text(result.stdout)
    require(result.returncode == 0, 'Command failed: ' + shlex.join([str(x) for x in command]) + '\n' + result.stdout[-5000:])
    return result.stdout


def clean_environment(env):
    env = dict(env)
    for name in list(env):
        if name in {'EMCC_CFLAGS', 'EMMAKEN_CFLAGS', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS'} or re.search(r'(^|_)(CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS)(_|$)', name):
            env.pop(name)
    env['EMCC_CFLAGS'] = FLAGS
    env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env['PATH']
    return env



def source_patch_environment(env, source):
    # Cache directories may live inside a user's unrelated Git checkout. Treat
    # the pinned tar source as a standalone tree and never inherit another index.
    result = {k: v for k, v in env.items() if not k.startswith('GIT_')}
    result['GIT_CEILING_DIRECTORIES'] = str(Path(source).resolve().parent)
    result['GIT_CONFIG_NOSYSTEM'] = '1'
    result['GIT_CONFIG_GLOBAL'] = os.devnull
    return result



def bitcode_ir_command(sdk, source, output):
    """Read existing bitcode with the SDK's matching clang; no optimization."""
    return [Path(sdk) / 'upstream/bin/clang', '-cc1', '-triple',
            'wasm32-unknown-emscripten', '-emit-llvm', '-x', 'ir',
            source, '-o', output]


def expanded(template, roots):
    return template.format(**{name: str(path) for name, path in roots.items()})


def tokenized(path, roots):
    path = Path(path).resolve()
    # Resolve the source SDK symlink so all SDK headers have one stable identity.
    for name in ('sdk', 'dependencies', 'build', 'source'):
        root = Path(roots[name]).resolve()
        if path.is_relative_to(root):
            return '{' + name + '}/' + path.relative_to(root).as_posix()
    raise ValueError('Dependency outside declared roots: ' + str(path))



def raw_tokenized(path, roots):
    """Map only declared root prefixes, preserving every reported suffix byte."""
    raw = os.fspath(path)
    require(Path(raw).is_absolute(), 'Raw dependency must be an absolute path')
    for name in ('sdk', 'dependencies', 'build', 'source'):
        root = os.fspath(roots[name]).rstrip('/')
        if raw.startswith(root + '/'):
            return '{' + name + '}/' + raw[len(root) + 1:]
    raise ValueError('Raw dependency outside declared roots: ' + raw)


def dependency_inventory(records, roots):
    """Retain every raw observation and reject conflicting canonical aliases."""
    raw_paths, canonical, mapping = Counter(), {}, []
    for row in records:
        raw = row['path']
        path = Path(expanded(raw, roots))
        resolved = tokenized(path, roots)
        signature = (row['sha256'], row['bytes'])
        require(resolved not in canonical or canonical[resolved] == signature,
                'Conflicting dependency aliases disagree in hash/bytes: ' + raw)
        canonical[resolved] = signature
        raw_paths[raw] += 1
        mapping.append({'path': raw, 'resolved_path': resolved,
                        'sha256': row['sha256'], 'bytes': row['bytes']})
    return raw_paths, canonical, mapping


def verify_dependency_records(observed, expected, roots):
    expected_raw, expected_unique, expected_mapping = dependency_inventory(expected, roots)
    actual_raw, actual_unique, actual_mapping = dependency_inventory(observed, roots)
    # Exact canonical set/hash/byte equality is the semantic gate. Raw spelling
    # and repeated observations remain complete, separately reported provenance.
    # Never accept a subset or discard conflicting hashes from canonical aliases.
    require(actual_unique == expected_unique, 'Compiled canonical dependency file set/hash/bytes differ from pinned provenance')
    return {'dependency_count': len(observed),
            'dependency_raw_unique_path_count': len(actual_raw),
            'dependency_canonical_unique_file_count': len(actual_unique),
            'dependency_raw_alias_mapping': actual_mapping,
            'expected_dependency_raw_alias_mapping': expected_mapping,
            'dependency_semantic_policy': 'exact canonical file set, sha256 and bytes; raw observations retained as provenance',
            'exact_canonical_dependency_set_hash_bytes_verified': True,
            'raw_dependency_multiset_equal': actual_raw == expected_raw,
            'raw_dependency_observation_delta': {
                'added': [{'path': key, 'count': count} for key, count in sorted((actual_raw - expected_raw).items())],
                'removed': [{'path': key, 'count': count} for key, count in sorted((expected_raw - actual_raw).items())],
            },
            'canonical_alias_hash_and_byte_agreement_verified': True}


def normalize_include_argument(argument):
    """Only terminal '/.' on a joined -I path; no filesystem/path resolution."""
    if argument.startswith('-I') and argument.endswith('/.'):
        return '-I/' if argument == '-I/.' else argument[:-2]
    return argument


def verify_include_arguments(actual, expected):
    require([normalize_include_argument(x) for x in actual] ==
            [normalize_include_argument(x) for x in expected],
            'Configured include order differs')


def check_expected_dependencies(roots, *, candidate=False):
    expected = copy.deepcopy(PINS['dependencies'])
    if candidate:
        for row in expected:
            if row['path'] == '{source}/' + PINS['source_header']:
                row['sha256'] = PINS['candidate_source_header_sha256']
                row['bytes'] = Path(expanded(row['path'], roots)).stat().st_size
        expected += PINS['candidate_additional_dependencies']
    for row in expected:
        p = Path(expanded(row['path'], roots))
        require(p.is_file() and p.stat().st_size == row['bytes'] and file_sha(p) == row['sha256'], 'Dependency pin mismatch: ' + row['path'])
    return expected


def parse_make_dependencies(text):
    # Emscripten emits a POSIX make depfile. Output is a fixed relative name,
    # so no drive-letter or escaped target ambiguity is accepted.
    text = text.replace('\\\n', ' ')
    require(': ' in text or ':\n' in text, 'Malformed dependency file')
    return shlex.split(text.split(':', 1)[1])


@stage('configure')
def configure_source(base, cache, work, env):
    commit = PINS['source_commit']
    url = f'https://codeload.github.com/microsoft/onnxruntime/tar.gz/{commit}'
    archive = base.cached_download(url, cache, f'onnxruntime-{commit}.tar.gz', expected_sha256=PINS['source_tar_sha256'])
    require(file_sha(archive) == PINS['source_tar_sha256'], 'ORT source tar pin mismatch')
    tree = base.unpack_cached(archive, work / 'sources')
    source = tree / ('onnxruntime-' + commit)
    require(file_sha(source / 'cmake/deps.txt') == PINS['deps_definition_sha256'], 'Dependency download definitions changed')
    header = source / PINS['source_header']
    patch_env = source_patch_environment(env, source)
    # Previous interrupted runs may have reached the exact approved candidate.
    # Restore only that known header before rebuilding the original control.
    if file_sha(header) == PINS['candidate_source_header_sha256']:
        run(['git', 'apply', '--reverse', str(ROOT / 'inplace_leaky.patch')], cwd=source, env=patch_env)
    require(file_sha(header) == PINS['original_source_header_sha256'], 'Source header is not pinned tar original')
    release_patch = ROOT / 'xnnpack-static-dependencies.patch'
    check = subprocess.run(['git', 'apply', '--check', str(release_patch)], cwd=source, env=patch_env, capture_output=True)
    if check.returncode == 0:
        run(['git', 'apply', str(release_patch)], cwd=source, env=patch_env)
    else:
        run(['git', 'apply', '--reverse', '--check', str(release_patch)], cwd=source, env=patch_env)
    sdk = Path(env['EMSDK']).resolve()
    link = source / 'cmake/external/emsdk'
    if link.is_dir() and not link.is_symlink() and not any(link.iterdir()):
        link.rmdir()
    if link.is_symlink():
        require(link.resolve() == sdk, 'Source tree refers to another SDK')
    elif not link.exists():
        link.symlink_to(sdk, target_is_directory=True)
    else:
        raise ValueError('Source tree SDK path is not the selected SDK symlink')
    roots = {'source': source, 'dependencies': work / 'dependency-cache', 'build': work / 'build/Release', 'sdk': sdk}
    command = [sys.executable, str(source / 'tools/ci_build/build.py'), '--build_dir', str(work / 'build'), '--config', 'Release', '--update', '--parallel', '2', '--build_wasm_static_lib', '--enable_wasm_simd', '--enable_wasm_threads', '--use_xnnpack', '--disable_wasm_exception_catching', '--disable_rtti', '--skip_tests', '--skip_submodule_sync', '--cmake_extra_defines', 'CMAKE_EXPORT_COMPILE_COMMANDS=ON', 'CMAKE_SKIP_INSTALL_RULES=ON', 'CMAKE_C_FLAGS=' + FLAGS, 'CMAKE_CXX_FLAGS=' + FLAGS, 'FETCHCONTENT_BASE_DIR=' + str(roots['dependencies']), 'onnxruntime_ENABLE_WEBASSEMBLY_RELAXED_SIMD=OFF', 'onnxruntime_ENABLE_CPU_FP16_OPS=OFF', 'XNNPACK_BUILD_ALL_MICROKERNELS=OFF']
    run(command, cwd=source, env=env, log=work / 'configure.log')
    with stage('generate_headers'):
        run(['cmake', '--build', str(roots['build']), '--target', 'gen_onnx_proto', 'gen_onnx_operators_proto', 'gen_onnx_data_proto', '--parallel', '2'], env=env, log=work / 'generate-headers.log')
    entries = json.loads((roots['build'] / 'compile_commands.json').read_text())
    matches = [x for x in entries if Path(x['file']).resolve() == (source / PINS['source_filename_suffix']).resolve()]
    require(len(matches) == 1, 'Ambiguous activations.cc compile command')
    entry = matches[0]
    require(Path(entry['directory']).resolve() == roots['build'].resolve(), 'Unexpected compiler working directory')
    actual = shlex.split(entry['command'])
    require(actual.count('-o') == 1, 'Unexpected compiler output arguments')
    actual[actual.index('-o') + 1] = '{object}'
    expected = [expanded(x, {**roots, 'object': '{object}'}) for x in PINS['compile_command']]
    require(actual == expected, 'Configured activation compile command differs from pinned reconstruction')
    rsp = roots['build'] / 'CMakeFiles/onnxruntime_providers.dir/includes_CXX.rsp'
    verify_include_arguments(shlex.split(rsp.read_text()), [expanded(x, roots) for x in PINS['include_arguments']])
    check_expected_dependencies(roots)
    return roots, entry


@stage('audit_toolchain')
def compiler_provenance(roots, env):
    sdk = roots['sdk']
    for name, digest in PINS['compiler_source_sha256'].items():
        require(file_sha(sdk / name) == digest, 'Emscripten compiler source pin mismatch: ' + name)
    llvm = sdk / 'upstream/bin/clang'
    version = run([llvm, '--version'], env=env)
    require(PINS['llvm_commit'] in version, 'LLVM commit mismatch')
    em = sdk / 'upstream/emscripten/em++'
    em_version = run([em, '--version'], env=env)
    require(PINS['emsdk_version'] in em_version, 'Emscripten version mismatch')
    tools = {name: {'sha256': file_sha(sdk / 'upstream/bin' / name)} for name in ('clang', 'llvm-nm', 'llvm-ar')}
    cmake_version = run(['cmake', '--version'], env=env)
    require(cmake_version.splitlines()[0] == 'cmake version ' + PINS['cmake_version'], 'CMake version mismatch')
    protoc = roots['dependencies'] / 'protoc_binary-src/bin/protoc'
    protoc_version = run([protoc, '--version'], env=env).strip()
    require(protoc_version == PINS['protoc_version'], 'Protobuf generator version mismatch')
    for row in PINS['generated_header_provenance']:
        path = Path(expanded(row['path'], roots))
        require(path.is_file() and path.stat().st_size == row['bytes'] and file_sha(path) == row['sha256'], 'Generated header/input provenance mismatch: ' + row['path'])
    configured = {name: file_sha(roots['build'] / name) for name in ('CMakeCache.txt', 'compile_commands.json', 'CMakeFiles/onnxruntime_providers.dir/includes_CXX.rsp')}
    return {'llvm_commit': PINS['llvm_commit'], 'emsdk_version': PINS['emsdk_version'], 'llvm_version': version, 'empp_version': em_version, 'host_tool_sha256': tools, 'ir_readback_command_template': [str(x) for x in bitcode_ir_command(Path('{sdk}'), '{input.bc}', '{output.ll}')], 'compiler_source_sha256': PINS['compiler_source_sha256'], 'environment_overrides': {'EMCC_CFLAGS': FLAGS}, 'python_version': sys.version, 'cmake_version': cmake_version.strip(), 'protoc_version': protoc_version, 'protoc_executable_sha256': file_sha(protoc), 'generated_header_provenance': PINS['generated_header_provenance'], 'nonselected_simd_header_provenance': PINS.get('nonselected_simd_header_provenance', []), 'configured_file_sha256': configured}


def compile_activation(kind, entry, roots, work, env):
    command = shlex.split(entry['command'])
    obj = work / ('activations.' + kind + '.bc')
    command[command.index('-o') + 1] = str(obj)
    with stage('compile_candidate' if kind == 'candidate' else 'compile_control'):
        run(command, cwd=entry['directory'], env=env, log=work / ('compile-' + kind + '.log'))
    with stage('audit_candidate_dependencies' if kind == 'candidate' else 'audit_control_dependencies'):
        audit = work / ('activations.' + kind + '.audit.bc')
        deps = work / (kind + '.d')
        audit_command = command.copy()
        audit_command[audit_command.index('-o') + 1] = str(audit)
        audit_command += ['-MD', '-MF', str(deps)]
        run(audit_command, cwd=entry['directory'], env=env, log=work / ('dependencies-' + kind + '.log'))
        require(obj.read_bytes() == audit.read_bytes(), 'Dependency-audit flags changed activation object')
        expected = check_expected_dependencies(roots, candidate=(kind == 'candidate'))
        observed = []
        for name in parse_make_dependencies(deps.read_text()):
            reported = name if Path(name).is_absolute() else str(entry['directory']) + '/' + name
            p = Path(reported)
            observed.append({'path': raw_tokenized(reported, roots),
                             'sha256': file_sha(p), 'bytes': p.stat().st_size})
        dependency_proof = verify_dependency_records(observed, expected, roots)
        ll = obj.with_suffix('.ll')
        run(bitcode_ir_command(roots['sdk'], obj, ll), env=env)
        proof = {'object_sha256': file_sha(obj), **dependency_proof, 'compiled_object_identical_with_dependency_flags': True, 'dependencies': expected, 'command_template': PINS['compile_command'], 'include_arguments': PINS['include_arguments']}
        write_json(work / ('dependencies-' + kind + '.json'), proof)
        return obj, ll, proof


def verify_complete(binary, archive_sha):
    binary = Path(binary)
    receipt_path = binary.parent / 'complete.json'
    receipt = json.loads(receipt_path.read_text())
    expected = {'core': '9b539761f3e152b966e08c2de0784129fe8cf68d', 'rust': '1.96.0', 'emscripten': '4.0.8', 'runtime': archive_sha, 'kind': 'browser-mt-xnnpack', 'optimization': 'z', 'graph_level': 1, 'xnnpack': True, 'wrapper': '76c21496eebbd122f41d43413878bbf3e2213a14bb8e86ae50a594bdcf54ed72', 'core_diagnostic_patch': 'b69c5ef4794c10225257aead4f00e2e3985c27e1fae6d6c7ac640075eff47053', 'core_fixed_shape_patch': 'e0908f6593b202f6990954155f15952ff318dfd681beb3077292ee52a57f2ef8'}
    for key, value in expected.items():
        require(receipt['identity'].get(key) == value, 'CORE build identity mismatch: ' + key)
    for name in (binary.name, binary.with_suffix('.wasm').name, 'native-bundle-verification.json', 'cargo-link.log'):
        require(name in receipt['files'], 'Missing receipt output: ' + name)
    for name, digest in receipt['files'].items():
        require(Path(name).name == name and file_sha(binary.parent / name) == digest, 'CORE output integrity mismatch: ' + name)
    bundle = json.loads((binary.parent / 'native-bundle-verification.json').read_text())
    require(bundle.get('verified') is True and bundle.get('runtime_sha256') == archive_sha, 'Native bundle archive mismatch')
    require(bundle.get('invalidated_packages') == ['voicevox_core', 'voicevox_benchmark'], 'Both CORE and wrapper must be invalidated')
    return receipt, bundle, file_sha(receipt_path)


def generate_source_builder(preparer, harness, dispatch, work):
    pristine = work / 'canonical_runtime_builder.py'
    preparer.generate_builder(harness, pristine)
    text = pristine.read_text()
    def replace(old, new):
        nonlocal text
        require(text.count(old) == 1, 'Pinned runtime builder extension marker changed')
        text = text.replace(old, new, 1)
    replace('DISPATCH_DIR = Path(__file__).resolve().parent', 'DISPATCH_DIR = Path(' + repr(str(dispatch)) + ')')
    marker = '        (folder / "native-bundle-verification.json").write_text(json.dumps(bundle_proof, indent=2))'
    replace(marker, '        from source_proof import verify_native_bundle\n        bundle_proof.update(verify_native_bundle(runtime, core_rlib))\n' + marker)
    replace('"native_bundle_policy": "v3: invalidate voicevox_core and voicevox_benchmark; inspect linked CORE rlib dispatcher; hash proof and link log"', '"native_bundle_policy": "v4-source-activation: invalidate CORE and wrapper; verify every ordered native member, both activation occurrences, dispatcher, proof and link log"')
    replace('identity = {"native_bundle_policy":', 'identity = {"research_policy": "inplace-source-pristine-auto-v1", "native_bundle_verifier_sha256": ' + repr(file_sha(ROOT / 'source_proof.py')) + ', "source_pins_sha256": ' + repr(file_sha(ROOT / 'source_pins.json')) + ', "native_bundle_policy":')
    generated = work / 'source_runtime_builder.py'
    generated.write_text(text)
    return load_module(generated, '_inplace_source_builder'), generated


def callback_proof(binary, key):
    candidate = key == 'candidate'
    prefix = 'candidate' if candidate else 'original'
    data = Path(binary).with_suffix('.wasm').read_bytes()
    sections, _, target, slot, imports, _ = discover(data, PINS[prefix + '_callback_body_sha256'], PINS[prefix + '_callback_body_bytes'])
    require(imports == 52, 'Pinned CORE imported function count changed')
    element = [s['payload'] for s in sections if s['id'] == 9]
    require(len(element) == 1, 'Expected one element section')
    return {'callback_table_slot': slot['slot'], 'callback_function_index': target['index'], 'callback_body_sha256': sha(target['body']), 'callback_body_bytes': len(target['body']), 'callback_imported_function_count': imports, 'callback_abi': {'params': ['i32'] * 3, 'results': []}, 'element_section_sha256': sha(element[0]), 'wasm_sha256': sha(data)}


def verify_cpu_reference(binary):
    binary = Path(binary).resolve()
    receipt_path = binary.parent / 'complete.json'
    r = json.loads(receipt_path.read_text())
    ident = r['identity']
    for key, value in {'runtime': '0d62a53ddb7924704abb640daa55153e15f0cc1eca63d5f2a191f23654f7b57a', 'kind': 'browser-mt', 'xnnpack': False, 'optimization': 'z', 'graph_level': 1, 'core': '9b539761f3e152b966e08c2de0784129fe8cf68d', 'rust': '1.96.0', 'emscripten': '4.0.8', 'wrapper': '76c21496eebbd122f41d43413878bbf3e2213a14bb8e86ae50a594bdcf54ed72'}.items():
        require(ident.get(key) == value, 'CPU reference identity mismatch: ' + key)
    require(binary.name in r['files'] and binary.with_suffix('.wasm').name in r['files'], 'CPU reference missing JS/WASM receipt')
    for name, digest in r['files'].items():
        require(Path(name).name == name and file_sha(binary.parent / name) == digest, 'CPU reference receipt mismatch')
    return {'binary': str(binary), 'complete_receipt_sha256': file_sha(receipt_path), 'wasm_sha256': file_sha(binary.with_suffix('.wasm')), 'build_identity': ident, 'threads': 2}


def prepare(args):
    with stage('verify_inputs'):
        check_payloads(args)
        require(os.name == 'posix', 'Source reconstruction currently requires a POSIX host; paths remain relocatable')
        cache = args.cache.resolve()
        dispatch = cache / 'vocoder-dispatch-v3'
        work = cache / 'inplace-leaky-on-source-v1'
        work.mkdir(parents=True, exist_ok=True)
        base = load_module(args.harness, '_inplace_pristine_harness')
        preparer = load_module(args.runtime_preparer, '_inplace_runtime_preparer')
        original = json.loads((dispatch / 'prepared-original.json').read_text())
        assets = json.loads((cache / 'research-assets.json').read_text())
        archive = base.find_one(Path(assets['runtimes']['browser_xnnpack']), 'libonnxruntime_webassembly.a')
        require(file_sha(archive) == PINS['pristine_runtime_archive_sha256'], 'Pristine archive pin mismatch')
        baseline_receipt, baseline_bundle, baseline_receipt_sha = verify_complete(original['binary'], file_sha(archive))
        assets = json.loads((cache / 'research-assets.json').read_text())
        require(assets['query_sha256'] == PINS['query_sha256'], 'Fixed 10-second query changed')
        model = Path(assets['model']).resolve()
        require(file_sha(model) == assets['model_sha256'] == PINS['model_sha256'], 'Original model pin mismatch')
        # Check the selected release lineage, not the generic harness label.
        release_info = json.loads(base.find_one(Path(assets['runtimes']['browser_xnnpack']), 'BUILD_INFO.json').read_text())
        for key, value in {'builder_commit': PINS['archive_builder_commit'], 'source_commit': PINS['source_commit'], 'emscripten_version': PINS['emsdk_version'], 'fast_math': False, 'fp_contract': 'off', 'relaxed_simd': False, 'xnnpack': True}.items():
            require(release_info.get(key) == value, 'Selected XNN release lineage mismatch: ' + key)
        env = clean_environment(base.ensure_toolchains(cache))
    roots, entry = configure_source(base, cache, work, env)
    compiler = compiler_provenance(roots, env)
    write_json(work / 'compiler-provenance.json', compiler)
    raw_archive = archive.read_bytes()
    original_member = [payload for name, _, payload, _, index in archive_entries(raw_archive) if index == PINS['activation_member_index'] and name == 'activations.cc.o']
    require(len(original_member) == 1 and sha(original_member[0]) == PINS['original_activation_object_sha256'], 'Archived CPU activation pin mismatch')
    archived = work / 'activations.archived.bc'
    archived.write_bytes(original_member[0])
    old_ir = archived.with_suffix('.ll')
    run(bitcode_ir_command(roots['sdk'], archived, old_ir), env=env)
    rebuilt_obj, rebuilt_ir, rebuilt_deps = compile_activation('rebuilt', entry, roots, work, env)
    with stage('verify_control_ir'):
        control = compare_control(old_ir.read_text(), rebuilt_ir.read_text(), PINS)
        nm = roots['sdk'] / 'upstream/bin/llvm-nm'
        symbols = run([nm, '--defined-only', archived], env=env)
        require(symbols == run([nm, '--defined-only', rebuilt_obj], env=env), 'Rebuilt defined symbols differ')
        control.update(all_defined_symbols_equal=True, original_object_sha256=file_sha(archived), rebuilt_object_sha256=file_sha(rebuilt_obj))
        write_json(work / 'original-control-proof.json', control)
    with stage('apply_source_patch'):
        header = roots['source'] / PINS['source_header']
        patch_env = source_patch_environment(env, roots['source'])
        run(['git', 'apply', '--check', str(ROOT / 'inplace_leaky.patch')], cwd=roots['source'], env=patch_env)
        run(['git', 'apply', str(ROOT / 'inplace_leaky.patch')], cwd=roots['source'], env=patch_env)
        require(file_sha(header) == PINS['candidate_source_header_sha256'], 'Applied source patch result mismatch')
    candidate_obj, candidate_ir, candidate_deps = compile_activation('candidate', entry, roots, work, env)
    with stage('verify_candidate_ir'):
        scope = compare_candidate(rebuilt_ir.read_text(), candidate_ir.read_text(), PINS)
        require(symbols == run([nm, '--defined-only', candidate_obj], env=env), 'Candidate defined symbols differ')
        scope.update(defined_symbols_equal=True, candidate_object_sha256=file_sha(candidate_obj))
        write_json(work / 'candidate-scope-proof.json', scope)
    with stage('replace_archives'):
        integrations = {}
        for key, obj in [('rebuilt', rebuilt_obj), ('candidate', candidate_obj)]:
            path = work / 'archives' / key / 'libonnxruntime_webassembly.a'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(replace_activation(raw_archive, obj.read_bytes(), PINS))
            run([roots['sdk'] / 'upstream/bin/llvm-ar', 's', path], env=env)
            proof = verify_archive_replacement(raw_archive, path.read_bytes(), file_sha(obj), PINS)
            proof.update(archive_sha256=file_sha(path), archive=str(path), symbol_index_regenerated=True)
            integrations[key] = proof
        write_json(work / 'archive-integration.json', integrations)
    builder, builder_path = generate_source_builder(preparer, args.harness.resolve(), dispatch, work)
    core_archive = base.cached_download(f'https://github.com/yamachu/voicevox_core/archive/{base.CORE_COMMIT}.tar.gz', cache, f'core-{base.CORE_COMMIT}.tar.gz')
    source = base.unpack_cached(core_archive, work / ('core-source-' + file_sha(builder_path)[:16])) / ('voicevox_core-' + base.CORE_COMMIT)
    rows = []
    for key in KEYS:
        runtime = archive if key == 'original' else Path(integrations[key]['archive'])
        runtime_sha = file_sha(runtime)
        expected_activation = PINS['original_activation_object_sha256'] if key == 'original' else file_sha(rebuilt_obj if key == 'rebuilt' else candidate_obj)
        staged = work / 'staged/lib/libonnxruntime_webassembly.a'
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(runtime, staged)
        require(file_sha(staged) == runtime_sha, 'Staging changed native archive')
        with stage('link_' + key + '_core'):
            binary = builder.build_runner(source, staged, cache, env, threaded=True, threads=2, xnnpack=True)
            require(file_sha(runtime) == file_sha(staged) == runtime_sha, 'Archive changed during CORE link')
            receipt, bundle, receipt_sha = verify_complete(binary, runtime_sha)
            require(receipt['identity'].get('research_policy') == 'inplace-source-pristine-auto-v1', 'Research cache identity mismatch')
            require(bundle.get('activation_member_sha256') == expected_activation, 'Linked CPU activation mismatch')
            require(bundle.get('duplicate_activation_sha256') == PINS['duplicate_activation_object_sha256'] and bundle.get('all_ordered_native_members_verified') == 1438, 'Incomplete ordered native bundle proof')
            require(bundle.get('dispatcher_sha256') == baseline_bundle['dispatcher_sha256'], 'Pristine dispatcher changed')
        row = {'key': key, 'label': {'original': 'Pristine XNN source baseline', 'rebuilt': 'Unmodified activation source rebuild', 'candidate': 'Exact-inplace strict-FP32 source candidate'}[key], 'binary': str(binary), 'model': str(model), 'model_sha256': file_sha(model), 'archive_sha256': runtime_sha, 'runtime_archive_sha256': runtime_sha, 'build_identity': receipt['identity'], 'complete_receipt_sha256': receipt_sha, 'native_bundle': bundle, 'threads': 2, 'ort_threads': 1, 'xnn_threads': 2, 'spin_off': True, 'fixed_shape': True, 'fixed_length': 962, 'provider': 'XNNPACK', 'model_target': 'vocoder', 'revectorize': True, 'dispatch_expected': 'auto', 'actual_export_map': preparer.verify_exports(binary)}
        row['activation_member_sha256'] = PINS['original_activation_object_sha256'] if key == 'original' else bundle['activation_member_sha256']
        rows.append(row)
    with stage('discover_callbacks'):
        for row in rows:
            row.update(callback_proof(row['binary'], row['key']))
            print('SOURCE_VARIANT_READY', row['key'], row['binary'], flush=True)
        require(len({(r['callback_table_slot'], r['callback_function_index'], r['element_section_sha256']) for r in rows}) == 1, 'Callback mapping or element segments changed across source variants')
        require(rows[0]['callback_body_sha256'] == rows[1]['callback_body_sha256'], 'Rebuilt control callback differs')
    proofs = {'original_control': control, 'candidate_scope': scope, 'compiler': compiler, 'rebuilt_dependency_count': rebuilt_deps['dependency_count'], 'candidate_dependency_count': candidate_deps['dependency_count'], 'archive_integration': integrations, 'all_element_segments_identical_across_three_modules': True, 'untouched_and_rebuilt_callback_bytes_identical': True, 'paths': {name: str(work / name) for name in ('compiler-provenance.json', 'dependencies-rebuilt.json', 'dependencies-candidate.json', 'original-control-proof.json', 'candidate-scope-proof.json', 'archive-integration.json')}}
    result = {'schema': 'inplace-leaky-on-source-manifest-v1', 'verified': True, 'variants': rows, 'source_refs': source_refs(), 'source_proofs': proofs, 'fixture_query_sha256': PINS['query_sha256'], 'model_sha256': PINS['model_sha256'], 'research_harness_sha256': PINS['harness_sha256'], 'preparer_sha256': file_sha(__file__), 'builder_source_sha256': file_sha(builder_path), 'source_pins_sha256': file_sha(ROOT / 'source_pins.json'), 'source_proof_helper_sha256': file_sha(ROOT / 'source_proof.py'), 'callback_discovery_sha256': file_sha(ROOT / 'callback_discovery.py'), 'require_exact_fp32_pcm_to_reference': True, 'require_per_mode_repeatability': True, 'require_actual_core_browser_dispatch': True, 'expected_browser_dispatch': 'splat', 'required_browser_hardware_concurrency': 4, 'required_revectorization': 'on', 'require_dedicated_worker_splat_dispatch': True, 'require_source_and_alias_gates': True, 'require_repeated_raw_fp32_and_pcm_checks': True, 'fixed_length': 962, 'ort_threads': 1, 'xnn_threads': 2, 'spin_off': True, 'note': 'Source/native-bundle proof is not inference, browser dispatch, alias, numerical quality, native lowering, or performance evidence. Generated artifact hashes are recorded per machine; only declared diagnostics normalize in IR comparisons.'}
    with stage('write_manifest'):
        if args.cpu_binary:
            result['cpu_reference'] = verify_cpu_reference(args.cpu_binary)
        write_json(args.output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--harness', required=True, type=Path)
    parser.add_argument('--runtime-preparer', required=True, type=Path)
    parser.add_argument('--cpu-binary', type=Path, help='Optional already-built ordinary CPU 2 reference; never built here')
    parser.add_argument('--dry-run', action='store_true', help='Validate bundled/source helper pins and emit a plan; no cache access, toolchain, download, build or inference')
    args = parser.parse_args(argv)
    if args.dry_run:
        write_json(args.output, dry_run_plan(args))
        print('SOURCE_DRY_RUN_READY', args.output)
        return
    import fcntl
    args.cache.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        # Same lock as canonical runtime preparation; avoid Cargo/native staging races.
        for name in ('vocoder-dispatch.lock', 'inplace-leaky-source.lock'):
            lock = stack.enter_context((args.cache / name).open('w'))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args)
    print('SOURCE_MANIFEST_READY', args.output)


if __name__ == '__main__':
    main()
