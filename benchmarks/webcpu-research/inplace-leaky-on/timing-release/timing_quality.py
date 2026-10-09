"""Predeclared whole-run quality, separate from structural/equality validity."""
import math
POLICY={'version':'on-quality-v1','minimum_available_bytes':1073741824,'sentinel_ratio_inclusive':[0.90,1.10],'stable_process_identity':True,'full_resource_coverage':True,'counter_regression_allowed':False,'swap_growth_allowed':False,'major_fault_policy':'Retain all primary observations; secondary sensitivity excludes the complete paired round from all comparisons if any of the three conditions has a major fault.'}

def evaluate_quality(r):
    issues=[]
    def bad(where,kind):issues.append({'where':where,'reason':kind})
    def processes(before,after,where):
        required={'pid','created','rss','cpu','major_faults','minor_faults','swap_bytes'}
        for side,rows in [('before',before),('after',after)]:
            if not rows:bad(where,'empty_process_coverage')
            for x in rows:
                if not required<=set(x) or any(x.get(k) is None for k in required):bad(where,'incomplete_process_resource_coverage')
                c=x.get('cpu',{})
                if not isinstance(c,dict) or not {'user','system'}<=set(c):bad(where,'incomplete_cpu_coverage')
        old={(x.get('pid'),x.get('created')):x for x in before};new={(x.get('pid'),x.get('created')):x for x in after}
        if len(old)!=len(before) or len(new)!=len(after) or set(old)!=set(new):bad(where,'unstable_process_identity')
        for key in old.keys() & new.keys():
            a,b=old[key],new[key]
            for k in ['major_faults','minor_faults']:
                if a.get(k) is not None and b.get(k) is not None and b[k]<a[k]:bad(where,'process_counter_regression_'+k)
            ac,bc=a.get('cpu',{}),b.get('cpu',{})
            if set(ac)!=set(bc):bad(where,'cpu_counter_coverage_changed')
            for k in set(ac)&set(bc):
                if bc[k]<ac[k]:bad(where,'cpu_counter_regression_'+k)
            if a.get('swap_bytes') is not None and b.get('swap_bytes') is not None and b['swap_bytes']>a['swap_bytes']:bad(where,'target_swap_growth')
    def hosts(a,b,where):
        required={'available_bytes','total_bytes','swap_used','swap_in','swap_out'}
        if not required<=set(a) or not required<=set(b):bad(where,'incomplete_host_resource_coverage')
        if min(a.get('available_bytes',0),b.get('available_bytes',0))<POLICY['minimum_available_bytes']:bad(where,'memory_below_1GiB')
        for k in ['swap_in','swap_out']:
            if k in a and k in b:
                if b[k]<a[k]:bad(where,'host_counter_regression_'+k)
                if b[k]>a[k]:bad(where,'host_swap_activity_'+k)
        if b.get('swap_used',0)>a.get('swap_used',0):bad(where,'host_swap_growth')
    for n,x in enumerate(r['trials']):
        where='trial_'+str(n+1);hosts(x['before']['host'],x['after']['host'],where);processes(x['before']['processes'],x['after']['processes'],where)
        if x['resource_observations']['unmatched_or_unknown_processes']:bad(where,'unmatched_or_unknown_processes')
    if 'initial_host' in r:
        hosts(r['initial_host'],r['initial_host'],'initial_host')
    previous_host=r.get('initial_host')
    for n,x in enumerate(r['trials']):
        if previous_host is not None:hosts(previous_host,x['before']['host'],'between_host_trials_'+str(n+1))
        previous_host=x['after']['host']
    previous={}
    for g in r['process_sets']:
        for mode,x in g['idle']['modes'].items():previous[(g['id'],mode)]=x['after']
    for n,x in enumerate(r['trials']):
        key=(x['process_set'],x['mode'])
        if key in previous:processes(previous[key],x['before']['processes'],'between_trials_'+str(n+1))
        previous[key]=x['after']['processes']
    ratios=[]
    for g in r['process_sets']:
        q=g['sentinels'][1]['elapsed_s']/g['sentinels'][0]['elapsed_s'];ratios.append(q)
        if not (0.90<=q<=1.10):bad('set_'+str(g['id']),'sentinel_drift')
        for mode,x in g['idle']['modes'].items():
            processes(x['before'],x['after'],'idle_'+str(g['id'])+'_'+mode)
            if x['unmatched_processes']:bad('idle_'+str(g['id'])+'_'+mode,'unmatched_idle_processes')
    return {'policy':POLICY,'passed':not issues,'status':'valid' if not issues else 'inconclusive_invalid_quality','issues':issues,'sentinel_ratios':ratios,'all_primary_observations_retained':True,'performance_claim_allowed':not issues}
