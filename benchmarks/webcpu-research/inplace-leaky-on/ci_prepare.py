#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0", "onnx==1.19.1", "protobuf==6.32.1", "ml-dtypes==0.5.1"]
# ///
"""Prepare only the pristine ON study; emit fixed preparation diagnostics."""
import argparse,hashlib,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'preparation'))
from prepare_on_source import STAGE_PHASES,STAGE_EXCEPTION_CLASSES

def relay(text):
 for line in text.splitlines():
  if not line.startswith('SOURCE_PREP_STAGE '):continue
  value=json.loads(line.split(' ',1)[1])
  if set(value)-{'schema','phase','status','exception_class'} or value.get('schema')!='inplace-source-prep-stage-v1' or value.get('phase') not in STAGE_PHASES or value.get('status') not in {'begin','pass','fail'}:raise ValueError('Invalid preparation diagnostic')
  if 'exception_class' in value and (value['status']!='fail' or value['exception_class'] not in STAGE_EXCEPTION_CLASSES):raise ValueError('Invalid preparation diagnostic exception')
  print('SOURCE_PREP_STAGE '+json.dumps(value,sort_keys=True),flush=True)

def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--private',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);a=p.parse_args();a.private.mkdir(parents=True,exist_ok=True)
 base=ROOT/'extracted/base';harness=base/'voicevox_webcpu_research.py';runtime=base/'runtime/prepare_vocoder_runtime.py'
 steps=[('canonical_assets',[harness,'--prepare-only','--backend-experiments','all','--threads','2','--cache-dir',a.cache]),('original_runtime',[runtime,'--cache',a.cache,'--output',a.private/'original.json','--harness',harness,'--kernel-dir',base/'kernel','--threads','2','--variants','original']),('source_controls',[ROOT/'preparation/prepare_on_source.py','--cache',a.cache,'--output',a.manifest,'--harness',harness,'--runtime-preparer',runtime])]
 env=dict(os.environ);env['PYTHONUNBUFFERED']='1';env.pop('PYTHONOPTIMIZE',None)
 for name,args in steps:
  log=a.private/(name+'.log')
  with log.open('wb') as output:r=subprocess.run([sys.executable,*map(str,args)],stdout=output,stderr=subprocess.STDOUT,env=env)
  data=log.read_bytes();relay(data.decode('utf-8',errors='replace'))
  record={'schema':'inplace-on-preparation-step-v1','step':name,'exit_code':r.returncode,'log_sha256':hashlib.sha256(data).hexdigest(),'log_bytes':len(data)}
  print('SOURCE_ON_PREPARATION '+json.dumps(record,sort_keys=True),flush=True)
  if r.returncode:raise SystemExit(r.returncode)
if __name__=='__main__':main()
