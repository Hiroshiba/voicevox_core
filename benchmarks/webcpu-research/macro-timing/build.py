from pathlib import Path
import re
exec((Path(__file__).parent/'rebuild_control.py').read_text().split('results=[]')[0])
assert all(x['exact_normalized_ir'] for x in json.loads((P/'manifests/control-rebuild.json').read_text())['results'])
(P/'wasm').mkdir(exist_ok=True)
flags=['-I',P/'sources/src',*flags]
objects={}
for kind in ['diagnostic','production']:
 obj=P/'objects'/f'{kind}_probe.o';run([SDK/'upstream/emscripten/emcc',*flags,'-O2','-c',P/'sources'/f'{kind}_probe.c','-o',obj]);objects[kind]=obj
 ll=obj.with_suffix('.ll');run([SDK/'upstream/bin/clang','-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',obj,'-o',ll])
 if kind=='production':
  text=ll.read_text();cycle=re.search(r'define[^\n]*@cycle\(.*?\n}',text,re.S).group()
  calls=re.findall(r'\bcall[^\n]*@([^ (]+)',cycle)
  assert set(calls)<= {'xnn_reshape_convolution2d_nhwc_f32','xnn_setup_convolution2d_nhwc_f32','xnn_run_operator','llvm.lifetime.start.p0','llvm.lifetime.end.p0'}
  assert all(x in calls for x in ['xnn_reshape_convolution2d_nhwc_f32','xnn_setup_convolution2d_nhwc_f32','xnn_run_operator'])
  assert not re.search(r'atomicrmw|cmpxchg|@kernel\(|@task\(',text)
  (P/'results/production-cycle.ll').write_text(cycle)
operator_objects=[]
for condition in range(3):
 d=P/'objects'/str(condition);d.mkdir(exist_ok=True)
 for name,rel in [('operator-run','src/operator-run.c'),('convolution-nhwc','src/operators/convolution-nhwc.c')]:run([SDK/'upstream/emscripten/emcc',*flags,'-O2',f'-DXNN_MACRO_CONDITION={condition}','-c',P/'sources'/rel,'-o',d/(name+'.c.o')])
 operator_objects.append(dict(condition=condition,operator_run_sha256=sha(d/'operator-run.c.o'),convolution_sha256=sha(d/'convolution-nhwc.c.o')))
 for kind,obj in objects.items():
  run([SDK/'upstream/emscripten/emcc','-Oz','-msimd128','-pthread','-fwasm-exceptions','-fno-fast-math','-ffp-contract=off',obj,d/'operator-run.c.o',d/'convolution-nhwc.c.o',A,'-sMODULARIZE=1','-sEXPORT_NAME=MacroProbe','-sENVIRONMENT=node,web,worker','-sPTHREAD_POOL_SIZE=1','-sPTHREAD_POOL_SIZE_STRICT=2','-sINITIAL_MEMORY=1073741824','-sMAXIMUM_MEMORY=2147483648','-sALLOW_MEMORY_GROWTH=1','-sASSERTIONS=1','-sEXPORTED_RUNTIME_METHODS=HEAPU8','-o',P/'wasm'/f'{kind}{condition}.js'])
# Read-only archive provenance for inactive pool parking, no scheduler changes.
pthread_obj=P/'objects/pthreads.archive.o'
with pthread_obj.open('wb') as out:run([SDK/'upstream/bin/llvm-ar','p',A,'pthreads.c.o'],stdout=out)
pthread_ll=pthread_obj.with_suffix('.ll');run([SDK/'upstream/bin/clang','-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',pthread_obj,'-o',pthread_ll])
pthread_text=pthread_ll.read_text();assert 'emscripten_futex_wait' in pthread_text and re.search(r'icmp eq i32 %\d+, 110',pthread_text)
wasm={}
for p in (P/'wasm').glob('*.wasm'):
 text=subprocess.check_output([str(SDK/'upstream/bin/wasm-dis'),str(p)],text=True);assert '.relaxed' not in text and 'relaxed_' not in text;wasm[p.name]=sha(p)
(P/'manifests/build.json').write_text(json.dumps(dict(commands=commands,operator_objects_shared_by_diagnostic_and_primary=operator_objects,archive_sha256=sha(A),wasm=wasm,production_cycle_calls=calls,precision='strict FP32 no contraction or relaxed SIMD',memory=dict(initial=1073741824,maximum=2147483648),pthreadpool_archive_object_sha256=sha(pthread_obj),worker_idle='archive IR110-spin bound then emscripten_futex_wait'),indent=2))
