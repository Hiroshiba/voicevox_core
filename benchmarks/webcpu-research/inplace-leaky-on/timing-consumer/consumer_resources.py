"""Fail-closed Linux process-tree collection and consumer-only coverage quality."""
from __future__ import annotations
import math
from pathlib import Path

CPU_FIELDS = {'user', 'system', 'children_user', 'children_system', 'iowait'}
PROCESS_FIELDS = {'pid', 'created', 'rss', 'cpu', 'major_faults', 'minor_faults', 'swap_bytes'}
POLICY = {'version': 'exact-code-process-coverage-v1',
          'enumeration': 'all_proc_task_children_before_and_after_resource_reads',
          'collection_errors': 'abort_without_skipping_processes',
          'root_binding': 'exactly_one_matching_browser_pid_and_creation_time_in_every_snapshot',
          'generation_continuity': 'idle_before_after_and_all_calls_through_final_sentinel_and_output_checks',
          'cpu_counters': sorted(CPU_FIELDS), 'nine_distinct_browser_generations': True,
          'scope': 'Boundary observations; not continuous detection of processes born and exited between observations.'}

def need(value, message):
    if not value:
        raise ValueError(message)

def stat_fields(path, pid):
    raw = path.read_text()
    need(raw.split(' ', 1)[0] == str(pid), 'Process stat PID mismatch')
    fields = raw.rsplit(')', 1)[1].split()
    need(len(fields) >= 10, 'Incomplete process stat')
    return fields

def task_children(proc_root, pid):
    """Read every thread's children file; never use psutil's best-effort children()."""
    task_root = proc_root / str(pid) / 'task'
    tasks = sorted(task_root.iterdir(), key=lambda p: p.name)
    need(tasks and all(p.name.isdecimal() and p.is_dir() for p in tasks), 'Incomplete task enumeration')
    children = []
    for task in tasks:
        values = (task / 'children').read_text().split()
        need(all(x.isdecimal() and int(x) > 0 for x in values), 'Invalid child enumeration')
        children.extend(int(x) for x in values)
    need([p.name for p in tasks] == sorted(p.name for p in task_root.iterdir()), 'Threads changed during enumeration')
    need(len(children) == len(set(children)), 'Duplicate child enumeration')
    return sorted(children)

def enumerate_tree(pid, created, process_factory, proc_root):
    found = {}
    pending = [(pid, None)]
    while pending:
        current, parent = pending.pop()
        need(current not in found, 'Duplicate/cyclic process tree')
        process = process_factory(current)
        birth = process.create_time()
        need(type(current) is int and current > 0 and type(birth) in (int, float) and math.isfinite(birth), 'Invalid process generation')
        if current == pid:
            need(birth == created, 'Browser process generation changed')
        stat = stat_fields(proc_root / str(current) / 'stat', current)
        if parent is not None:
            need(int(stat[1]) == parent, 'Child was reparented during enumeration')
        found[current] = birth
        pending.extend((child, current) for child in task_children(proc_root, current))
        need(process_factory(current).create_time() == birth, 'Process generation changed during enumeration')
    return found

def collect(pid, created, *, process_factory=None, proc_root=Path('/proc')):
    """Any missing/read-denied/exited process or changed tree aborts this run."""
    if process_factory is None:
        import psutil
        process_factory = psutil.Process
    proc_root = Path(proc_root)
    before = enumerate_tree(pid, created, process_factory, proc_root)
    rows = []
    for current, birth in sorted(before.items()):
        process = process_factory(current)
        need(process.create_time() == birth, 'Process generation changed before resource read')
        stat = stat_fields(proc_root / str(current) / 'stat', current)
        status = (proc_root / str(current) / 'status').read_text().splitlines()
        swap = [line.split() for line in status if line.startswith('VmSwap:')]
        need(len(swap) == 1 and len(swap[0]) == 3 and swap[0][2] == 'kB', 'Missing process swap coverage')
        cpu = process.cpu_times()._asdict()
        need(set(cpu) == CPU_FIELDS, 'Incomplete Linux CPU counter coverage')
        row = {'pid': current, 'created': birth, 'rss': process.memory_info().rss, 'cpu': cpu,
               'major_faults': int(stat[9]), 'minor_faults': int(stat[7]), 'swap_bytes': int(swap[0][1]) * 1024}
        need(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for k, v in row.items() if k != 'cpu'), 'Invalid process resource counter')
        need(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in cpu.values()), 'Invalid CPU resource counter')
        need(process_factory(current).create_time() == birth, 'Process generation changed during resource read')
        rows.append(row)
    after = enumerate_tree(pid, created, process_factory, proc_root)
    need(before == after, 'Process tree changed during resource collection')
    return rows

def evaluate_coverage(report):
    issues = []
    roots = []
    owners = {}
    def bad(where, reason):
        issues.append({'where': where, 'reason': reason})
    def snapshot(rows, root, where):
        identities = []
        for row in rows:
            identity = (row.get('pid'), row.get('created'))
            identities.append(identity)
            if set(row) != PROCESS_FIELDS or any(row.get(k) is None for k in PROCESS_FIELDS):
                bad(where, 'incomplete_process_resource_coverage')
            if set(row.get('cpu', {})) != CPU_FIELDS:
                bad(where, 'incomplete_linux_cpu_counter_coverage')
        if len(identities) != len(set(identities)):
            bad(where, 'duplicate_process_identity')
        if identities.count(root) != 1:
            bad(where, 'browser_root_missing_or_generation_mismatch')
        return {identity: row for identity, row in zip(identities, rows)}
    def continuity(before, after, where):
        if set(before) != set(after):
            bad(where, 'process_generation_coverage_changed')
        for identity in before.keys() & after.keys():
            a, b = before[identity], after[identity]
            for counter in ['major_faults', 'minor_faults']:
                if a.get(counter) is not None and b.get(counter) is not None and b[counter] < a[counter]:
                    bad(where, 'process_counter_regression_' + counter)
            if a.get('swap_bytes') is not None and b.get('swap_bytes') is not None and b['swap_bytes'] > a['swap_bytes']:
                bad(where, 'target_swap_growth')
            for counter in set(a.get('cpu', {})) & set(b.get('cpu', {})):
                if b['cpu'][counter] < a['cpu'][counter]:
                    bad(where, 'cpu_counter_regression_' + counter)
    for group in report['process_sets']:
        seen = set()
        final = group.get('coverage_final', {})
        if set(final) != set(group['runtime']):
            bad('set_' + str(group['id']), 'missing_final_process_coverage')
        for mode, runtime in group['runtime'].items():
            root = (runtime['browser_pid'], runtime['browser_created']); roots.append(root)
            label = f'set_{group["id"]}_{mode}'
            idle = group['idle']['modes'][mode]
            sequence = [('idle_before', idle['before']), ('idle_after', idle['after'])]
            for row in report['trials']:
                if (row['process_set'], row['mode']) == (group['id'], mode):
                    sequence.extend([(f'pair_{row["pair"]}_before', row['before']['processes']), (f'pair_{row["pair"]}_after', row['after']['processes'])])
            if mode in final:
                sequence.append(('after_final_sentinel_and_checks', final[mode]['processes']))
            previous = None
            for phase, rows in sequence:
                where = label + '_' + phase
                current = snapshot(rows, root, where)
                for identity in current:
                    owner = (group['id'], mode)
                    if identity in owners and owners[identity] != owner:
                        bad(where, 'process_generation_shared_between_browser_generations')
                    owners[identity] = owner
                if previous is not None:
                    continuity(previous, current, where)
                previous = current
            generation = set(snapshot(idle['before'], root, label + '_ownership'))
            if seen & generation:
                bad(label, 'process_generation_shared_between_conditions')
            seen |= generation
    if len(roots) != 9 or len(set(roots)) != 9:
        bad('whole_run', 'nine_distinct_browser_generations_required')
    # Cover the end sentinel/check period, beyond the last measured-call boundary.
    for group in report['process_sets']:
        rows = [x for x in report['trials'] if x['process_set'] == group['id']]
        if not rows:
            continue
        previous = rows[-1]['after']['host']
        for mode in group['runtime']:
            end = group.get('coverage_final', {}).get(mode)
            if end is None:
                continue
            host = end['host']; where = f'set_{group["id"]}_{mode}_final_host'
            required = {'available_bytes', 'total_bytes', 'swap_used', 'swap_in', 'swap_out'}
            if not required <= set(host):
                bad(where, 'incomplete_host_resource_coverage')
            if host.get('available_bytes', 0) < 1024 ** 3:
                bad(where, 'memory_below_1GiB')
            for key in ['swap_in', 'swap_out']:
                if key in previous and key in host and previous[key] != host[key]:
                    bad(where, 'host_swap_counter_changed_' + key)
            if host.get('swap_used', 0) > previous.get('swap_used', 0):
                bad(where, 'host_swap_growth')
            previous = host
    return {'policy': POLICY, 'passed': not issues, 'issues': issues}

def self_test():
    """Check this host supports strict collection before any browser measurement."""
    import os, psutil, select, subprocess, sys
    child = subprocess.Popen([sys.executable, '-c', "import sys; print('ready', flush=True); sys.stdin.read()"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        readable, _, _ = select.select([child.stdout], [], [], 10)
        need(readable, 'Collector self-test child readiness timed out')
        need(child.stdout.readline().strip() == 'ready', 'Collector self-test child not ready')
        pid = os.getpid()
        rows = collect(pid, psutil.Process(pid).create_time())
        need({pid, child.pid} <= {x['pid'] for x in rows}, 'Collector self-test missed root/child')
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()
        child.stdout.close()
    print('SOURCE_ON_STRICT_PROCESS_COLLECTOR_SELF_TEST_PASSED', flush=True)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true', required=True)
    parser.parse_args()
    try:
        self_test()
    except FileNotFoundError:
        raise SystemExit('strict_process_collection_host_check_failed: interface_or_process_missing') from None
    except PermissionError:
        raise SystemExit('strict_process_collection_host_check_failed: access_denied') from None
    except ValueError:
        raise SystemExit('strict_process_collection_host_check_failed: tree_generation_or_resource_integrity') from None
    except OSError:
        raise SystemExit('strict_process_collection_host_check_failed: operating_system_read_error') from None
    except Exception:
        raise SystemExit('strict_process_collection_host_check_failed: collection_error') from None
