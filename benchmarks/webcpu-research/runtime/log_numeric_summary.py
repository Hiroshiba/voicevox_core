#!/usr/bin/env python3
"""Fail-closed allowlist for FUTURE CI numeric logs. Never logs arbitrary source JSON.

This does not retrieve or re-expose previously denied artifacts. Inputs are fresh
outputs from the current authorized job. No waveform/model/tensor contents pass.
"""
import argparse,json,pathlib,math,re,statistics
VARIANTS={'original','untouched','auto_roundtrip','auto-roundtrip','splat','loadsplat'}
def number(v,positive=False):
 if type(v) not in (int,float) or not math.isfinite(v) or (v<=0 if positive else v<0):raise ValueError('Invalid numeric metric')
 return v
def integer(v,low=0,high=1_000_000):
 if type(v) is not int or not low<=v<=high:raise ValueError('Invalid integer metric')
 return v
DISPATCH_ERRORS={'Invalid expected dispatcher','Dispatch proof must run inside the actual dedicated CORE Worker','Worker hardwareConcurrency unavailable','Shared isolated CORE required','Actual CORE dispatcher export/stdout capture missing','Expected exactly one actual CORE dispatch record','Unexpected dispatcher record schema','Actual CORE architecture/precision/tile/context mismatch','Inconsistent actual CORE pointer family','Actual CORE dispatcher does not match manifest/heuristic','Unexpected dispatcher exception','CORE Worker hardwareConcurrency changed across variants','Untouched/roundtrip auto dispatcher differs'}
def failed_dispatch(p,success_fields):
 base={'schema_version','source','context','expected_dispatch','worker_hardware_concurrency','numeric_records','verified','error_type','error_message'}
 if set(p) not in (base,success_fields|{'error_type','error_message'}):raise ValueError('Unexpected failed dispatch schema')
 if p['schema_version']!=1 or p['source']!='actual_CORE_module' or p['context'] not in ['dedicated_worker','not_dedicated_worker'] or p['expected_dispatch'] not in ['auto','splat','loadsplat','invalid']:raise ValueError('Invalid failed dispatch context')
 if p['error_type'] not in ['Error','TypeError','RangeError','SyntaxError','RuntimeError'] or p['error_message'] not in DISPATCH_ERRORS:raise ValueError('Unapproved error string')
 hc=p['worker_hardware_concurrency']
 if hc is not None:integer(hc,0,2147483647)
 safe={k:p[k] for k in base-{'numeric_records'}}
 records=p.get('numeric_records',[])
 if not isinstance(records,list) or len(records)>2:raise ValueError('Too many failed records')
 keys={'is_x86','navigatorHardwareConcurrency','mr','nr','loadsplatPointers','splatPointers','checkedPointers','relaxedSimd'}
 for record in records:
  if not isinstance(record,dict) or not set(record)<=keys:raise ValueError('Unexpected failed record')
  for value in record.values():integer(value,-2147483647,2147483647)
 safe['numeric_records']=records
 if 'actual_dispatch' in p:
  if p['actual_dispatch'] not in ['splat','loadsplat']:raise ValueError('Invalid observed family')
  safe['actual_dispatch']=p['actual_dispatch']
  for key in ['splat_pointers','loadsplat_pointers','checked_pointers','probe_return_code','probe_hardware_concurrency']:safe[key]=integer(p[key],0,2147483647)
 return safe
def core_dispatch(data):
 rows=[]
 required={'schema_version','source','context','expected_dispatch','actual_dispatch','worker_hardware_concurrency','probe_hardware_concurrency','is_x86','relaxed_simd','mr','nr','splat_pointers','loadsplat_pointers','checked_pointers','probe_return_code','cross_origin_isolated','shared_memory','verified'}
 for key,p in data.items():
  variant=key.removesuffix('_dispatch')
  if variant not in VARIANTS:raise ValueError('Unexpected dispatch variant')
  if p.get('verified') is False:
   rows.append({'variant':variant,**failed_dispatch(p,required)});continue
  if set(p)!=required:raise ValueError('Unexpected dispatch schema/variant')
  if p['schema_version']!=1 or p['source']!='actual_CORE_module' or p['context']!='dedicated_worker':raise ValueError('Not actual CORE Worker evidence')
  if any(p[k] is not True for k in ['is_x86','cross_origin_isolated','shared_memory','verified']) or p['relaxed_simd'] is not False:raise ValueError('Dispatch verification failed')
  hc=integer(p['worker_hardware_concurrency'],1,4096)
  if integer(p['probe_hardware_concurrency'],1,4096)!=hc or p['mr']!=4 or p['nr']!=8 or p['checked_pointers']!=12:raise ValueError('Dispatch context mismatch')
  expected=p['expected_dispatch'];actual=p['actual_dispatch']
  if expected not in ['auto','splat','loadsplat'] or actual not in ['splat','loadsplat']:raise ValueError('Unknown dispatch family')
  if actual!=(('loadsplat' if hc>4 else 'splat') if expected=='auto' else expected):raise ValueError('Wrong dispatcher')
  if integer(p['loadsplat_pointers'],0,12)+integer(p['splat_pointers'],0,12)!=12 or p[actual+'_pointers']!=12 or integer(p['probe_return_code'],0,1)!=int(actual=='loadsplat'):raise ValueError('Pointer mismatch')
  rows.append({'variant':variant,**p})
 if not rows:raise ValueError('No actual CORE dispatch records')
 successful=[r for r in rows if r['verified']]
 if len({r['worker_hardware_concurrency'] for r in successful})>1:raise ValueError('Different Worker concurrency between variants')
 if len({r['actual_dispatch'] for r in successful if r['expected_dispatch']=='auto'})>1:raise ValueError('Auto controls differ')
 return {'schema_version':1,'kind':'actual_core_worker_dispatch','records':rows}
def kernel(data):
 meta=data['metadata']
 if meta.get('context')!='dedicated Worker' or meta.get('crossOriginIsolated') is not True:raise ValueError('Not isolated browser Worker kernel evidence')
 version=meta.get('browserVersion','')
 if not re.fullmatch(r'\d+(?:\.\d+){1,4}',version):raise ValueError('Invalid browser version')
 revec=meta['requestedRevectorization']
 if revec not in ['off','on'] or meta.get('traceEnabled') is not False:raise ValueError('Not an untraced timing result')
 if meta.get('requestedJsFlags')!=('--no-wasm-revectorize' if revec=='off' else '--wasm-revectorize'):raise ValueError('Unexpected timing flags')
 shapes={f'residual_c{c}_k{k}' for c in [32,64,128,256] for k in [3,7,11]}|{f'gemm_c{c}' for c in [32,64,128,256,512]}|{'conv_pre'}
 records=[]
 for r in data['results']:
  name=r['name']
  if name not in shapes:raise ValueError('Unexpected kernel shape name')
  row={'shape':name}
  for key in ['ig','mr','nc','kc','ks','reps']:row[key]=integer(r[key],0 if key=='ig' else 1)
  samples={}
  for family in ['splat','loadsplat']:
   values=r['samples'][family]
   if not isinstance(values,list) or len(values)!=10:raise ValueError('Expected10 timing samples per kernel')
   samples[family]=[number(x,True) for x in values]
  ms=statistics.median(samples['splat']);ml=statistics.median(samples['loadsplat'])
  row.update(samples_ms_per_call=samples,splat_median_us=ms*1000,loadsplat_median_us=ml*1000,loadsplat_speedup=ms/ml,reported_upper_median_speedup=number(r['loadsplatSpeedup'],True))
  records.append(row)
 if len(records)!=18 or {r['shape'] for r in records}!=shapes:raise ValueError('Missing/duplicate kernel shapes')
 return {'schema_version':1,'kind':'standalone_browser_kernel_probe','browser_version':version,'revectorization':revec,'worker_hardware_concurrency':integer(meta['hardwareConcurrency'],1,4096),'validation_cases':integer(meta['validationCases'],1),'max_absolute_validation_error':number(meta['maxAbsoluteError']),'median_convention':'mean_of_two_central_order_statistics','records':records}
def privacy_guard(value):
 forbidden={'wav','raw','audio','pcm','waveform','weights','model','tensor','spectrogram','private_files','raw_bytes','waveform_samples','access_token','password','api_key'}
 if isinstance(value,dict):
  for key,item in value.items():
   if str(key).lower() in forbidden:raise ValueError('Private data key is not permitted in numeric log inputs')
   privacy_guard(item)
 elif isinstance(value,list):
  if len(value)>128:raise ValueError('Large arrays are not permitted in numeric log inputs')
  for item in value:privacy_guard(item)
def main():
 p=argparse.ArgumentParser();p.add_argument('--kind',choices=['core-dispatch','kernel'],required=True);p.add_argument('--input',type=pathlib.Path,required=True);a=p.parse_args()
 if a.input.suffix!='.json' or a.input.stat().st_size>2_000_000:raise ValueError('Expected bounded JSON evidence only')
 data=json.loads(a.input.read_text());privacy_guard(data);safe=core_dispatch(data) if a.kind=='core-dispatch' else kernel(data)
 text=json.dumps(safe,sort_keys=True,separators=(',',':'),allow_nan=False)
 # Fixed schemas above intentionally carry no waveform/model paths, raw arrays,
 # profiles, freeform descriptions, source strings, tokens or attachments.
 print('BENCH_NUMERIC_JSON_BEGIN');print(text);print('BENCH_NUMERIC_JSON_END')
if __name__=='__main__':main()
