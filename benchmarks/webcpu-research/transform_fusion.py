"""Pinned, selective ORT 1.23.2 ConvActivationFusion equivalent; CPU candidate only."""
import copy,hashlib,json,struct
from pathlib import Path
import onnx
from onnx import helper
ORIGINAL_SHA256='80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820'
SOURCE_URL='https://raw.githubusercontent.com/microsoft/onnxruntime/v1.23.2/onnxruntime/core/optimizer/conv_activation_fusion.cc'
def fuse(m):
 m=copy.deepcopy(m); ops={o.domain:o.version for o in m.opset_import};assert ops['']==12 and ops['com.microsoft']==1
 assert not any(a.type in (onnx.AttributeProto.GRAPH,onnx.AttributeProto.GRAPHS) for n in m.graph.node for a in n.attribute)
 consumers={}; producers={}; outputs={x.name for x in m.graph.output};initial={x.name:x for x in m.graph.initializer}
 for i,n in enumerate(m.graph.node):
  for v in n.input:consumers.setdefault(v,[]).append(i)
  for v in n.output:assert v not in producers;producers[v]=i
 replacements={};removed=set();edits=[]
 for i,n in enumerate(m.graph.node):
  if n.domain or n.op_type!='Conv' or len(n.output)!=1:continue
  out=n.output[0];cs=consumers.get(out,[])
  if out in outputs or len(cs)!=1:continue
  j=cs[0];a=m.graph.node[j]
  if a.domain or a.op_type!='LeakyRelu' or list(a.input)!=[out] or len(a.output)!=1:continue
  assert len(n.input)==3 and initial[n.input[1]].data_type==initial[n.input[2]].data_type==onnx.TensorProto.FLOAT
  aa={x.name:x for x in a.attribute}; assert set(aa)=={'alpha'} and aa['alpha'].type==onnx.AttributeProto.FLOAT
  fused=copy.deepcopy(n);fused.domain='com.microsoft';fused.op_type='FusedConv';fused.output[:]=a.output
  fused.attribute.extend([helper.make_attribute('activation','LeakyRelu'),helper.make_attribute('activation_params',[aa['alpha'].f])])
  assert struct.pack('<f',fused.attribute[-1].floats[0])==struct.pack('<f',aa['alpha'].f)
  replacements[i]=fused;removed.add(j)
  edits.append({'conv':n.name,'activation':a.name,'removed_tensor':out,'output':a.output[0],'alpha_fp32_hex':struct.pack('<f',aa['alpha'].f).hex()})
 nodes=[replacements.get(i,n) for i,n in enumerate(m.graph.node) if i not in removed];del m.graph.node[:];m.graph.node.extend(nodes)
 # Retain original metadata and shapes, dropping only value_info for dead intermediates.
 dead={e['removed_tensor'] for e in edits};vi=[v for v in m.graph.value_info if v.name not in dead];del m.graph.value_info[:];m.graph.value_info.extend(vi)
 onnx.checker.check_model(m);return m,edits

def transform(src,dst):
 src,dst=Path(src),Path(dst);assert src.resolve()!=dst.resolve();data=src.read_bytes();assert hashlib.sha256(data).hexdigest()==ORIGINAL_SHA256
 m=onnx.load_model_from_string(data);new,edits=fuse(m);assert len(edits)==37
 assert [x.SerializeToString() for x in m.graph.initializer]==[x.SerializeToString() for x in new.graph.initializer]
 assert m.opset_import==new.opset_import and m.graph.input==new.graph.input and m.graph.output==new.graph.output
 onnx.save(new,dst)
 return {'source_sha256':ORIGINAL_SHA256,'candidate_sha256':hashlib.sha256(dst.read_bytes()).hexdigest(),'ort_reference':SOURCE_URL,'edits':edits}
if __name__=='__main__':
 import sys
 print(json.dumps(transform(*sys.argv[1:]),indent=2))
