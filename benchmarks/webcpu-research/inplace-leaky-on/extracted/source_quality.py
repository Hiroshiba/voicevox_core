"""New untimed CPU-versus-forced-loadsplat comparator; old comparator is unchanged."""
import math,struct
from source_manifest import need,sha

def check_bytes(h,wav,raw):
 metrics=h.waveform_comparison(wav,raw,wav,raw)
 need(metrics['finite'] and metrics['fp32_exact'] and metrics['pcm_exact'],'Invalid private output')
 return metrics

def run_quality(h,open_browser,close_browser,save,report):
 values={};record={'scope':'Untimed after all primary browsers close; original CPU2 versus forced-loadsplat XNN2, no additional primary condition.','modes':{},'passed':False};report['cpu_quality']=record;save()
 try:
  for key in ['cpu','original']:
   browser,page,pid,info=open_browser(key);entry={'initialization':info,'repeats':[]};record['modes'][key]=entry;save()
   try:
    first=None
    for i in range(2):
     response=page.evaluate('d=>request(d)',{'command':'check'});wav=bytes(response['wav']);raw=bytes(response['raw']);row={'index':i,'wav_sha256':sha(wav),'raw_sha256':sha(raw)};entry['repeats'].append(row);save();row['self_metrics']=check_bytes(h,wav,raw)
     if first is None:first=(wav,raw)
     row['repeat_metrics']=h.waveform_comparison(*first,wav,raw);save();need(row['repeat_metrics']['finite'] and row['repeat_metrics']['pcm_exact'] and row['repeat_metrics']['fp32_exact'],'CPU quality repeatability')
    values[key]=first
   finally:entry['cleanup']=close_browser(browser,pid);save();need(entry['cleanup']['confirmed'],'Quality browser cleanup')
  record['forced_loadsplat_vs_cpu']=h.waveform_comparison(*values['cpu'],*values['original']);save();need(record['forced_loadsplat_vs_cpu']['finite'],'Quality nonfinite');record['passed']=True;save()
 finally:values.clear()
