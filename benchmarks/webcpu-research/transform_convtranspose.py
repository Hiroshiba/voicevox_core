"""Promote the four pinned sample vocoder ConvTranspose nodes to rank four.
Preserves graph-facing tensors and all FP32 arithmetic. No work at import.
"""
import argparse,copy,hashlib,json
from pathlib import Path
import onnx
from onnx import helper
ORIGINAL_SHA256="80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820"
EXPECTED_NAMES=['ConvTranspose_4','ConvTranspose_54','ConvTranspose_104','ConvTranspose_154']
def _promote(model):
 m=copy.deepcopy(model); assert next(x.version for x in m.opset_import if not x.domain)==12
 initial={x.name:x for x in m.graph.initializer}; nodes=[]; edits=[]
 for old in m.graph.node:
  if old.op_type!='ConvTranspose': nodes.append(old);continue
  n=copy.deepcopy(old);a={v.name:helper.get_attribute_value(v) for v in old.attribute}
  assert not old.domain and set(a)<=set('auto_pad dilations group kernel_shape pads strides output_padding output_shape'.split())
  w=initial[n.input[1]]; assert len(w.dims)==3
  wn=w.name+'__deconv2d'; ww=copy.deepcopy(w); ww.name=wn; ww.dims.insert(2,1)
  assert ww.raw_data==w.raw_data and list(ww.dims)==[w.dims[0],w.dims[1],1,w.dims[2]]
  m.graph.initializer.append(ww);n.input[1]=wn
  attrs=copy.deepcopy(a)
  for key in ['kernel_shape','strides','dilations']:
   if key in attrs: assert len(attrs[key])==1; attrs[key]=[1,*attrs[key]]
  if 'pads' in attrs: assert len(attrs['pads'])==2; attrs['pads']=[0,attrs['pads'][0],0,attrs['pads'][1]]
  if 'output_padding' in attrs: assert len(attrs['output_padding'])==1; attrs['output_padding']=[0,*attrs['output_padding']]
  if 'output_shape' in attrs: assert len(attrs['output_shape'])==1; attrs['output_shape']=[1,*attrs['output_shape']]
  del n.attribute[:];n.attribute.extend(helper.make_attribute(k,v) for k,v in attrs.items())
  inp=n.input[0];res=n.output[0]; n.input[0]=inp+'__deconv2d_input';n.output[0]=res+'__deconv2d_output'
  nodes += [helper.make_node('Unsqueeze',[inp],[n.input[0]],axes=[2],name=n.name+'__expand'),n,helper.make_node('Squeeze',[n.output[0]],[res],axes=[2],name=n.name+'__squeeze')]
  edits.append({'name':n.name,'before':{k:v.decode() if isinstance(v,bytes) else v for k,v in a.items()},'after':{k:v.decode() if isinstance(v,bytes) else v for k,v in attrs.items()}})
 del m.graph.node[:];m.graph.node.extend(nodes)
 used={i for n in nodes for i in n.input}; keep=[x for x in m.graph.initializer if x.name in used];del m.graph.initializer[:];m.graph.initializer.extend(keep)
 onnx.checker.check_model(m);return m,edits

def transform(input_path, output_path):
 input_path,output_path=Path(input_path),Path(output_path)
 assert input_path.resolve()!=output_path.resolve(), "Never overwrite source"
 data=input_path.read_bytes()
 assert hashlib.sha256(data).hexdigest()==ORIGINAL_SHA256, "Unrecognized source model"
 model=onnx.load_model_from_string(data)
 assert [n.name for n in model.graph.node if n.op_type=='ConvTranspose']==EXPECTED_NAMES
 names={v for n in model.graph.node for v in [n.name,*n.input,*n.output]}
 names.update(t.name for t in model.graph.initializer)
 assert not any('__deconv2d' in n for n in names), "Inserted-name collision"
 # Every original output remains produced by a Squeeze with the same name.
 # All consumers remain unchanged, so multi-consumer tensors are safe.
 consumers={n.output[0]:[(c.name,i) for c in model.graph.node for i,v in enumerate(c.input) if v==n.output[0]] for n in model.graph.node if n.op_type=='ConvTranspose'}
 promoted,edits=_promote(model)
 assert len(edits)==4
 for output,expected in consumers.items():
  actual=[(c.name,i) for c in promoted.graph.node for i,v in enumerate(c.input) if v==output]
  assert actual==expected
 outputs=[v for n in promoted.graph.node for v in n.output if v]
 assert len(outputs)==len(set(outputs)), "Duplicate tensor producer"
 onnx.checker.check_model(promoted)
 output_path.parent.mkdir(parents=True,exist_ok=True)
 onnx.save(promoted,output_path)
 return {'source_sha256':ORIGINAL_SHA256,'candidate_sha256':hashlib.sha256(output_path.read_bytes()).hexdigest(),'edits':edits}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('input_path');p.add_argument('output_path');args=p.parse_args()
 print(json.dumps(transform(args.input_path,args.output_path),indent=2))
