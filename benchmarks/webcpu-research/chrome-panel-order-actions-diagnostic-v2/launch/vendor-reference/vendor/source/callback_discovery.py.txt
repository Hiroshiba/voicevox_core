#!/usr/bin/env python3
"""Read-only callback discovery; no transform or execution capability."""
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
def discover(data, body_sha=BODY_SHA, body_bytes=344):
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
 matches=[x for x in bodies if len(x['body'])==body_bytes and sha(x['body'])==body_sha];require(len(matches)==1,'Expected exactly one pinned callback body')
 target=matches[0];type_index=func_types[target['defined_index']];require(types[type_index]==([0x7f]*3,[]),'Callback ABI mismatch')
 membership=[{'table':t,'slot':s} for (t,s),fn in slots.items() if fn==target['index']];require(len(membership)==1 and membership[0]['table']==0,'Expected one table0 callback entry')
 return ss,bodies,target,membership[0],len(import_types),custom
