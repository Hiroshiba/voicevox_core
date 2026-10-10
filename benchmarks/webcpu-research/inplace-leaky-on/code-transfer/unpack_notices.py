"""Lossless frozen notice materialization only; no arbitrary archive paths."""
import argparse,base64,gzip,hashlib,io,json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
PINS_SHA='163e5c8a6ef68eca51ee7e65e7df0444e2c431b806dc3466ddf98d957f250e04'
def unpack(output):
 raw=(ROOT/'notice_pins.json').read_bytes()
 if hashlib.sha256(raw).hexdigest()!=PINS_SHA:raise ValueError('notice_pin_hash')
 pins=json.loads(raw)
 if set(pins)!={'LICENSES.txt','NOTICE_INVENTORY.json'} or output.exists():raise ValueError('notice_output')
 results={}
 for name,pin in pins.items():
  encoded=(ROOT/(name+'.gz.b64')).read_bytes()
  if hashlib.sha256(encoded).hexdigest()!=pin['encoded_sha256']:raise ValueError('encoded_notice_hash')
  with gzip.GzipFile(fileobj=io.BytesIO(base64.b64decode(encoded,validate=True))) as f:data=f.read(pin['decoded_bytes']+1)
  if len(data)!=pin['decoded_bytes'] or hashlib.sha256(data).hexdigest()!=pin['decoded_sha256']:raise ValueError('decoded_notice_hash')
  data.decode('utf-8');results[name]=data
 output.mkdir(parents=True)
 for name,data in results.items():(output/name).write_bytes(data)
 return {k:hashlib.sha256(v).hexdigest() for k,v in results.items()}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 try:r=unpack(a.output)
 except Exception:raise SystemExit('notice_unpack_failed') from None
 print(json.dumps({'schema':'notice_unpack_v1','hashes':r},sort_keys=True))
