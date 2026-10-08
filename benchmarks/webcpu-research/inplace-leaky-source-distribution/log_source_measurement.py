#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0", "onnx==1.19.1", "protobuf==6.32.1", "ml-dtypes==0.5.1"]
# ///
"""Revalidate with the pinned distribution, reproduce HTML, emit only verified artifacts."""
import argparse,hashlib,importlib.util,json,sys,tempfile
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--distribution',type=Path,required=True);p.add_argument('--sha256',required=True);p.add_argument('--result',type=Path,required=True);p.add_argument('--html',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 digest=lambda b:hashlib.sha256(b).hexdigest()
 if digest(a.distribution.read_bytes())!=a.sha256:raise ValueError('Distribution SHA mismatch')
 spec=importlib.util.spec_from_file_location('source_distribution',a.distribution);h=importlib.util.module_from_spec(spec);sys.modules[spec.name]=h;spec.loader.exec_module(h)
 value=json.loads(a.result.read_text());assert value['schema']==h.INPLACE_SCHEMA and value['status']=='complete' and value['private_helpers_deleted'] is True and value['standalone_sha256']==a.sha256
 c=value['confirmation'];assert c['distribution']['sha256']==a.sha256
 h.inplace_check_privacy(value);summary=h.inplace_validate_saved_confirmation(c)
 assert summary==value['validated_summary'] and h.inplace_summary(c)==value['summary']
 html=a.html.read_bytes()
 with tempfile.TemporaryDirectory() as directory:
  reproduced=Path(directory)/'verified.html';h.inplace_report_from(value,reproduced)
  assert reproduced.read_bytes()==html,'HTML differs from pinned native renderer'
 numeric=json.dumps(c,ensure_ascii=False,indent=2,allow_nan=False).encode()
 receipt={'distribution_sha256':a.sha256,'confirmation_sha256':digest(numeric),'html_sha256':digest(html),'html_bytes':len(html),'primary_calls':45,'native_html_byte_identical':True,'private_helpers_deleted':True}
 a.output.mkdir(parents=True,exist_ok=False)
 for name,data in [('measured.html',html),('confirmation.json',numeric),('receipt.json',json.dumps(receipt,indent=2).encode())]:(a.output/name).write_bytes(data)
 assert {p.name for p in a.output.iterdir()}=={'measured.html','confirmation.json','receipt.json'}
 print('SOURCE_MEASURED_RECEIPT '+json.dumps(receipt))
 for label,data in [('HTML',html),('NUMERIC',numeric)]:
  text=data.decode();parts=[text[i:i+10000] for i in range(0,len(text),10000)]
  for i,part in enumerate(parts):print('SOURCE_MEASURED_'+label+'_CHUNK '+json.dumps({'index':i,'total':len(parts),'text':part},ensure_ascii=True,allow_nan=False))
if __name__=='__main__':main()
