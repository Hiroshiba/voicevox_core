#!/usr/bin/env python3
"""Select only a fully verified cached loadsplat baseline; exit3 means preparation needed."""
import argparse,json,tempfile
from pathlib import Path
from prepare_inplace_leaky import ARCHIVE_SHA,prepare,sha
MODEL_SHA='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9'
def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();archive=a.cache/'vocoder-dispatch-v3/archives/loadsplat/libonnxruntime_webassembly.a';models=[a.cache/'original.vvm']
 if (a.cache/'research-assets.json').exists():models.insert(0,Path(json.loads((a.cache/'research-assets.json').read_text())['model']))
 model=next((x for x in models if x.exists() and sha(x.read_bytes())==MODEL_SHA),None)
 if not archive.exists() or model is None:raise SystemExit(3)
 receipts=[]
 for p in sorted((a.cache/'builds').glob('*/complete.json')):
  r=json.loads(p.read_text());ident=r.get('identity',{})
  if ident.get('runtime')==ARCHIVE_SHA and ident.get('kind')=='browser-mt-xnnpack' and ident.get('optimization')=='z' and 'native_bundle_policy' in ident:receipts.append(p)
 if not receipts:raise SystemExit(3)
 # Verified candidate bodies must match the pin. Do not silently accept another ABI or reinterpret a failure.
 for receipt in receipts:
  binary=receipt.parent/'voicevox_benchmark.js'
  with tempfile.TemporaryDirectory(prefix='inplace-cache-check-') as tmp:proof=prepare(binary,archive,Path(tmp)/'check')
  a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps({'binary':str(binary),'archive':str(archive),'model':str(model),'receipt_sha256':proof['source_receipt_sha256'],'wasm_sha256':proof['source_wasm_sha256']},indent=2));print('VERIFIED_CACHE_BASELINE 1');return
 raise SystemExit(3)
if __name__=='__main__':main()
