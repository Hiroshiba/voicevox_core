#!/usr/bin/env python3
"""Isolated experimental IR edit; not a source rebuild or published runtime.
Requires the matching LLVM toolchain from Emscripten 4.0.8.
"""
import argparse,difflib,hashlib,json,pathlib,re,shutil,subprocess
p=argparse.ArgumentParser();p.add_argument('--archive',type=pathlib.Path,required=True);p.add_argument('--sdk',type=pathlib.Path,required=True);p.add_argument('--out',type=pathlib.Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
clang=str(a.sdk/'upstream/bin/clang'); ar=str(a.sdk/'upstream/bin/llvm-ar')
def run(*args,**kw):return subprocess.run(args,check=True,**kw)
def digest(b):return hashlib.sha256(b).hexdigest()
def filehash(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def members(path):
 result=[];names=b''
 with path.open('rb') as f:
  assert f.read(8)==b'!<arch>\n'
  while header:=f.read(60):
   assert len(header)==60 and header[-2:]==b'`\n'
   name=header[:16].decode().strip();size=int(header[48:58]);data=f.read(size)
   assert len(data)==size
   if size%2:f.read(1)
   if name=='//':names=data;continue
   if name in ('/','/SYM64/'):continue
   if name.startswith('/'):
    offset=int(name[1:]);name=names[offset:names.index(b'/\n',offset)].decode()
   else:name=name.removesuffix('/')
   result.append({'name':name,'size':size,'sha256':digest(data)})
 return result
expected_archive_sha256='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd'
assert filehash(a.archive)==expected_archive_sha256,'Archive is not the verified original release'
sdk_version=(a.sdk/'upstream/emscripten/emscripten-version.txt').read_text().strip().strip('"')
assert sdk_version=='4.0.8',f'Unexpected Emscripten: {sdk_version}'
toolchain={'emscripten_version':sdk_version}
for tool,path in [('clang',clang),('llvm_ar',ar)]:
 toolchain[tool]={'version':subprocess.check_output([path,'--version'],text=True).strip(),'sha256':filehash(pathlib.Path(path))}
original_members=members(a.archive);indices=[i for i,m in enumerate(original_members) if m['name']=='gemm-config.c.o'];assert len(indices)==1;idx=indices[0]
raw=a.out/'original-gemm-config.c.o'
with raw.open('wb') as f:run(ar,'p',str(a.archive),'gemm-config.c.o',stdout=f)
assert filehash(raw)==original_members[idx]['sha256']
assert filehash(raw)=='a8fdbbe614d51cc201068c79bcfbcaeab215ef8202396bcdaf8b4b0acc50b7ff','Unexpected original dispatcher'
source=a.out/'original.ll';run(clang,'-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',str(raw),'-o',str(source))
s=source.read_text();assert toolchain['clang']['version'].splitlines()[0] in s,'LLVM producer/toolchain mismatch'
func=re.search(r'define internal void @init_f32_gemm_config\(\).*?\n}',s,re.S);assert func
anchor='  %7 = icmp sgt i32 %6, 4';assert s.count(anchor)==1 and anchor in func.group()
assert re.search(r'%6 = tail call i32 @hardware_concurrency\(\)',func.group())
selects=re.findall(r'  %\d+ = select i1 %7, ptr @(\w+), ptr @(\w+)',func.group());assert len(selects)==12
for left,right in selects:assert left.endswith('_loadsplat') and right==left.removesuffix('_loadsplat')+'_splat'
assert '+simd128' in s and '+atomics' in s and '+exception-handling' in s
assert '+relaxed-simd' not in s
normalize=lambda t:'\n'.join(t.splitlines()[1:])
reports=[]
for name,replacement in [('loadsplat','  %7 = icmp eq i32 0, 0')]:
 dest=a.out/name;dest.mkdir(exist_ok=True);ll=dest/'gemm-config.ll';bc=dest/'gemm-config.c.o';roundtrip=dest/'verified.ll';archive=dest/'libonnxruntime_webassembly.a'
 text=s.replace(anchor,replacement);ll.write_text(text)
 run(clang,'-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm-bc','-x','ir',str(ll),'-o',str(bc))
 run(clang,'-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',str(bc),'-o',str(roundtrip))
 assert normalize(roundtrip.read_text())==normalize(text),'Unexpected IR changes during assembly'
 diff=''.join(difflib.unified_diff(s.splitlines(True),text.splitlines(True),fromfile='original.ll',tofile=name+'.ll'));(dest/'dispatch-only.diff').write_text(diff)
 shutil.copyfile(a.archive,archive);run(ar,'rs',str(archive),str(bc))
 new=members(archive);assert len(new)==len(original_members)
 changed=[i for i,(m,n) in enumerate(zip(original_members,new)) if m!=n]
 assert changed==[idx],changed
 assert [m['name'] for m in new]==[m['name'] for m in original_members]
 report={'variant':name,'toolchain':toolchain,'archive_symbol_index_rebuilt':True,'experimental_method':'LLVM IR dispatch-only edit; not source rebuild','original_archive':str(a.archive),'original_archive_sha256':filehash(a.archive),'variant_archive_sha256':filehash(archive),'archive_member_count':len(new),'changed_member_count':1,'changed_member':{'index':idx,'before':original_members[idx],'after':new[idx]},'condition_before':anchor.strip(),'condition_after':replacement.strip(),'selected_pointer_pairs':selects,'other_member_payloads_byte_identical':True,'roundtrip_ir_exact_except_module_id':True,'target_features_preserved':True,'cpuinfo_and_pthreadpool_unchanged':True,'math_operations_unchanged':True,'source_revision':'fe98e0b93565382648129271381c14d6205255e3','original_builder_revision':'72a65470c9873e073d137cef11bb6c9389e62d6a'}
 (dest/'PROVENANCE.json').write_text(json.dumps(report,indent=2)+'\n');reports.append(report);print(name,report['variant_archive_sha256'])
(a.out/'original-member-manifest.json').write_text(json.dumps(original_members,indent=2)+'\n')
(a.out/'VARIANTS.json').write_text(json.dumps(reports,indent=2)+'\n')
