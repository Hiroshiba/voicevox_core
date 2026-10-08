import json,pathlib,math
P=pathlib.Path(__file__).resolve().parent
expected=json.loads((P/'expected.json').read_text())
def validate(report):
 assert report['passed'] and report['screen_requested']
 assert report['schedule']==json.loads((P/'schedule.json').read_text())
 assert len(report['sets'])==3 and len(report['schedule'])==3 and len(report['diagnostic'])==3
 for c,diagnostic in enumerate(report['diagnostic']):
  assert diagnostic['metadata']['condition']==c and len(diagnostic['records'])==3
  assert [r['dilation'] for r in diagnostic['records']]==[1,3,5]
  for record in diagnostic['records']:
   e=expected[str(record['dilation'])];assert record['condition']==c and record['hashes']==[e['output'][0],e['output'][0],e['output'][1]] and record['packedHash']==e['packed']
   assert record['selection']['which']==c and record['selection']['threads']==2 and record['selection']['kernel_mr4_linear_loadsplat']==1
   assert len(record['runs'])==3
   for run in record['runs']:assert run['bad']==0 and run['callbacks']==(15392 if c==0 else 1924) and run['calls']==(61568 if c==2 else 15392) and len(set(run['thread_ids']))==2 and all(run['thread_callbacks'])
 for s,trial in enumerate(report['sets']):
  assert trial['engine']==report['diagnosticEngine']
  assert trial['set']==s and len(trial['metadata'])==3 and len(trial['pre'])==3 and len(trial['post'])==3 and len(trial['warmups'])==27 and len(trial['rounds'])==3
  for c,m in enumerate(trial['metadata']):assert m['condition']==c and m['heapBytes']==1073741824 and m['shared'] and m['isolated'] and m['hardwareConcurrency']==report['diagnostic'][0]['metadata']['hardwareConcurrency']
  for phase in ['pre','post']:
   for c,rows in enumerate(trial[phase]):
    assert [r['dilation'] for r in rows]==[1,3,5]
    for r in rows:assert r['selection'] and r['hashes']==expected[str(r['dilation'])]['output'] and r['packedHash']==expected[str(r['dilation'])]['packed']
  assert sorted((r['condition'],r['dilation'],r['iteration']) for r in trial['warmups'])==[(c,d,i) for c in range(3) for d in [1,3,5] for i in range(3)]
  assert 2<=len(trial['idleReadiness'])<=10 and all(w['cpuSeconds']<=0.10 and not w['newProcesses'] and not w['exitedProcesses'] for w in trial['idleReadiness'][-2:])
  last_end=0
  for r,round_record in enumerate(trial['rounds']):
   assert 'resourceExposure' in round_record
   for phase in ['before','after']:assert set(round_record[phase])>={'processes','hostAvailableBytes','hostSwapUsedBytes','pressure','cgroupCpuStat'}
   assert round_record['round']==r and len(round_record['calls'])==9
   assert [(x['condition'],x['dilation']) for x in round_record['calls']]==[(x['condition'],x['dilation']) for x in report['schedule'][s][r]]
   for call in round_record['calls']:
    assert call['startEpochMs']>=last_end and call['endEpochMs']>call['startEpochMs'];last_end=call['endEpochMs']
    assert call['heapBytes']==1073741824 and math.isfinite(call['latencyMs']) and call['latencyMs']>0
    assert abs(call['latencyMs']-(call['endEpochMs']-call['startEpochMs']))<0.01
  for d in [1,3,5]:
   orders=[[c['condition'] for c in r['calls'] if c['dilation']==d] for r in trial['rounds']]
   for position in range(3):assert sorted(order[position] for order in orders)==[0,1,2]
 for d in [1,3,5]:
  orders=[[c['condition'] for c in r['calls'] if c['dilation']==d] for t in report['sets'] for r in t['rounds']]
  for a,b in [(0,1),(0,2),(1,2)]:assert sum(order.index(a)<order.index(b) for order in orders) in [4,5]
 for t in report['sets']:
  for w in t['warmups']:assert math.isfinite(w['latencyMs']) and w['latencyMs']>0 and w['heapBytes']==1073741824
 assert len(report['paired_geomean_ratios_per_set_and_dilation'])==9
 return dict(passed=True,primary_calls=81,retained_warmups=81,independent_fresh_process_sets=3,phase='reshape+setup+run',pre_post_exactness=True,diagnostic_dispatch_and_thread_counts=True,schedule_balanced=True,no_trials_removed=True,scope='Preliminary operator-only screen; no E2E conclusion')
if __name__=='__main__':
 result=validate(json.loads((P/'results/screen.json').read_text()));(P/'results/SCREEN_GATE.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
