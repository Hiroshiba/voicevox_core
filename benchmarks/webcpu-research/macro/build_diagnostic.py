from pathlib import Path
exec((Path(__file__).parent/'rebuild_control.py').read_text().split('results=[]')[0])
assert all(x['exact_normalized_ir'] for x in json.loads((P/'manifests/control-rebuild.json').read_text())['results'])
(P/'wasm').mkdir(exist_ok=True)
flags=['-I',P/'sources/src',*flags]
obj=P/'objects/probe.o';run([SDK/'upstream/emscripten/emcc',*flags,'-O2','-c',P/'sources/probe.c','-o',obj])
for cond in [0,1,2]:
 d=P/'objects'/str(cond);d.mkdir(exist_ok=True)
 for name,rel in [('operator-run','src/operator-run.c'),('convolution-nhwc','src/operators/convolution-nhwc.c')]:
  run([SDK/'upstream/emscripten/emcc',*flags,'-O2',f'-DXNN_MACRO_CONDITION={cond}','-c',P/'sources'/rel,'-o',d/(name+'.c.o')])
 # Link replacement TUs before archive; archive members cannot be pulled twice.
 run([SDK/'upstream/emscripten/emcc','-Oz','-msimd128','-pthread','-fwasm-exceptions','-fno-fast-math','-ffp-contract=off',obj,d/'operator-run.c.o',d/'convolution-nhwc.c.o',A,'-sMODULARIZE=1','-sEXPORT_NAME=MacroProbe','-sENVIRONMENT=node,web,worker','-sPTHREAD_POOL_SIZE=1','-sPTHREAD_POOL_SIZE_STRICT=2','-sINITIAL_MEMORY=536870912','-sMAXIMUM_MEMORY=2147483648','-sALLOW_MEMORY_GROWTH=1','-sASSERTIONS=1','-sEXPORTED_RUNTIME_METHODS=HEAPU8','-o',P/'wasm'/f'condition{cond}.js'])
(P/'manifests/diagnostic-build.json').write_text(json.dumps(dict(commands=commands,archive_sha=sha(A),wasm={str(i):sha(P/'wasm'/f'condition{i}.wasm') for i in range(3)}),indent=2))
