"""Regenerate private, source-pinned vocoder candidates; never publish model bytes."""
import argparse,hashlib,importlib.util,json,sys,zipfile
from pathlib import Path
import onnx
from transform_convtranspose import _promote, ORIGINAL_SHA256
from transform_fusion import fuse

def digest(data):return hashlib.sha256(data).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 root=Path(__file__).parent;sp=importlib.util.spec_from_file_location('benchmark',root/'voicevox_webcpu_research.py');b=importlib.util.module_from_spec(sp);sys.modules[sp.name]=b;sp.loader.exec_module(b)
 assets=json.loads((a.cache/'research-assets.json').read_text());original=Path(assets['model']);binary=Path(assets['binaries']['mt']);receipt=json.loads((binary.parent/'complete.json').read_text())
 q=b.prepared_query(10);query_hash=digest(json.dumps(q,ensure_ascii=False,separators=(',',':')).encode());assert assets['query_sha256']==query_hash
 with zipfile.ZipFile(original) as z:original_bytes=z.read('vocoder.onnx')
 assert digest(original_bytes)==ORIGINAL_SHA256
 model=onnx.load_model_from_string(original_bytes)
 ct,ct_edits=_promote(model);fused,fusion_edits=fuse(model);combined,combined_edits=fuse(ct)
 assert len(ct_edits)==4 and len(fusion_edits)==len(combined_edits)==37
 reverse,_=_promote(fused);assert reverse.SerializeToString()==combined.SerializeToString()
 variants=[('original','Original CPU x2',None),('convtranspose_2d','ConvTranspose2D CPU x2',ct),('fusion','Selective ConvLeaky fusion CPU x2',fused),('combined','ConvTranspose2D + fusion CPU x2',combined)]
 rows=[];provenance={}
 for key,label,changed in variants:
  path=original
  if changed is not None:
   onnx.checker.check_model(changed);data=changed.SerializeToString();path=a.output.parent/(key+'-private.vvm')
   with zipfile.ZipFile(original) as source,zipfile.ZipFile(path,'w') as target:
    for info in source.infolist():target.writestr(info,data if info.filename=='vocoder.onnx' else source.read(info.filename))
   provenance[key]={'vocoder_sha256':digest(data),'vvm_sha256':b.sha256_file(path)}
  rows.append({'key':key,'label':label,'threads':2,'binary':str(binary),'build_identity':receipt['identity'],'model':str(path),'model_sha256':b.sha256_file(path),'revectorize':False,'description':'Exploratory browser CPU-only FP32 graph candidate; explicit V8 OFF; no global graph-optimization-level change'})
 a.output.write_text(json.dumps({'variants':rows,'fixture_query_sha256':query_hash,'transform_provenance':provenance},indent=2))
 print('Prepared four private vocoder variants; model weights and audio are not artifacts.')
if __name__=='__main__':main()
