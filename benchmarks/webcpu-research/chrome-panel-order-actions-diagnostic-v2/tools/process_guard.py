#!/usr/bin/env python3
"""Disposable Linux subreaper ownership and independently enforced deadlines.

No browser/model work occurs on import. A fresh supervisor owns exactly one
command and every descendant, including setsid/double-fork orphans. pidfds bind
signals to captured generations. Unknown enumeration is never zero survivors.
Control/receipt traffic uses a private socket, separate from command stdio.
"""
from __future__ import annotations

# Activation uses only the shared immutable root/child admission contract.
from launch.source_only import require_execution_admission, child_python_command
require_execution_admission('tools.process_guard')
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time

POLL = .02
OWNERS = set()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def valid_deadline(value):
    require(value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0,
            'Invalid owned process deadline')
    return value


def become_subreaper():
    require(sys.platform.startswith('linux'), 'Owned supervision requires Linux')
    require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'Generation-safe pidfds required')
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, 'Cannot enable child subreaper')
    value = ctypes.c_int()
    require(libc.prctl(37, ctypes.byref(value), 0, 0, 0) == 0 and value.value == 1,
            'Subreaper not verified')


def _children(pid, proc_root=Path('/proc')):
    """Strict owned-only traversal of every thread's kernel child list.

    Unsupported/missing/denied kernel enumeration remains an ownership failure.
    There is deliberately no global /proc scan or production best-effort fallback.
    """
    root = proc_root / str(pid) / 'task'
    tasks = sorted(root.iterdir())
    require(bool(tasks), 'Unknown process threads')
    children = []
    for task in tasks:
        require(task.name.isdecimal() and task.is_dir(), 'Unknown process thread')
        words = (task / 'children').read_text().split()
        require(all(word.isdecimal() and int(word) > 0 for word in words), 'Unknown process children')
        children.extend(map(int, words))
    require(tasks == sorted(root.iterdir()) and len(children) == len(set(children)), 'Changing process threads')
    return sorted(children)


class OwnedJob:
    """Used only inside a fresh verified subreaper, never the user's process."""
    def __init__(self):
        import psutil
        self.ps = psutil
        self.pid = os.getpid()
        self.known = {}  # (pid, birth time): pidfd, never a reusable PID signal
        self.child = None
        self.last_error = None

    def _ended(self, fd):
        return bool(select.select([fd], [], [], 0)[0])

    def _capture_one(self, pid, expected_parent):
        proc = self.ps.Process(pid)
        birth = proc.create_time()
        require(proc.ppid() == expected_parent, 'Descendant moved during capture')
        key = (pid, birth)
        if key not in self.known:
            fd = os.pidfd_open(pid)
            try:
                # Binding both sides of pidfd_open prevents capturing a reused ID.
                check = self.ps.Process(pid)
                require(check.create_time() == birth and check.ppid() == expected_parent,
                        'Process generation changed during capture')
                self.known[key] = fd
            except BaseException:
                os.close(fd)
                raise
        return key

    def capture(self):
        pending = [(self.pid, None)]
        seen = set()
        try:
            while pending:
                pid, key = pending.pop()
                require(pid not in seen, 'Duplicate owned process')
                seen.add(pid)
                try:
                    children = _children(pid)
                except (FileNotFoundError, self.ps.NoSuchProcess):
                    # A captured parent may have exited and reparented children
                    # after the supervisor's earlier child-list read. This pass
                    # is incomplete even when its pidfd proves that parent ended.
                    raise
                for child in children:
                    try:
                        child_key = self._capture_one(child, pid)
                        pending.append((child, child_key))
                    except (ProcessLookupError, FileNotFoundError, self.ps.NoSuchProcess):
                        # Vanishing before pidfd capture can hide newly adopted
                        # descendants. Keep this pass unknown; a later complete
                        # traversal can establish their actual ownership.
                        raise
            self.last_error = None
            return True
        except Exception as error:
            self.last_error = type(error).__name__
            return False

    def living(self):
        return [(key, fd) for key, fd in self.known.items() if not self._ended(fd)]

    def reap(self):
        # Obtain the command's exact return code before reaping adopted children.
        if self.child is not None and self.child.poll() is None:
            return
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    return
            except ChildProcessError:
                return

    def signal_all(self, sig):
        # Always discover again: terminating parents may adopt new generations.
        good = self.capture()
        for _, fd in reversed(self.living()):
            try:
                signal.pidfd_send_signal(fd, sig)
            except ProcessLookupError:
                pass
            except (OSError, PermissionError):
                good = False
        return good

    def clean(self, deadline=None):
        deadline = time.monotonic() + 5 if deadline is None else deadline
        term_end = min(deadline, time.monotonic() + .5)
        while time.monotonic() < deadline:
            self.signal_all(signal.SIGTERM if time.monotonic() < term_end else signal.SIGKILL)
            self.reap()
            good = self.capture()
            living = self.living()
            if good and not living:
                self.reap()
                # Two independent empty direct-child reads plus exited pidfds
                # prove there is no remaining descendant able to create children.
                try:
                    if not _children(self.pid) and not _children(self.pid):
                        return {'confirmed': True, 'remaining_processes': 0}
                except Exception:
                    pass
            time.sleep(POLL)
        self.reap()
        good = self.capture()
        living = self.living()
        return {'confirmed': False, 'remaining_processes': len(living) if good else None}

    def release(self):
        for fd in self.known.values():
            os.close(fd)
        self.known.clear()


def _send(sock, value):
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    require(len(data) <= 65536, 'Oversized ownership control message')
    sock.sendall(data)


def _supervise(command, cwd, control_fd, deadline, cleanup_deadline, cpu_limit=None):
    if cpu_limit is not None:
        require(type(cpu_limit) is int and 0 < cpu_limit <= 86400, 'Invalid per-process CPU limit')
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        require(resource.getrlimit(resource.RLIMIT_CPU) == (cpu_limit, cpu_limit), 'CPU limit not established')
    control = socket.socket(fileno=control_fd)
    job = None
    interrupted = False
    reason = None
    ceiling = deadline
    def interrupt(*_):
        nonlocal interrupted
        interrupted = True
    try:
        become_subreaper()
        job = OwnedJob()
        require(not _children(job.pid), 'Supervisor already owns another child')
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, interrupt)
        require(deadline is None or time.monotonic() < deadline, 'Deadline reached before launch')
        job.child = subprocess.Popen(command, cwd=cwd, close_fds=True)
        if not job.capture():
            reason = 'ownership_unknown'
            raise ValueError('Owned subtree startup enumeration failed')
        _send(control, {'event': 'started', 'identity': [job.pid, job.ps.Process(job.pid).create_time()],
                        'owned_enumeration_verified': True})
        while job.child.poll() is None:
            if interrupted:
                reason = 'interrupted'; break
            if deadline is not None and time.monotonic() >= deadline:
                reason = 'deadline'; break
            if select.select([control], [], [], POLL)[0]:
                raw = control.recv(65536)
                if not raw:
                    reason = 'owner_disconnected'; break
                message = json.loads(raw)
                if message['op'] == 'stop':
                    reason = 'stopped'; break
                require(message['op'] == 'deadline', 'Invalid ownership control')
                new = valid_deadline(message['deadline'])
                # Parent may rearm an operation watchdog only inside the fixed
                # lifetime limit established when this owner was created.
                require(ceiling is None or new is not None and new <= ceiling, 'Deadline exceeds owner ceiling')
                deadline = new
                _send(control, {'event': 'armed'})
        code = job.child.poll()
        if reason is None and deadline is not None and time.monotonic() >= deadline:
            reason = 'deadline'
        captured = job.capture()
        leaked = bool(job.living()) if captured else None
        if reason is None and not captured:
            reason = 'ownership_unknown'
        cleanup = job.clean(cleanup_deadline)
        code = job.child.poll()
        failed = reason or ('command_failed' if code != 0 else 'leaked_descendants' if leaked else None)
        receipt = dict(cleanup, returncode=code, failure=failed,
                       cleanup_failure=None if cleanup['confirmed'] else 'cleanup_unconfirmed',
                       timed_out=reason == 'deadline')
        _send(control, {'event': 'finished', 'receipt': receipt})
        return 0 if cleanup['confirmed'] else 87
    except BaseException as error:
        cleanup = {'confirmed': job is None, 'remaining_processes': 0 if job is None else None}
        if job is not None:
            try:
                cleanup = job.clean(cleanup_deadline)
            except BaseException:
                pass
        try:
            _send(control, {'event': 'finished', 'receipt': dict(cleanup, returncode=None,
                   failure=reason or 'supervisor_failed', cleanup_failure=None if cleanup['confirmed'] else 'cleanup_unconfirmed', timed_out=False)})
        except BaseException:
            pass
        return 87
    finally:
        if job is not None:
            job.release()
        control.close()


class OwnedProcessError(RuntimeError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(receipt.get('failure') or receipt.get('cleanup_failure') or 'Owned command failed')


def assert_no_unresolved_owners():
    require(not OWNERS, 'Previous process ownership remains unresolved')


class OwnedProcess:
    """A supervisor lease. Removal from OWNERS requires a verified final receipt."""
    def __init__(self, command, cwd=None, *, deadline=None, cleanup_deadline=None,
                 env=None, stdin=None, stdout=None, stderr=None, cpu_limit=None):
        self.deadline = valid_deadline(deadline)
        self.cleanup_deadline = valid_deadline(cleanup_deadline if cleanup_deadline is not None else (deadline+6 if deadline is not None else None))
        cleanup_deadline = self.cleanup_deadline
        self.receipt = None
        self.process = None
        self.control = None
        self.closed = False
        self.identity = None
        self.owned_enumeration_verified = False
        require(cpu_limit is None or type(cpu_limit) is int and 0 < cpu_limit <= 86400, 'Invalid per-process CPU limit')
        require(command, 'Missing owned command')
        require(deadline is None or time.monotonic() < deadline, 'Owned deadline already expired')
        if deadline is not None and cleanup_deadline is not None:
            require(cleanup_deadline >= deadline, 'Cleanup reserve precedes operation deadline')
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.control = parent
        OWNERS.add(self)
        try:
            args = ['--control-fd', str(child.fileno())]
            if cpu_limit is not None:
                args += ['--cpu-limit', str(cpu_limit)]
            if cwd is not None:
                args += ['--cwd', str(cwd)]
            if deadline is not None:
                args += ['--deadline', str(deadline)]
            if cleanup_deadline is not None:
                args += ['--cleanup-deadline', str(cleanup_deadline)]
            args += ['--', *map(str, command)]
            # Bind the complete supervisor argv once; never append after leasing.
            args = child_python_command(Path(__file__).resolve(), args)
            self.process = subprocess.Popen(args, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                            pass_fds=(child.fileno(),), start_new_session=True)
        except BaseException as error:
            child.close()
            if isinstance(error, OSError) and self.process is None:
                # Popen's exec-error path reaps the failed child before raising.
                OWNERS.discard(self)
                parent.close()
                raise
            # Interruption can occur after fork but before Popen is assigned.
            # Stop via the still-owned private control socket; absent Popen
            # identity remains unresolved rather than assuming no launch.
            receipt = self.close(deadline=self.cleanup_deadline)
            raise OwnedProcessError(dict(receipt, failure='interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'launch_unknown')) from None
        finally:
            child.close()

    def _receive(self, end):
        if self.control is None:
            return None
        remaining = max(0, end - time.monotonic())
        if not select.select([self.control], [], [], min(POLL, remaining))[0]:
            return None
        raw = self.control.recv(65536)
        if not raw:
            return {'event': 'disconnected'}
        message = json.loads(raw)
        if message.get('event') == 'started':
            identity = message.get('identity')
            require(type(identity) is list and len(identity) == 2 and self.process is not None
                    and identity[0] == self.process.pid and type(identity[1]) in (int, float)
                    and math.isfinite(identity[1]) and identity[1] > 0, 'Invalid supervisor identity')
            require(message.get('owned_enumeration_verified') is True, 'Owned enumeration not verified')
            self.identity = tuple(identity)
            self.owned_enumeration_verified = True
        if message.get('event') == 'finished':
            self.receipt = message['receipt']
        return message

    def set_deadline(self, deadline):
        deadline = valid_deadline(deadline)
        require(deadline is not None and time.monotonic() < deadline, 'Operation deadline reached')
        require(self.deadline is None or deadline <= self.deadline, 'Operation exceeds owner deadline')
        _send(self.control, {'op': 'deadline', 'deadline': deadline, 'ceiling': self.deadline})
        while time.monotonic() < deadline:
            event = self._receive(deadline)
            if event and event.get('event') == 'armed':
                return
            if self.receipt is not None or event and event.get('event') == 'disconnected':
                break
        raise OwnedProcessError(self.wait(deadline=deadline))

    def wait(self, *, deadline=None):
        end = valid_deadline(deadline)
        if end is None:
            end = self.cleanup_deadline or (time.monotonic() + 6)
        while self.receipt is None and time.monotonic() < end:
            event = self._receive(end)
            if event and event.get('event') == 'disconnected':
                break
        if self.receipt is not None:
            if self.process is None:
                return dict(self.receipt, confirmed=False, remaining_processes=None, cleanup_failure='supervisor_identity_unknown')
            try:
                self.process.wait(timeout=max(0, end-time.monotonic()))
            except subprocess.TimeoutExpired:
                return dict(self.receipt, confirmed=False, remaining_processes=None, cleanup_failure='supervisor_exit_unknown')
            if self.receipt['confirmed']:
                OWNERS.discard(self)
                self.closed = True
            return dict(self.receipt)
        return dict(confirmed=False, remaining_processes=None, returncode=None,
                    failure='ownership_unknown', cleanup_failure='cleanup_unconfirmed', timed_out=False)

    def close(self, *, deadline=None):
        if self.closed:
            return dict(self.receipt)
        try:
            _send(self.control, {'op': 'stop'})
        except (OSError, ValueError):
            pass
        return self.wait(deadline=deadline)

    def release_streams(self):
        for name in ('stdin', 'stdout', 'stderr'):
            stream = getattr(self.process, name, None)
            if stream is not None:
                stream.close()
        if self.closed and self.control is not None:
            self.control.close()
            self.control = None


def run_owned(command, cwd=None, *, deadline=None, cleanup_deadline=None, env=None):
    assert_no_unresolved_owners()
    deadline = time.monotonic()+3600 if deadline is None else deadline
    owner = OwnedProcess(command, cwd, deadline=deadline, cleanup_deadline=cleanup_deadline, env=env)
    try:
        receipt = owner.wait(deadline=owner.cleanup_deadline)
    except BaseException as error:
        cleanup = owner.close(deadline=owner.cleanup_deadline)
        error.cleanup_receipt = {key: cleanup[key] for key in ('confirmed', 'remaining_processes')}
        error.cleanup_failure = cleanup.get('cleanup_failure')
        raise
    finally:
        owner.release_streams()
    if not receipt['confirmed'] or receipt['failure'] is not None or receipt['returncode'] != 0:
        raise OwnedProcessError(receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd')
    parser.add_argument('--control-fd', type=int, required=True)
    parser.add_argument('--deadline', type=float)
    parser.add_argument('--cleanup-deadline', type=float)
    parser.add_argument('--cpu-limit', type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    return _supervise(command, args.cwd, args.control_fd, valid_deadline(args.deadline), valid_deadline(args.cleanup_deadline), args.cpu_limit)


if __name__ == '__main__':
    raise SystemExit(main())
