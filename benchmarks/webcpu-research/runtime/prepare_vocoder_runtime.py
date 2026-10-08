#!/usr/bin/env python3
"""Portable, source-pinned CORE vocoder-XNN runtime preparation.
Run after voicevox_webcpu_research.py --prepare-only. No measurements or uploads.
"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, re, shlex, shutil, subprocess, sys
from pathlib import Path

HARNESS_SHA='d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe'
WRAPPER_SHA='76c21496eebbd122f41d43413878bbf3e2213a14bb8e86ae50a594bdcf54ed72'
ORT_SHA='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd'
PROBE_SHA='c2753086a8d5fae75940a3edeee4d7f94c66c47be73c32459592f86eb1a0629b'
TRANSFORM_SHA='183468c51a29746ae50871e2f637ab2fc4c941294358b4531f78c854ea067647'
XNN_COMMIT='fe98e0b93565382648129271381c14d6205255e3'
XNN_TAR_SHA='639fa4fda5dbf0e501642db4a93ed1dba91aa4d9f2ce48ed5d01602adc0447cc'
PTHREAD_SHA='546f40bfb687562e812ba226bf4afcb848e3a2bcc2fc8ca781047b37789f0d72'


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def load_module(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module


def replace_once(text, old, new):
    if text.count(old)!=1:raise ValueError('Unexpected pinned harness marker: '+old[:100])
    return text.replace(old,new,1)


BUILD_AND_VERIFY = '''        # CORE bundles the native static library; cleaning only the final wrapper is unsafe.
        clean_log = checked_run([cargo, f"+{RUST_VERSION}", "clean", "--manifest-path", str(source / "Cargo.toml"), "-p", "voicevox_core", "-p", "voicevox_benchmark", "--profile", "c-api", "--target", "wasm32-unknown-emscripten"], env=build_env)
        link_log = checked_run(command + ["-vv"], env=build_env)
        (folder / "cargo-link.log").write_text(clean_log + link_log)
        rlibs = set(re.findall(r"--extern voicevox_core=([^\\s`]+)", link_log))
        if len(rlibs) != 1: raise RuntimeError(f"Ambiguous actually linked CORE rlib: {rlibs}")
        core_rlib = Path(next(iter(rlibs)))
        llvm_ar = str(Path(build_env["EMSDK"]) / "upstream/bin/llvm-ar")
        intended = subprocess.check_output([llvm_ar, "p", str(runtime), "gemm-config.c.o"], env=build_env)
        bundled = subprocess.check_output([llvm_ar, "p", str(core_rlib), "gemm-config.c.o"], env=build_env)
        if not intended or intended != bundled: raise RuntimeError("CORE rlib contains a stale or unexpected native dispatcher")
        bundle_proof = {"verified": True, "core_rlib": str(core_rlib), "dispatcher_sha256": hashlib.sha256(intended).hexdigest(), "runtime_sha256": sha256_file(runtime), "invalidated_packages": ["voicevox_core", "voicevox_benchmark"]}
        (folder / "native-bundle-verification.json").write_text(json.dumps(bundle_proof, indent=2))
    finally:'''


def generate_builder(harness, output):
    harness=Path(harness);output=Path(output)
    if sha(harness)!=HARNESS_SHA:raise ValueError('Research harness changed; review and update its source pin')
    base=load_module(harness,'_dispatch_original_harness')
    if hashlib.sha256(base.RUST_SOURCE.encode()).hexdigest()!=WRAPPER_SHA:raise ValueError('Original vocoder wrapper changed')
    text=harness.read_text()
    text=replace_once(text,'TRIALS_PER_BLOCK = 5','DISPATCH_DIR = Path(__file__).resolve().parent\nTRIALS_PER_BLOCK = 5')
    text=replace_once(text,'    write_wrapper(source)\n    kind =', '''    if not xnnpack or threaded is not True: raise ValueError("Only threaded vocoder-XNN builds supported")
    probe_info = json.loads((DISPATCH_DIR / "probe_compile.json").read_text())
    probe_object = DISPATCH_DIR / "dispatch_probe.o"
    if sha256_file(probe_object) != probe_info["object_sha256"] or sha256_file(DISPATCH_DIR / "dispatch_probe.c") != probe_info["source_sha256"]: raise RuntimeError("Probe compile provenance mismatch")
    write_wrapper(source)
    kind =''')
    text=replace_once(text,'identity = {"build_adapter_sha256":','identity = {"native_bundle_policy": "v3: invalidate voicevox_core and voicevox_benchmark; inspect linked CORE rlib dispatcher; hash proof and link log", "dispatch_probe": probe_info, "build_adapter_sha256":')
    text=replace_once(text,'_bench_xnn_sessions,_bench_finish_profile','_bench_xnn_sessions,_bench_finish_profile,_probe_xnn_f32_dispatch')
    text=replace_once(text,'        if optimization == "3":\n            flags +=','        flags += ["-C", f"link-arg={probe_object}"]\n        if optimization == "3":\n            flags +=')
    text=replace_once(text,'        checked_run(command, env=build_env)\n    finally:',BUILD_AND_VERIFY)
    text=replace_once(text,'    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())','    for proof in ("native-bundle-verification.json", "cargo-link.log"):\n        hashes[proof] = sha256_file(folder / proof)\n    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())')
    text=replace_once(text,'if __name__ == "__main__":','if __name__ == "__main__":\n    raise SystemExit("Generated builder module; use prepare_vocoder_runtime.py")\n\nif False:')
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(text)
    return load_module(output,'_dispatch_generated_harness')


def archive_members(path):
    """Ordered payload hashes retain duplicate member names, ignoring ar metadata."""
    result=[];long_names=b''
    with Path(path).open('rb') as f:
        if f.read(8)!=b'!<arch>\n':raise ValueError('Not a regular static archive')
        while header:=f.read(60):
            if len(header)!=60 or header[-2:]!=b'`\n':raise ValueError('Malformed archive')
            name=header[:16].decode().strip();size=int(header[48:58]);payload=f.read(size)
            if len(payload)!=size:raise ValueError('Truncated archive member')
            if size&1:f.read(1)
            if name=='//':long_names=payload;continue
            if name in ('/','/SYM64/'):continue
            if name.startswith('/'):
                offset=int(name[1:]);name=long_names[offset:].split(b'/\n',1)[0].decode()
            else:name=name.rstrip('/')
            result.append((name,hashlib.sha256(payload).hexdigest()))
    return result


def verify_archive_change(original_members, candidate):
    members=archive_members(candidate)
    if len(members)!=len(original_members):raise ValueError('Archive member count changed')
    changed=[(before,after) for before,after in zip(original_members,members) if before!=after]
    if len(changed)!=1 or changed[0][0][0]!='gemm-config.c.o' or changed[0][1][0]!='gemm-config.c.o':
        raise ValueError('Unexpected archive member changes')


def verify_exports(binary):
    """Follow Emscripten's JS mapping to minified WASM exports."""
    binary=Path(binary);data=binary.with_suffix('.wasm').read_bytes()
    if data[:8]!=b'\0asm\1\0\0\0':raise ValueError('Invalid WASM')
    def leb(pos):
        value=shift=0
        while True:
            byte=data[pos];pos+=1;value|=(byte&127)<<shift
            if not byte&128:return value,pos
            shift+=7
    names=[];pos=8
    while pos<len(data):
        section=data[pos];size,start=leb(pos+1);end=start+size
        if section==7:
            count,p=leb(start)
            for _ in range(count):
                n,p=leb(p);name=data[p:p+n].decode();p+=n+1;_,p=leb(p);names.append(name)
        pos=end
    js=binary.read_text();mapping={}
    for name in ['bench_init','bench_synthesize','bench_raw','bench_raw_ptr','bench_raw_len','bench_fixed_length','bench_fixed_matches','bench_xnn_threads','bench_xnn_sessions','bench_finish_profile','probe_xnn_f32_dispatch']:
        match=re.search(r'Module\["_'+re.escape(name)+r'"\]=wasmExports\["([^"]+)"\]',js)
        if not match or match[1] not in names:raise ValueError('Missing actual export '+name)
        mapping[name]=match[1]
    return mapping


def ensure_headers(base, cache, kernel):
    archive=base.cached_download(f'https://codeload.github.com/google/XNNPACK/tar.gz/{XNN_COMMIT}',cache,'xnnpack-dispatch.tar.gz',expected_sha256=XNN_TAR_SHA)
    tree=base.unpack_cached(archive,cache/'xnnpack-headers')
    xnn=tree/f'XNNPACK-{XNN_COMMIT}'
    if not (xnn/'src/xnnpack/config.h').is_file():raise ValueError('Pinned XNN headers unavailable')
    pthread=base.cached_download('https://raw.githubusercontent.com/Maratyszcza/pthreadpool/4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0/include/pthreadpool.h',cache,'pthreadpool.h',expected_sha256=PTHREAD_SHA)
    return xnn,pthread


def compile_probe(base, kernel, directory, env, xnn, pthread):
    source=kernel/'dispatch_probe.c'
    if sha(source)!=PROBE_SHA:raise ValueError('Dispatch probe source changed')
    target=directory/'dispatch_probe.c';shutil.copyfile(source,target)
    sdk=Path(env['EMSDK']);cc=sdk/'upstream/emscripten/emcc';obj=directory/'dispatch_probe.o';deps=directory/'dispatch_probe.d'
    flags=['-O2','-pthread','-msimd128','-fwasm-exceptions','-fno-fast-math','-ffp-contract=off','-I',str(pthread.parent),'-I',str(xnn/'src'),'-I',str(xnn/'include'),'-MMD','-MF',str(deps),'-c',str(target),'-o',str(obj)]
    base.checked_run([str(cc),*flags],env=env)
    headers=shlex.split(deps.read_text().replace('\\\n','').split(':',1)[1])
    proof={'source_sha256':sha(target),'object_sha256':sha(obj),'compile_flags':flags,'dependency_sha256':{p:sha(p) for p in sorted(set(headers))},'compiler_version':subprocess.check_output([str(cc),'--version'],env=env,text=True),'compiler_sha256':sha(cc),'xnn_commit':XNN_COMMIT}
    (directory/'probe_compile.json').write_text(json.dumps(proof,indent=2))


def prepare(args):
    cache=args.cache.resolve();kernel=args.kernel_dir.resolve();harness=args.harness.resolve()
    base=load_module(harness,'_portable_research')
    if sha(harness)!=HARNESS_SHA:raise ValueError('Research harness source pin mismatch')
    assets=json.loads((cache/'research-assets.json').read_text())
    expected_query=hashlib.sha256(json.dumps(base.prepared_query(10),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if assets['query_sha256']!=expected_query:raise ValueError('Prepared assets are not the fixed 10-second query fixture')
    original=base.find_one(Path(assets['runtimes']['browser_xnnpack']),'libonnxruntime_webassembly.a')
    if sha(original)!=ORT_SHA:raise ValueError('Unexpected original runtime archive')
    model=Path(assets['model']);model_hash=sha(model)
    if model_hash!=assets['model_sha256']:raise ValueError('Original private model changed')
    directory=cache/'vocoder-dispatch-v3';directory.mkdir(parents=True,exist_ok=True)
    env=base.ensure_toolchains(cache)
    env['PATH']=str(Path(sys.executable).parent)+os.pathsep+env['PATH']
    xnn,pthread=ensure_headers(base,directory,kernel)
    compile_probe(base,kernel,directory,env,xnn,pthread)
    transform=kernel/'make_archive_variants.py'
    if sha(transform)!=TRANSFORM_SHA:raise ValueError('IR transform source changed')
    archives=directory/'archives'
    base.checked_run([sys.executable,str(transform),'--archive',str(original),'--sdk',env['EMSDK'],'--out',str(archives)],env=env)
    generated=directory/'runtime_builder.py';builder=generate_builder(harness,generated)
    core_archive=base.cached_download(f'https://github.com/yamachu/voicevox_core/archive/{base.CORE_COMMIT}.tar.gz',cache,f'core-{base.CORE_COMMIT}.tar.gz')
    tree=base.unpack_cached(core_archive,directory/('source-'+sha(generated)[:16]));source=tree/f'voicevox_core-{base.CORE_COMMIT}'
    original_members=archive_members(original);rows=[];staged=directory/'staged/lib/libonnxruntime_webassembly.a';staged.parent.mkdir(parents=True,exist_ok=True)
    for variant in args.variants:
        runtime=original if variant=='original' else archives/variant/'libonnxruntime_webassembly.a'
        digest=sha(runtime)
        if variant!='original':
            provenance=json.loads((runtime.parent/'PROVENANCE.json').read_text())
            if provenance['original_archive_sha256']!=ORT_SHA or provenance['variant_archive_sha256']!=digest:raise ValueError('Variant archive provenance mismatch')
            verify_archive_change(original_members,runtime)
        shutil.copyfile(runtime,staged)
        if sha(staged)!=digest:raise ValueError('Staging changed archive')
        binary=builder.build_runner(source,staged,cache,env,threaded=True,threads=args.threads,xnnpack=True)
        if sha(staged)!=digest or sha(runtime)!=digest:raise ValueError('Archive changed during link')
        receipt=json.loads((binary.parent/'complete.json').read_text())
        for name,expected in receipt['files'].items():
            if sha(binary.parent/name)!=expected:raise ValueError('Compiled output integrity mismatch')
        bundle=json.loads((binary.parent/'native-bundle-verification.json').read_text())
        if not bundle['verified'] or bundle['runtime_sha256']!=digest:raise ValueError('Native bundling proof mismatch')
        export_map=verify_exports(binary)
        dispatch_smoke=None
        if args.dispatch_smoke_script:
            if not args.node:raise ValueError('No Node executable for explicit smoke')
            expected='loadsplat' if variant=='loadsplat' else 'splat'
            smoke=subprocess.run([str(args.node),'--no-experimental-wasm-revectorize',str(args.dispatch_smoke_script.resolve()),str(binary),expected],text=True,capture_output=True,check=True)
            dispatch_smoke=json.loads(smoke.stdout.strip())
            if not dispatch_smoke.get('passed') or dispatch_smoke.get('actual')!=expected:raise ValueError('Actual CORE dispatch smoke mismatch')
            dispatch_smoke.update(node_executable=str(args.node),script_sha256=sha(args.dispatch_smoke_script))
        rows.append({'actual_core_node_smoke':dispatch_smoke,'key':variant.replace('-','_'),'label':'Vocoder XNN '+variant,'threads':args.threads,'binary':str(binary),'build_identity':receipt['identity'],'model':str(model),'model_sha256':model_hash,'provider':'XNNPACK','fixed_shape':True,'spin_off':True,'model_target':'vocoder','revectorize':False,'dispatch_expected':'auto' if variant in ('original','auto-roundtrip') else variant,'runtime_archive_sha256':digest,'native_bundle':bundle,'actual_export_map':export_map,'description':'Strict FP32; original model; vocoder-XNN only; global ORT1; actual browser dispatch gate required'})
        (directory/('prepared-'+variant+'.json')).write_text(json.dumps(rows[-1],indent=2))
        print('PORTABLE_VARIANT_READY',variant,str(binary),json.dumps(dispatch_smoke),flush=True)
    result={'preparer_sha256':sha(Path(__file__)),'schema':'voicevox-vocoder-runtime-manifest-v1','variants':rows,'fixture_query_sha256':assets['query_sha256'],'research_harness_sha256':HARNESS_SHA,'builder_source_sha256':sha(generated),'require_exact_fp32_pcm_to_reference':True,'require_actual_core_browser_dispatch':True,'require_repeated_raw_fp32_and_pcm_checks':True,'note':'Build/native-bundle proof is not actual browser dispatch or performance evidence. No model/audio upload.'}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2))
    print('Prepared runtime manifest; actual browser dispatch and output gates still required:',args.output)


def main():
    root=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser();parser.add_argument('--cache',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--harness',type=Path,default=root/'voicevox_webcpu_research.py');parser.add_argument('--kernel-dir',type=Path,default=root/'kernel');parser.add_argument('--threads',type=int,default=2);parser.add_argument('--dispatch-smoke-script',type=Path);parser.add_argument('--node',type=Path,default=shutil.which('node'));parser.add_argument('--variants',nargs='+',choices=['original','auto-roundtrip','loadsplat','splat'],default=['original','auto-roundtrip','loadsplat']);args=parser.parse_args()
    if args.threads<1 or len(set(args.variants))!=len(args.variants) or 'original' not in args.variants:parser.error('Require original control, unique variants, and positive threads')
    import fcntl
    args.cache.mkdir(parents=True,exist_ok=True)
    with (args.cache/'vocoder-dispatch.lock').open('w') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        prepare(args)
if __name__=='__main__':main()
