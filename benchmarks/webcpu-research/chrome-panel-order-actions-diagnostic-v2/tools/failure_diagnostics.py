"""Pure closed failure metadata. Never serialize exception text, argv or paths."""
import json
from pathlib import Path
POLICY=json.loads((Path(__file__).resolve().parents[1]/'protocol/failure-diagnostic-policy.json').read_text())
FIELDS={'schema','stage','operation','operation_id','error_class','check','http_status','errno'}

def diagnose(error, stage, operation='unknown', operation_id=None):
    name=type(error).__name__
    args=getattr(error,'args',())
    message=args[0] if args and type(args[0]) is str else ''
    check=message.split(':',1)[0]
    status=getattr(error,'code',None) if name=='HTTPError' else None
    number=getattr(error,'errno',None)
    return {'schema':'panel-failure-diagnostic-v1',
        'stage':stage if stage in POLICY['stages'] else 'unknown',
        'operation':operation if operation in POLICY['operations'] else 'unknown',
        'operation_id':operation_id if operation_id in POLICY['operation_ids'] else None,
        'error_class':name if name in POLICY['error_classes'] else 'OtherException',
        'check':check if check in POLICY['checks'] else 'unclassified_exception',
        'http_status':status if type(status) is int and 100<=status<=599 else None,
        'errno':number if type(number) is int and 0<number<=4095 else None}

def validate(value):
    if type(value) is not dict or set(value)!=FIELDS:raise ValueError('invalid_failure_diagnostic')
    if value['schema']!='panel-failure-diagnostic-v1':raise ValueError('invalid_failure_diagnostic')
    for key,roster in (('stage','stages'),('operation','operations'),('error_class','error_classes'),('check','checks')):
        if type(value[key]) is not str or value[key] not in POLICY[roster]:raise ValueError('invalid_failure_diagnostic')
    if value['operation_id'] is not None and value['operation_id'] not in POLICY['operation_ids']:raise ValueError('invalid_failure_diagnostic')
    for key,minimum,maximum in (('http_status',100,599),('errno',1,4095)):
        if value[key] is not None and (type(value[key]) is not int or not minimum<=value[key]<=maximum):raise ValueError('invalid_failure_diagnostic')
    return dict(value)

def known_records(rows):
    if type(rows) is not list or len(rows)>POLICY['max_known_records']:raise ValueError('invalid_failure_diagnostic')
    result=[]
    for row in rows:
        if type(row) is not dict:raise ValueError('invalid_failure_diagnostic')
        token=row.get('id',row.get('command_id'));kind=row.get('kind','owned_command' if 'command_id' in row else 'recorded')
        record={'id':token if token in POLICY['operation_ids'] else None,
                'kind':kind if kind in POLICY['known_record_kinds'] else 'recorded'}
        for key in ('attempted','acknowledged','complete'):
            value=row.get(key);record[key]=value if type(value) is bool else None
        code=row.get('returncode');record['returncode']=code if type(code) is int and -255<=code<=255 else None
        result.append(record)
    return result

def emit(stage, child=None, outer=None, commands=None, operations=None):
    """One short parent-log record; unknown/malformed data cannot be echoed."""
    value={'schema':'panel-parent-failure-log-v1','stage':stage if stage in POLICY['stages'] else 'unknown',
           'child':validate(child) if child is not None else None,
           'outer':validate(outer) if outer is not None else None,
           'known_commands':known_records(commands or []),'known_operations':known_records(operations or [])}
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    if len(raw.encode())>POLICY['max_log_bytes']:raise ValueError('invalid_failure_diagnostic')
    print('PANEL_FAILURE_DIAGNOSTIC '+raw,flush=True)
    return value


def phase_failure(stage, error, journal):
    diagnostic=validate(getattr(error,'panel_failure_diagnostic',diagnose(error,stage,'phase_receipt')))
    return {'schema':'panel-phase-failure-v1','stage':stage,'status':'failed','complete':False,
            'first_failure':{'type':diagnostic['error_class'],'check':diagnostic['check']},
            'failure_diagnostic':diagnostic,'known_commands':journal,
            'known_operations':known_records(getattr(error,'panel_known_operations',[])),
            'operator_calls':None,'browser_launches':None}
