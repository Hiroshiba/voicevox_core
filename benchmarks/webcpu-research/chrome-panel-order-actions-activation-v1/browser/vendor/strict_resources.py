"""Exact pure enumeration functions copied from the audited ON consumer.

No browser, operator, subprocess, or self-test is invoked by this module.
Only /proc task-child enumeration and generation binding are reused here.
"""
from __future__ import annotations

# Activation uses only the shared immutable root/child admission contract.
from launch.source_only import require_execution_admission, child_python_command
require_execution_admission('browser.vendor.strict_resources')
import math
from pathlib import Path

AUDITED_SOURCE_SHA256 = '3b559b1dec28cf9234e8e55035c7a5eac167853122bd31fc7b16b5ef17f28e5b'

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

