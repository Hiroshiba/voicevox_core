#!/usr/bin/env python3
"""Fail-closed, read-only producer notice/inventory gate. No inference or upload.

Run after source_manifest.verify_manifest and before code export. This checks
notice bytes and local dependency identity. It does not provide legal assurance
or replace the native-member/source verifier or the code-only export gate.
"""
from __future__ import annotations
import argparse,hashlib,json,os,pathlib,re,shlex,subprocess,sys,tomllib,urllib.request
HERE=pathlib.Path(__file__).resolve().parent
class GateError(Exception): pass
def need(ok,code):
    if not ok: raise GateError(code)
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def read_json(p):return json.loads(pathlib.Path(p).read_text())
def within(p,root):
    p=pathlib.Path(p).resolve();need(p.is_relative_to(root.resolve()),'path_outside_cache');return p
def command(args,env,*,cwd=None):
    p=subprocess.run([str(x) for x in args],env=env,cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    need(p.returncode==0,'cargo_inventory_command_failed');return p.stdout

# The selected graph is an attestation of the original, full-cache resolution.
# Never rerun a workspace-wide resolver here: Cargo can request unrelated source
# archives even for tree --target, and those need not exist after a valid build.
SELECTION={'package':'voicevox_benchmark','target':'wasm32-unknown-emscripten','features':['browser','threaded'],'dependency_edges':['normal','build'],'default_features':True,'rust':'1.96.0','host':'x86_64-unknown-linux-gnu'}
REGISTRY='registry+https://github.com/rust-lang/crates.io-index'
def package_key(p):return (p['name'],p['version'],p.get('source'))
def unique_packages(rows,code):
    result={package_key(p):p for p in rows};need(len(result)==len(rows),code);return result

def verify_build_selection(log):
    """Use the final wrapper rustc command in the receipt-hashed build log.

    A shared target-directory fingerprint may belong to a later variant. It is
    deliberately not accepted as a substitute for per-output feature evidence.
    """
    commands=[]
    for line in log.splitlines():
        if 'Running `' not in line or 'voicevox_benchmark' not in line:continue
        try:args=shlex.split(line.split('Running `',1)[1].rstrip('`'))
        except ValueError:raise GateError('benchmark_rustc_command_invalid') from None
        need(not any(a.startswith('@') for a in args),'benchmark_response_file')
        def values(flag):
            result=[]
            for i,arg in enumerate(args):
                if arg==flag:
                    need(i+1<len(args),'benchmark_rustc_command_invalid');result.append(args[i+1])
                elif arg.startswith(flag+'='):result.append(arg[len(flag)+1:])
            return result
        names=values('--crate-name')
        if 'voicevox_benchmark' not in names:continue
        need(names==['voicevox_benchmark'],'benchmark_crate_name')
        need(values('--target')==[SELECTION['target']],'benchmark_target_mismatch')
        features=sorted(values('--cfg'))
        need(features==['feature="browser"','feature="threaded"'],'benchmark_features_mismatch')
        commands.append(args)
    need(len(commands)==1,'benchmark_rustc_command_missing_or_ambiguous')
    return {'features':SELECTION['features'],'target':SELECTION['target'],'evidence':'receipt-hashed rustc command'}

def verify_dependencies(cache,source,inventory):
    need(sha(source/'Cargo.lock')==inventory['cargo_lock_sha256'],'core_cargo_lock_mismatch')
    need(sha(source/'Cargo.toml')==inventory['workspace_cargo_manifest_sha256'],'workspace_manifest_mismatch')
    need(inventory['cargo_selection']==SELECTION,'cargo_selection_mismatch')
    expected=unique_packages(inventory['cargo_packages'],'duplicate_cargo_package')
    graph=inventory['frozen_cargo_graph']
    need(graph['schema']=='cargo-notice-selection-v1' and graph['selected_packages']==len(expected),'frozen_graph_schema_or_count')
    frozen=[tuple(p) for p in graph['package_identities']]
    need(len(frozen)==len(set(frozen)) and set(frozen)==set(expected),'frozen_graph_package_set_mismatch')
    # Include every workspace member, not only selected packages: workspace
    # inheritance, membership and feature-unification inputs must not drift.
    for relative,digest in inventory['workspace_manifests'].items():
        manifest=within(source/relative,source)
        need(manifest.is_file() and sha(manifest)==digest,'workspace_member_manifest_mismatch')
    configs={str(p.relative_to(source)):sha(p) for p in source.rglob('.cargo/config*') if p.is_file()}
    need(configs==inventory['workspace_configs'] and configs.get('.cargo/config.toml')==inventory['workspace_cargo_config_sha256'],'workspace_config_mismatch')
    # No unrecorded Cargo config may change source replacement or resolution.
    for base in [cache/'cargo',*source.parents,*pathlib.Path.cwd().resolve().parents,pathlib.Path.cwd().resolve()]:
        for name in ('config','config.toml'):
            config=base/name if base==cache/'cargo' else base/'.cargo'/name
            need(not config.exists() or config.resolve() in { (source/p).resolve() for p in configs },'unrecorded_cargo_config')
    env=dict(os.environ,CARGO_HOME=str(cache/'cargo'),RUSTUP_HOME=str(cache/'rustup'))
    cargo=cache/'cargo/bin/cargo'
    meta=json.loads(command([cargo,'+1.96.0','metadata','--no-deps','--locked','--offline','--manifest-path',source/'Cargo.toml','--format-version','1'],env,cwd=source))
    workspace=unique_packages(meta['packages'],'ambiguous_workspace_package')
    need({str(within(p['manifest_path'],source).relative_to(source)) for p in workspace.values()}==set(inventory['workspace_manifests']),'workspace_member_set_mismatch')
    core_lock=unique_packages(tomllib.loads((source/'Cargo.lock').read_text())['package'],'duplicate_lock_package')
    libraries=list((cache/'rustup/toolchains').glob('1.96.0-*/lib/rustlib/src/rust/library/Cargo.lock'))
    need(len(libraries)==1,'rust_source_lock_ambiguous')
    need(libraries[0].parents[5].name==SELECTION['rust']+'-'+SELECTION['host'],'rust_host_toolchain_mismatch')
    need(sha(libraries[0])==inventory['rust_library_cargo_lock_sha256'],'rust_source_lock_mismatch')
    library=libraries[0].parent
    std_lock=unique_packages(tomllib.loads(libraries[0].read_text())['package'],'duplicate_std_lock_package')
    for item in inventory['rust_source_notices']:
        need(sha(within(library/item['source_path'],library))==item['sha256'],'rust_source_notice_mismatch')
    allowed=dict(expected)
    for key,p in unique_packages(inventory['build_std_packages'],'duplicate_build_std_package').items():
        if key not in expected:need(key in std_lock,'build_std_package_not_locked')
        else:need(p=={k:v for k,v in expected[key].items() if k!='manifest_relpath'},'build_std_identity_mismatch')
        allowed[key]=p
    registry_dirs=list((cache/'cargo/registry/src').glob('*'))
    git_checkouts=list((cache/'cargo/git/checkouts').glob('*/*'))
    git_roots={};registry_sources={};git_sources={}
    for key,p in allowed.items():
        origin=p['source'];locked=(core_lock if key in expected else std_lock).get(key)
        need(locked is not None,'cargo_source_not_locked')
        if origin is None:
            need(key in workspace,'workspace_package_missing');actual=workspace[key]
            manifest=within(source/p['manifest_relpath'],source)
            need(pathlib.Path(actual['manifest_path']).resolve()==manifest,'workspace_package_path_mismatch')
        elif origin==REGISTRY:
            name=p['name']+'-'+p['version']
            choices=[d/name/'Cargo.toml' for d in registry_dirs if (d/name/'Cargo.toml').is_file()]
            need(len(choices)==1,'registry_crate_source_missing_or_ambiguous');manifest=choices[0]
            need(manifest.parent.parent.name.startswith('index.crates.io-'),'registry_source_mismatch')
            actual=tomllib.loads(manifest.read_text())['package']
            archives=list((cache/'cargo/registry/cache').glob('*/'+name+'.crate'))
            need(len(archives)==1,'registry_archive_missing_or_ambiguous')
            need(archives[0].parent.name==manifest.parent.parent.name,'registry_archive_source_mismatch')
            need(p['package_checksum_sha256']==locked.get('checksum') and sha(archives[0])==locked.get('checksum'),'registry_archive_checksum_mismatch')
            registry_sources[manifest.parent.parent.name+'/'+name]=(key,manifest.parent.resolve())
        elif origin.startswith('git+'):
            revision=origin.rsplit('#',1)[-1];need(bool(re.fullmatch('[0-9a-f]{40}',revision)),'git_revision_format')
            if origin not in git_roots:
                roots=[]
                for directory in git_checkouts:
                    result=subprocess.run(['git','-C',str(directory),'rev-parse','HEAD'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                    if result.returncode==0 and result.stdout.strip()==revision:roots.append(directory)
                need(len(roots)==1,'git_source_missing_or_ambiguous');git_roots[origin]=roots[0]
                for relative,digest in inventory['git_workspace_manifests'][origin].items():
                    need(sha(within(roots[0]/relative,roots[0]))==digest,'git_workspace_manifest_mismatch')
            root=git_roots[origin];manifest=within(root/p['manifest_relpath'],root)
            actual=tomllib.loads(manifest.read_text())['package']
            git_sources[manifest.resolve()]=key
        else:raise GateError('unsupported_cargo_source')
        need((actual['name'],actual['version'])==key[:2],'cargo_package_identity_mismatch')
        need(actual.get('license')==p['declared_license'],'cargo_license_metadata_mismatch')
        need(sha(manifest)==p['cargo_manifest_sha256'],'cargo_manifest_mismatch')
    target=cache/'cargo-target/browser-mt-xnnpack'
    observed={};git_seen=set()
    for label,profile in [('target',target/'wasm32-unknown-emscripten/c-api'),('host',target/'c-api')]:
        depfiles=list((profile/'deps').glob('*.d'));buildfiles=list((profile/'build').glob('*/*.d'))
        need(bool(depfiles),label+'_dependency_files_missing')
        if label=='host':need(bool(buildfiles),'host_build_script_dependency_files_missing')
        seen=set()
        for file in depfiles+buildfiles:
            text=file.read_text()
            for full,registry,name in set(re.findall(r'(/[^\s:]*?/registry/src/([^/\s]+)/([^/\s]+))(?:/|(?=\s|$))',text)):
                found=registry_sources.get(registry+'/'+name);need(found is not None,'uncovered_compiled_crate')
                key,directory=found;need(pathlib.Path(full).resolve()==directory,'compiled_registry_source_path_mismatch');seen.add(key)
            for checkout,relative in set(re.findall(r'(/[^\s:]*?/git/checkouts/[^/\s]+/[^/\s]+)(?:/([^\s:]+))?',text)):
                roots=[p.resolve() for p in git_roots.values() if p.resolve()==pathlib.Path(checkout).resolve()]
                need(len(roots)==1,'uncovered_compiled_git_crate');root=roots[0]
                actual=within(root/relative,root)
                # A checkout can contain several packages. A root package does
                # not cover nested crates such as ort/backends/candle.
                directory=actual if actual.is_dir() else actual.parent
                manifests=[d/'Cargo.toml' for d in [directory,*directory.parents] if d.is_relative_to(root) and (d/'Cargo.toml').is_file() and 'package' in tomllib.loads((d/'Cargo.toml').read_text())]
                need(bool(manifests) and manifests[0].resolve() in git_sources,'uncovered_compiled_git_crate')
                git_seen.add(git_sources[manifests[0].resolve()])

        need(bool(seen),label+'_registry_evidence_empty');observed[label]=len(seen)
    # Open JTalk is fetched outside Cargo.lock by open_jtalk-sys CMake.
    native_dirs=list((target/'wasm32-unknown-emscripten/c-api/build').glob('open_jtalk-sys-*/out/build/_deps/openjtalk-src'))
    need(bool(native_dirs),'open_jtalk_native_source_missing')
    for directory in native_dirs:
        result=subprocess.run(['git','-C',str(directory),'rev-parse','HEAD'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        need(result.returncode==0 and result.stdout.strip()==inventory['open_jtalk_commit'],'open_jtalk_native_commit_mismatch')
        for p in inventory['open_jtalk_notices']:
            need(sha(directory/p['source_path'])==p['sha256'],'open_jtalk_notice_mismatch')
    return {'cargo_packages':len(expected),'frozen_selection_verified':True,'registry_archives_verified':len(registry_sources),'observed_target_registry_packages':observed['target'],'observed_host_registry_packages':observed['host'],'observed_git_packages':len(git_seen),'open_jtalk_source_trees':len(native_dirs),'workspace_resolution':'no-deps locked offline'}

def verify_eigen_source(source,inventory,*,check_network=True):
    need(source.is_dir(),'eigen_source_missing')
    files={str(p.relative_to(source)):sha(p) for p in source.rglob('*') if p.is_file()}
    need(files==inventory['eigen_source_inventory']['files'],'eigen_source_changed')
    if check_network:
        try:
            request=urllib.request.Request(inventory['eigen_source_url'],method='HEAD',headers={'User-Agent':'VOICEVOX-code-notice-verifier'})
            with urllib.request.urlopen(request,timeout=60) as response:
                need(response.status==200 and response.url.startswith(('https://github.com/','https://codeload.github.com/')),'eigen_source_unavailable')
        except GateError:raise
        except Exception:raise GateError('eigen_source_unavailable') from None
    return {'files':len(files),'unchanged':True,'source_url_reachable':check_network}

def verify(manifest,cache,inventory_path,notices):
    cache=cache.resolve();inv=read_json(inventory_path)
    need(inv['schema']=='voicevox-code-notice-inventory-v1','inventory_schema')
    need(inv['notice_materials_complete'] is True,'notice_materials_incomplete')
    need(sha(notices)==inv['combined_notices_sha256'],'notices_hash_mismatch')
    m=read_json(manifest)
    need(m['schema']=='inplace-leaky-on-source-manifest-v1' and m['verified'] is True,'source_manifest_schema')
    bh=m['builder_source_sha256'];need(bool(re.fullmatch('[0-9a-f]{64}',bh)),'builder_hash_format')
    source=within(cache/'inplace-leaky-on-source-v1'/('core-source-'+bh[:16])/('voicevox_core-'+inv['core_commit']),cache)
    need(source.is_dir(),'producer_core_source_missing')
    for k,v in inv['required_source_refs'].items():need(m['source_refs'].get(k)==v,'native_source_pin_mismatch')
    rows=m['variants'];need([x['key'] for x in rows]==['original','rebuilt','candidate'],'variant_set')
    evidence={}
    for row in rows:
        identity=row['build_identity']
        for k,v in inv['required_build_identity'].items():need(identity.get(k)==v,'build_identity_mismatch')
        binary=within(row['binary'],cache);need(binary.name=='voicevox_benchmark.js','binary_name')
        receipt_file=binary.parent/'complete.json';receipt=read_json(receipt_file)
        need(sha(receipt_file)==row['complete_receipt_sha256'] and receipt['identity']==identity,'receipt_identity')
        for name in ('voicevox_benchmark.js','voicevox_benchmark.wasm','cargo-link.log','native-bundle-verification.json'):
            need(sha(binary.parent/name)==receipt['files'][name],'build_file_digest')
        need(binary.with_suffix('.wasm').read_bytes()[:8]==b'\x00asm\x01\x00\x00\x00','wasm_magic')
        bundle=read_json(binary.parent/'native-bundle-verification.json')
        need(bundle['verified'] is True and bundle['all_ordered_native_members_verified']==1438,'native_members_not_verified')
        need(bundle['runtime_sha256']==identity['runtime'],'native_runtime_identity')
        # These exact experiments compile code only. Inputs must be runtime files.
        log=(binary.parent/'cargo-link.log').read_text()
        need('--embed-file' not in log and '--preload-file' not in log,'embedded_data_link_flag')
        verify_build_selection(log)
        evidence[row['key']]={n:sha(binary.parent/n) for n in ('voicevox_benchmark.js','voicevox_benchmark.wasm')}
    dependency_result=verify_dependencies(cache,source,inv)
    eigen_result=verify_eigen_source(cache/'inplace-leaky-on-source-v1/dependency-cache/eigen3-src',inv)
    return {'schema':'voicevox-code-notice-gate-v1','verified':True,'not_legal_assurance':True,'notice_materials_complete':True,'dependency_checks':dependency_result,'eigen_source_checks':eigen_result,'notices_sha256':sha(notices),'inventory_sha256':sha(inventory_path),'code_hashes':evidence,'scope':'notice identity and local Cargo/Rust/OpenJTalk source inventory; existing native-source and code-only export gates remain required'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--manifest',type=pathlib.Path,required=True);p.add_argument('--cache',type=pathlib.Path,required=True);p.add_argument('--inventory',type=pathlib.Path,default=HERE/'NOTICE_INVENTORY.json');p.add_argument('--notices',type=pathlib.Path,default=HERE/'LICENSES.txt');p.add_argument('--output',type=pathlib.Path)
    a=p.parse_args()
    try:r=verify(a.manifest,a.cache,a.inventory,a.notices)
    except GateError as e: print(json.dumps({'schema':'voicevox-code-notice-gate-v1','verified':False,'error_code':str(e)}));return 1
    except Exception as e: print(json.dumps({'schema':'voicevox-code-notice-gate-v1','verified':False,'error_code':'verification_failed','exception_class':type(e).__name__}));return 1
    text=json.dumps(r,indent=2)+'\n'
    if a.output:a.output.write_text(text)
    print(text,end='');return 0
if __name__=='__main__':sys.exit(main())
