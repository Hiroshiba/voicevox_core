import pathlib,subprocess,os,json,hashlib,difflib,re
P=pathlib.Path(__file__).parent
X=pathlib.Path(os.environ['XNN_ROOT'])
R=pathlib.Path(os.environ['PTHREADPOOL_INCLUDE'])
SDK=pathlib.Path(os.environ['EMSDK'])
A=pathlib.Path(os.environ['LOADSPLAT_ARCHIVE'])
ORIG=pathlib.Path(os.environ['ORIGINAL_ARCHIVE'])
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(A)=='0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf'
assert sha(ORIG)=='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd'
assert (SDK/'upstream/emscripten/emscripten-version.txt').read_text().strip().strip(chr(34))=='4.0.8'
assert sha(R/'pthreadpool.h')=='546f40bfb687562e812ba226bf4afcb848e3a2bcc2fc8ca781047b37789f0d72'
env=os.environ.copy()
for k in ['EMCC_CFLAGS','CFLAGS','CXXFLAGS','LDFLAGS']:env.pop(k,None)
env.update(EM_CONFIG=str(SDK/'.emscripten'),EMCC_CORES='2')
for d in ['objects','sources','manifests','results']:(P/d).mkdir(exist_ok=True)
commands=[]
def run(cmd,**kw):
 commands.append(list(map(str,cmd)));subprocess.run(list(map(str,cmd)),env=env,check=True,**kw)
flags=['-include','math.h','-flto','-std=c99','-DNDEBUG','-fPIC','-msimd128','-pthread','-fwasm-exceptions','-fno-fast-math','-ffp-contract=off','-fno-math-errno','-DXNN_LOG_LEVEL=0','-DXNN_ENABLE_GEMM_M_SPECIALIZATION=1','-I',X/'src','-I',X/'include','-I',R]
results=[]
for name,src,opt in [('operator-run',X/'src/operator-run.c','-O2'),('convolution-nhwc',X/'src/operators/convolution-nhwc.c','-O2')]:
 arch=P/'objects'/f'{name}.archive.o';reb=P/'objects'/f'{name}.rebuilt.o'
 with arch.open('wb') as f:run([SDK/'upstream/bin/llvm-ar','p',A,name+'.c.o'],stdout=f)
 assert arch.read_bytes()==subprocess.check_output([str(SDK/'upstream/bin/llvm-ar'),'p',str(ORIG),name+'.c.o'])
 run([SDK/'upstream/emscripten/emcc',*flags,opt,'-c',src,'-o',reb])
 texts=[]
 for p in [arch,reb]:
  ll=p.with_suffix('.ll');run([SDK/'upstream/bin/clang','-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',p,'-o',ll]);texts.append(ll.read_text())
 norm=lambda t:re.sub(r'(!\d+ = !\{i64 )\d+(\})',r'\g<1>SOURCE_LOCATION\2','\n'.join(t.splitlines()[2:]))
 a,b=map(norm,texts);(P/'results'/f'{name}.diff').write_text(''.join(difflib.unified_diff(a.splitlines(True),b.splitlines(True))))
 results.append(dict(name=name,exact_normalized_ir=a==b,archive_sha=sha(arch),rebuilt_sha=sha(reb)))
(P/'manifests/control-rebuild.json').write_text(json.dumps(dict(results=results,commands=commands),indent=2))
print(json.dumps(results))
