#!/usr/bin/env python3
"""Validate pinned measured output, then preserve its exact native HTML and numeric projection."""
import argparse,hashlib,importlib.util,json,math,sys,tempfile
from pathlib import Path
PIN='10edbd6a4cc120c7bab10ff40b47e41f880e54f3ba46756e78de88fa6d41facf'
FORBIDDEN={'wav_base64','audio_outputs','raw_samples','model_bytes','tensor_data','weights','audio_base64','raw_wave','wav_bytes'}
def check(value,schema,path=''):
    rule=schema.get(path)
    kind='null' if value is None else 'bool' if type(value)is bool else 'number' if type(value)in(int,float) else 'dict' if isinstance(value,dict) else 'list' if isinstance(value,list) else 'str' if isinstance(value,str) else 'invalid'
    if rule is None or kind not in rule['types']:raise ValueError('Unexpected schema type at '+path)
    if isinstance(value,dict):
        if set(value)-set(rule['keys']):raise ValueError('Unknown keys at '+path)
        for key,item in value.items():
            if key.lower() in FORBIDDEN:raise ValueError('Forbidden payload')
            if key.lower() in {'fp32_samples','pcm_samples'} and (type(item)is not int or item<0):raise ValueError('Invalid sample count')
            check(item,schema,path+'/'+key)
    elif isinstance(value,list):
        for item in value:check(item,schema,path+'/*')
    elif isinstance(value,float) and not math.isfinite(value):raise ValueError('Nonfinite metric')
    elif isinstance(value,str) and 'data:audio/' in value.lower():raise ValueError('Audio payload')
def load_candidate(path):
    if hashlib.sha256(path.read_bytes()).hexdigest()!=PIN:raise ValueError('Candidate pin mismatch')
    spec=importlib.util.spec_from_file_location('measured_distribution',path);h=importlib.util.module_from_spec(spec);sys.modules[spec.name]=h;spec.loader.exec_module(h);return h

def validate(value,h,schema):
    if value.get('standalone_sha256')!=PIN or value.get('schema')!='voicevox-standalone-kernel-experiment-v1':raise ValueError('Result identity mismatch')
    if value.get('status')!='complete' or value.get('private_helpers_deleted') is not True or value.get('revectorization')!='off':raise ValueError('Incomplete or wrong-flag result')
    projected={k:value[k] for k in ('confirmation','cpu_reference','summary')};check(projected,schema)
    c=projected['confirmation'];q=projected['cpu_reference']
    if c['status']!='complete' or c['revectorization']!='off' or c.get('private_temporary_directory_deleted') is not True:raise ValueError('Incomplete confirmation')
    validator={};exec(compile(h.KERNEL_EMBEDDED_SOURCES['runtime/validate_runtime_confirmation.py'],'embedded-validator','exec'),validator);validator['validate'](c,3,5,5)
    if q.get('passed') is not True or q.get('private_temporary_directory_deleted') is not True:raise ValueError('Reference gate failed')
    if set(q['references'])!={'cpu','xnn'} or q['revectorization']!='off' or q['style_id']!=302:raise ValueError('Reference identity mismatch')
    for item in q['references'].values():
        m=item['repeat_comparison']
        if not all(m[k] is True for k in ('finite','fp32_exact','pcm_exact')):raise ValueError('Reference repeat failed')
    if q['xnn_vs_cpu']['finite'] is not True:raise ValueError('Nonfinite CPU reference')
    if h.kernel_summary(projected)!=projected['summary']:raise ValueError('Summary differs from recomputation')
    return dict(projected,revectorization='off')
def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True);p.add_argument('--result',type=Path,required=True);p.add_argument('--html',type=Path,required=True);p.add_argument('--validated-output',type=Path,required=True);a=p.parse_args()
    h=load_candidate(a.candidate);raw=a.result.read_bytes();value=json.loads(raw);schema=json.loads(Path(__file__).with_name('numeric_schema.json').read_text());projected=validate(value,h,schema)
    html=a.html.read_bytes()
    with tempfile.TemporaryDirectory() as t:
        rendered=Path(t)/'verified.html';h.render_kernel_report(projected,rendered)
        if rendered.read_bytes()!=html:raise ValueError('Native HTML differs from pinned renderer')
    safe=json.dumps(projected,ensure_ascii=False,indent=2,allow_nan=False).encode();receipt={'candidate_sha256':PIN,'source_json_sha256':hashlib.sha256(raw).hexdigest(),'projection_sha256':hashlib.sha256(safe).hexdigest(),'html_sha256':hashlib.sha256(html).hexdigest(),'html_bytes':len(html),'primary_calls':45,'cpu_companions':9,'native_html_byte_identical_to_pinned_renderer':True}
    a.validated_output.mkdir(parents=True,exist_ok=False)
    for name,b in [('measured.html',html),('numeric-projection.json',safe),('receipt.json',json.dumps(receipt,indent=2).encode())]:(a.validated_output/name).write_bytes(b)
    # All validation is complete before the first output. Exact UTF-8 chunks keep original bytes recoverable.
    print('MEASURED_FILE_RECEIPT '+json.dumps(receipt,allow_nan=False))
    for label,b in [('HTML',html),('NUMERIC',safe)]:
        text=b.decode('utf-8');parts=[text[i:i+10000] for i in range(0,len(text),10000)]
        for i,part in enumerate(parts):print('MEASURED_'+label+'_CHUNK '+json.dumps({'index':i,'total':len(parts),'text':part},ensure_ascii=True,allow_nan=False))
if __name__=='__main__':main()
