"""Fetch pinned public dependencies; reuse only hash-verified archive/SDK cache."""
import os,pathlib,hashlib,urllib.request,tarfile,subprocess,json
P=pathlib.Path(__file__).resolve().parent;cache=pathlib.Path(os.environ['BENCH_CACHE']);work=pathlib.Path(os.environ['RUNNER_TEMP'])/'xnn-macro-deps';work.mkdir(exist_ok=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def fetch(url,name,digest):
 p=work/name
 if not p.exists() or sha(p)!=digest:
  with urllib.request.urlopen(url,timeout=180) as r:p.write_bytes(r.read())
 assert sha(p)==digest
 return p
def unpack(p,dest):
 dest.mkdir(parents=True,exist_ok=True)
 with tarfile.open(p) as t:t.extractall(dest,filter='data')
version='4.0.8';commit='419021fa040428bc69ef1559b325addb8e10211f'
sdk=cache/f'emsdk-{version}'/f'emsdk-{commit}'
if not (sdk/'upstream/emscripten/emcc.py').is_file():
 archive=fetch(f'https://github.com/emscripten-core/emsdk/archive/{commit}.tar.gz','emsdk.tar.gz','26c3ef13fc8285a209fde5ac3e6eec17e2a24e59d6af76f92f54e6bf62998a72');unpack(archive,work/'sdk');sdk=work/'sdk'/f'emsdk-{commit}'
 subprocess.run(['python',str(sdk/'emsdk.py'),'install',version],check=True)
subprocess.run(['python',str(sdk/'emsdk.py'),'activate',version],check=True)
assert (sdk/'upstream/emscripten/emscripten-version.txt').read_text().strip().strip('"')==version
expected='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd'
found=[p for p in (cache/'runtimes').glob('**/libonnxruntime_webassembly.a') if sha(p)==expected]
if found:archive=found[0]
else:
 name='onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2'
 tar=fetch(f'https://github.com/Hiroshiba/onnxruntime-builder/releases/download/{name}/{name}.tgz','runtime.tgz','76182743b15a31e0d361432d8db297a78e3da64aad356730fe93576993af5a2a');unpack(tar,work/'runtime');archive=next((work/'runtime').glob('**/libonnxruntime_webassembly.a'))
assert sha(archive)==expected
xcommit='fe98e0b93565382648129271381c14d6205255e3'
x=fetch(f'https://codeload.github.com/google/XNNPACK/tar.gz/{xcommit}','xnn.tar.gz','639fa4fda5dbf0e501642db4a93ed1dba91aa4d9f2ce48ed5d01602adc0447cc');unpack(x,work/'xnn');xroot=work/'xnn'/f'XNNPACK-{xcommit}'
pthread=fetch('https://raw.githubusercontent.com/Maratyszcza/pthreadpool/4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0/include/pthreadpool.h','pthreadpool.h','546f40bfb687562e812ba226bf4afcb848e3a2bcc2fc8ca781047b37789f0d72')
subprocess.run(['python',str(P/'make_archive_variants.py'),'--archive',str(archive),'--sdk',str(sdk),'--out',str(work/'variants')],check=True)
env={'EMSDK':str(sdk),'XNN_ROOT':str(xroot),'PTHREADPOOL_INCLUDE':str(work),'ORIGINAL_ARCHIVE':str(archive),'LOADSPLAT_ARCHIVE':str(work/'variants/loadsplat/libonnxruntime_webassembly.a')}
with open(os.environ['GITHUB_ENV'],'a') as f:
 for k,v in env.items():f.write(f'{k}={v}\n')
(P/'results').mkdir(exist_ok=True)
(P/'results/dependency-provenance.json').write_text(json.dumps(dict(xnn_commit=xcommit,pthreadpool_commit='4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0',sdk_version=version,sdk_commit=commit,original_archive_sha256=sha(archive),loadsplat_archive_sha256=sha(pathlib.Path(env['LOADSPLAT_ARCHIVE'])),pthreadpool_header_sha256=sha(pthread)),indent=2))
