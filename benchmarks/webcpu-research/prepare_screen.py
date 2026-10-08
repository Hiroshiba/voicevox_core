"""Prepare a bounded fixed-predictor/thread screen, private models only."""
import argparse,hashlib,importlib.util,json,sys,zipfile
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 root=Path(__file__).parent
 spec=importlib.util.spec_from_file_location('benchmark',root/'voicevox_webcpu_research.py');b=importlib.util.module_from_spec(spec);sys.modules['benchmark']=b;spec.loader.exec_module(b)
 assets=json.loads((a.cache/'research-assets.json').read_text());model=Path(assets['model']);binary=Path(assets['binaries']['mt']);receipt=json.loads((binary.parent/'complete.json').read_text())
 q=b.prepared_query(10);query_hash=hashlib.sha256(json.dumps(q,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
 if assets['query_sha256'] != query_hash:raise ValueError('Query differs from prepared fixture')
 # This fixed shape is derived from the pinned prepared-query native diagnostic:
 # 239104 samples /256 =934 unpadded frames; predictor adds38+38 =>1010.
 # Browser input-shape checking and full waveform comparison remain mandatory.
 import onnx
 fixed=a.output.parent/'predictor-fixed1010-private.vvm'
 with zipfile.ZipFile(model) as source,zipfile.ZipFile(fixed,'w') as target:
  for info in source.infolist():
   data=source.read(info.filename)
   if info.filename=='predict_spectrogram.onnx':
    m=onnx.load_from_string(data)
    if [(v.name,[d.dim_value or d.dim_param for d in v.type.tensor_type.shape.dim]) for v in m.graph.input] != [('f0',['length',1]),('phoneme',['length',45]),('speaker_id',[1])]:
     raise ValueError('Unexpected predictor input topology')
    for value in [*m.graph.input,*m.graph.output,*m.graph.value_info]:
     for dim in value.type.tensor_type.shape.dim:
      if dim.dim_param=='length':dim.dim_value=1010
    onnx.checker.check_model(m);data=m.SerializeToString()
   target.writestr(info,data)
 rows=[]
 for key,label,threads,path in [('cpu2','CPU x2 original',2,model),('fixed2','CPU x2 predictor fixed',2,fixed),('cpu4','CPU x4 original',4,model),('fixed4','CPU x4 predictor fixed',4,fixed)]:
  rows.append({'key':key,'label':label,'threads':threads,'binary':str(binary),'build_identity':receipt['identity'],
               'model':str(path),'model_sha256':b.sha256_file(path),'revectorize':False,
               'description':'Preliminary same-host explicit V8-OFF screen; predictor-only fixed shape or thread count; no arithmetic rewrite'})
 a.output.write_text(json.dumps({'variants':rows,'fixture_query_sha256':query_hash,'fixed_predictor_length':1010},indent=2))
 print('Prepared4variant manifest; no model/audio published')
if __name__=='__main__':main()
