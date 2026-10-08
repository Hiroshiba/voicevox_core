import pathlib,json,sys
P=pathlib.Path(__file__).parent
mode='browser' if '--browser' in sys.argv else 'node'
results=[json.loads((P/'results'/f'{mode}-condition{i}.json').read_text()) for i in range(3)]
expected=[[m,128,11,d,seed] for seed in [17,731] for d in [1,3,5] for m in [1,2,3,4,5,31,32,33,63,64,65,61568]]+[[65,c,k,1,17] for c,k in [(128,7),(64,11),(256,11)]]
assert len(expected)==75 and len({tuple(c) for c in expected})==75
for condition,data in enumerate(results):
 assert len(data['records'])==75
 assert [r['case'] for r in data['records']]==expected
 for r in data['records']:
  assert r['condition']==condition and len(r['runs'])==3 and len(r['hashes'])==3
  assert [run['input'] for run in r['runs']]==[0,0,1]
actual=[];excluded=0
for a,b,c in zip(*(r['records'] for r in results)):
 assert a['case']==b['case']==c['case'];m,ch,k,d,seed=a['case']
 assert a['hashes']==b['hashes']==c['hashes']
 assert a['packedHash']==b['packedHash']==c['packedHash']
 assert a['hashes'][0]==a['hashes'][1] and a['hashes'][0]!=a['hashes'][2]
 for cond,r in enumerate([a,b,c]):
  s=r['selection'];active=m>=32 and ch==128 and k==11
  assert s['which']==(cond if active else 0)
  assert s['alignment']==0 and s['threads']==2
  assert s['mr']==(1 if m==1 else 4)
  if m>1:assert s['kernel_mr4_linear_loadsplat']==1
  for run in r['runs']:
   assert run['bad']==0
   if m==61568:
    assert run['callbacks']==(15392 if cond==0 else 1924)
    assert run['calls']==(61568 if cond==2 else 15392)
    assert len(set(run['thread_ids']))==2 and all(run['thread_callbacks'])
   if active and cond:
    assert s['tile']==[32,128]
    expected=[[i,n,min(4,m-i),nc] for n,nc in ([(0,128)] if cond==1 else [(0,32),(32,32),(64,32),(96,32)]) for i in range(0,min(32,m),4)]
    assert run['first_trace']==expected
 if m==61568:actual.append(dict(case=a['case'],hashes_input0_repeat_input1=a['hashes'],callbacks=[r['runs'][0]['callbacks'] for r in [a,b,c]],calls=[r['runs'][0]['calls'] for r in [a,b,c]]))
 if ch!=128 or k!=11:excluded+=1
summary=dict(passed=True,scope=('Untimed dedicated browser worker real XNN API, pthreadpool2' if mode=='browser' else 'Untimed Node24 real XNN API, pthreadpool2; browser gate blocked by sandbox Unix sockets'),conditions=3,cases_per_condition=75,runs=675,bitwise_cross_condition_hashes_equal=True,repeat_and_changed_input_address=True,guards_and_per_task_rectangle_checks=True,canonical_grid_ownership=True,packed_weights_equal=True,indirection_mapping_and_persistent_snapshots=True,excluded_neighbor_shapes=excluded,actual_cases=actual,browser_gate_passed=mode=='browser',timing_authorized=False)
(P/'results'/('BROWSER_GATE.json' if mode=='browser' else 'LOCAL_GATE.json')).write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
