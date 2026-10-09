#!/usr/bin/env python3
"""Sequential untimed browser references/profiling; private waveforms never escape.

Run before or after the research timing processes, never during timing. Uses
provided matching diagnostics or obtains one private untimed diagnostic first.
This file prepares no external publication and is separate from timing code.
"""
import argparse,collections,hashlib,importlib.util,json,pathlib,shutil,sys,tempfile,os
p=argparse.ArgumentParser()
p.add_argument('--harness',type=pathlib.Path,required=True)
p.add_argument('--manifest',type=pathlib.Path,required=True)
p.add_argument('--cpu-binary',type=pathlib.Path,required=True)
p.add_argument('--cpu-receipt',type=pathlib.Path,required=True)
p.add_argument('--activation-evidence',type=pathlib.Path)
p.add_argument('--output',type=pathlib.Path,required=True)
p.add_argument('--browser-path',type=pathlib.Path)
p.add_argument('--audio-query',type=pathlib.Path)
p.add_argument('--target-seconds',type=float,default=10)
p.add_argument('--style-id',type=int,default=302)
p.add_argument('--profile-xnn',action='store_true')
p.add_argument('--revec',choices=['off','on'],default='off')
a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)
enable_revec=a.revec=='on';js_flag='--wasm-revectorize' if enable_revec else '--no-wasm-revectorize'
spec=importlib.util.spec_from_file_location('core_browser_reference_harness',a.harness);h=importlib.util.module_from_spec(spec);sys.modules[spec.name]=h;spec.loader.exec_module(h)
if "data.command==='dispatch'" not in h.BROWSER_WORKER:raise ValueError('Patched actual-CORE Worker dispatch support required')
sha=lambda path:hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
manifest=json.loads(a.manifest.read_text());entry=manifest['variants'][0]
if entry['provider']!='XNNPACK' or not entry['fixed_shape'] or not entry['spin_off'] or entry.get('model_target','vocoder')!='vocoder':raise ValueError('First variant must be the strict-FP32 vocoder XNN control')
if entry.get('revectorize',False)!=enable_revec:raise ValueError('Manifest revectorization must match --revec')
if entry.get('dispatch_expected')!='auto' or entry.get('threads',2)!=2 or entry.get('key') not in ['original','untouched'] or entry.get('build_identity',{}).get('runtime')!='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd':raise ValueError('Pinned untouched XNN2 control required')
xnn=pathlib.Path(entry['binary']);model=pathlib.Path(entry['model'])
if sha(model)!=entry['model_sha256']:raise ValueError('Model hash mismatch')
if entry['model_sha256']!='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9':raise ValueError('Expected pinned original model only')
receipts={}
for label,binary,receipt_path in [('cpu',a.cpu_binary,a.cpu_receipt),('xnn',xnn,xnn.parent/'complete.json')]:
 receipt=json.loads(receipt_path.read_text());receipts[label]=receipt
 if binary.name not in receipt['files'] or binary.with_suffix('.wasm').name not in receipt['files']:raise ValueError(label+' JS/WASM absent from receipt')
 for filename,digest in receipt['files'].items():
  if sha(binary.parent/filename)!=digest:raise ValueError(label+' artifact receipt mismatch')
if receipts['cpu']['identity'].get('xnnpack',False) or receipts['cpu']['identity'].get('kind')!='browser-mt':raise ValueError('Ordinary CPU-MT reference artifact required')
if receipts['xnn']['identity']!=entry['build_identity']:raise ValueError('XNN manifest/build identity mismatch')
expected_engine=None;activation=None
if a.activation_evidence:
 diagnostics=json.loads(a.activation_evidence.read_text());activation=diagnostics[entry['key']]
 if not activation.get(a.revec+'_verified') or activation[a.revec]['flag_rejected'] or not activation[a.revec]['launch_configuration_matches'] or (activation[a.revec]['transformed_groups']>0)!=enable_revec:raise ValueError('Successful matching explicit activation evidence required')
 expected_engine={key:activation[a.revec][key] for key in ['product','js_version']}
query=json.loads(a.audio_query.read_text()) if a.audio_query else h.prepared_query(a.target_seconds)
query_bytes=json.dumps(query,separators=(',',':'),ensure_ascii=False).encode()
report={'schema_version':1,'scope':'sequential untimed actual browser CORE numerical reference and optional vocoder-only profile','engine':expected_engine,'revectorization':a.revec,'requested_v8_flags':['--js-flags='+js_flag],'sequential_browser_processes':True,'inputs':{'harness_sha256':sha(a.harness),'manifest_sha256':sha(a.manifest),'cpu_js_sha256':sha(a.cpu_binary),'cpu_wasm_sha256':sha(a.cpu_binary.with_suffix('.wasm')),'xnn_js_sha256':sha(xnn),'xnn_wasm_sha256':sha(xnn.with_suffix('.wasm')),'model_sha256':sha(model),'query_sha256':hashlib.sha256(query_bytes).hexdigest(),'activation_evidence_sha256':sha(a.activation_evidence) if a.activation_evidence else None},'style_id':a.style_id,'references':{}}
private_root=None;samples={}
try:
 from playwright.sync_api import sync_playwright
 with tempfile.TemporaryDirectory(prefix='voicevox-browser-reference-private-') as temporary:
  work=pathlib.Path(temporary);private_root=work
  (work/'query.json').write_bytes(query_bytes);(work/'index.html').write_text(h.BROWSER_PAGE);(work/'worker.js').write_text(h.BROWSER_WORKER);shutil.copyfile(model,work/'sample.vvm')
  for label,binary in [('cpu',a.cpu_binary),('xnn',xnn)]:
   folder=work/label;folder.mkdir()
   for suffix in ['.js','.wasm']:shutil.copyfile(binary.with_suffix(suffix),folder/('voicevox_benchmark'+suffix))
   os.link(work/'sample.vvm',folder/'sample.vvm')
  options={'headless':True,'args':['--js-flags='+js_flag]}
  if a.browser_path:options['executable_path']=str(a.browser_path)
  with h.serve_assets(work) as url,sync_playwright() as playwright:
   if activation is None:
    mode=h.Mode('xnn','Untimed XNN reference',2,'private diagnostic',experimental=True,fixed_shape=True,spin_off=True,execution_provider='XNNPACK',revectorize=enable_revec)
    activation=h.verify_research_revectorization(entry,mode,options,url,work,work/'activation.json',a.style_id,require_on=enable_revec)
    if not activation.get(a.revec+'_verified'):raise RuntimeError('Matching explicit activation was not verified')
    expected_engine={key:activation[a.revec][key] for key in ['product','js_version']}
   report['engine']=expected_engine
   report['activation']={key:activation[a.revec][key] for key in ['transformed_groups','revectorizable_nodes','flag_rejected','launch_configuration_matches','trace_sha256']}
   def run_case(label,profile=False):
    # Browser is fully closed before the next case starts; only one full heap.
    browser=playwright.chromium.launch(**options);runner=None
    try:
     if h.browser_engine_info(browser)!=expected_engine:raise RuntimeError('Actual browser/V8 differs from matching diagnostic')
     is_xnn=label=='xnn'
     runner=h.BrowserRunner(browser,url,'/'+label+'/voicevox_benchmark.js',2,True,a.style_id,work/(label+('-profile' if profile else '')+'.wav'),fixed_shape=is_xnn,spin_off=is_xnn,xnn_threads=2 if is_xnn else 0,profile=profile,model_url='/sample.vvm',model_target='vocoder')
     info={'threads':2,'configured_ort_global_threads':1 if is_xnn else 2,'configured_xnn_threads':2 if is_xnn else 0,'pthreads_after_init':runner.pthreads_created,'shared_memory':runner.shared_memory,'fixed_length':runner.fixed_length,'fixed_matches':runner.fixed_matches,'xnn_sessions':runner.xnn_sessions}
     if not profile:report['references'][label]=info
     else:report['vocoder_profile']=info
     if is_xnn:info['dispatch']=runner.page.evaluate("data => request(data)",{'command':'dispatch','expected_dispatch':'auto'})['dispatch']
     if is_xnn and not info['dispatch'].get('verified'):raise RuntimeError('Actual CORE dispatch proof failed')
     if profile:
      for index in range(3):runner.synthesize(save=index==0)  # durations intentionally discarded
      private_profile=work/'vocoder-profile.json';profile_info=runner.finish_profile(private_profile)
      events=json.loads(private_profile.read_text());groups=collections.defaultdict(lambda:{'names':set(),'calls':0,'kernel_duration_us':0})
      for event in events:
       if event.get('cat')!='Node' or not event.get('name','').endswith('_kernel_time'):continue
       args=event.get('args',{});provider=args.get('provider','unknown');op=args.get('op_name','unknown');group=groups[(provider,op)];group['names'].add(event['name']);group['calls']+=1;group['kernel_duration_us']+=event.get('dur',0)
      rows=[{'provider':provider,'operator':op,'unique_nodes':len(value['names']),'calls':value['calls'],'kernel_duration_us':value['kernel_duration_us']} for (provider,op),value in sorted(groups.items())]
      counts={row['operator']:row['unique_nodes'] for row in rows if row['provider']=='XnnpackExecutionProvider'}
      info.update(profile_scope='first untouched-XNN control only; placement evidence across 3 cold+warm syntheses; summed kernel durations are not warm performance or latency',syntheses=3,profile_events=len(events),operator_counts=rows,xnn_conv_nodes=counts.get('Conv',0),xnn_convtranspose_nodes=counts.get('ConvTranspose',0),profile_assignment_sha256=profile_info['provider_assignment_sha256'])
      info['all_expected_convolutions_observed']=info['xnn_conv_nodes']==74 and info['xnn_convtranspose_nodes']==4
      report['vocoder_profile']=info
      if not info['all_expected_convolutions_observed']:raise RuntimeError('Expected74 Conv+4 ConvTranspose were not all observed on XNN')
     else:
      first=None
      for index in range(2):
       runner.synthesize(save=True)  # all elapsed times discarded
       current=(runner.wav.read_bytes(),runner.raw_wave())
       if first is None:first=current
       else:
        repeat=h.waveform_comparison(*first,*current);info['repeat_comparison']=repeat
        if not repeat['finite'] or not repeat['fp32_exact'] or not repeat['pcm_exact']:raise RuntimeError('Repeated browser outputs are not exact')
        samples[label]=current
      info['pthreads_after_checks']=runner.thread_state();report['references'][label]=info
    finally:
     if runner:runner.close()
     browser.close()
   run_case('cpu');run_case('xnn')
   report['xnn_vs_cpu']=h.waveform_comparison(*samples['cpu'],*samples['xnn'])
   if not report['xnn_vs_cpu']['finite']:raise RuntimeError('Nonfinite browser reference output')
   samples.clear()
   if a.profile_xnn:run_case('xnn',profile=True)
 report['passed']=True
except BaseException as error:
 report['passed']=False;report['error_type']=type(error).__name__
 raise
finally:
 samples.clear();report['private_temporary_directory_deleted']=private_root is None or not private_root.exists();a.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
