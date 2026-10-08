#!/usr/bin/env python3
"""Fail-closed source-only guard transform for a receipt-verified CI loadsplat build."""
import argparse, hashlib, json, os
from pathlib import Path
BODY_SHA='30e52f7d9f1af8e3f406938ec6847c3c9801c7e05a411d829f3837a819298ee6'
ARCHIVE_SHA='0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf'
CORE='9b539761f3e152b966e08c2de0784129fe8cf68d'
def sha(b):return hashlib.sha256(b).hexdigest()
def require(ok,msg):
 if not ok:raise ValueError(msg)
def u(b,p=0):
 v=s=0;start=p
 while True:
  require(p<len(b) and s<35,'Malformed u32 LEB');x=b[p];p+=1;v|=(x&127)<<s
  if x<128:
   require(v<=0xffffffff,'u32 overflow');return v,p
  s+=7
def enc(v):
 b=[]
 while v>=128:b.append((v&127)|128);v>>=7
 return bytes(b+[v])
def string(b,p):n,p=u(b,p);require(p+n<=len(b),'Truncated name');return b[p:p+n].decode(),p+n
def limits(b,p):
 flags,p=u(b,p);require(flags in (0,1,3),'Unsupported limits');minimum,p=u(b,p)
 if flags&1:maximum,p=u(b,p)
 return p
def section(i,payload):return bytes([i])+enc(len(payload))+payload
def sections(data):
 require(data[:8]==b'\0asm\1\0\0\0','Bad Wasm header');p=8;out=[]
 while p<len(data):
  start=p;i=data[p];n,q=u(data,p+1);require(q+n<=len(data),'Truncated section');p=q+n
  out.append({'id':i,'raw':data[start:p],'payload':data[q:p],'offset':start,'length_offset':start+1,'length_bytes':data[start+1:q].hex()})
 require(len({x['id'] for x in out if x['id']})==len([x for x in out if x['id']]),'Duplicate standard section');return out
def discover(data):
 ss=sections(data);byid={s['id']:s['payload'] for s in ss if s['id']};types=[];import_types=[];func_types=[];slots={};custom=[]
 for s in ss:
  if s['id']==0:custom.append(string(s['payload'],0)[0])
 require(not any(x.startswith('.debug') for x in custom),'Debug offset sections require review')
 b=byid[1];n,p=u(b)
 for _ in range(n):
  require(b[p]==0x60,'Unsupported function type');p+=1;k,p=u(b,p);params=list(b[p:p+k]);p+=k;k,p=u(b,p);results=list(b[p:p+k]);p+=k;types.append((params,results))
 require(p==len(b),'Trailing type bytes')
 if 2 in byid:
  b=byid[2];n,p=u(b)
  for _ in range(n):
   _,p=string(b,p);_,p=string(b,p);kind=b[p];p+=1
   if kind==0:t,p=u(b,p);import_types.append(t)
   elif kind==1:require(b[p] in (0x70,0x6f),'Unsupported ref type');p=limits(b,p+1)
   elif kind==2:p=limits(b,p)
   elif kind==3:p+=2
   elif kind==4:require(b[p]==0,'Unexpected tag attribute');_,p=u(b,p+1)
   else:raise ValueError('Unknown import kind')
  require(p==len(b),'Trailing import bytes')
 b=byid[3];n,p=u(b)
 for _ in range(n):t,p=u(b,p);func_types.append(t)
 require(p==len(b),'Trailing function bytes')
 if 9 in byid:
  b=byid[9];n,p=u(b)
  for _ in range(n):
   flags,p=u(b,p);require(flags in (0,1,2,3),'Unsupported element expression encoding');table=0;offset=None
   if flags==2:table,p=u(b,p)
   if flags in (0,2):
    require(b[p]==0x41,'Nonconstant element offset');offset,p=u(b,p+1);require(not (b[p-1]&0x40) and offset<0x80000000 and b[p]==0x0b,'Invalid element offset');p+=1
   if flags in (1,2,3):require(b[p]==0,'Unexpected element kind');p+=1
   count,p=u(b,p)
   for i in range(count):
    fn,p=u(b,p)
    if offset is not None:
     key=(table,offset+i);require(key not in slots,'Overlapping active element segments');slots[key]=fn
  require(p==len(b),'Trailing element bytes')
 b=byid[10];count,p=u(b);require(count==len(func_types),'Function/code counts differ');bodies=[]
 for i in range(count):
  length_at=p;n,p=u(b,p);start=p;body=b[p:p+n];require(len(body)==n,'Truncated body');p+=n
  bodies.append({'defined_index':i,'index':len(import_types)+i,'body':body,'start':start,'length_at':length_at,'length_bytes':b[length_at:start].hex()})
 require(p==len(b),'Trailing code bytes')
 matches=[x for x in bodies if len(x['body'])==344 and sha(x['body'])==BODY_SHA];require(len(matches)==1,'Expected exactly one pinned callback body')
 target=matches[0];type_index=func_types[target['defined_index']];require(types[type_index]==([0x7f]*3,[]),'Callback ABI mismatch')
 membership=[{'table':t,'slot':s} for (t,s),fn in slots.items() if fn==target['index']];require(len(membership)==1 and membership[0]['table']==0,'Expected one table0 callback entry')
 return ss,bodies,target,membership[0],len(import_types),custom
def transform(data):
 ss,bodies,target,slot,imports,custom=discover(data);body=target['body'];require(body[76:84]==bytes.fromhex('20 08 20 01 6b 41 10 49'),'Guard pin mismatch')
 patched=body[:84]+bytes.fromhex('20 08 20 01 47 71')+body[84:];output=[data[:8]];control=[data[:8]];length_fields=[]
 for s in ss:
  if s['id']!=10:output.append(s['raw']);control.append(s['raw']);continue
  original=[enc(len(bodies))];candidate=[enc(len(bodies))]
  for x in bodies:
   bb=x['body'];original.extend([enc(len(bb)),bb]);cc=patched if x['index']==target['index'] else bb;candidate.extend([enc(len(cc)),cc])
  original=b''.join(original);candidate=b''.join(candidate);control.append(section(10,original));output.append(section(10,candidate))
  length_fields=[{'kind':'code_section_payload_length','file_offset':s['length_offset'],'before':s['length_bytes'],'after':enc(len(candidate)).hex()}, {'kind':'function_body_length','code_payload_offset':target['length_at'],'before':target['length_bytes'],'after':enc(len(patched)).hex()}]
 output=b''.join(output);control=b''.join(control);require(control==data,'Noncanonical no-op reconstruction differs');require(len(output)==len(data)+6,'Unexpected LEB boundary growth')
 out_sections=sections(output)
 for a,b in zip(ss,out_sections):require(a['id']==b['id'] and (a['id']==10 or a['raw']==b['raw']),'Other section changed')
 # Compare every body using code-section vector, independent of discovery (candidate no longer matches original hash).
 code=next(x['payload'] for x in out_sections if x['id']==10);count,p=u(code);different=[]
 for i in range(count):
  n,p=u(code,p);bb=code[p:p+n];p+=n
  if bb!=bodies[i]['body']:different.append(i);require(bb==patched,'Unexpected changed body')
 require(different==[target['defined_index']],'Not a single-body difference')
 return output,{'source_wasm_sha256':sha(data),'candidate_wasm_sha256':sha(output),'original_body_sha256':BODY_SHA,'candidate_body_sha256':sha(patched),'absolute_function_index':target['index'],'defined_function_index':target['defined_index'],'imported_function_count':imports,'callback_table':slot,'abi':{'params':['i32']*3,'results':[]},'source_bytes':len(data),'candidate_bytes':len(output),'insertion_body_offset':84,'changed_length_fields':length_fields,'noop_roundtrip_sha256':sha(control),'all_other_functions_sections_identical':True,'custom_sections':custom}
def prepare(binary,archive,out):
 binary=Path(binary);archive=Path(archive);out=Path(out);receipt_path=binary.parent/'complete.json';r=json.loads(receipt_path.read_text());identity=r['identity']
 expected={'core':CORE,'rust':'1.96.0','emscripten':'4.0.8','runtime':ARCHIVE_SHA,'kind':'browser-mt-xnnpack','optimization':'z','graph_level':1,'xnnpack':True,'wrapper':'76c21496eebbd122f41d43413878bbf3e2213a14bb8e86ae50a594bdcf54ed72','core_diagnostic_patch':'b69c5ef4794c10225257aead4f00e2e3985c27e1fae6d6c7ac640075eff47053','core_fixed_shape_patch':'e0908f6593b202f6990954155f15952ff318dfd681beb3077292ee52a57f2ef8'}
 for k,v in expected.items():require(identity.get(k)==v,'Build identity mismatch: '+k)
 require(sha(archive.read_bytes())==ARCHIVE_SHA,'Archive hash mismatch')
 require(binary.name in r['files'] and binary.with_suffix('.wasm').name in r['files'],'Missing JS/Wasm receipt entries')
 for name,digest in r['files'].items():require(Path(name).name==name and sha((binary.parent/name).read_bytes())==digest,'Build asset mismatch: '+name)
 require('native-bundle-verification.json' in r['files'],'Missing native bundle receipt');bundle=json.loads((binary.parent/'native-bundle-verification.json').read_text());require(bundle.get('verified') is True and bundle.get('runtime_sha256')==ARCHIVE_SHA,'Native bundle proof mismatch')
 require(set(bundle.get('invalidated_packages',[]))=={'voicevox_core','voicevox_benchmark'},'Native bundle invalidation missing')
 candidate,proof=transform(binary.with_suffix('.wasm').read_bytes());out.mkdir(parents=True,exist_ok=True);require(not (out/'candidate.wasm').exists(),'Refuse overwrite')
 (out/'candidate.wasm').write_bytes(candidate);proof.update({'source_binary':str(binary),'source_receipt_sha256':sha(receipt_path.read_bytes()),'source_js_sha256':sha(binary.read_bytes()),'archive_sha256':ARCHIVE_SHA,'native_bundle_proof':bundle,'build_identity':identity,'transform_source_sha256':sha(Path(__file__).read_bytes()),'session_id':os.environ.get('CODEX_SESSION_ID'),'scope':'Experimental guard-only Wasm transform. No browser execution or timing.'});(out/'transform.json').write_text(json.dumps(proof,indent=2)+'\n');return proof
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--binary',required=True,type=Path);p.add_argument('--archive',required=True,type=Path);p.add_argument('--output-dir',required=True,type=Path);a=p.parse_args();print(json.dumps(prepare(a.binary,a.archive,a.output_dir),indent=2))
