#!/usr/bin/env python3
"""Fail-closed, read-only producer notice/inventory gate. No inference or upload.

Run after source_manifest.verify_manifest and before code export. This checks
notice bytes and local dependency identity. It does not provide legal assurance
or replace the native-member/source verifier or the code-only export gate.
"""
from __future__ import annotations
import argparse,hashlib,json,os,pathlib,re,subprocess,sys,tomllib,urllib.request
HERE=pathlib.Path(__file__).resolve().parent
class GateError(Exception): pass
def need(ok,code):
    if not ok: raise GateError(code)
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def read_json(p):return json.loads(pathlib.Path(p).read_text())
def within(p,root):
    p=pathlib.Path(p).resolve();need(p.is_relative_to(root.resolve()),'path_outside_cache');return p
def command(args,env):
    p=subprocess.run([str(x) for x in args],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=180)
    need(p.returncode==0,'cargo_inventory_command_failed');return p.stdout

def verify_dependencies(cache,source,inventory):
    need(sha(source/'Cargo.lock')==inventory['cargo_lock_sha256'],'core_cargo_lock_mismatch')
    env=dict(os.environ,CARGO_HOME=str(cache/'cargo'),RUSTUP_HOME=str(cache/'rustup'))
    cargo=cache/'cargo/bin/cargo'
    base=[cargo,'+1.96.0','tree','--locked','--offline','--manifest-path',source/'Cargo.toml','-p','voicevox_benchmark','--target','wasm32-unknown-emscripten','--features','browser,threaded','--edges','normal,build','--prefix','none','--format','{p}']
    tree=command(base,env)
    keys=set(re.findall(r'^([^ ]+) v([^ ]+?)(?:[ (\n]|$)',tree,re.M))
    expected={(p['name'],p['version']) for p in inventory['cargo_packages']}
    need(keys==expected,'cargo_dependency_set_mismatch')
    meta=json.loads(command([cargo,'+1.96.0','metadata','--locked','--offline','--manifest-path',source/'Cargo.toml','--filter-platform','wasm32-unknown-emscripten','--features','voicevox_benchmark/browser,voicevox_benchmark/threaded','--format-version','1'],env))
    packages={(p['name'],p['version']):p for p in meta['packages']}
    for p in inventory['cargo_packages']:
        actual=packages[p['name'],p['version']]
        need(actual['source']==p['source'],'cargo_source_mismatch')
        need(actual['license']==p['declared_license'],'cargo_license_metadata_mismatch')
        need(sha(actual['manifest_path'])==p['cargo_manifest_sha256'],'cargo_manifest_mismatch')
    libraries=list((cache/'rustup/toolchains').glob('1.96.0-*/lib/rustlib/src/rust/library/Cargo.lock'))
    need(len(libraries)==1,'rust_source_lock_ambiguous')
    need(sha(libraries[0])==inventory['rust_library_cargo_lock_sha256'],'rust_source_lock_mismatch')
    library=libraries[0].parent
    for item in inventory['rust_source_notices']:
        need(sha(library/item['source_path'])==item['sha256'],'rust_source_notice_mismatch')
    depdir=cache/'cargo-target/browser-mt-xnnpack/wasm32-unknown-emscripten/c-api/deps'
    depfiles=list(depdir.glob('*.d'));need(bool(depfiles),'target_dependency_files_missing')
    registry_dirs=list((cache/'cargo/registry/src').glob('*'))
    seen=set()
    for f in depfiles:
        for name in re.findall(r'/registry/src/[^/\s]+/([^/\s]+)/',f.read_text()):
            choices=[d/name/'Cargo.toml' for d in registry_dirs if (d/name/'Cargo.toml').is_file()]
            need(len(choices)==1,'compiled_crate_source_ambiguous')
            p=tomllib.loads(choices[0].read_text())['package'];seen.add((p['name'],p['version']))
    allowed=expected|{(p['name'],p['version']) for p in inventory['build_std_packages']}
    need(seen<=allowed,'uncovered_compiled_crate')
    # Open JTalk is fetched outside Cargo.lock by open_jtalk-sys CMake.
    native_dirs=list((cache/'cargo-target/browser-mt-xnnpack/wasm32-unknown-emscripten/c-api/build').glob('open_jtalk-sys-*/out/build/_deps/openjtalk-src'))
    need(bool(native_dirs),'open_jtalk_native_source_missing')
    for d in native_dirs:
        result=subprocess.run(['git','-C',str(d),'rev-parse','HEAD'],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        need(result.returncode==0 and result.stdout.strip()==inventory['open_jtalk_commit'],'open_jtalk_native_commit_mismatch')
        for p in inventory['open_jtalk_notices']:
            need(sha(d/p['source_path'])==p['sha256'],'open_jtalk_notice_mismatch')
    return {'cargo_packages':len(expected),'observed_target_registry_packages':len(seen),'open_jtalk_source_trees':len(native_dirs)}

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
