#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]
# ///
"""Normal-tier OFF correctness only. Private outputs stay in memory, never artifacts."""
import argparse,hashlib,importlib.util,json,os,shutil,struct,sys,tempfile
from pathlib import Path
from prepare_inplace_leaky import prepare,require,sha
ROOT=Path(__file__).parent
HARNESS_SHA='d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe'
MODEL_SHA='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9'
def load(path):
 spec=importlib.util.spec_from_file_location('inplace_gate_harness',path);h=importlib.util.module_from_spec(spec);sys.modules[spec.name]=h;spec.loader.exec_module(h);return h
def pcm(wav):
 require(wav[:4]==b'RIFF' and wav[8:12]==b'WAVE','WAV header');p=12
 while p+8<=len(wav):
  tag=wav[p:p+4];n=int.from_bytes(wav[p+4:p+8],'little');require(p+8+n<=len(wav),'WAV length')
  if tag==b'data':return wav[p+8:p+8+n]
  p+=8+n+(n&1)
 raise ValueError('Missing PCM')
def check_alias(x,variant,pin):
 require(x['variant']==variant and x['table_slot']==pin['callback_table']['slot'] and x['function_index']==pin['absolute_function_index'],'Alias identity')
 require(x['restored'] and x['metadata_errors']==0,'Hook restoration/metadata');require(x['callbacks']==69 and x['elements']==435901440,'Callback coverage')
 require(x['classes']=={'exact_inplace':{'calls':41,'elements':256615424},'disjoint':{'calls':28,'elements':179286016}},'Alias topology')
 require(x['length_histogram']=={'492544':1,'1970176':17,'7880704':51},'Actual lengths')
 require(x['predicted_branch']==({'simd':{'calls':69,'elements':435901440},'scalar':{'calls':0,'elements':0}} if variant=='candidate' else {'simd':{'calls':28,'elements':179286016},'scalar':{'calls':41,'elements':256615424}}),'Guard classification')
def main():
 p=argparse.ArgumentParser();p.add_argument('--selection',type=Path,required=True);p.add_argument('--harness',type=Path,required=True);p.add_argument('--dispatch-helper',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();s=json.loads(a.selection.read_text());require(sha(a.harness.read_bytes())==HARNESS_SHA,'Harness pin');require(sha(a.dispatch_helper.read_bytes())=='abe7453d3feeaf6bc7f489452055a8ba0d02d22c7c158481112c51e0ada10c2b','Dispatch helper pin');h=load(a.harness);model=Path(s['model']);require(sha(model.read_bytes())==MODEL_SHA,'Model pin');binary=Path(s['binary']);report={'schema':1,'scope':'Untimed normal-tier explicit-OFF browser correctness; no timing claim','normal_tiering':True,'requested_browser_flags':['--js-flags=--no-wasm-revectorize'],'session_id':os.environ.get('CODEX_SESSION_ID'),'variants':{},'passed':False};first=None;private=None
 try:
  from playwright.sync_api import sync_playwright
  with tempfile.TemporaryDirectory(prefix='inplace-leaky-private-') as temp:
   work=Path(temp);private=work;proof=prepare(binary,Path(s['archive']),work/'patched');report['transform']={k:v for k,v in proof.items() if k not in ['source_binary','native_bundle_proof','build_identity']}
   (work/'transform.json').write_text(json.dumps(proof));shutil.copyfile(model,work/'sample.vvm');(work/'query.json').write_text(json.dumps(h.prepared_query(10),separators=(',',':'),ensure_ascii=False));(work/'index.html').write_text(h.BROWSER_PAGE);(work/'worker.js').write_text(h.BROWSER_WORKER)
   for variant in ['original','candidate']:
    folder=work/variant;folder.mkdir();shutil.copyfile(binary,folder/'voicevox_benchmark.js');shutil.copyfile(binary.with_suffix('.wasm') if variant=='original' else work/'patched/candidate.wasm',folder/'voicevox_benchmark.wasm');os.link(work/'sample.vvm',folder/'sample.vvm')
   shutil.copyfile(ROOT/'core_alias_check.js',work/'core_alias_check.js');shutil.copyfile(a.dispatch_helper,work/'core_dispatch_check.js');options={'headless':True,'args':report['requested_browser_flags']}
   with h.serve_assets(work) as url,sync_playwright() as pw:
    mode=h.Mode('original','Untimed inplace guard control',2,'correctness only',experimental=True,fixed_shape=True,spin_off=True,execution_provider='XNNPACK',revectorize=False)
    activation=h.verify_research_revectorization({'model_target':'vocoder'},mode,options,url,work,work/'private-activation.json',302,require_on=False);require(activation.get('off_verified'),'Explicit OFF diagnostic');expected={k:activation['off'][k] for k in ['product','js_version']}
    report['activation']={k:activation['off'][k] for k in ['product','js_version','transformed_groups','revectorizable_nodes','flag_rejected','launch_configuration_matches','trace_sha256']};(work/'worker.js').write_bytes((ROOT/'browser_worker.js').read_bytes())
    for variant in ['original','candidate']:
     browser=pw.chromium.launch(**options)
     try:
      require(h.browser_engine_info(browser)==expected,'Engine differs from OFF diagnostic');page=browser.new_page();page.goto(url);info=page.evaluate('d=>request(d)',{'command':'init','variant':variant});require(info['ready'] and info['callback_identity_checked'],'Runtime ready');entry={'initialization':info,'checks':[]};report['variants'][variant]=entry
      for i in range(3):
       result=page.evaluate('d=>request(d)',{'command':'check'});wav=bytes(result.pop('wav'));raw=bytes(result.pop('raw'))
       observation={'index':i,'wav_bytes':len(wav),'raw_bytes':len(raw),'wav_sha256':sha(wav),'raw_sha256':sha(raw),'exact_bytes_to_first':False,'alias':result['alias']};entry['checks'].append(observation)
       # Hashes/alias metadata are checkpointed before any numerical or coverage assertion.
       a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
       pcmbytes=pcm(wav);observation['pcm_sha256']=sha(pcmbytes)
       if first is None:first=(wav,raw,pcmbytes)
       observation['exact_bytes_to_first']=(wav,raw,pcmbytes)==first
       a.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
       require(len(raw)%4==0 and all(__import__('math').isfinite(v[0]) for v in struct.iter_unpack('<f',raw)),'Nonfinite raw')
       for alias in result['alias']:check_alias(alias,variant,proof)
       require(observation['exact_bytes_to_first'],'Raw FP32/PCM/WAV bytes differ')
       del wav,raw,pcmbytes,result
     finally:browser.close()
   first=None;report['passed']=True
 finally:
  first=None;report['private_temporary_directory_deleted']=private is None or not private.exists();a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
if __name__=='__main__':main()
