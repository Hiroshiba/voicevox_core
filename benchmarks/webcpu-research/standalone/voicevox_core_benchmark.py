#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0", "onnx==1.19.1", "protobuf==6.32.1", "ml-dtypes==0.5.1"]
# ///
"""Reproducible VOICEVOX CORE CPU/browser benchmark (one-file distribution).

Run: uv run benchmark.py [--threads 4]
Optimization experiment: uv run benchmark.py --experiments --threads 2
Graph experiment (V8 OFF): uv run benchmark.py --graph-experiments --threads 2
Optional separate ON study: add --graph-revectorize on
Kernel/runtime experiment (Linux/WSL x86-64): --kernel-experiments --threads 2
  --kernel-revectorize off is the default; use a new --output for each run.
  --prepare-only builds/proves runtimes without inference or timing.
  --kernel-profile-xnn adds separate untimed provider placement evidence.
Kernel runs keep the original model; XNN equality is not CPU bit-exactness.

Graph candidates are experimental and generated only after explicit opt-in.
The original model, default modes, and precision/compiler flags stay unchanged.
No graph speedup is assumed; each host must pass exact PCM/FP32 output checks.

The first run downloads pinned Rust/Emscripten, models, runtimes and Chromium,
then builds CORE; allow several GB and several minutes. A native C/C++ compiler
is required (Windows: MSVC Native Tools prompt; macOS: Xcode Command Line Tools;
Linux: cc and c++). Linux also needs Chromium's system libraries; if missing,
run: uv run --with playwright==1.63.0 python -m playwright install-deps chromium
That system-package step may require administrator permission. Later runs reuse
checked downloads and builds.

Outputs: standalone HTML with CPU time-series/charts/collapsed CSV, plus JSON.
No text analyzer or dictionary is used. No benchmark measurements are simulated.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

CORE_COMMIT = "9b539761f3e152b966e08c2de0784129fe8cf68d"
ORT_BUILDER_COMMIT = "117593885cd2a66e9cf17b4059e6424d7ea528c9"
ORT_VERSION = "1.23.2"
VOCODER_MODEL_SHA256 = "80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820"
TRIALS_PER_BLOCK = 5
BLOCKS_PER_MODE = 3
SCHEMA_VERSION = 4


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    threads: int
    backend: str
    core_optimization: str = "z"
    graph_optimization: int = 1
    experimental: bool = False
    fixed_shape: bool = False
    spin_off: bool = False
    execution_provider: str = "CPU"
    revectorize: bool = False


@dataclass(frozen=True)
class Trial:
    order: int
    mode: str
    block: int
    trial: int
    threads: int
    elapsed_s: float
    audio_s: float
    rtf: float
    cpu_time_s: float
    cpu_window_s: float
    cpu_avg_cores: float
    cpu_percent: float
    cpu_samples: int
    cpu_processes: int
    cpu_incomplete: bool
    cpu_trace: tuple[CpuInterval, ...]


@dataclass(frozen=True)
class Block:
    mode: str
    number: int


CPU_SAMPLE_INTERVAL_S = 0.1
CPU_PADDING_S = 1.0
PROGRESS_INTERVAL_S = 15.0


def progress(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class CpuInterval:
    start_s: float
    end_s: float
    cpu_time_s: float
    cpu_percent: float
    cpu_processes: int
    cpu_incomplete: bool
    phase: str = "action"


@dataclass(frozen=True)
class CpuMeasurement:
    cpu_time_s: float
    cpu_window_s: float
    cpu_avg_cores: float
    cpu_percent: float
    cpu_samples: int
    cpu_processes: int
    cpu_incomplete: bool
    cpu_trace: tuple[CpuInterval, ...]


class ProcessCpuSampler:
    """Sum user+system CPU for one explicitly selected process tree.

    The CPU window surrounds the request/response and counter reads, separately
    from the existing internal synthesis timer. CPU times include all threads.
    Never add children_user/system: descendant processes are counted directly.
    Lost/late processes mark CPU data incomplete; those rows remain in CSV but
    are excluded from the report's CPU average. Polling cannot observe a child
    that starts and exits entirely between samples.
    """
    def __init__(self, root_pid: int, label: str, interval: float = CPU_SAMPLE_INTERVAL_S):
        import psutil
        import threading
        if root_pid == os.getpid():
            raise ValueError("Refusing to include the Python orchestrator in target CPU")
        if interval <= 0 or not math.isfinite(interval):
            raise ValueError("CPU sample interval must be positive")
        self.psutil = psutil
        self.root = psutil.Process(root_pid)
        self.root_identity = self.root
        self.label, self.interval = label, interval
        self.stop = threading.Event()
        self.sample_lock = threading.Lock()
        self.phase = "pre"
        self.phase_started_wall = 0.0
        # Process equality/hash guards PID reuse using psutil's stable identity.
        # A (pid, wall-clock create_time) tuple can duplicate a live Linux PID
        # when /proc/stat btime shifts (for example after clock adjustments).
        self.processes: dict[Any, Any] = {}
        self.previous: dict[Any, float] = {}
        self.lost: set[Any] = set()
        self.total = 0.0
        self.samples = 0
        self.incomplete = False
        self.error: Exception | None = None
        self.started_epoch = 0.0
        self.started_wall = 0.0
        self.baseline_wall = 0.0
        self.previous_sample_wall = 0.0
        self.intervals: list[CpuInterval] = []

    def _sample(self, initial: bool = False) -> None:
        try:
            before = self.total
            if not self.root.is_running():
                raise RuntimeError("Target CPU root exited or its PID was reused")
            candidates = [self.root] + self.root.children(recursive=True)
            for process in candidates:
                try:
                    process.create_time()  # Check existence; birth time is attribution only.
                    identity = process
                    self.processes.setdefault(identity, process)
                except self.psutil.NoSuchProcess:
                    self.incomplete = True
            for identity, process in list(self.processes.items()):
                if identity in self.lost:
                    continue
                try:
                    # cpu_times alone does not guard against PID reuse.
                    if not process.is_running():
                        raise self.psutil.NoSuchProcess(process.pid)
                    times = process.cpu_times()
                    value = float(times.user + times.system)
                    if not math.isfinite(value) or value < 0:
                        raise RuntimeError("Invalid process CPU counter")
                    if identity not in self.previous:
                        if initial:
                            self.previous[identity] = value
                            continue
                        phase_epoch = self.started_epoch + self.phase_started_wall - self.started_wall
                        if process.create_time() < phase_epoch:
                            # Never move a newly discovered child's earlier-phase
                            # lifetime CPU into this phase's counters. Its late
                            # discovery also invalidates earlier coverage where
                            # it could already have consumed unobserved CPU.
                            birth_wall = self.started_wall + process.create_time() - self.started_epoch
                            affected = {point.phase for point in self.intervals if point.end_s > birth_wall}
                            self.intervals = [replace(point, cpu_incomplete=True) if point.phase in affected else point
                                              for point in self.intervals]
                            self.previous[identity] = value
                            self.incomplete = True
                            continue
                        previous_epoch = self.started_epoch + self.previous_sample_wall - self.started_wall
                        if process.create_time() < previous_epoch:
                            # Lifetime CPU is attributable to this trial, but a
                            # late discovery cannot place it in the right interval.
                            # Birth-time rounding is treated conservatively too.
                            self.incomplete = True
                        self.previous[identity] = 0.0
                    delta = value - self.previous[identity]
                    if delta < -1e-6:
                        raise RuntimeError("Process CPU counter moved backwards")
                    self.total += max(0.0, delta)
                    self.previous[identity] = value
                except self.psutil.NoSuchProcess:
                    if identity == self.root_identity:
                        raise RuntimeError("Target CPU root disappeared before the final sample")
                    self.lost.add(identity)
                    self.incomplete = True
            sampled = time.perf_counter()
            if initial:
                self.baseline_wall = sampled
            else:
                span = sampled - self.previous_sample_wall
                if span <= 0:
                    raise RuntimeError("CPU snapshot clock did not advance")
                delta = self.total - before
                self.intervals.append(CpuInterval(self.previous_sample_wall, sampled, delta,
                                                  100 * delta / span, len(self.processes), self.incomplete, self.phase))
            self.previous_sample_wall = sampled
            self.samples += 1
        except Exception as error:
            self.error = error
            self.stop.set()

    def _observe_padding(self, started: float) -> None:
        """Require a full interval on the same clock used by the CPU trace.

        A timed wait is not itself proof of elapsed perf_counter time (notably
        across virtualized clock domains). Never invent or stretch samples.
        """
        deadline = started + CPU_PADDING_S
        while True:
            if self.error:
                raise RuntimeError(f"CPU observation failed for {self.label}: {self.error}") from self.error
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            if self.stop.wait(remaining):
                raise RuntimeError(f"CPU observation interrupted for {self.label}: {self.error}") from self.error

    def measure(self, action: Callable[[], tuple[float, float]]) -> tuple[float, float, CpuMeasurement]:
        import threading
        self.started_wall = time.perf_counter()
        self.started_epoch = time.time()
        with self.sample_lock:
            self._sample(initial=True)
            self.phase_started_wall = self.previous_sample_wall
        if self.error:
            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
        def sample_loop() -> None:
            last_heartbeat = time.perf_counter()
            while not self.stop.wait(self.interval):
                with self.sample_lock:
                    self._sample()
                now = time.perf_counter()
                if now - last_heartbeat >= 30:
                    progress(f"{self.label}: synthesis still running ({now - self.started_wall:.0f}s)")
                    last_heartbeat = now
        worker = threading.Thread(target=sample_loop, name="benchmark-cpu-sampler", daemon=True)
        worker.start()
        try:
            self._observe_padding(self.baseline_wall)
            with self.sample_lock:
                self._sample()
                action_cpu_start, action_wall_start = self.total, self.previous_sample_wall
                self.phase = "action"
                self.phase_started_wall = action_wall_start
                self.incomplete = False
            if self.error:
                raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
            request_started = time.perf_counter()
            elapsed, duration = action()
            with self.sample_lock:
                self._sample()
                action_cpu_end, action_wall_end = self.total, self.previous_sample_wall
                self.phase = "post"
                self.phase_started_wall = action_wall_end
                self.incomplete = False
            if self.error:
                raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
            self._observe_padding(action_wall_end)
        finally:
            self.stop.set()
            worker.join(timeout=5)
            if worker.is_alive():
                self.error = RuntimeError("CPU sampler did not stop")
            elif self.error is None:
                with self.sample_lock:
                    self._sample()
        if self.error:
            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
        total, window = action_cpu_end - action_cpu_start, action_wall_end - action_wall_start
        cores = total / window
        trace = tuple(CpuInterval(item.start_s - request_started, item.end_s - request_started,
                                  item.cpu_time_s, item.cpu_percent, item.cpu_processes, item.cpu_incomplete, item.phase)
                      for item in self.intervals)
        action_incomplete = any(item.cpu_incomplete for item in trace if item.phase == "action")
        return elapsed, duration, CpuMeasurement(total, window, cores, 100 * cores,
                                                self.samples, len(self.processes), action_incomplete, trace)


def chromium_process_id(browser: Any) -> int:
    """Use the public browser-level CDP API, never Playwright internals."""
    session = browser.new_browser_cdp_session()
    try:
        response = session.send("SystemInfo.getProcessInfo")
    except Exception as error:
        raise RuntimeError("This Chromium does not provide browser process IDs for CPU sampling; use the bundled Chromium") from error
    finally:
        session.detach()
    roots = [int(process["id"]) for process in response["processInfo"] if process.get("type") == "browser"]
    if len(roots) != 1 or roots[0] <= 0 or roots[0] == os.getpid():
        raise RuntimeError("Could not identify the benchmark Chromium process safely")
    return roots[0]


def weighted_cpu_percent(trials: list[dict[str, Any]]) -> float | None:
    complete = [row for row in trials if not row["cpu_incomplete"]]
    if not complete:
        return None
    return 100 * sum(row["cpu_time_s"] for row in complete) / sum(row["cpu_window_s"] for row in complete)


def block_bootstrap_median_ci(samples: list[tuple[int, float]]) -> dict[str, Any] | None:
    """Nominal percentile interval; three intact blocks, all 3**3 draws.

    This preserves within-block dependence. Three clusters are too few to
    guarantee reliable 95% coverage; the report labels the interval exploratory.
    """
    import itertools
    groups: dict[int, list[float]] = {}
    for block, value in samples:
        groups.setdefault(block, []).append(value)
    if len(groups) != BLOCKS_PER_MODE:
        return None
    keys = sorted(groups)
    estimates = sorted(statistics.median(value for key in draw for value in groups[key])
                       for draw in itertools.product(keys, repeat=BLOCKS_PER_MODE))
    def quantile(probability: float) -> float:
        position = (len(estimates) - 1) * probability
        lo, hi = math.floor(position), math.ceil(position)
        return estimates[lo] + (estimates[hi] - estimates[lo]) * (position - lo)
    return {"low": quantile(0.025), "high": quantile(0.975), "nominal_level": 0.95,
            "resampling_unit": "whole five-trial block", "blocks": len(groups), "draws": len(estimates),
            "quantile_method": "linear interpolation at (n-1)*p", "exploratory": True}


def make_schedule(modes: list[Mode], seed: int, *, balanced: bool = False) -> list[Block]:
    """Three shuffled rounds, each containing one five-trial block per mode."""
    if not modes or len({mode.key for mode in modes}) != len(modes):
        raise ValueError("Mode keys must be nonempty and unique")
    rng = random.Random(seed)
    if balanced:
        if len(modes) not in (2, 3, 4, 5, 6, 7) or BLOCKS_PER_MODE != 3:
            raise ValueError("Balanced experiments require two to seven modes and three rounds")
        base = list(modes)
        rng.shuffle(base)
        if len(modes) == 7:
            indices = ((0, 1, 2, 3, 4, 5, 6), (3, 5, 4, 6, 2, 1, 0), (6, 4, 5, 2, 0, 3, 1))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 6:
            indices = ((0, 1, 2, 3, 4, 5), (5, 4, 3, 2, 1, 0), (2, 3, 4, 5, 0, 1))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 5:
            indices = ((0, 1, 2, 3, 4), (4, 3, 0, 2, 1), (2, 4, 1, 0, 3))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 4:
            indices = ((0, 1, 2, 3), (1, 0, 3, 2), (2, 3, 0, 1))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 3:
            rotations = [base[i:] + base[:i] for i in range(3)]
        else:
            rotations = [base[:], base[::-1], base[::rng.choice((1, -1))]]
        rng.shuffle(rotations)
        return [Block(mode.key, number) for number, row in enumerate(rotations, 1) for mode in row]
    schedule = []
    for block_number in range(1, BLOCKS_PER_MODE + 1):
        round_modes = list(modes)
        rng.shuffle(round_modes)
        schedule.extend(Block(mode.key, block_number) for mode in round_modes)
    return schedule


def logical_cpu_count() -> int:
    # Respect the process's available logical CPUs in containers/affinity masks.
    process_count = getattr(os, "process_cpu_count", lambda: None)()
    if process_count:
        return process_count
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def default_threads() -> int:
    return max(1, logical_cpu_count() // 2)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cached_download(url: str, root: Path, filename: str, *,
                    expected_sha256: str | None = None) -> Path:
    """A partial download is never reused. Every cache hit rechecks its digest.

    An upstream SHA-256 is checked when supplied. Otherwise the manifest records
    the digest seen on the first successful HTTPS download (integrity on reuse,
    not a claim of independent supply-chain authentication).
    """
    if not url.startswith("https://"):
        raise ValueError("Downloads require HTTPS")
    if Path(filename).name != filename:
        raise ValueError("Download filename must not contain a directory")
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    folder = root / "downloads" / key
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    receipt = folder / "complete.json"
    if target.is_file() and receipt.is_file():
        try:
            saved = json.loads(receipt.read_text("utf-8"))
            expected = expected_sha256 or saved["sha256"]
            if saved["url"] == url and target.stat().st_size == saved["bytes"] and sha256_file(target) == expected:
                progress(f"Cache hit: {filename}")
                return target
        except (OSError, ValueError, KeyError):
            pass
    descriptor, name = tempfile.mkstemp(prefix=".download-", dir=folder)
    partial = Path(name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "voicevox-core-benchmark/1"})
        digest = hashlib.sha256()
        total = 0
        progress(f"Downloading {filename}")
        last_progress = time.monotonic()
        with os.fdopen(descriptor, "wb") as out, urllib.request.urlopen(request, timeout=120) as response:
            if not response.geturl().startswith("https://"):
                raise RuntimeError("Download redirected away from HTTPS")
            length = response.headers.get("Content-Length")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                out.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                now = time.monotonic()
                if now - last_progress >= 5:
                    size = f" / {int(length) / 2**20:.1f} MiB" if length else " MiB"
                    progress(f"{filename}: {total / 2**20:.1f}{size} downloaded")
                    last_progress = now
            if length is not None and total != int(length):
                raise RuntimeError(f"Incomplete download: {filename}")
            out.flush()
            os.fsync(out.fileno())
        actual = digest.hexdigest()
        progress(f"Downloaded {filename}: {total / 2**20:.1f} MiB; verifying checksum")
        if expected_sha256 and actual != expected_sha256:
            raise RuntimeError(f"SHA-256 mismatch: {filename}")
        os.replace(partial, target)
        atomic_write(receipt, json.dumps({"url": url, "sha256": actual, "bytes": total}).encode())
        return target
    finally:
        partial.unlink(missing_ok=True)


def cache_root() -> Path:
    try:
        from platformdirs import user_cache_path
    except ImportError:
        # Keep report-only and self-test usable with the Python standard library.
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library/Caches"
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return base / "voicevox-core-benchmark"
    return user_cache_path("voicevox-core-benchmark", appauthor=False)


def checked_run(command: list[str], *, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> str:
    """Capture output for errors/return values while keeping long work visible."""
    import queue
    import threading
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    messages: queue.Queue[str | None] = queue.Queue()
    def read_output() -> None:
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output: list[str] = []
    last_line = ""
    last_report = started
    label = Path(command[0]).name
    if len(command) > 1:
        label += " " + Path(command[1]).name
    try:
        finished = False
        while not finished:
            try:
                line = messages.get(timeout=1.0)
                if line is None:
                    finished = True
                else:
                    output.append(line)
                    if line.strip():
                        last_line = line.strip()[-180:]
            except queue.Empty:
                pass
            now = time.monotonic()
            if not finished and now - last_report >= PROGRESS_INTERVAL_S:
                progress(f"{label}: running {now - started:.0f}s" + (f"; {last_line}" if last_line else ""))
                last_report = now
        code = process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    text_output = "".join(output)
    duration = time.monotonic() - started
    if code:
        raise RuntimeError(f"Command failed ({code}): {' '.join(command)}\n{text_output[-10000:]}")
    if duration >= 5:
        progress(f"{label}: completed in {duration:.1f}s")
    return text_output.strip()

def command_version(command: list[str]) -> str | None:
    try:
        return checked_run(command).splitlines()[0]
    except (OSError, RuntimeError, IndexError):
        return None


def cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        if sys.platform == "darwin":
            return checked_run(["sysctl", "-n", "machdep.cpu.brand_string"])
    except (OSError, RuntimeError):
        pass
    return platform.processor() or platform.machine() or "unknown"


def environment_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "cpu": cpu_model(),
        "available_logical_cpus": logical_cpu_count(),
        "host_logical_cpus": os.cpu_count() or "unknown",
        "python": platform.python_version(),
        "core_commit": CORE_COMMIT,
        "onnxruntime_version": ORT_VERSION,
        "onnxruntime_builder_commit": ORT_BUILDER_COMMIT,
    }
    if sys.platform == "darwin":
        info["os"] = f"macOS {platform.mac_ver()[0]} (Darwin {platform.release()})"
    elif sys.platform.startswith("linux"):
        try:
            info["os"] = f"{platform.freedesktop_os_release().get('PRETTY_NAME', 'Linux')} (kernel {platform.release()})"
        except OSError:
            pass
    for key, command in (("uv", ["uv", "--version"]), ("rust", ["rustc", "--version"]),
                         ("emscripten", ["emcc", "--version"])):
        if version := command_version(command):
            info[key] = version
    # CPU affinity / cgroup limits explain cloud results without exposing hostnames.
    if hasattr(os, "sched_getaffinity"):
        info["affinity_logical_cpus"] = len(os.sched_getaffinity(0))
    limit_file = Path("/sys/fs/cgroup/cpu.max")
    if sys.platform.startswith("linux") and limit_file.exists():
        try:
            quota, period = limit_file.read_text().strip().split()
            if quota != "max":
                info["cgroup_cpu_quota"] = int(quota) / int(period)
        except (OSError, ValueError, ZeroDivisionError):
            pass
    if hasattr(os, "sysconf"):
        try:
            info["memory_gib"] = round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 2)
        except (ValueError, OSError):
            pass
    elif sys.platform == "win32":
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            info["memory_gib"] = round(status.total_phys / 2**30, 2)
    return info


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getnframes() < 1 or wav.getframerate() < 1:
            raise ValueError("Generated WAV is empty")
        return wav.getnframes() / wav.getframerate()


def pcm_wav_info(data: bytes) -> tuple[dict[str, int], bytes]:
    with wave.open(io.BytesIO(data), "rb") as stream:
        info = {"channels": stream.getnchannels(), "sample_bytes": stream.getsampwidth(),
                "sample_rate": stream.getframerate(), "frames": stream.getnframes()}
        pcm = stream.readframes(info["frames"])
    if info["sample_bytes"] != 2 or min(info.values()) <= 0 or len(pcm) != info["frames"] * info["channels"] * 2:
        raise ValueError("Spectrogram requires complete nonempty PCM16 WAV audio")
    return info, pcm


SPECTROGRAM_METHOD = {"window": "periodic Hann", "nfft": 1024, "hop": 256,
                      "channel": "first", "scale": "one-sided amplitude dBFS",
                      "range_db": [-100, 0], "centered_zero_padding": True,
                      "amplitude_normalization": "2/sum(window); DC and Nyquist not doubled"}


def spectrogram_db(wav: bytes) -> tuple[Any, int, float]:
    import numpy as np
    info, pcm = pcm_wav_info(wav)
    signal = np.frombuffer(pcm, dtype="<i2").reshape(-1, info["channels"])[:, 0].astype(np.float64) / 32768.0
    nfft, hop = 1024, 256
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(signal, (nfft // 2, nfft // 2 + (-signal.size) % hop)), nfft)[::hop]
    amplitude = np.abs(np.fft.rfft(frames * window, axis=1)) * (2.0 / window.sum())
    amplitude[:, (0, -1)] *= 0.5
    db = 20 * np.log10(np.maximum(amplitude, 1e-5))
    return np.clip(db.T, -100, 0), info["sample_rate"], info["frames"] / info["sample_rate"]


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Accept pixels only: no metadata chunks, appended data, or audio payload."""
    from PIL import Image
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Expected a PNG spectrogram")
    offset, chunks = 8, []
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated PNG")
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        if kind not in {b"IHDR", b"IDAT", b"IEND"} or offset + 12 + size > len(data):
            raise ValueError("Spectrogram PNG must contain only image pixels")
        chunks.append(kind)
        offset += 12 + size
        if kind == b"IEND":
            break
    if offset != len(data) or not chunks or chunks[0] != b"IHDR" or chunks[-1] != b"IEND":
        raise ValueError("Malformed PNG or trailing payload")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.mode != "RGB" or not (1 <= image.width <= 4096 and 1 <= image.height <= 4096):
            raise ValueError("Unexpected spectrogram image format or dimensions")
        size = image.size
        image.verify()
    return size


def encode_spectrograms(wavs: dict[str, bytes], checks: dict[str, Any], provenance: str) -> list[dict[str, Any]]:
    """Convert local diagnostics into image-only report data; never serialize audio."""
    groups: dict[str, dict[str, Any]] = {}
    previous_config = os.environ.get("MPLCONFIGDIR")
    previous_cache = os.environ.get("XDG_CACHE_HOME")
    with tempfile.TemporaryDirectory(prefix="voicevox-plot-") as config:
        os.environ["MPLCONFIGDIR"] = config
        os.environ["XDG_CACHE_HOME"] = config
        try:
            import matplotlib
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            from matplotlib.figure import Figure
            from PIL import Image
            for key, wav in wavs.items():
                info, pcm = pcm_wav_info(wav)
                digest = hashlib.sha256(pcm).hexdigest()
                if digest != checks[key]["pcm_sha256"] or info != checks[key]["wav_format"]:
                    raise ValueError(f"Audio does not match the recorded output for {key}")
                if digest not in groups:
                    spectrum, rate, duration = spectrogram_db(wav)
                    with matplotlib.rc_context({"font.family": "DejaVu Sans", "font.size": 8.5}):
                        figure = Figure(figsize=(5.4, 2.25), dpi=130, layout="constrained")
                        FigureCanvasAgg(figure)
                        axes = figure.subplots()
                        dt, df = 256 / rate, rate / 1024 / 1000
                        image = axes.imshow(spectrum, origin="lower", aspect="auto", extent=(-dt / 2, (spectrum.shape[1] - .5) * dt, -df / 2, rate / 2000 + df / 2),
                                            vmin=-100, vmax=0, cmap="magma", interpolation="nearest")
                        axes.set(xlabel="Time (s)", ylabel="Frequency (kHz)", xlim=(0, duration), ylim=(0, rate / 2000))
                        figure.colorbar(image, ax=axes, label="dBFS", ticks=(-100, -50, 0))
                        rendered = io.BytesIO()
                        figure.savefig(rendered, format="png")
                    # Re-encode pixel data only, removing metadata and ancillary chunks.
                    image_bytes = io.BytesIO()
                    with Image.open(io.BytesIO(rendered.getvalue())) as source:
                        pixels = Image.frombytes("RGB", source.size, source.convert("RGB").tobytes())
                        pixels.save(image_bytes, format="PNG")
                    png = image_bytes.getvalue()
                    width, height = png_dimensions(png)
                    groups[digest] = {"pcm_sha256": digest, "wav_format": info, "modes": [],
                                      "png_base64": base64.b64encode(png).decode(), "png_sha256": hashlib.sha256(png).hexdigest(),
                                      "width": width, "height": height, "method": SPECTROGRAM_METHOD.copy(), "provenance": provenance}
                groups[digest]["modes"].append(key)
        finally:
            if previous_config is None:
                os.environ.pop("MPLCONFIGDIR", None)
            else:
                os.environ["MPLCONFIGDIR"] = previous_config
            if previous_cache is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = previous_cache
    return list(groups.values())


def attach_saved_audio(result: dict[str, Any], directory: Path) -> None:
    checks = result.get("output_checks", {}).get("modes", {})
    if not checks:
        raise ValueError("Saved audio requires recorded PCM hashes and formats")
    by_digest = {}
    for path in sorted(directory.glob("*.wav")):
        wav = path.read_bytes()
        _, pcm = pcm_wav_info(wav)
        by_digest.setdefault(hashlib.sha256(pcm).hexdigest(), wav)
    missing = [key for key, check in checks.items() if check["pcm_sha256"] not in by_digest]
    if missing:
        raise ValueError("No hash-matched audio for: " + ", ".join(missing))
    result["spectrograms"] = encode_spectrograms({key: by_digest[check["pcm_sha256"]] for key, check in checks.items()}, checks,
        "Generated locally from retained untimed diagnostic WAVs after checking PCM hashes and formats against the measurement record. Only PNG pixels and provenance are distributed; CI verifies those images, not the private source WAVs.")


def validate_spectrograms(result: dict[str, Any]) -> None:
    groups = result.get("spectrograms", [])
    if not groups:
        return
    labels = {mode["key"] for mode in result["modes"]}
    fields = {"pcm_sha256", "wav_format", "modes", "png_base64", "png_sha256", "width", "height", "method", "provenance"}
    represented = [key for group in groups for key in group["modes"]]
    if set(represented) != labels or len(represented) != len(labels):
        raise ValueError("Spectrogram groups must represent every mode exactly once")
    for group in groups:
        if set(group) != fields or group["method"] != SPECTROGRAM_METHOD:
            raise ValueError("Unexpected spectrogram metadata or analysis method")
        png = base64.b64decode(group["png_base64"], validate=True)
        if hashlib.sha256(png).hexdigest() != group["png_sha256"] or png_dimensions(png) != (group["width"], group["height"]):
            raise ValueError("Spectrogram image integrity check failed")
        for key in group["modes"]:
            check = result["output_checks"]["modes"][key]
            if check["pcm_sha256"] != group["pcm_sha256"] or check["wav_format"] != group["wav_format"]:
                raise ValueError("Spectrogram provenance does not match its measured mode")


def attach_saved_spectrograms(result: dict[str, Any], directory: Path) -> None:
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if set(manifest) != {"schema_version", "spectrograms"} or manifest["schema_version"] != 1:
        raise ValueError("Unexpected spectrogram manifest")
    groups = []
    for item in manifest["spectrograms"]:
        group = dict(item)
        filename = group.pop("file")
        if not re.fullmatch(r"[a-z0-9-]+\.png", filename):
            raise ValueError("Invalid spectrogram filename")
        group["png_base64"] = base64.b64encode((directory / filename).read_bytes()).decode()
        groups.append(group)
    result["spectrograms"] = groups
    validate_spectrograms(result)


def spectrograms_html(result: dict[str, Any]) -> str:
    groups = result.get("spectrograms", [])
    if not groups:
        return ""
    validate_spectrograms(result)
    labels = {mode["key"]: mode["label"] for mode in result["modes"]}
    figures = []
    for group in groups:
        title = " / ".join(labels[key] for key in group["modes"])
        figures.append(f'<figure class="spectrogram" data-pcm-sha256="{group["pcm_sha256"]}" data-png-sha256="{group["png_sha256"]}"><figcaption>{html.escape(title)}</figcaption><img alt="{html.escape(title, quote=True)} spectrogram" src="data:image/png;base64,{group["png_base64"]}"/></figure>')
    provenance = " / ".join(sorted({group["provenance"] for group in groups}))
    return '<h2>出力音声のスペクトログラム</h2><div class="spectrogram-grid">' + ''.join(figures) + '</div><p class="cpu-caption">PCMが完全一致するモードはまとめて表示。共通の時間・周波数・色スケール（−100〜0 dBFS）、先頭チャンネル、Hann窓1024点・hop256点。画像作成時にPCMハッシュと音声形式を実測記録と照合済み。</p><details><summary>音声の出典・解析条件</summary><p>' + html.escape(provenance) + '</p><p>PCM16を32768で割り、窓の総和で振幅を正規化した片側FFT（DC/Nyquistは倍化しない）。端は解析窓用にゼロpadding。1つの保存音声から作った画像であり、15試行の平均音声ではありません。配布するHTML・JSONにはスペクトログラム画像だけを含み、音声データは含みません。</p></details>'


def waveform_comparison(reference_wav: bytes, reference_raw: bytes, wav: bytes, raw: bytes) -> dict[str, Any]:
    """Untimed exactness check, with error magnitudes but no perceptual tolerance."""
    import array
    def pcm(data: bytes) -> tuple[tuple[int, ...], bytes]:
        with wave.open(io.BytesIO(data), "rb") as stream:
            shape = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes())
            if shape[1] != 2 or shape[3] < 1:
                raise RuntimeError("Expected nonempty signed PCM16 output")
            payload = stream.readframes(shape[3])
            if len(payload) != shape[0] * shape[1] * shape[3]:
                raise RuntimeError("Truncated PCM waveform")
            return shape, payload
    reference_shape, reference_pcm = pcm(reference_wav)
    shape, values_pcm = pcm(wav)
    if shape != reference_shape or len(raw) != len(reference_raw) or not raw or len(raw) % 4:
        raise RuntimeError("Output shape/dtype differs from the same-run reference")
    def values(data: bytes, typecode: str) -> Any:
        result = array.array(typecode)
        result.frombytes(data)
        if sys.byteorder != "little":
            result.byteswap()
        return result
    floats, ref_floats = values(raw, "f"), values(reference_raw, "f")
    if not all(math.isfinite(value) for value in floats) or not all(math.isfinite(value) for value in ref_floats):
        raise RuntimeError("FP32 output contains NaN or infinity")
    float_errors = [float(a) - float(b) for a, b in zip(floats, ref_floats)]
    pcm_errors = [int(a) - int(b) for a, b in zip(values(values_pcm, "h"), values(reference_pcm, "h"))]
    rmse = math.sqrt(math.fsum(error * error for error in float_errors) / len(float_errors))
    reference_rms = math.sqrt(math.fsum(float(value) ** 2 for value in ref_floats) / len(ref_floats))
    return {"wav_format": {"channels": shape[0], "sample_bytes": shape[1], "sample_rate": shape[2], "frames": shape[3]},
            "fp32_samples": len(floats), "finite": True, "pcm_exact": values_pcm == reference_pcm,
            "pcm_sha256": hashlib.sha256(values_pcm).hexdigest(), "pcm_changed_samples": sum(error != 0 for error in pcm_errors),
            "pcm_max_abs_lsb": max(map(abs, pcm_errors)), "pcm_rmse_lsb": math.sqrt(math.fsum(error * error for error in pcm_errors) / len(pcm_errors)),
            "fp32_exact": raw == reference_raw, "fp32_sha256": hashlib.sha256(raw).hexdigest(),
            "fp32_changed_samples": sum(a != b for a, b in zip(values(raw, "I"), values(reference_raw, "I"))),
            "fp32_max_abs": max(map(abs, float_errors)), "fp32_rmse": rmse,
            "fp32_relative_rmse": rmse / max(reference_rms, 1e-12), "relative_rms_floor": 1e-12}


def verify_outputs(runners: dict[str, Any], reference: str, log: list[dict[str, Any]]) -> dict[str, Any]:
    progress(f"Checking PCM and pre-PCM FP32 outputs against {reference} (untimed)")
    reference_wav = runners[reference].wav.read_bytes()
    reference_raw = runners[reference].raw_wave()
    runners[reference].synthesize(save=True)
    repeated = waveform_comparison(reference_wav, reference_raw, runners[reference].wav.read_bytes(), runners[reference].raw_wave())
    comparisons = {}
    raw_outputs = {reference: reference_raw}
    wav_outputs = {key: runner.wav.read_bytes() for key, runner in runners.items()}
    for key, runner in runners.items():
        raw = reference_raw if key == reference else runner.raw_wave()
        raw_outputs[key] = raw
        comparisons[key] = waveform_comparison(reference_wav, reference_raw, runner.wav.read_bytes(), raw)
        progress(f"Output {key}: PCM {'exact' if comparisons[key]['pcm_exact'] else 'DIFFERS'}, FP32 {'exact' if comparisons[key]['fp32_exact'] else 'DIFFERS'}")
    result = {"reference_mode": reference, "reference_deterministic": repeated["pcm_exact"] and repeated["fp32_exact"],
              "reference_repeat": repeated, "modes": comparisons}
    if "browser_xnnpack" in runners:
        fixed = "browser_mt_fixed"
        runners[fixed].synthesize(save=True)
        repeat_fixed = waveform_comparison(wav_outputs[fixed], raw_outputs[fixed], runners[fixed].wav.read_bytes(), runners[fixed].raw_wave())
        result["fixed_control_repeat"] = repeat_fixed
        result["reference_deterministic"] = result["reference_deterministic"] and repeat_fixed["pcm_exact"] and repeat_fixed["fp32_exact"]
        result["xnnpack_vs_fixed_control"] = waveform_comparison(wav_outputs[fixed], raw_outputs[fixed], wav_outputs["browser_xnnpack"], raw_outputs["browser_xnnpack"])
    if "browser_xnnpack_revectorize" in runners:
        xnn, combined = "browser_xnnpack", "browser_xnnpack_revectorize"
        runners[xnn].synthesize(save=True)
        repeat_xnn = waveform_comparison(wav_outputs[xnn], raw_outputs[xnn], runners[xnn].wav.read_bytes(), runners[xnn].raw_wave())
        result["xnnpack_reference_repeat"] = repeat_xnn
        result["reference_deterministic"] = result["reference_deterministic"] and repeat_xnn["pcm_exact"] and repeat_xnn["fp32_exact"]
        result["combined_vs_xnnpack"] = waveform_comparison(wav_outputs[xnn], raw_outputs[xnn], wav_outputs[combined], raw_outputs[combined])
        result["combined_vs_fixed_control"] = waveform_comparison(wav_outputs["browser_mt_fixed"], raw_outputs["browser_mt_fixed"], wav_outputs[combined], raw_outputs[combined])
    result["per_mode_repeat"] = {reference: repeated}
    for key, runner in runners.items():
        if key == reference:
            continue
        runner.synthesize(save=True)
        result["per_mode_repeat"][key] = waveform_comparison(
            wav_outputs[key], raw_outputs[key], runner.wav.read_bytes(), runner.raw_wave())
    result["spectrograms"] = encode_spectrograms(wav_outputs, comparisons, "Generated locally from same-run untimed output-check WAVs before measured trials; only spectrogram PNG pixels are retained in this report.")
    log.append({"event": "untimed_output_checks", "reference": reference, "reference_deterministic": result["reference_deterministic"]})
    return result


def trial_csv(trials: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[key for key in Trial.__dataclass_fields__ if key != "cpu_trace"], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trials)
    return stream.getvalue()


def cpu_trace_csv(trials: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    context = ("order", "mode", "block", "trial", "threads")
    writer = csv.DictWriter(stream, fieldnames=[*context, "interval", *CpuInterval.__dataclass_fields__])
    writer.writeheader()
    for trial in trials:
        for index, point in enumerate(trial.get("cpu_trace", ()), 1):
            writer.writerow({**{key: trial[key] for key in context}, "interval": index, **point, "phase": point.get("phase", "action")})
    return stream.getvalue()


def validate_results(result: dict[str, Any]) -> None:
    if result.get("schema_version") not in (2, 3, SCHEMA_VERSION):
        raise ValueError("Unsupported result schema")
    expected_trials = result.get("trials_per_block", TRIALS_PER_BLOCK)
    if expected_trials != (1 if result.get("research_screen") is True else TRIALS_PER_BLOCK):
        raise ValueError("Invalid research screen/trial count")
    for field in ("style_id", "seed"):
        if type(result[field]) is not int:
            raise ValueError(f"Invalid {field}")
    if not isinstance(result["audio_s"], (float, int)) or not math.isfinite(result["audio_s"]) or result["audio_s"] <= 0:
        raise ValueError("Invalid reference audio duration")
    for mode in result["modes"]:
        if not isinstance(mode["key"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", mode["key"]):
            raise ValueError("Invalid mode key")
        if type(mode["threads"]) is not int or not 1 <= mode["threads"] <= 65535:
            raise ValueError("Invalid inference thread count")
        if not all(isinstance(mode[field], str) for field in ("label", "backend")):
            raise ValueError("Invalid mode description")
    modes = {mode["key"]: mode for mode in result["modes"]}
    if not modes or len(modes) != len(result["modes"]):
        raise ValueError("Missing or duplicate modes")
    if checks := result.get("output_checks"):
        if checks["reference_mode"] not in modes or set(checks["modes"]) != set(modes):
            raise ValueError("Output checks do not match measured modes")
        if type(checks["reference_deterministic"]) is not bool:
            raise ValueError("Invalid output repeatability result")
        optional_checks = [checks[key] for key in ("fixed_control_repeat", "xnnpack_vs_fixed_control", "xnnpack_reference_repeat", "combined_vs_xnnpack", "combined_vs_fixed_control") if key in checks]
        for check in [checks["reference_repeat"], *checks["modes"].values(), *optional_checks, *checks.get("per_mode_repeat", {}).values()]:
            if check["finite"] is not True or check["fp32_samples"] <= 0:
                raise ValueError("Invalid FP32 output check")
            for field in ("pcm_exact", "fp32_exact"):
                if type(check[field]) is not bool:
                    raise ValueError("Invalid output exactness result")
            for field in ("pcm_changed_samples", "pcm_max_abs_lsb", "pcm_rmse_lsb", "fp32_changed_samples", "fp32_max_abs", "fp32_rmse", "fp32_relative_rmse"):
                if not isinstance(check[field], (int, float)) or not math.isfinite(check[field]) or check[field] < 0:
                    raise ValueError("Invalid output error statistic")
    by_block: dict[tuple[str, int], list[dict[str, Any]]] = {}
    orders = []
    for trial in result["trials"]:
        if trial["mode"] not in modes or trial["threads"] != modes[trial["mode"]]["threads"]:
            raise ValueError("Trial does not match its configured mode")
        for field in ("elapsed_s", "audio_s", "rtf"):
            if not isinstance(trial[field], (int, float)) or not math.isfinite(trial[field]) or trial[field] <= 0:
                raise ValueError(f"Invalid {field}")
        if not math.isclose(trial["rtf"], trial["elapsed_s"] / trial["audio_s"], rel_tol=1e-9):
            raise ValueError("RTF does not match measured duration")
        for field in ("cpu_time_s", "cpu_window_s", "cpu_avg_cores", "cpu_percent"):
            if not isinstance(trial[field], (float, int)) or not math.isfinite(trial[field]) or trial[field] < 0:
                raise ValueError(f"Invalid {field}")
        if trial["cpu_window_s"] <= 0 or type(trial["cpu_incomplete"]) is not bool:
            raise ValueError("Invalid CPU observation window/coverage")
        for field in ("cpu_samples", "cpu_processes"):
            if type(trial[field]) is not int or trial[field] < 1:
                raise ValueError(f"Invalid {field}")
        if not math.isclose(trial["cpu_avg_cores"], trial["cpu_time_s"] / trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("CPU core equivalent does not match the observation window")
        if not math.isclose(trial["cpu_percent"], 100 * trial["cpu_avg_cores"], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("CPU percent does not match the one-core normalization")
        if result["schema_version"] >= 3:
            trace = trial["cpu_trace"]
            if not trace or len(trace) != trial["cpu_samples"] - 1:
                raise ValueError("CPU trace does not match the number of snapshots")
            previous_end = None
            incomplete = False
            phases = []
            for point in trace:
                phase = point.get("phase", "action")
                if phase not in ("pre", "action", "post"):
                    raise ValueError("Invalid CPU observation phase")
                if phases and phase != phases[-1]:
                    incomplete = False
                for key in ("start_s", "end_s", "cpu_time_s", "cpu_percent"):
                    if not isinstance(point[key], (int, float)) or not math.isfinite(point[key]):
                        raise ValueError(f"Invalid CPU interval {key}")
                span = point["end_s"] - point["start_s"]
                if span <= 0 or point["cpu_time_s"] < 0 or point["cpu_percent"] < 0:
                    raise ValueError("Invalid CPU interval duration/counter")
                if previous_end is not None and not math.isclose(point["start_s"], previous_end, abs_tol=1e-9):
                    raise ValueError("CPU intervals must be contiguous")
                if not math.isclose(point["cpu_percent"], 100 * point["cpu_time_s"] / span, rel_tol=1e-8, abs_tol=1e-8):
                    raise ValueError("CPU interval percent does not match its actual duration")
                if type(point["cpu_processes"]) is not int or not 1 <= point["cpu_processes"] <= trial["cpu_processes"]:
                    raise ValueError("Invalid CPU interval process count")
                if type(point["cpu_incomplete"]) is not bool or incomplete and not point["cpu_incomplete"]:
                    raise ValueError("CPU interval coverage cannot recover after a lost process")
                incomplete = point["cpu_incomplete"]
                previous_end = point["end_s"]
                phases.append(phase)
            action_trace = [point for point in trace if point.get("phase", "action") == "action"]
            if not action_trace or action_trace[-1]["cpu_incomplete"] != trial["cpu_incomplete"]:
                raise ValueError("Action CPU coverage does not match its headline result")
            if not action_trace or not math.isclose(sum(point["cpu_time_s"] for point in action_trace), trial["cpu_time_s"], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Action CPU intervals do not sum to headline CPU time")
            if not math.isclose(sum(point["end_s"] - point["start_s"] for point in action_trace), trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Action CPU intervals do not match headline CPU window")
            if result["schema_version"] >= 4:
                if phases != sorted(phases, key={"pre": 0, "action": 1, "post": 2}.get) or set(phases) != {"pre", "action", "post"}:
                    raise ValueError("CPU trace must contain ordered pre/action/post phases")
                for phase in ("pre", "post"):
                    seconds = sum(point["end_s"] - point["start_s"] for point in trace if point["phase"] == phase)
                    if seconds < CPU_PADDING_S - 1e-6:
                        raise ValueError("CPU padding does not contain a full observed second")
        by_block.setdefault((trial["mode"], trial["block"]), []).append(trial)
        orders.append(trial["order"])
    if sorted(orders) != list(range(1, len(orders) + 1)):
        raise ValueError("Trial order must be unique and consecutive")
    for mode in modes:
        for block in range(1, BLOCKS_PER_MODE + 1):
            trials = by_block.get((mode, block), [])
            if sorted(row["trial"] for row in trials) != list(range(1, expected_trials + 1)):
                raise ValueError(f"Incomplete block: {mode} / {block}")
            positions = sorted(row["order"] for row in trials)
            if positions != list(range(positions[0], positions[0] + expected_trials)):
                raise ValueError("Trials within a block must be consecutive")
    if len(by_block) != len(modes) * BLOCKS_PER_MODE:
        raise ValueError("Unexpected block")
    if "schedule" in result:
        actual = [{"mode": row["mode"], "number": row["block"]} for row in sorted(result["trials"], key=lambda row: row["order"]) if row["trial"] == 1]
        if actual != result["schedule"]:
            raise ValueError("Recorded block schedule differs from actual trials")
        if result.get("schedule_method", "").startswith("seeded position-balanced"):
            for mode in modes:
                positions = [index % len(modes) for index, block in enumerate(actual) if block["mode"] == mode]
                counts = [positions.count(position) for position in range(len(modes))]
                if max(counts) - min(counts) > 1:
                    raise ValueError("Settings pass is not position-balanced")


def run_schedule(modes: list[Mode], seed: int,
                 synthesize: Callable[[Mode], tuple[float, float, CpuMeasurement]],
                 log: list[dict[str, Any]], *, balanced: bool = False,
                 checkpoint: Callable[[list[Trial]], None] | None = None) -> list[Trial]:
    """Only one callback runs at a time; all model initialization is external."""
    schedule = make_schedule(modes, seed, balanced=balanced)
    indexed = {mode.key: mode for mode in modes}
    trials = []
    for block in schedule:
        mode = indexed[block.mode]
        progress(f"{mode.label}: block {block.number}/{BLOCKS_PER_MODE}")
        for repetition in range(1, TRIALS_PER_BLOCK + 1):
            elapsed, duration, cpu = synthesize(mode)
            if not (math.isfinite(elapsed) and elapsed > 0 and math.isfinite(duration) and duration > 0):
                raise RuntimeError(f"{mode.label} returned an invalid measurement")
            trials.append(Trial(len(trials) + 1, mode.key, block.number, repetition,
                                mode.threads, elapsed, duration, elapsed / duration, **asdict(cpu)))
            if checkpoint is not None:
                checkpoint(trials)
            coverage = " (CPU incomplete)" if cpu.cpu_incomplete else ""
            progress(f"Trial {len(trials)}/{len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK}: {mode.label}, block {block.number}, {repetition}/{TRIALS_PER_BLOCK}; synthesis {elapsed:.3f}s, CPU {cpu.cpu_percent:.1f}%{coverage}")
        log.append({"event": "block_completed", "mode": mode.key,
                    "block": block.number, "samples": TRIALS_PER_BLOCK})
    return trials


def svg_results(result: dict[str, Any]) -> str:
    modes = result["modes"]
    rows = result["trials"]
    width, height = 880, max(230, 85 + 66 * len(modes))
    left, right, top, bottom = 220, 30, 24, 45
    maximum = max(row["elapsed_s"] for row in rows) * 1.12
    plot_width = width - left - right
    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"]
    output = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="各モードの合成時間。点は全試行、太線は中央値">']
    for tick in range(6):
        value = maximum * tick / 5
        x = left + value / maximum * plot_width
        output.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height-bottom}" stroke="#e2e6ec"/>')
        output.append(f'<text x="{x:.2f}" y="{height-20}" text-anchor="middle">{value:.2f}</text>')
    for index, mode in enumerate(modes):
        y = top + 33 + index * 66
        samples = [row for row in rows if row["mode"] == mode["key"]]
        color = colors[index % len(colors)]
        output.append(f'<text x="{left-15}" y="{y+5}" text-anchor="end">{html.escape(mode["label"])}</text>')
        for number, sample in enumerate(samples):
            x = left + sample["elapsed_s"] / maximum * plot_width
            dy = ((number % 5) - 2) * 5
            label = html.escape(f'Block {sample["block"]}, trial {sample["trial"]}: {sample["elapsed_s"]:.6f} s, RTF {sample["rtf"]:.6f}')
            output.append(f'<circle cx="{x:.2f}" cy="{y+dy}" r="3.5" fill="{color}" fill-opacity="0.65"><title>{label}</title></circle>')
        median = statistics.median(row["elapsed_s"] for row in samples)
        x = left + median / maximum * plot_width
        interval = block_bootstrap_median_ci([(row["block"], row["elapsed_s"]) for row in samples])
        if interval:
            low, high = (left + interval[key] / maximum * plot_width for key in ("low", "high"))
            output.append(f'<path class="timing-ci" d="M{low:.2f},{y}H{high:.2f}M{low:.2f},{y-9}V{y+9}M{high:.2f},{y-9}V{y+9}" stroke="{color}" stroke-width="1.8" fill="none"><title>中央値の参考95%区間: {interval["low"]:.6f}–{interval["high"]:.6f}秒（3ブロックbootstrap）</title></path>')
        output.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{y-17}" y2="{y+17}" stroke="{color}" stroke-width="3"><title>Median: {median:.6f} s</title></line>')
    output.append(f'<text x="{left+plot_width/2}" y="{height-2}" text-anchor="middle">合成時間（秒、短いほど速い）</text></svg>')
    return "".join(output)


def svg_sequence(result: dict[str, Any]) -> str:
    rows = sorted(result["trials"], key=lambda row: row["order"])
    modes = result["modes"]
    colors = {mode["key"]: color for mode, color in zip(modes, ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"] * len(modes))}
    width, height, left, top = 880, 180, 65, 15
    plot_width, plot_height = width - left - 25, height - top - 40
    maximum = max(row["rtf"] for row in rows) * 1.1
    output = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="測定順とRTF">']
    for tick in range(4):
        value = maximum * tick / 3
        y = top + plot_height - value / maximum * plot_height
        output.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-25}" y2="{y:.2f}" stroke="#e2e6ec"/><text x="{left-10}" y="{y+4:.2f}" text-anchor="end">{value:.2f}</text>')
    for row in rows:
        x = left + (row["order"] - 1) / max(1, len(rows) - 1) * plot_width
        y = top + plot_height - row["rtf"] / maximum * plot_height
        output.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{colors[row["mode"]]}"><title>#{row["order"]}: {html.escape(row["mode"])} / RTF {row["rtf"]:.6f}</title></circle>')
    output.append(f'<text x="{left}" y="{height-18}" text-anchor="middle">1</text><text x="{width-25}" y="{height-18}" text-anchor="middle">{len(rows)}</text><text x="{width/2}" y="{height-2}" text-anchor="middle">測定順（縦軸：RTF）</text></svg>')
    return "".join(output)


def aggregate_cpu_profiles(result: dict[str, Any], bin_seconds: float = 0.25) -> list[dict[str, Any]]:
    """Actual request-relative pre/action/post; pointwise block-bootstrap CI."""
    if not math.isfinite(bin_seconds) or bin_seconds <= 0:
        raise ValueError("CPU profile bin duration must be positive")
    profiles = []
    padded = result["schema_version"] >= 4
    minimum = -CPU_PADDING_S if padded else 0.0
    for mode in result["modes"]:
        trials = [row for row in result["trials"] if row["mode"] == mode["key"]]
        phase_counts = {phase: 0 for phase in ("pre", "action", "post")}
        trial_segments = []
        response_ends = []
        for trial in trials:
            segments = []
            for phase in phase_counts:
                intervals = [point for point in trial.get("cpu_trace", []) if point.get("phase", "action") == phase]
                if not intervals or any(point["cpu_incomplete"] for point in intervals):
                    continue
                phase_counts[phase] += 1
                if phase == "action":
                    response_ends.append(intervals[-1]["end_s"])
                post_limit = intervals[0]["start_s"] + CPU_PADDING_S if phase == "post" else math.inf
                for point in intervals:
                    start, end = point["start_s"], min(point["end_s"], post_limit)
                    rate = point["cpu_time_s"] / (point["end_s"] - point["start_s"])
                    if padded and phase == "action":
                        start = max(0.0, start)
                    elif phase == "pre":
                        end = min(0.0, end)
                    if end > start:
                        segments.append((start, end, rate))
            trial_segments.append((trial["block"], segments))
        maximum = max((end for _, segments in trial_segments for _, end, _ in segments), default=0.0)
        count = max(0, math.ceil((maximum - minimum) / bin_seconds))
        contributions: list[list[tuple[int, float, float]]] = [[] for _ in range(count)]
        for block, segments in trial_segments:
            cpu, observed = [0.0] * count, [0.0] * count
            for start, end, rate in segments:
                start, end = max(minimum, start), min(maximum, end)
                index = max(0, math.floor((start - minimum) / bin_seconds))
                while index < count and minimum + index * bin_seconds < end:
                    left = minimum + index * bin_seconds
                    overlap = max(0.0, min(end, left + bin_seconds) - max(start, left))
                    cpu[index] += rate * overlap
                    observed[index] += overlap
                    index += 1
            for index, seconds in enumerate(observed):
                if seconds > 0:
                    contributions[index].append((block, cpu[index], seconds))
        points = []
        for index, entries in enumerate(contributions):
            if not entries:
                continue
            left, right = minimum + index * bin_seconds, min(maximum, minimum + (index + 1) * bin_seconds)
            cpu, seconds = math.fsum(entry[1] for entry in entries), math.fsum(entry[2] for entry in entries)
            samples = [(block, 100 * value / observed) for block, value, observed in entries]
            full_count = sum(observed >= right - left - 1e-9 for _, _, observed in entries)
            interval = block_bootstrap_median_ci(samples) if full_count == len(trials) else None
            points.append({"start_s": left, "end_s": right, "cpu_percent": 100 * cpu / seconds,
                           "median_percent": statistics.median(value for _, value in samples),
                           "ci_low_percent": interval["low"] if interval else None,
                           "ci_high_percent": interval["high"] if interval else None,
                           "cpu_time_s": cpu, "observed_s": seconds, "trial_count": len(entries),
                           "full_trial_count": full_count, "contributing_blocks": len({block for block, _ in samples})})
        profiles.append({"mode": mode["key"], "label": mode["label"], "complete_trials": phase_counts["action"],
                         "phase_complete_trials": phase_counts, "total_trials": len(trials), "bin_seconds": bin_seconds,
                         "response_end_median_s": statistics.median(response_ends) if response_ends else None,
                         "response_end_min_s": min(response_ends) if response_ends else None,
                         "response_end_max_s": max(response_ends) if response_ends else None,
                         "points": points})
    return profiles


def cpu_profile_html(result: dict[str, Any]) -> str:
    if result["schema_version"] < 3:
        return ""
    profiles = aggregate_cpu_profiles(result)
    padded = result["schema_version"] >= 4
    xmin = -CPU_PADDING_S if padded else 0.0
    xmax = max((point["end_s"] for row in profiles for point in row["points"]), default=0.25)
    ymax = max(100, math.ceil(max((point["ci_high_percent"] if point["ci_high_percent"] is not None else point["median_percent"]
                                  for row in profiles for point in row["points"]), default=0) / 100) * 100)
    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"]
    figures = []
    for index, profile in enumerate(profiles):
        left, top, width, height = 52, 30, 365, 140
        def x(value: float) -> float:
            return left + (value - xmin) / (xmax - xmin) * width
        def y(value: float) -> float:
            return top + height - value / ymax * height
        color = colors[index % len(colors)]
        parts = [f'<svg class="cpu-profile" data-mode="{profile["mode"]}" viewBox="0 0 440 215" role="img" aria-label="{html.escape(profile["label"], quote=True)} CPU時系列、全試行の中央値と参考95%区間"><title>{html.escape(profile["label"])}: {profile["total_trials"]}回／3ブロック</title>',
                 f'<text x="{left}" y="16">{html.escape(profile["label"])}</text>']
        if padded:
            parts.append(f'<rect x="{left}" y="{top}" width="{x(0)-left:.2f}" height="{height}" fill="#edf0f5"/>')
        for tick in range(3):
            value = ymax * tick / 2
            parts.append(f'<line x1="{left}" y1="{y(value):.2f}" x2="{left+width}" y2="{y(value):.2f}" stroke="#e2e6ec"/><text x="{left-7}" y="{y(value)+4:.2f}" text-anchor="end">{value:.0f}%</text>')
        for tick in range(5):
            value = xmax * tick / 4
            parts.append(f'<text x="{x(value):.2f}" y="190" text-anchor="middle">{value:.1f}</text>')
        if padded:
            parts.append(f'<text x="{left}" y="190" text-anchor="end">−1</text>')
        if profile["response_end_median_s"] is not None:
            a, b, median = (x(profile[key]) for key in ("response_end_min_s", "response_end_max_s", "response_end_median_s"))
            parts.append(f'<rect class="response-end-range" x="{a:.2f}" y="{top}" width="{max(0.5,b-a):.2f}" height="{height}" fill="#8793a1" fill-opacity="0.16"/>')
            parts.append(f'<line class="response-end-median" x1="{median:.2f}" x2="{median:.2f}" y1="{top}" y2="{top+height}" stroke="#647082" stroke-dasharray="3 3"><title>応答直後の採取境界: 中央値{profile["response_end_median_s"]:.3f}秒、範囲{profile["response_end_min_s"]:.3f}–{profile["response_end_max_s"]:.3f}秒</title></line>')
        previous, previous_end = None, None
        for point in profile["points"]:
            partial = point["full_trial_count"] < profile["total_trials"]
            opacity, dash = ("0.45", ' stroke-dasharray="3 2"') if partial else ("1", "")
            a, b = x(point["start_s"]), x(point["end_s"])
            if point["ci_low_percent"] is not None:
                parts.append(f'<rect class="cpu-ci" x="{a:.2f}" y="{y(point["ci_high_percent"]):.2f}" width="{b-a:.2f}" height="{y(point["ci_low_percent"])-y(point["ci_high_percent"]):.2f}" fill="{color}" fill-opacity="0.18"/>')
            path = f'M{a:.2f},{y(point["median_percent"]):.2f}L{b:.2f},{y(point["median_percent"]):.2f}'
            if previous is not None and math.isclose(previous_end, point["start_s"], abs_tol=1e-9):
                path = f'M{a:.2f},{y(previous):.2f}L{a:.2f},{y(point["median_percent"]):.2f}' + path
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" opacity="{opacity}"{dash}/>')
            interval = (f'{point["ci_low_percent"]:.1f}–{point["ci_high_percent"]:.1f}%' if point["ci_low_percent"] is not None else 'なし（全試行の区間全体を観測できず）')
            title = f'{point["start_s"]:.2f}–{point["end_s"]:.2f}秒: 中央値{point["median_percent"]:.1f}%, 参考95%区間{interval}, 寄与n={point["trial_count"]}, 区間全体の観測n={point["full_trial_count"]}'
            parts.append(f'<rect x="{a:.2f}" y="{top}" width="{max(1,b-a):.2f}" height="{height}" fill="transparent"><title>{html.escape(title)}</title></rect>')
            previous, previous_end = point["median_percent"], point["end_s"]
        axis_caption = "合成要求開始からの秒数（前後も同じ軸）" if padded else "合成要求開始からの秒数"
        parts.append(f'<text x="{left+width/2}" y="211" text-anchor="middle">{axis_caption}</text></svg>')
        tail = profile["points"][-1]["trial_count"] if profile["points"] else 0
        figures.append('<figure>' + ''.join(parts) + f'<figcaption>全{profile["total_trials"]}回を集計 · 末尾の寄与 n={tail}</figcaption></figure>')
    payload = json.dumps(profiles, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    span_note = "開始前1秒から各試行の応答完了後1秒までを同じ実秒軸に表示。" if padded else "合成中の観測を要求開始からの実秒軸に表示。"
    return '<h2>CPU 使用率の時間推移（モード別集計）</h2><p>中央値・参考95%区間（15回／3ブロック、各時点）</p><div class="cpu-grid">' + ''.join(figures) + '</div><p class="cpu-caption">' + span_note + '縦の点線は応答直後の採取境界の中央値、灰色は最短〜最長。95%帯は全15回で区間全体を観測できた箇所だけに表示し、末尾は実測の中央値とnのみ（0埋めなし）。100%＝論理1コア。平均CPU表は合成中のみ。</p><script id="cpu-profile-data" type="application/json">' + payload + '</script>'


def render_report(result: dict[str, Any], target: Path) -> None:
    validate_results(result)
    raw_csv = trial_csv(result["trials"])
    profile = cpu_profile_html(result)
    spectra = spectrograms_html(result)
    profile_csv = (f'<details><summary>CPU 時系列（CSV）</summary><button data-csv="cpu-csv" data-filename="voicevox-cpu-timeseries.csv" type="button">CSV を保存</button><pre id="cpu-csv">{html.escape(cpu_trace_csv(result["trials"]))}</pre></details>'
                   if result["schema_version"] >= 3 else "")
    summaries = []
    checks = result.get("output_checks")
    output_header = "<th>PCM / FP32</th>" if checks else ""
    for mode in result["modes"]:
        trials = [row for row in result["trials"] if row["mode"] == mode["key"]]
        cpu_percent = weighted_cpu_percent(trials)
        interval = block_bootstrap_median_ci([(row["block"], row["elapsed_s"]) for row in trials])
        output_cell = ""
        if checks:
            check = checks["modes"][mode["key"]]
            status = ("一致" if check["pcm_exact"] else "差分あり") + " / " + ("一致" if check["fp32_exact"] else "差分あり")
            if not checks["reference_deterministic"]:
                status = "基準が非決定的"
            output_cell = f"<td>{status}</td>"
        summaries.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
            mode["label"], mode["threads"], len(trials),
            f'{statistics.median(row["elapsed_s"] for row in trials):.3f}',
            f'{statistics.median(row["rtf"] for row in trials):.3f}',
            "—" if cpu_percent is None else f"{cpu_percent:.1f}%",
            f'{interval["low"]:.3f}–{interval["high"]:.3f}' if interval else "—",
        )) + output_cell + "</tr>")
    report_environment = {**result["environment"], "cpu_plot_aggregation": "actual request-start axis including post; 0.25s per-trial overlap-weighted rates, then median; nominal pointwise 95% whole-block percentile bootstrap, 27 draws; no tail CI without 15 fully observed trials",
                          "report_script_sha256": sha256_file(Path(__file__)),
                          "statistics": "15 trials in 3 intact blocks; all 27 draws of 3 blocks with replacement; median estimator; linearly interpolated 2.5/97.5 percentiles; exploratory, only 3 clusters"}
    env_rows = "".join(f"<tr><th>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>" for key, value in report_environment.items())
    mode_rows = "".join(f'<li>{html.escape(mode["label"])}: {mode["threads"]} inference thread(s), {html.escape(mode["backend"])}</li>' for mode in result["modes"])
    note_values = list(result.get("notes", []))
    incomplete = sum(row["cpu_incomplete"] for row in result["trials"])
    if incomplete:
        note_values.append(f"CPU追跡が不完全な{incomplete}試行はCPU平均から除外。試行のCPU総量は観測できた下限値で、時系列では区間への帰属にも不確実性あり。")
    if checks and not checks["reference_deterministic"]:
        note_values.append("基準の繰り返し出力が一致しないため、この実行では精度維持の判定を行わない。")
    output_details = (f'<details><summary>出力一致の検証</summary><p>同じ実行の {html.escape(checks["reference_mode"])} が基準。PCMは整数16bit、FP32はPCM化前。完全一致のみ一致と表示し、誤差の許容しきい値は設定しない。</p><pre>{html.escape(json.dumps(checks, ensure_ascii=False, indent=2))}</pre></details>'
                      if checks else "")
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in note_values)
    duration = result["audio_s"]
    log_lines = "\n".join(json.dumps(event, ensure_ascii=False) for event in result.get("log", []))
    ordering = ("乱数で基本順とラウンド順を決め、各位置の回数差を最大1に制限。各ペアの前後順は両方を含む" if result.get("schedule_method", "").startswith("seeded position-balanced")
                else "各ラウンドでモード順をシャッフル")
    document = f'''<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOICEVOX CORE benchmark</title>
<style>
:root{{font-family:system-ui,-apple-system,sans-serif;color:#202936;background:#fff;font-size:15px;line-height:1.5}}body{{max-width:960px;margin:36px auto;padding:0 24px}}h1{{font-size:24px;letter-spacing:-.03em;margin:0 0 6px}}h2{{font-size:17px;margin:28px 0 10px}}p{{margin:6px 0 18px;color:#546170}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:9px 12px;border-bottom:1px solid #e2e6ec;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{font-weight:600;background:#f6f8fa}}figure{{margin:18px 0}}figcaption{{color:#546170;font-size:13px}}svg{{width:100%;height:auto}}svg text{{font-size:12px;fill:#546170}}details{{margin:18px 0;border-top:1px solid #d8dee7;padding-top:12px}}summary{{cursor:pointer;font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f6f8fa;padding:14px;max-height:420px;overflow:auto}}button{{font:inherit;background:#fff;border:1px solid #a8b4c4;border-radius:4px;padding:6px 12px;cursor:pointer;margin-top:12px}}.cpu-grid,.spectrogram-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 12px}}.cpu-grid figure,.spectrogram-grid figure{{margin:0}}.spectrogram figcaption{{min-height:3em}}.spectrogram img{{display:block;width:100%;height:auto}}.cpu-caption{{font-size:12px;margin-top:12px}}.scroll{{overflow-x:auto}}.environment th{{text-align:left;width:34%}}.environment td{{text-align:left;overflow-wrap:anywhere}}ul{{padding-left:22px}}@media(max-width:600px){{.cpu-grid,.spectrogram-grid{{grid-template-columns:1fr}}body{{margin:20px auto;padding:0 14px}}th,td{{padding:7px}}}}
</style>
<h1>VOICEVOX CORE benchmark</h1>
<p>{html.escape(result["created_at"])} · 音声 {duration:.3f} 秒 · 各モード 5 回 × 3 ブロック</p>
<p>合成時間：中央値・参考95%区間（15回／3ブロック）</p>
<div class="scroll"><table><thead><tr><th>モード</th><th>スレッド</th><th>回数</th><th>中央値（秒）</th><th>中央値 RTF</th><th>平均 CPU</th><th>参考95%区間（秒）</th>{output_header}</tr></thead><tbody>{''.join(summaries)}</tbody></table></div>
<figure>{svg_results(result)}<figcaption>点は15回の各試行、太線は中央値、横の誤差棒は参考95%区間（3ブロックbootstrap）。RTF = 合成時間 ÷ 出力音声の長さ</figcaption></figure>
<p class="cpu-caption">3ブロックしかないため、95%区間は参考値です。安定した95%被覆を保証するものではありません。</p>
<details><summary>95%区間の計算方法</summary><p>5回連続のブロックを分割せず、3ブロックを3個復元抽出する全27通りで中央値を再計算。その分布の2.5%・97.5%分位を線形補間して表示する名目95%percentile区間です。15回を独立標本とは扱いません。CPUは5本の曲線をブロック単位で再抽出し、各時点の中央値について同じ計算を行います。点ごとの区間であり、曲線全体を同時に95%で覆う帯ではありません。以前の四分位帯（中央50%範囲）とは異なります。</p></details>
<figure>{svg_sequence(result)}</figure>
{profile}
{spectra}
<h2>測定条件</h2>
<ul><li>同一 sample.vvm・Style ID {result["style_id"]}・準備済み AudioQuery JSON を使用。CPU 推論のみ、辞書・テキスト解析なし</li>
<li>モデル初期化・ダウンロード・ビルド・AudioQuery 作成・ウォームアップ・音声保存は測定外。合成から WAV 生成までを測定</li>
<li>{ordering}。1 ブロック内は 5 回連続。3 ラウンド、seed = {result["seed"]}。モード間の同時実行なし</li>
<li>表示スレッド数は推論に設定した値。Web Worker 数ではない</li>
<li>CPU は対象プロセスと子孫の user＋system 時間 ÷ カウンター採取間の観測時間。100%＝論理1コア、各試行の時間で重み付けした平均</li>
<li>browser はモード別 Chromium 全体を集計（Python・制御用 Node は除外）。100ms ごとに追跡し、短命プロセスは取りこぼす場合あり</li>{mode_rows}{notes}</ul>
<details><summary>実行環境・ログ</summary><table class="environment">{env_rows}</table><pre>{html.escape(log_lines)}</pre></details>
{output_details}
<details><summary>生データ（CSV）</summary><button id="save-csv" data-csv="raw-csv" data-filename="voicevox-benchmark.csv" type="button">CSV を保存</button><pre id="raw-csv">{html.escape(raw_csv)}</pre></details>
{profile_csv}
<script>
document.querySelectorAll('button[data-csv]').forEach(button=>button.addEventListener('click',()=>{{
 const blob=new Blob([document.getElementById(button.dataset.csv).textContent],{{type:'text/csv;charset=utf-8'}});
 const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=button.dataset.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}}));
</script></html>'''
    atomic_write(target, document.encode("utf-8"))


# The generated Rust program uses no text analyzer or dictionary on either target.
RUST_SOURCE = r'''
use anyhow::{Context as _, ensure};
use std::{io::{self, BufRead, Write}, sync::{Mutex, OnceLock}};
use voicevox_core::{AccelerationMode, AudioQuery, StyleId, VoiceModelId, blocking::{Onnxruntime, Synthesizer, VoiceModelFile}};
static SYNTH: OnceLock<Synthesizer<()>> = OnceLock::new();
static QUERY: OnceLock<AudioQuery> = OnceLock::new();
static WAV: Mutex<Vec<u8>> = Mutex::new(Vec::new());
static RAW: Mutex<Vec<u8>> = Mutex::new(Vec::new());
static SPIN_OFF: OnceLock<bool> = OnceLock::new();
static FIXED_LENGTH: OnceLock<usize> = OnceLock::new();
static MODEL_ID: OnceLock<VoiceModelId> = OnceLock::new();
fn setup(runtime: &'static Onnxruntime, model: &str, query: &str, threads: u16, fixed_shape: bool, xnn_threads: u16, profile: bool) -> anyhow::Result<()> {
    let query: AudioQuery = serde_json::from_slice(&std::fs::read(query)?)?;
    query.validate()?;
    let padded_length = if fixed_shape { Some(Synthesizer::<()>::benchmark_vocoder_length(&query)?) } else { None };
    voicevox_core::__benchmark_fixed_shape::configure(padded_length, xnn_threads.into(), profile)?;
    let synth = Synthesizer::builder(runtime).acceleration_mode(AccelerationMode::Cpu).cpu_num_threads(if xnn_threads > 0 { 1 } else { threads }).build()?;
    let model = VoiceModelFile::open(model)?;
    MODEL_ID.set(model.id()).map_err(|_| anyhow::anyhow!("Model ID already set"))?;
    synth.load_voice_model(&model).perform()?;
    voicevox_core::__benchmark_fixed_shape::verify_loaded()?;
    FIXED_LENGTH.set(padded_length.unwrap_or(0)).map_err(|_| anyhow::anyhow!("Fixed-shape configuration already set"))?;
    SYNTH.set(synth).map_err(|_| anyhow::anyhow!("Already initialized"))?;
    QUERY.set(query).map_err(|_| anyhow::anyhow!("Already initialized"))?;
    Ok(())
}
fn synthesize(style: u32) -> anyhow::Result<()> {
    let synth = SYNTH.get().context("Not initialized")?;
    let query = QUERY.get().context("No AudioQuery")?;
    let wav = synth.synthesis(query, StyleId::new(style)).perform()?;
    ensure!(wav.len() >= 44 && &wav[..4] == b"RIFF" && &wav[8..12] == b"WAVE", "Invalid WAV");
    *WAV.lock().unwrap() = wav;
    Ok(())
}
fn raw_wave(style: u32) -> anyhow::Result<()> {
    let synth = SYNTH.get().context("Not initialized")?;
    let wave = synth.benchmark_raw_synthesis(QUERY.get().context("No AudioQuery")?, StyleId::new(style))?;
    ensure!(wave.iter().all(|sample| sample.is_finite()), "Nonfinite FP32 waveform");
    *RAW.lock().unwrap() = wave.iter().flat_map(|sample| sample.to_le_bytes()).collect();
    Ok(())
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_init(threads: u16, spin_off: bool, fixed_shape: bool, xnn_threads: u16, profile: bool) -> i32 {
    let result = browser_runtime(threads, spin_off, xnn_threads).and_then(|runtime| setup(runtime, "/sample.vvm", "/query.json", threads, fixed_shape, xnn_threads, profile));
    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
fn browser_runtime(threads: u16, spin_off: bool, xnn_threads: u16) -> anyhow::Result<&'static Onnxruntime> {
    #[cfg(feature="threaded")]
    {
        // ORT's pthread WASM build requires one global pool. Register it before
        // CORE creates its environment; the pinned ort crate automatically
        // disables per-session pools when a global pool is present.
        let base = unsafe { &*ort::sys::OrtGetApiBase() };
        let api = unsafe { (base.GetApi)(ort::sys::ORT_API_VERSION) };
        ensure!(!api.is_null(), "ORT API unavailable");
        ensure!(ort::set_api(unsafe { api.read() }), "ORT API already initialized");
        let pool = ort::environment::GlobalThreadPoolOptions::default()
            .with_intra_threads(if xnn_threads > 0 { 1 } else { threads.into() })?.with_inter_threads(1)?.with_spin_control(!spin_off)?;
        ensure!(ort::init().with_name("voicevox_benchmark").with_global_thread_pool(pool).commit(), "ORT environment already initialized");
        SPIN_OFF.set(spin_off).map_err(|_| anyhow::anyhow!("Spin configuration already set"))?;
    }
    #[cfg(not(feature="threaded"))]
    let _ = (threads, spin_off, xnn_threads);
    Ok(Onnxruntime::init_once()?)
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_synthesize(style: u32) -> i32 {
    match synthesize(style) { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_wav_ptr() -> *const u8 { WAV.lock().unwrap().as_ptr() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_wav_len() -> usize { WAV.lock().unwrap().len() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_raw(style: u32) -> i32 {
    match raw_wave(style) { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_raw_ptr() -> *const u8 { RAW.lock().unwrap().as_ptr() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_raw_len() -> usize { RAW.lock().unwrap().len() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_spin_off() -> i32 { i32::from(SPIN_OFF.get().copied().unwrap_or(false)) }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_fixed_length() -> usize { FIXED_LENGTH.get().copied().unwrap_or(0) }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_fixed_matches() -> usize { voicevox_core::__benchmark_fixed_shape::verified_sessions() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_xnn_threads() -> usize { voicevox_core::__benchmark_fixed_shape::xnn_threads() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_xnn_sessions() -> usize { voicevox_core::__benchmark_fixed_shape::xnn_sessions() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_finish_profile() -> i32 {
    let result = (|| -> anyhow::Result<()> {
        ensure!(voicevox_core::__benchmark_fixed_shape::profiling_enabled(), "Profiling is disabled");
        println!("BENCH_PROFILE_BEGIN"); io::stdout().flush()?;
        SYNTH.get().context("Not initialized")?.unload_voice_model(*MODEL_ID.get().context("No model ID")?)?;
        println!("BENCH_PROFILE_END"); io::stdout().flush()?;
        Ok(())
    })();
    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
fn main() {}
#[cfg(not(target_os="emscripten"))]
fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    ensure!(args.len() == 7, "Expected runtime, model, query, threads, style, wav destination");
    let runtime = Onnxruntime::load_once().filename(&args[1]).perform()?;
    let threads = args[4].parse()?;
    let style = args[5].parse()?;
    setup(runtime, &args[2], &args[3], threads, false, 0, false)?;
    println!("{}", serde_json::json!({"ready": true, "threads": threads}));
    io::stdout().flush()?;
    for line in io::stdin().lock().lines() {
        let line = line?;
        if line == "quit" { break; }
        if line == "raw-save" {
            raw_wave(style)?;
            let raw = RAW.lock().unwrap();
            std::fs::write(std::path::Path::new(&args[6]).with_extension("f32"), &*raw)?;
            println!("{}", serde_json::json!({"raw_bytes": raw.len()}));
            io::stdout().flush()?;
            continue;
        }
        ensure!(line == "synthesize" || line == "synthesize-save", "Unknown request");
        let start = std::time::Instant::now();
        synthesize(style)?;
        let elapsed_s = start.elapsed().as_secs_f64();
        let wav = WAV.lock().unwrap();
        if line == "synthesize-save" { std::fs::write(&args[6], &*wav)?; }
        println!("{}", serde_json::json!({"elapsed_s": elapsed_s, "wav_bytes": wav.len()}));
        io::stdout().flush()?;
    }
    Ok(())
}
'''

BROWSER_WORKER = r'''
let configuredThreads=1, threaded=false, ready=false, wasmStdout=[];
function failure(error){postMessage({error:String(error && error.stack || error)});}
onmessage=async ({data})=>{
 try{
  if(data.command==='init'){
   configuredThreads=data.threads;threaded=data.threaded;
   if(data.threaded && (!self.crossOriginIsolated || typeof SharedArrayBuffer==='undefined'))
    throw new Error('Multithreaded WASM requires cross-origin isolation and SharedArrayBuffer');
   globalThis.Module={noInitialRun:true,benchmarkPoolSize:Math.max(1,configuredThreads),mainScriptUrlOrBlob:new URL(data.module,self.location).href,
    locateFile:(path)=>new URL(path,new URL(data.module,self.location)).href,
    printErr:(...values)=>postMessage({log:values.join(' ')}),
    print:(...values)=>wasmStdout.push(values.join(' ')),
    onAbort:failure,
    onRuntimeInitialized:async()=>{
     try{
      for(const file of ['sample.vvm','query.json']){
       const response=await fetch(file==='sample.vvm' && data.model_url ? data.model_url : '/'+file);if(!response.ok)throw new Error('HTTP '+response.status+' '+file);
       Module.FS.writeFile('/'+file,new Uint8Array(await response.arrayBuffer()));
      }
      if(Module._bench_init(configuredThreads,Number(data.spin_off),Number(data.fixed_shape),data.xnn_threads||0,Number(data.profile))!==0)throw new Error('CORE initialization failed');
      Module.FS.unlink('/sample.vvm');Module.FS.unlink('/query.json');ready=true;
      const pthreads=threaded ? Module.PThread.runningWorkers.length : 0;
      if(threaded && configuredThreads>1 && pthreads<configuredThreads-1)
       throw new Error('ORT did not create the requested inference pthreads');
      postMessage({ready:true,threads:configuredThreads,shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads_created:pthreads,spin_off:Boolean(Module._bench_spin_off()),fixed_length:Module._bench_fixed_length(),fixed_matches:Module._bench_fixed_matches(),xnn_threads:Module._bench_xnn_threads(),xnn_sessions:Module._bench_xnn_sessions(),model_target:typeof Module._bench_vocoder_fixed_matches==='function'?'predictor':'vocoder',vocoder_fixed_matches:typeof Module._bench_vocoder_fixed_matches==='function'?Module._bench_vocoder_fixed_matches():Module._bench_fixed_matches(),vocoder_xnn_sessions:typeof Module._bench_vocoder_xnn_sessions==='function'?Module._bench_vocoder_xnn_sessions():Module._bench_xnn_sessions()});
     }catch(error){failure(error);}
    }};
   importScripts(data.module);
  }else if(data.command==='synthesize'){
   if(!ready)throw new Error('CORE not ready');
   const start=performance.now(),code=Module._bench_synthesize(data.style);
   const elapsed_s=(performance.now()-start)/1000;
   if(code!==0)throw new Error('Synthesis failed: '+code);
   const ptr=Module._bench_wav_ptr(),length=Module._bench_wav_len();
   if(!ptr||length<44)throw new Error('Invalid WAV');
   const result={elapsed_s,wav_bytes:length};
   if(data.save){result.wav=Module.HEAPU8.slice(ptr,ptr+length).buffer;postMessage(result,[result.wav]);}
   else postMessage(result);
  }else if(data.command==='raw'){
   if(!ready || Module._bench_raw(data.style)!==0)throw new Error('Untimed FP32 synthesis failed');
   const ptr=Module._bench_raw_ptr(),length=Module._bench_raw_len();
   if(!ptr||length<4||length%4)throw new Error('Invalid FP32 output');
   const raw=Module.HEAPU8.slice(ptr,ptr+length).buffer;postMessage({raw},[raw]);
  }else if(data.command==='finish-profile'){
   const start=wasmStdout.length;
   if(Module._bench_finish_profile()!==0)throw new Error('Failed to flush untimed provider profile');
   ready=false;postMessage({profile_text:wasmStdout.slice(start).join('\n')});
  }else if(data.command==='thread-state'){
   if(!ready)throw new Error('CORE not ready');
   postMessage({pthreads:threaded ? Module.PThread.runningWorkers.length : 0});
  }
 }catch(error){failure(error);}
};
'''

BROWSER_PAGE = r'''<!doctype html><meta charset="utf-8"><title>VOICEVOX benchmark worker</title>
<script>
let worker=null, pending=null;
window.logs=[];
window.request=(data)=>new Promise((resolve,reject)=>{
 if(pending) return reject(new Error('Concurrent requests are forbidden'));
 if(data.command==='init'){
  worker=new Worker('/worker.js');
  worker.onerror=(event)=>{if(pending){clearTimeout(pending.timer);pending.reject(new Error(event.message));pending=null;worker.terminate();}};
  worker.onmessage=({data})=>{
   if(data.log){logs.push(data.log);return;}
   if(!pending)return;
   const {resolve,reject,timer}=pending;clearTimeout(timer);pending=null;
   if(data.error)reject(new Error(data.error+'\n'+logs.slice(-20).join('\n')));
   else {if(data.wav)data.wav=Array.from(new Uint8Array(data.wav));if(data.raw)data.raw=Array.from(new Uint8Array(data.raw));resolve(data);}
  };
 }
 const timer=setTimeout(()=>{worker.terminate();pending=null;reject(new Error('Worker timed out after 10 minutes'));},600000);
 pending={resolve,reject,timer};worker.postMessage(data);
});
</script>'''


def prepared_query(target_seconds: float) -> dict[str, Any]:
    """Hand-authored benchmark phonemes, independent of text analysis/models.

    Repeated /konnichiwa/ is intentionally a fixed synthetic query, not claimed
    to represent a natural 10-second utterance. Measured WAV duration is used
    for RTF rather than assuming the requested duration is exact.
    """
    if not math.isfinite(target_seconds) or not 1 <= target_seconds <= 60:
        raise ValueError("--target-seconds must be between 1 and 60")
    phrase = {"moras": [
        {"text": "コ", "consonant": "k", "consonant_length": .07, "vowel": "o", "vowel_length": .12, "pitch": 5.4},
        {"text": "ン", "consonant": None, "consonant_length": None, "vowel": "N", "vowel_length": .12, "pitch": 5.6},
        {"text": "ニ", "consonant": "n", "consonant_length": .06, "vowel": "i", "vowel_length": .12, "pitch": 5.6},
        {"text": "チ", "consonant": "ch", "consonant_length": .07, "vowel": "i", "vowel_length": .12, "pitch": 5.6},
        {"text": "ワ", "consonant": "w", "consonant_length": .06, "vowel": "a", "vowel_length": .14, "pitch": 5.3}],
        "accent": 5, "pause_mora": {"text": "、", "consonant": None, "consonant_length": None, "vowel": "pau", "vowel_length": .15, "pitch": 0.0}, "is_interrogative": False}
    phrases = [json.loads(json.dumps(phrase)) for _ in range(8)]
    total = .2 + sum(m["vowel_length"] + (m["consonant_length"] or 0) for p in phrases for m in p["moras"]) + 8 * .15
    return {"accent_phrases": phrases, "speedScale": total / target_seconds, "pitchScale": 0.0, "intonationScale": 1.0, "volumeScale": 1.0, "prePhonemeLength": .1, "postPhonemeLength": .1, "outputSamplingRate": 24000, "outputStereo": False}


CORE_BENCH_HELPER = '''        // Untimed diagnostic only: uses the pinned Talk/StreamingTalk domain dispatch
        // up to decode(), before PCM conversion/resampling. Not called by synthesis().
        #[doc(hidden)]
        pub fn benchmark_raw_synthesis(&self, audio_query: &AudioQuery, style_id: StyleId) -> crate::Result<Vec<f32>> {
            let audio_query = audio_query.to_validated()?;
            let super::DecoderFeature { f0, phoneme } = audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);
            self.0.decode(f0.len(), super::PhonemeCode::num_phoneme(), &f0, phoneme.as_flattened(), style_id, ()).block_on()
        }

'''


CORE_FIXED_CONFIG = r'''
#[doc(hidden)]
pub mod __benchmark_fixed_shape {
    use std::sync::{
        OnceLock,
        atomic::{AtomicUsize, Ordering},
    };

    pub const VOCODER_SHA256_HEX: &str =
        "80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820";
    pub(crate) const VOCODER_SHA256: [u8; 32] = [0x80, 0xa8, 0x1f, 0xd0, 0x59, 0x8b, 0x6e, 0x7d, 0x4e, 0x21, 0xfe, 0xf7, 0x4e, 0x03, 0xfe, 0xd1, 0x4e, 0x22, 0xf1, 0x11, 0xc6, 0xb0, 0xf4, 0xc4, 0x45, 0x45, 0x61, 0xba, 0xae, 0x07, 0x58, 0x20];
    pub(crate) const VOCODER_BYTES: usize = 55_734_297;

    static REQUESTED: OnceLock<Option<usize>> = OnceLock::new();
    static VERIFIED_SESSIONS: AtomicUsize = AtomicUsize::new(0);
    static XNN_THREADS: OnceLock<usize> = OnceLock::new();
    static PROFILE: OnceLock<bool> = OnceLock::new();
    static XNN_SESSIONS: AtomicUsize = AtomicUsize::new(0);

    pub fn configure(padded_length: Option<usize>, xnn_threads: usize, profile: bool) -> anyhow::Result<()> {
        anyhow::ensure!((xnn_threads == 0 && !profile) || padded_length.is_some(), "Provider diagnostics require verified fixed vocoder shape");
        if let Some(n) = padded_length {
            anyhow::ensure!(n > 0, "Fixed vocoder length must be positive");
            let _ = i64::try_from(n)?;
        }
        REQUESTED
            .set(padded_length)
            .map_err(|_| anyhow::anyhow!("Benchmark configuration already initialized"))?;
        XNN_THREADS.set(xnn_threads).map_err(|_| anyhow::anyhow!("XNNPACK already configured"))?;
        PROFILE.set(profile).map_err(|_| anyhow::anyhow!("Profiling already configured"))?;
        Ok(())
    }

    pub fn xnn_threads() -> usize { XNN_THREADS.get().copied().unwrap_or(0) }
    pub fn profiling_enabled() -> bool { PROFILE.get().copied().unwrap_or(false) }
    pub fn xnn_sessions() -> usize { XNN_SESSIONS.load(Ordering::SeqCst) }
    pub(crate) fn record_xnn_session() -> anyhow::Result<()> {
        anyhow::ensure!(XNN_SESSIONS.fetch_add(1, Ordering::SeqCst) == 0, "Multiple XNNPACK sessions registered");
        Ok(())
    }

    pub(crate) fn requested_length() -> Option<usize> {
        REQUESTED.get().copied().flatten()
    }

    pub(crate) fn record_verified_session() -> anyhow::Result<()> {
        let old = VERIFIED_SESSIONS.fetch_add(1, Ordering::SeqCst);
        anyhow::ensure!(old == 0, "More than one fixed-shape vocoder session matched");
        Ok(())
    }

    pub fn verified_sessions() -> usize {
        VERIFIED_SESSIONS.load(Ordering::SeqCst)
    }

    pub fn verify_loaded() -> anyhow::Result<()> {
        let requested = REQUESTED
            .get()
            .ok_or_else(|| anyhow::anyhow!("Benchmark configuration was not initialized"))?;
        let expected = usize::from(requested.is_some());
        anyhow::ensure!(
            verified_sessions() == expected,
            "Expected {} verified fixed-shape vocoder session(s), got {}",
            expected,
            verified_sessions()
        );
        anyhow::ensure!(xnn_sessions() == usize::from(xnn_threads() > 0), "XNNPACK vocoder registration count mismatch");
        Ok(())
    }
}
'''

CORE_FIXED_HELPER = r'''
        #[doc(hidden)]
        pub fn benchmark_vocoder_length(audio_query: &AudioQuery) -> anyhow::Result<usize> {
            let audio_query = audio_query.to_validated()?;
            let super::DecoderFeature { f0, phoneme } =
                audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);
            let length = f0.len();
            let phoneme_size = super::PhonemeCode::num_phoneme();
            anyhow::ensure!(length > 0 && phoneme_size == 45, "Unexpected decoder query shape");
            anyhow::ensure!(phoneme.len() == length, "f0/phoneme frame count differs");

            // Full-range streaming synthesis passes all intermediate rows,
            // including MARGIN frames at each end, to the pinned vocoder.
            let n = length.checked_add(2 * super::MARGIN)
                .ok_or_else(|| anyhow::anyhow!("Vocoder length overflow"))?;
            let _ = i64::try_from(n)?;
            Ok(n)
        }

'''

CORE_FIXED_OPTION = r'''
        let benchmark_fixed_length =
            match (crate::__benchmark_fixed_shape::requested_length(), model) {
                (Some(n), ModelBytes::Onnx(bytes))
                    if bytes.len() == crate::__benchmark_fixed_shape::VOCODER_BYTES =>
                {
                    use sha2::{Digest as _, Sha256};
                    let digest: [u8; 32] = Sha256::digest(bytes).into();
                    (digest == crate::__benchmark_fixed_shape::VOCODER_SHA256).then_some(n)
                }
                _ => None,
            };
        if let Some(n) = benchmark_fixed_length {
            builder = builder
                .with_dimension_override("length", i64::try_from(n)?)
                .map_err(ort::Error::<()>::from)?
                .with_dimension_override("feats", 80)
                .map_err(ort::Error::<()>::from)?;
            if let Some(threads) = std::num::NonZeroUsize::new(crate::__benchmark_fixed_shape::xnn_threads()) {
                // Direct register calls the real C API and propagates errors;
                // the pinned binding's platform hint omits WASM.
                ort::ep::XNNPACK::default().with_intra_op_num_threads(threads).register(&mut builder)?;
            }
            if crate::__benchmark_fixed_shape::profiling_enabled() {
                builder = builder.with_profiling("bench-ep").map_err(ort::Error::<()>::from)?;
            }
        }

'''

CORE_FIXED_VERIFY = r'''
        if let Some(n) = benchmark_fixed_length {
            let n = i64::try_from(n)?;
            ensure!(sess.inputs().len() == 1, "Unexpected fixed vocoder input count");
            for (name, expected_type, expected_shape) in [
                ("spec", TensorElementType::Float32, vec![n, 80]),
            ] {
                let info = sess.inputs().iter()
                    .find(|input| input.name() == name)
                    .with_context(|| format!("Missing fixed vocoder input {name}"))?;
                let ValueType::Tensor { ty, shape, .. } = info.dtype() else {
                    bail!("Fixed vocoder input {name} is not a tensor");
                };
                ensure!(*ty == expected_type, "Unexpected fixed vocoder dtype for {name}");
                ensure!(
                    &shape[..] == expected_shape.as_slice(),
                    "Fixed vocoder shape mismatch for {}: expected {:?}, got {:?}",
                    name, expected_shape, shape
                );
            }
            ensure!(
                sess.outputs().len() == 1 && sess.outputs()[0].name() == "wave",
                "Unexpected fixed vocoder output"
            );
            let ValueType::Tensor { ty, .. } = sess.outputs()[0].dtype() else {
                bail!("Fixed vocoder output is not a tensor");
            };
            ensure!(*ty == TensorElementType::Float32, "Unexpected fixed vocoder output dtype");
            crate::__benchmark_fixed_shape::record_verified_session()?;
            if crate::__benchmark_fixed_shape::xnn_threads() > 0 {
                crate::__benchmark_fixed_shape::record_xnn_session()?;
            }
        }

'''


def patch_fixed_shape(source: Path) -> None:
    def insert(path: Path, marker: str, addition: str, token: str) -> None:
        content = path.read_text("utf-8")
        if token in content:
            if addition not in content or content.count(token) != 1:
                raise RuntimeError("Cached CORE fixed-shape patch differs from this script")
            return
        if content.count(marker) != 1:
            raise RuntimeError("Pinned CORE does not match a fixed-shape patch marker")
        path.write_text(content.replace(marker, addition + marker, 1), encoding="utf-8")
    crate = source / "crates/voicevox_core"
    manifest = crate / "Cargo.toml"
    content = manifest.read_text("utf-8")
    if "sha2.workspace = true" not in content:
        if content.count("[dependencies]\n") != 1 or re.search(r"^sha2\s*[.=]", content, re.M):
            raise RuntimeError("Unexpected CORE sha2 dependency declaration")
        manifest.write_text(content.replace("[dependencies]\n", "[dependencies]\nsha2.workspace = true\n", 1), encoding="utf-8")
    content = manifest.read_text("utf-8")
    ort_line = 'ort = { workspace = true, features = ["std", "ndarray", "tracing", "api-17", "alternative-backend"], default-features = false }'
    ort_xnn_line = ort_line.replace('"alternative-backend"', '"alternative-backend", "xnnpack"')
    if ort_xnn_line not in content:
        if content.count(ort_line) != 1:
            raise RuntimeError("Pinned CORE ort feature declaration differs")
        manifest.write_text(content.replace(ort_line, ort_xnn_line, 1), encoding="utf-8")
    library = crate / "src/lib.rs"
    content = library.read_text("utf-8")
    if "pub mod __benchmark_fixed_shape" not in content:
        library.write_text(content + "\n" + CORE_FIXED_CONFIG, encoding="utf-8")
    elif CORE_FIXED_CONFIG not in content:
        raise RuntimeError("Cached CORE fixed-shape configuration differs from this script")
    insert(crate / "src/synthesizer.rs", "        // Untimed diagnostic only: uses the pinned Talk/StreamingTalk domain dispatch\n",
           CORE_FIXED_HELPER, "pub fn benchmark_vocoder_length")
    runtime = crate / "src/core/infer/runtimes/onnxruntime.rs"
    insert(runtime, "        let sess = match model {\n", CORE_FIXED_OPTION, "let benchmark_fixed_length =")
    insert(runtime, "        let input_param_infos = sess\n", CORE_FIXED_VERIFY, "Unexpected fixed vocoder input count")


def write_wrapper(source: Path) -> None:
    synthesizer = source / "crates/voicevox_core/src/synthesizer.rs"
    contents = synthesizer.read_text("utf-8")
    if "pub fn benchmark_raw_synthesis" not in contents:
        marker = "        /// AudioQueryから直接WAVフォーマットで音声波形を生成する。\n"
        if contents.count(marker) != 1:
            raise RuntimeError("Pinned CORE synthesis source does not match the FP32 diagnostic patch")
        synthesizer.write_text(contents.replace(marker, CORE_BENCH_HELPER + marker, 1), encoding="utf-8")
    elif CORE_BENCH_HELPER not in contents:
        raise RuntimeError("Cached CORE FP32 diagnostic patch differs from this script")
    patch_fixed_shape(source)
    crate = source / "crates/voicevox_benchmark"
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text('''[package]
name = "voicevox_benchmark"
version = "0.0.0"
edition = "2024"
[features]
native = ["voicevox_core/load-onnxruntime"]
browser = ["voicevox_core/link-onnxruntime"]
threaded = []
[dependencies]
anyhow.workspace = true
serde_json.workspace = true
voicevox_core.workspace = true
ort.workspace = true
''', encoding="utf-8")
    (crate / "src/main.rs").write_text(RUST_SOURCE, encoding="utf-8")


def prepare_model(source: Path, destination: Path) -> None:
    import zipfile
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted((source / "model/sample.vvm").iterdir()):
            if path.is_file():
                info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                with path.open("rb") as input_file, archive.open(info, "w") as output_file:
                    shutil.copyfileobj(input_file, output_file)


class NativeRunner:
    def __init__(self, executable: Path, runtime: Path, model: Path, query: Path, threads: int, style: int, wav: Path):
        self.wav = wav
        self.process = subprocess.Popen([str(executable), str(runtime), str(model), str(query), str(threads), str(style), str(wav)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        import queue
        import threading
        self._lines: queue.Queue[str | None] = queue.Queue()
        def read_lines() -> None:
            for line in self.process.stdout:
                self._lines.put(line)
            self._lines.put(None)
        self._reader = threading.Thread(target=read_lines, daemon=True)
        self._reader.start()
        try:
            message = self._read()
            if not message.get("ready") or message.get("threads") != threads:
                raise RuntimeError("Native initialization did not confirm its thread setting")
        except Exception:
            self.close()
            raise
        self.duration = 0.0
        self.bytes = 0

    def _read(self) -> dict[str, Any]:
        import queue
        try:
            line = self._lines.get(timeout=600)
        except queue.Empty:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("Native runner timed out after 10 minutes") from None
        if line is None:
            raise RuntimeError(f"Native runner exited unexpectedly ({self.process.poll()})")
        return json.loads(line)

    def synthesize(self, save: bool = False) -> tuple[float, float]:
        self.process.stdin.write("synthesize-save\n" if save else "synthesize\n")
        self.process.stdin.flush()
        message = self._read()
        if save:
            self.duration = wav_duration(self.wav.read_bytes())
            self.bytes = message["wav_bytes"]
        if not self.duration or message["wav_bytes"] != self.bytes:
            raise RuntimeError("Native WAV length changed between trials")
        return message["elapsed_s"], self.duration

    def raw_wave(self) -> bytes:
        self.process.stdin.write("raw-save\n")
        self.process.stdin.flush()
        message = self._read()
        data = self.wav.with_suffix(".f32").read_bytes()
        if len(data) != message["raw_bytes"]:
            raise RuntimeError("Native FP32 length mismatch")
        return data

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.write("quit\n")
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()
        self._reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


class BrowserRunner:
    def __init__(self, browser: Any, url: str, module: str, threads: int, threaded: bool, style: int, wav: Path,
                 *, fixed_shape: bool = False, spin_off: bool = False, xnn_threads: int = 0, profile: bool = False, model_url: str | None = None, model_target: str = "vocoder"):
        self.page = browser.new_page()
        self.page.set_default_timeout(600_000)
        self.page.goto(url, wait_until="load")
        message = self.page.evaluate("data => request(data)", {"command": "init", "module": module, "threads": threads, "threaded": threaded, "fixed_shape": fixed_shape, "spin_off": spin_off, "xnn_threads": xnn_threads, "profile": profile, "model_url": model_url})
        if not message.get("ready") or message.get("threads") != threads or message.get("shared_memory") != threaded or message.get("spin_off") != spin_off:
            self.page.close()
            raise RuntimeError("Browser did not confirm the required thread/shared-memory configuration")
        if message.get("model_target") != model_target:
            raise RuntimeError("Compiled CORE target differs from manifest")
        self.model_target = model_target
        self.vocoder_fixed_matches = message["vocoder_fixed_matches"]
        self.vocoder_xnn_sessions = message["vocoder_xnn_sessions"]
        if model_target == "predictor" and (self.vocoder_fixed_matches or self.vocoder_xnn_sessions):
            raise RuntimeError("Predictor-only variant changed vocoder registration")
        self.style, self.wav, self.duration, self.bytes = style, wav, 0.0, 0
        self.shared_memory = message["shared_memory"]
        self.pthreads_created = message.get("pthreads_created", 0)
        self.spin_off = message["spin_off"]
        self.xnn_threads, self.xnn_sessions = message["xnn_threads"], message["xnn_sessions"]
        if self.xnn_threads != xnn_threads or self.xnn_sessions != int(xnn_threads > 0):
            self.page.close()
            raise RuntimeError("XNNPACK was not registered for exactly the requested vocoder session")
        self.fixed_length, self.fixed_matches = message["fixed_length"], message["fixed_matches"]
        if (fixed_shape and (self.fixed_length <= 0 or self.fixed_matches != 1)) or (not fixed_shape and (self.fixed_length != 0 or self.fixed_matches != 0)):
            self.page.close()
            raise RuntimeError("Fixed vocoder shape was not applied to exactly the requested session")

    def synthesize(self, save: bool = False) -> tuple[float, float]:
        message = self.page.evaluate("data => request(data)", {"command": "synthesize", "style": self.style, "save": save})
        if save:
            data = bytes(message["wav"])
            atomic_write(self.wav, data)
            self.duration = wav_duration(data)
            self.bytes = len(data)
        if not self.duration or message["wav_bytes"] != self.bytes:
            raise RuntimeError("Browser WAV length changed between trials")
        return message["elapsed_s"], self.duration

    def close(self) -> None:
        self.page.close()

    def raw_wave(self) -> bytes:
        message = self.page.evaluate("data => request(data)", {"command": "raw", "style": self.style})
        return bytes(message["raw"])

    def thread_state(self) -> int:
        return int(self.page.evaluate("() => request({command:'thread-state'})")["pthreads"])

    def finish_profile(self, profile_path: Path | None = None) -> dict[str, Any]:
        message = self.page.evaluate("() => request({command:'finish-profile'})")
        text = message["profile_text"]
        if text.count("BENCH_PROFILE_BEGIN") != 1 or text.count("BENCH_PROFILE_END") != 1:
            raise RuntimeError("Provider profile stdout markers are missing or ambiguous")
        events = json.loads(text.split("BENCH_PROFILE_BEGIN", 1)[1].split("BENCH_PROFILE_END", 1)[0].strip())
        if profile_path:
            atomic_write(profile_path, json.dumps(events, ensure_ascii=False, indent=2).encode())
        providers: dict[str, set[str]] = {}
        assignments: set[tuple[str, str, str]] = set()
        for event in events:
            if event.get("cat") == "Node" and event.get("name", "").endswith("_kernel_time") and event.get("args", {}).get("provider"):
                providers.setdefault(event["args"]["provider"], set()).add(event["name"])
                assignments.add((event["args"]["provider"], event["name"], event["args"].get("op_name", "")))
        return {"verified": bool(providers.get("XnnpackExecutionProvider")),
                "provider_kernel_counts": {key: len(nodes) for key, nodes in providers.items()},
                "provider_assignment_sha256": hashlib.sha256(json.dumps(sorted(assignments), separators=(",", ":")).encode()).hexdigest(),
                "profile_events": len(events), "profiling_scope": "separate untimed diagnostic browser only"}


def browser_engine_info(browser: Any) -> dict[str, Any]:
    session = browser.new_browser_cdp_session()
    try:
        version = session.send("Browser.getVersion")
        return {"product": version["product"], "js_version": version["jsVersion"]}
    finally:
        session.detach()


def revectorization_diagnostic_child(config_path: Path) -> None:
    from playwright.sync_api import sync_playwright
    config = json.loads(config_path.read_text("utf-8"))
    with sync_playwright() as playwright:
        options = dict(config["options"])
        options["args"] = ["--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if config["enable"] else "--js-flags=--no-wasm-revectorize,--trace-wasm-revectorize"]
        browser = playwright.chromium.launch(**options)
        runner = None
        try:
            info = browser_engine_info(browser)
            info["requested_js_flags"] = options["args"]
            xnnpack = config.get("xnnpack", False)
            runner = BrowserRunner(browser, config["url"], config.get("module", "/xnnpack/voicevox_benchmark.js" if xnnpack else "/mt/voicevox_benchmark.js"), config["threads"], True,
                                   config["style"], Path(config["wav"]), fixed_shape=config.get("fixed_shape", xnnpack), spin_off=config.get("spin_off", xnnpack),
                                   xnn_threads=config.get("xnn_threads", config["threads"] if xnnpack else 0), profile=config.get("profile", xnnpack),
                                   model_url=config.get("model_url"), model_target=config.get("model_target", "vocoder"))
            for _ in range(3):
                runner.synthesize(save=True)
            if xnnpack:
                pthreads = runner.thread_state()
                if pthreads != config["threads"] - 1:
                    raise RuntimeError("Combined diagnostic has unexpected thread-pool ownership")
                info["xnnpack_profile"] = {**runner.finish_profile(Path(config["profile_result"])),
                                           "fixed_length": runner.fixed_length, "fixed_matches": runner.fixed_matches,
                                           "pthreads": pthreads, "xnn_threads": runner.xnn_threads, "xnn_sessions": runner.xnn_sessions}
            atomic_write(Path(config["result"]), json.dumps(info).encode())
        finally:
            if runner:
                runner.close()
            browser.close()


def verify_revectorization(url: str, options: dict[str, Any], threads: int, style: int, work: Path,
                          trace_dir: Path | None = None, *, xnnpack: bool = False) -> dict[str, Any]:
    records = {}
    prefix = "combined" if xnnpack else "revec"
    for enable, key in ((False, "control"), (True, "candidate")):
        progress(f"Checking {prefix} {key} vector transformations in a separate untimed browser")
        config_path, result_path = work / f"{prefix}-{key}-config.json", work / f"{prefix}-{key}-result.json"
        atomic_write(config_path, json.dumps({"options": options, "url": url, "threads": threads, "style": style, "enable": enable,
                                            "xnnpack": xnnpack, "profile_result": str((trace_dir or work) / f"{key}-provider-profile.json"),
                                            "wav": str(work / f"{prefix}-{key}.wav"), "result": str(result_path)}).encode())
        env = dict(os.environ)
        env["DEBUG"] = "pw:browser"
        try:
            output = checked_run([sys.executable, str(Path(__file__).resolve()), "--diagnostic-config", str(config_path)], env=env)
        except RuntimeError as error:
            if trace_dir:
                atomic_write(trace_dir / f"{key}-failed-tail.log", str(error).encode())
            raise
        if trace_dir:
            atomic_write(trace_dir / f"{key}.log", output.encode())
        nodes = [int(value) for value in re.findall(r"Decide(?:d)? to vectorize, ([1-9][0-9]*) revectorizable nodes", output)]
        rejected = bool(re.search(r"(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|Error:.*(?:wasm-revectorize|trace-wasm-revectorize)", output, re.I))
        info = json.loads(result_path.read_text("utf-8"))
        expected = "--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if enable else "--js-flags=--no-wasm-revectorize,--trace-wasm-revectorize"
        records[key] = {**info, "transformed_groups": len(nodes), "revectorizable_nodes": sum(nodes),
                        "flag_rejected": rejected, "launch_configuration_matches": info["requested_js_flags"] == [expected]}
    control, candidate = records["control"], records["candidate"]
    verified = (control["transformed_groups"] == 0 and candidate["transformed_groups"] > 0
                and all(value["launch_configuration_matches"] and not value["flag_rejected"] for value in records.values())
                and (control["product"], control["js_version"]) == (candidate["product"], candidate["js_version"]))
    if xnnpack:
        a, b = control["xnnpack_profile"], candidate["xnnpack_profile"]
        verified = verified and a["verified"] and b["verified"] and all(
            a[field] == b[field] for field in ("provider_assignment_sha256", "fixed_length", "fixed_matches", "pthreads", "xnn_threads", "xnn_sessions"))
    return {**records, "verified": verified,
            "evidence": "no transformations in trace-only control; positive nonempty V8 optimizer transformations with revectorization; actual CORE WASM, three untimed syntheses each; flags record supplied launch configuration, activation is established by traces"}


def verify_xnnpack(playwright: Any, url: str, options: dict[str, Any], threads: int, style: int, work: Path,
                   profile_path: Path | None = None) -> dict[str, Any]:
    progress("Checking XNNPACK kernel execution in a separate untimed profiled browser")
    browser = playwright.chromium.launch(**options)
    runner = None
    try:
        runner = BrowserRunner(browser, url, "/xnnpack/voicevox_benchmark.js", threads, True, style,
                               work / "xnnpack-diagnostic.wav", fixed_shape=True, spin_off=True, xnn_threads=threads, profile=True)
        runner.synthesize(save=True)
        pthreads = runner.thread_state()
        if pthreads != threads - 1:
            raise RuntimeError("XNNPACK process created an unexpected number of inference pthreads")
        return {**runner.finish_profile(profile_path), "pthreads_after_warmup": pthreads, "fixed_length": runner.fixed_length, "fixed_matches": runner.fixed_matches,
                "xnn_threads": runner.xnn_threads, "xnn_sessions": runner.xnn_sessions}
    finally:
        if runner:
            runner.close()
        browser.close()


@contextlib.contextmanager
def serve_assets(folder: Path) -> Iterator[str]:
    import functools
    import http.server
    import threading
    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()
        def log_message(self, *_: Any) -> None:
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(folder)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

RUST_VERSION = "1.96.0"
EMSDK_VERSION = "4.0.8"
EMSDK_COMMIT = "419021fa040428bc69ef1559b325addb8e10211f"
BASE_RELEASE = "https://github.com/yamachu/onnxruntime-builder/releases/download/onnxruntime-1.23.2"
ORT_ARCHIVE_SHA256 = {'onnxruntime-linux-arm64-1.23.2.tgz': '121888dc9d8c6267f6373df150eed9cd2da5dfd4e277d99b22e092533790f61a', 'onnxruntime-linux-x64-1.23.2.tgz': '2e147a06354a4b75362d4a26e6c55b126d05e25dc19c609c1733aedb7156e8a2', 'onnxruntime-osx-arm64-1.23.2.tgz': 'a80514d3ecf04f8c7e8e8c2d0f1dd603bee0c383e84c1ca01e40bf7807c3d0ce', 'onnxruntime-osx-x86_64-1.23.2.tgz': '0c6489e161ea803e52e8153bdba4ea1541252c171776925f0f230091e1a5a949', 'onnxruntime-wasm-static-1.23.2.tgz': '2dc2c5073337b0e77e08314ca9936f5b9a355ff4e237c16294b969e7f2db6593', 'onnxruntime-win-arm64-1.23.2.tgz': '119b1e2fefde9b139c8a43d3e7ef1817b7ff8d551209916d5f1f8528dd451829', 'onnxruntime-win-x64-1.23.2.tgz': '9db1a87c4502e435319d24602ebd280c98d3d62f7c2b31f16e34de2143732bde'}
THREADED_ORT_URL = "https://github.com/Hiroshiba/onnxruntime-builder/releases/download/onnxruntime-wasm-static-simd-threaded-1.23.2/onnxruntime-wasm-static-simd-threaded-1.23.2.tgz"
XNNPACK_ORT_URL = "https://github.com/Hiroshiba/onnxruntime-builder/releases/download/onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2/onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2.tgz"
ORT_ARCHIVE_SHA256["onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2.tgz"] = "76182743b15a31e0d361432d8db297a78e3da64aad356730fe93576993af5a2a"
ORT_ARCHIVE_SHA256["onnxruntime-wasm-static-simd-threaded-1.23.2.tgz"] = "bdc024237b8303feb24c237bc7c8c07fdabd12a2bb7049684ed2894c580a63d1"


def unpack_cached(archive: Path, destination: Path) -> Path:
    import tarfile
    receipt = destination / ".complete.json"
    digest = sha256_file(archive)
    if receipt.is_file():
        try:
            if json.loads(receipt.read_text())["sha256"] == digest:
                return destination
        except (OSError, ValueError, KeyError):
            pass
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=destination.parent))
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(temporary, filter="data")
        atomic_write(temporary / ".complete.json", json.dumps({"sha256": digest}).encode())
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def find_one(folder: Path, name: str) -> Path:
    matches = [p for p in folder.rglob(name) if p.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {name}; found {len(matches)}")
    return matches[0].resolve()


def native_platform() -> tuple[str, str]:
    machine = platform.machine().lower()
    arm = machine in {"aarch64", "arm64"}
    x64 = machine in {"x86_64", "amd64"}
    if not (arm or x64):
        raise RuntimeError(f"Unsupported architecture: {machine}; use 64-bit x86 or ARM")
    if sys.platform == "win32":
        return f"win-{'arm64' if arm else 'x64'}", f"{'aarch64' if arm else 'x86_64'}-pc-windows-msvc"
    if sys.platform == "darwin":
        return f"osx-{'arm64' if arm else 'x86_64'}", f"{'aarch64' if arm else 'x86_64'}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"linux-{'arm64' if arm else 'x64'}", f"{'aarch64' if arm else 'x86_64'}-unknown-linux-gnu"
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")


def ensure_toolchains(root: Path) -> dict[str, str]:
    _, triple = native_platform()
    if sys.platform == "win32" and not shutil.which("cl.exe"):
        raise RuntimeError("Windows requires Visual Studio C++ Build Tools and an x64/ARM64 Native Tools command prompt. Install/activate those tools, then rerun the same uv command.")
    if sys.platform != "win32" and not (shutil.which("cc") and shutil.which("c++")):
        raise RuntimeError("A native C/C++ compiler is required. Install Xcode Command Line Tools on macOS, or your distribution's C/C++ build tools on Linux, then rerun.")
    import clang.cindex
    env = dict(os.environ)
    env["RUSTUP_HOME"] = str(root / "rustup")
    env["CARGO_HOME"] = str(root / "cargo")
    env["LIBCLANG_PATH"] = str(Path(clang.cindex.__file__).parent / "native")
    cargo_bin = root / "cargo/bin"
    env["PATH"] = str(cargo_bin) + os.pathsep + env["PATH"]
    suffix = ".exe" if sys.platform == "win32" else ""
    rustup = cargo_bin / ("rustup" + suffix)
    if not rustup.is_file():
        installer = cached_download(f"https://static.rust-lang.org/rustup/dist/{triple}/rustup-init{suffix}", root, "rustup-init" + suffix)
        if sys.platform != "win32":
            installer.chmod(0o755)
        checked_run([str(installer), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", RUST_VERSION, "--target", "wasm32-unknown-emscripten"], env=env)
    # rustup reuses installed components and repairs an interrupted installation.
    checked_run([str(rustup), "toolchain", "install", RUST_VERSION, "--profile", "minimal", "--target", "wasm32-unknown-emscripten", "--component", "rust-src"], env=env)
    emsdk_dir = root / f"emsdk-{EMSDK_VERSION}"
    archive = cached_download(f"https://github.com/emscripten-core/emsdk/archive/{EMSDK_COMMIT}.tar.gz", root, f"emsdk-{EMSDK_VERSION}.tar.gz")
    unpack_cached(archive, emsdk_dir)
    emsdk = find_one(emsdk_dir, "emsdk.py")
    installed = emsdk.parent / "upstream/emscripten"
    if not (installed / "emcc.py").is_file():
        checked_run([sys.executable, str(emsdk), "install", EMSDK_VERSION], env=env)
    checked_run([sys.executable, str(emsdk), "activate", EMSDK_VERSION], env=env)
    # Use the activated SDK's explicit paths without evaluating a shell script.
    node = find_one(emsdk.parent / "node", "node.exe" if sys.platform == "win32" else "node")
    env["EMSDK"] = str(emsdk.parent)
    env["EM_CONFIG"] = str(emsdk.parent / ".emscripten")
    env["EMSDK_NODE"] = str(node)
    env["EMSDK_PYTHON"] = sys.executable
    env["PATH"] = os.pathsep.join(map(str, [emsdk.parent, installed, node.parent])) + os.pathsep + env["PATH"]
    return env


def build_runner(source: Path, runtime: Path, root: Path, env: dict[str, str], *, threaded: bool | None, threads: int,
                 optimization: str = "z", graph_level: int = 1, xnnpack: bool = False) -> Path:
    if optimization not in ("z", "3") or graph_level not in (1, 3):
        raise ValueError("Unsupported benchmark optimization variant")
    write_wrapper(source)
    kind = "native" if threaded is None else ("browser-mt" if threaded else "browser-st")
    if optimization != "z":
        kind += "-o3"
    if graph_level != 1:
        kind += "-graph3"
    if xnnpack:
        kind += "-xnnpack"
    pool = 'Module["benchmarkPoolSize"]' if threaded else 0
    import inspect
    build_adapter = inspect.getsource(build_runner) + inspect.getsource(write_wrapper) + inspect.getsource(patch_fixed_shape)
    identity = {"build_adapter_sha256": hashlib.sha256(build_adapter.encode()).hexdigest(), "core": CORE_COMMIT, "rust": RUST_VERSION, "emscripten": EMSDK_VERSION,
                "runtime": sha256_file(runtime), "wrapper": hashlib.sha256(RUST_SOURCE.encode()).hexdigest(),
                "core_diagnostic_patch": hashlib.sha256(CORE_BENCH_HELPER.encode()).hexdigest(),
                "core_fixed_shape_patch": hashlib.sha256((CORE_FIXED_CONFIG + CORE_FIXED_HELPER + CORE_FIXED_OPTION + CORE_FIXED_VERIFY + "sha2.workspace = true; ort/xnnpack").encode()).hexdigest(),
                "kind": kind, "optimization": optimization, "graph_level": graph_level, "xnnpack": xnnpack,
                "pool": pool, "rebuild_std": bool(threaded), "platform": native_platform()[1]}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    folder = root / "builds" / key
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    binary = folder / ("voicevox_benchmark" + (suffix if threaded is None else ".js"))
    receipt = folder / "complete.json"
    if binary.is_file() and receipt.is_file():
        info = json.loads(receipt.read_text())
        if info.get("identity") == identity and all((folder / name).is_file() and sha256_file(folder / name) == digest for name, digest in info["files"].items()):
            print(f"Cached build: {kind}", flush=True)
            return binary
    build_env = dict(env)
    # A caller's pre-existing optimization flags must not silently change this
    # experiment or reuse a binary built under a different precision policy.
    for name in list(build_env):
        if (name.startswith("CARGO_PROFILE_") or name in {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "EMCC_CFLAGS", "EMMAKEN_CFLAGS", "CARGO_BUILD_TARGET"}
                or name.startswith("CARGO_") and name.endswith("_RUSTFLAGS")
                or re.search(r"(^|_)(CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS)(_|$)", name)):
            build_env.pop(name)
    build_env["CARGO_PROFILE_C_API_OPT_LEVEL"] = optimization
    build_env["CARGO_TARGET_DIR"] = str(root / "cargo-target" / kind)
    cargo = str(root / "cargo/bin" / ("cargo.exe" if sys.platform == "win32" else "cargo"))
    command = [cargo, f"+{RUST_VERSION}", "build", "--manifest-path", str(source / "Cargo.toml"), "-p", "voicevox_benchmark", "--profile", "c-api"]
    if threaded is None:
        command += ["--features", "native"]
        output_folder = Path(build_env["CARGO_TARGET_DIR"]) / "c-api"
    else:
        command += ["--features", "browser,threaded" if threaded else "browser", "--target", "wasm32-unknown-emscripten"]
        features = "+simd128,+atomics,+bulk-memory,+mutable-globals" if threaded else "+simd128"
        flags = ["-C", f"target-feature={features}", "-C", "link-arg=-msimd128", "-C", "link-arg=-fwasm-exceptions", "-C", "link-arg=-fno-fast-math", "-C", "link-arg=-ffp-contract=off", "-C", "link-arg=-sALLOW_MEMORY_GROWTH=1",
                 "-C", "link-arg=-sINITIAL_MEMORY=1073741824", "-C", "link-arg=-sSTACK_SIZE=8388608",
                 "-C", "link-arg=-sEXPORTED_RUNTIME_METHODS=FS,HEAPU8" + (",PThread" if threaded else ""),
                 "-C", "link-arg=-sEXPORTED_FUNCTIONS=_main,_bench_init,_bench_synthesize,_bench_wav_ptr,_bench_wav_len,_bench_raw,_bench_raw_ptr,_bench_raw_len,_bench_spin_off,_bench_fixed_length,_bench_fixed_matches,_bench_xnn_threads,_bench_xnn_sessions,_bench_finish_profile",
                 "-L", f"native={runtime.parent}"]
        if optimization == "3":
            flags += ["-C", "link-arg=-O3"]
        if threaded:
            # The distributed wasm std has no atomics. Rebuild the matching 1.96
            # sources instead of mixing in a different nightly compiler.
            command += ["-Z", "build-std=std,panic_abort"]
            build_env["RUSTC_BOOTSTRAP"] = "1"
            flags += ["-C", "link-arg=-pthread", "-C", f"link-arg=-sPTHREAD_POOL_SIZE={pool}"]
            build_env["CFLAGS_wasm32_unknown_emscripten"] = "-pthread -msimd128"
            build_env["CXXFLAGS_wasm32_unknown_emscripten"] = "-pthread -msimd128"
        build_env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
        build_env["CARGO_TARGET_WASM32_UNKNOWN_EMSCRIPTEN_LINKER"] = "emcc.bat" if sys.platform == "win32" else "emcc"
        output_folder = Path(build_env["CARGO_TARGET_DIR"]) / "wasm32-unknown-emscripten/c-api"
    progress(f"Building {kind}: CORE opt-level={optimization}, ORT graph Level{graph_level} (first run may take several minutes)")
    graph_source = source / "crates/voicevox_core/src/core/infer/runtimes/onnxruntime.rs"
    original = graph_source.read_text("utf-8")
    pattern = ".with_optimization_level(GraphOptimizationLevel::Level1)"
    if original.count(pattern) != 1:
        raise RuntimeError("Pinned CORE graph optimization source does not match Level1")
    try:
        if graph_level == 3:
            graph_source.write_text(original.replace(pattern, ".with_optimization_level(GraphOptimizationLevel::Level3)"), encoding="utf-8")
        checked_run(command, env=build_env)
    finally:
        if graph_level == 3:
            graph_source.write_text(original, encoding="utf-8")
    generated = [output_folder / binary.name]
    if threaded is not None:
        generated += list(output_folder.glob("voicevox_benchmark.wasm")) + list(output_folder.glob("voicevox_benchmark.worker.js"))
        if not any(path.suffix == ".wasm" for path in generated):
            raise RuntimeError("Build did not produce WASM")
    hashes = {}
    for path in generated:
        shutil.copy2(path, folder / path.name)
        hashes[path.name] = sha256_file(folder / path.name)
    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())
    return binary


def verify_threaded_runtime(folder: Path, *, xnnpack: bool = False) -> dict[str, Any]:
    info_path = find_one(folder, "BUILD_INFO.json")
    info = json.loads(info_path.read_text("utf-8"))
    expected = {"schema_version": 1, "library": "onnxruntime", "version": ORT_VERSION,
                "source_commit": "a83fc4d58cb48eb68890dd689f94f28288cf2278",
                "emscripten_version": EMSDK_VERSION, "target": "wasm32-unknown-emscripten",
                "simd": True, "pthreads": True, "signed": False, "exception_abi": "wasm", "thread_pool_scope": "global"}
    if xnnpack:
        expected.update({"xnnpack": True, "relaxed_simd": False, "fast_math": False, "fp_contract": "off",
                         "thread_pool_scope": "global_ort_plus_per_session_xnnpack", "pthreadpool_backend": "pthreads",
                         "pthreadpool_execution_smoke_passed": True})
    if any(info.get(key) != value for key, value in expected.items()):
        raise RuntimeError("The threaded archive does not match the required generic ORT/SIMD/pthread build")
    if not info.get("smoke_test", {}).get("passed"):
        raise RuntimeError("The threaded runtime's build smoke test did not pass")
    if xnnpack and not all(info["smoke_test"].get(key) for key in ("profile_verified", "numerical_reference_verified")):
        raise RuntimeError("XNNPACK build lacks verified provider activity and numerical smoke checks")
    runtime = find_one(folder, "libonnxruntime_webassembly.a")
    sums = (info_path.parent / "SHA256SUMS").read_text("utf-8")
    entry = next((line.split()[0] for line in sums.splitlines() if line.split() and line.split()[-1].lstrip("*") == "lib/libonnxruntime_webassembly.a"), None)
    if entry != sha256_file(runtime):
        raise RuntimeError("Threaded runtime library checksum mismatch")
    return info


def verify_archive_sidecar(archive: Path) -> str:
    sidecar = archive.with_name(archive.name + ".sha256")
    entries = [line.split() for line in sidecar.read_text("utf-8").splitlines() if line.strip()]
    match = next((parts[0] for parts in entries if len(parts) == 2 and Path(parts[1].lstrip("*")).name == archive.name), None)
    if not match or not re.fullmatch("[a-fA-F0-9]{64}", match) or sha256_file(archive) != match.lower():
        raise RuntimeError("Threaded archive checksum mismatch")
    return match.lower()


def save_measurements_and_report(result: dict[str, Any], output: Path) -> None:
    """Persist observations before validation or rendering can reject them."""
    raw_path = output.with_suffix(".json")
    atomic_write(raw_path, json.dumps(result, ensure_ascii=False, indent=2).encode())
    progress(f"Raw measurements saved: {raw_path.resolve()}")
    try:
        validate_results(result)
        render_report(result, output)
    except Exception as error:
        raise RuntimeError(f"Report generation failed; raw measurements remain at {raw_path.resolve()}: {error}") from error


def run_benchmark(args: argparse.Namespace) -> dict[str, Any] | None:
    from playwright.sync_api import sync_playwright
    import psutil
    progress("Preparing benchmark inputs and cached tools")
    if sum(map(bool, (args.experiments, args.baseline_only, args.backend_experiments))) > 1:
        raise ValueError("Choose only one of --experiments, --baseline-only, or --backend-experiments")
    use_xnnpack = args.backend_experiments == "all"
    if use_xnnpack and args.style_id != 302:
        raise ValueError("XNNPACK experiments target sample.vvm streaming Style302; use --style-id 302")
    if args.experiments and args.experimental_threads > logical_cpu_count():
        progress(f"Warning: experimental threads={args.experimental_threads} exceeds available logical CPUs={logical_cpu_count()}")
    if not 0 <= args.style_id <= 2**32 - 1:
        raise ValueError("--style-id must be an unsigned 32-bit integer")
    query = json.loads(args.audio_query.read_text("utf-8")) if args.audio_query else prepared_query(args.target_seconds)
    if not isinstance(query, dict) or not isinstance(query.get("outputSamplingRate"), int) or query["outputSamplingRate"] <= 0:
        raise ValueError("AudioQuery must contain a positive integer outputSamplingRate")
    query_bytes = json.dumps(query, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    root = args.cache_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    threaded_url = args.threaded_ort_url or THREADED_ORT_URL
    xnnpack_url = args.xnnpack_ort_url or XNNPACK_ORT_URL
    if use_xnnpack and not xnnpack_url and not args.xnnpack_ort_archive:
        raise RuntimeError("The XNNPACK runtime is not configured until its validated release is available; no XNNPACK measurement was made")
    if not args.baseline_only and not threaded_url and not args.threaded_ort_archive:
        raise RuntimeError("The unsigned multithreaded ORT release is not yet configured. Supply its verified archive URL using --threaded-ort-url; single-thread results are not a substitute for this mode.")
    begin = time.monotonic()
    progress(f"Preparing Rust {RUST_VERSION} and Emscripten {EMSDK_VERSION}")
    env = ensure_toolchains(root)
    progress("Preparing CORE source and ONNX Runtime archives")
    core_archive = cached_download(f"https://github.com/yamachu/voicevox_core/archive/{CORE_COMMIT}.tar.gz", root, f"core-{CORE_COMMIT}.tar.gz")
    source_patch_key = hashlib.sha256((CORE_BENCH_HELPER + CORE_FIXED_CONFIG + CORE_FIXED_HELPER + CORE_FIXED_OPTION + CORE_FIXED_VERIFY).encode()).hexdigest()[:16]
    core_tree = unpack_cached(core_archive, root / f"source-{CORE_COMMIT}-{source_patch_key}")
    source = next(path.parent for path in core_tree.glob("*/Cargo.toml"))
    platform_name, _ = native_platform()
    runtime_archives = {}
    urls = {} if args.backend_experiments == "v8" or args.prepare_only else {
        "native": f"{BASE_RELEASE}/onnxruntime-{platform_name}-{ORT_VERSION}.tgz",
        "browser": f"{BASE_RELEASE}/onnxruntime-wasm-static-{ORT_VERSION}.tgz",
    }
    if not args.baseline_only:
        urls["browser_mt"] = threaded_url
    if use_xnnpack:
        urls["browser_xnnpack"] = xnnpack_url or "local-xnnpack-archive"
    for name, url in urls.items():
        local_archive = args.threaded_ort_archive if name == "browser_mt" else (args.xnnpack_ort_archive if name == "browser_xnnpack" else None)
        if local_archive:
            archive = local_archive.resolve()
            verify_archive_sidecar(archive)
        else:
            from urllib.parse import urlparse
            filename = Path(urlparse(url).path).name
            archive = cached_download(url, root, filename, expected_sha256=ORT_ARCHIVE_SHA256.get(filename))
        (root / "runtimes").mkdir(exist_ok=True)
        extracted = unpack_cached(archive, root / "runtimes" / (name + "-" + sha256_file(archive)[:16]))
        runtime_archives[name] = (archive, extracted)
    library_name = "onnxruntime.dll" if sys.platform == "win32" else (f"libonnxruntime.{ORT_VERSION}.dylib" if sys.platform == "darwin" else f"libonnxruntime.so.{ORT_VERSION}")
    threaded_info = verify_threaded_runtime(runtime_archives["browser_mt"][1]) if not args.baseline_only else None
    xnnpack_info = verify_threaded_runtime(runtime_archives["browser_xnnpack"][1], xnnpack=True) if use_xnnpack else None
    native_runtime = find_one(runtime_archives["native"][1], library_name) if "native" in runtime_archives else None
    st_runtime = find_one(runtime_archives["browser"][1], "libonnxruntime_webassembly.a") if "browser" in runtime_archives else None
    mt_runtime = find_one(runtime_archives["browser_mt"][1], "libonnxruntime_webassembly.a") if not args.baseline_only else None
    native_binary = build_runner(source, native_runtime, root, env, threaded=None, threads=args.threads) if native_runtime else None
    browser_st = build_runner(source, st_runtime, root, env, threaded=False, threads=1) if st_runtime else None
    browser_mt = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads) if mt_runtime else None
    browser_binaries = {"st": browser_st} if browser_st else {}
    if browser_mt:
        browser_binaries["mt"] = browser_mt
    if use_xnnpack:
        xnn_runtime = find_one(runtime_archives["browser_xnnpack"][1], "libonnxruntime_webassembly.a")
        browser_binaries["xnnpack"] = build_runner(source, xnn_runtime, root, env, threaded=True, threads=args.threads, xnnpack=True)
    if args.experiments:
        browser_binaries["o3"] = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads, optimization="3")
        browser_binaries["graph3"] = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads, graph_level=3)
    model = root / f"sample-v1-{CORE_COMMIT}.vvm"
    if not model.exists():
        descriptor, temporary_name = tempfile.mkstemp(prefix=".sample-", dir=root)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            prepare_model(source, temporary)
            os.replace(temporary, model)
        finally:
            temporary.unlink(missing_ok=True)
    if args.prepare_only:
        info = {"binaries": {key: str(binary) for key, binary in browser_binaries.items()}, "model": str(model),
                "model_sha256": sha256_file(model), "source": str(source),
                "runtimes": {key: str(folder) for key, (_, folder) in runtime_archives.items()},
                "query_sha256": hashlib.sha256(query_bytes).hexdigest()}
        atomic_write(root / "research-assets.json", json.dumps(info, indent=2).encode())
        progress("Verified browser assets prepared; no performance measurements made")
        return info
    modes = [] if args.backend_experiments == "v8" else [Mode("native", f"Native ×{args.threads}", args.threads, "native CPU, per-session thread pools"),
             Mode("browser", "Browser ×1", 1, "WebAssembly SIMD, unshared memory")]
    if not args.baseline_only:
        modes.append(Mode("browser_mt", f"Browser pthreads ×{args.threads}", args.threads, "WebAssembly SIMD + pthreads, global thread pool"))
    if args.experiments:
        modes.extend([
            Mode("browser_mt_threads", f"実験 MT ×{args.experimental_threads}", args.experimental_threads, "experimental: thread count only; global pool", experimental=True),
            Mode("browser_mt_o3", f"実験 MT ×{args.threads} O3", args.threads, "experimental: CORE opt-level=3 + final Emscripten -O3; existing LTO retained", core_optimization="3", experimental=True),
            Mode("browser_mt_graph3", f"実験 MT ×{args.threads} Graph L3", args.threads, "experimental: ORT GraphOptimizationLevel::Level3 (ORT_ENABLE_LAYOUT)", graph_optimization=3, experimental=True),
        ])
    if use_xnnpack:
        modes.extend([
            Mode("browser_mt_fixed", f"CPU ×{args.threads} fixed shape", args.threads, "matching CPU control for XNNPACK: query-derived vocoder-only fixed length", fixed_shape=True),
            Mode("browser_xnnpack", f"実験 XNNPACK ×{args.threads}", args.threads, "XNNPACK vocoder-only pool; ORT global fallback intra=1/inter=1, spin disabled; compare with fixed-shape CPU control", experimental=True, fixed_shape=True, spin_off=True, execution_provider="XNNPACK"),
        ])
    if args.backend_experiments:
        modes.append(Mode("browser_revectorize", f"実験 V8 revectorize ×{args.threads}", args.threads, "same dynamic-shape CPU WASM as MT control; only --js-flags=--wasm-revectorize", experimental=True, revectorize=True))
    if use_xnnpack:
        modes.append(Mode("browser_xnnpack_revectorize", f"実験 XNNPACK + V8 ×{args.threads}", args.threads,
                          "same fixed-shape XNNPACK WASM and pools; add only --js-flags=--wasm-revectorize", experimental=True,
                          fixed_shape=True, spin_off=True, execution_provider="XNNPACK", revectorize=True))
    log: list[dict[str, Any]] = [{"event": "assets_ready", "seconds": round(time.monotonic() - begin, 3)}]
    environment = environment_info()
    environment["requested_backend_experiments"] = args.backend_experiments or "none"
    environment.update({"cpu_metric": "sum target-tree user+system CPU seconds between action boundary snapshots / action snapshot seconds; excludes pre/post padding; 100%=one logical CPU", "cpu_sample_interval_ms": CPU_SAMPLE_INTERVAL_S * 1000,
                        "cpu_padding_seconds": CPU_PADDING_S,
                        "cpu_trace_clock": "actual snapshot-completion times relative to synthesis request start; synchronized pre/action/post boundary snapshots; no internal operator-phase attribution",
                        "cpu_plot_aggregation": "common 0.25s request-relative pre/action bins and separate response-end-relative post bins; overlap-weighted rates per phase-complete trial, then median/inclusive quartiles; no zero padding",
                        "cpu_browser_scope": "separate Chromium instance per mode; root and descendants; Python/Playwright Node excluded", "psutil": psutil.__version__})
    environment.update({"rust": checked_run([str(root / "cargo/bin" / ("rustc.exe" if sys.platform == "win32" else "rustc")), f"+{RUST_VERSION}", "--version"], env=env),
                        "emscripten": EMSDK_VERSION, "sample_vvm_sha256": sha256_file(model),
                        "audio_query_sha256": hashlib.sha256(query_bytes).hexdigest(),
                        "browser_thread_support": "single-threaded baseline" if args.baseline_only else "shared WASM memory + global ORT pool verified at initialization"})
    if threaded_info:
        environment.update({"threaded_builder_commit": threaded_info["builder_commit"],
                            "threaded_validation_commit": threaded_info["validation_commit"],
                            "threaded_validation_run_id": threaded_info["validation_run_id"],
                            "threaded_rust_std": "Rust 1.96.0 rebuilt with atomics (build-std)",
                            "threaded_pthread_pool_size": args.threads,
                            "browser_mt_thread_pool": "global; intra-op=requested threads, inter-op=1"})
    for name, (archive, _) in runtime_archives.items():
        environment[f"{name}_ort_archive_sha256"] = sha256_file(archive)
    environment["core_variants"] = {mode.key: {"opt_level": mode.core_optimization, "graph_level": mode.graph_optimization,
                                               "threads": mode.threads, "experimental": mode.experimental,
                                               "fixed_shape": mode.fixed_shape, "global_spin_off": mode.spin_off,
                                               "execution_provider": mode.execution_provider, "revectorize": mode.revectorize} for mode in modes}
    if xnnpack_info:
        environment["xnnpack_builder_commit"] = xnnpack_info["builder_commit"]
        environment["xnnpack_thread_ownership"] = f"XNNPACK intra={args.threads}; ORT global intra=1/inter=1; only one SHA-matched vocoder session registers XNNPACK"
    environment["wasm_bytes"] = {key: binary.with_suffix(".wasm").stat().st_size for key, binary in browser_binaries.items()}
    environment["precision_flags"] = "FP32 model; unchanged strict FP/SIMD flags; no fast-math, relaxed SIMD, FP16 or quantization"
    environment["block_order"] = [block.mode for block in make_schedule(modes, args.seed, balanced=args.backend_experiments)]
    # Browser cache is scoped to this script, including on a second run.
    previous_browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "playwright")
    try:
        if not args.browser_path:
            progress("Preparing cached Chromium")
            checked_run([sys.executable, "-m", "playwright", "install", "chromium"], env=dict(os.environ))
        with tempfile.TemporaryDirectory(prefix="voicevox-benchmark-") as temporary:
            work = Path(temporary)
            # Only explicit benchmark assets are reachable from the loopback server.
            shutil.copyfile(model, work / "sample.vvm")
            atomic_write(work / "query.json", query_bytes)
            atomic_write(work / "index.html", BROWSER_PAGE.encode())
            atomic_write(work / "worker.js", BROWSER_WORKER.encode())
            for mode_key, binary in browser_binaries.items():
                (work / mode_key).mkdir()
                for path in binary.parent.glob("voicevox_benchmark.*"):
                    if path.suffix in {".js", ".wasm"}:
                        shutil.copy2(path, work / mode_key / path.name)
            with serve_assets(work) as url, sync_playwright() as playwright:
                options: dict[str, Any] = {"headless": not args.headed}
                if args.browser_path:
                    options["executable_path"] = str(args.browser_path)
                environment["browser_headless"] = not args.headed
                skipped: dict[str, str] = {}
                if args.backend_experiments:
                    environment["xnnpack_diagnostic"] = {"requested": use_xnnpack, "verified": False}
                    environment["combined_diagnostic"] = {"requested": use_xnnpack, "verified": False}
                    if use_xnnpack:
                        try:
                            environment["xnnpack_diagnostic"] = verify_xnnpack(playwright, url, options, args.threads, args.style_id, work,
                                                                            args.output.parent / "xnnpack-diagnostic-profile.json")
                        except Exception as error:
                            reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")
                            environment["xnnpack_diagnostic"] = {"verified": False, "error": reason[-1500:]}
                    try:
                        environment["revectorization_diagnostic"] = verify_revectorization(url, options, args.threads, args.style_id, work,
                                                                                          args.output.parent / "v8-diagnostic-traces")
                    except Exception as error:
                        reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")
                        environment["revectorization_diagnostic"] = {"verified": False, "error": reason[-1500:]}
                    if use_xnnpack and environment["xnnpack_diagnostic"]["verified"]:
                        try:
                            environment["combined_diagnostic"] = verify_revectorization(url, options, args.threads, args.style_id, work,
                                                                                       args.output.parent / "combined-diagnostic-traces", xnnpack=True)
                        except Exception as error:
                            reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")
                            environment["combined_diagnostic"] = {"verified": False, "error": reason[-1500:]}
                    if use_xnnpack and not environment["xnnpack_diagnostic"]["verified"]:
                        skipped["browser_xnnpack"] = "XNNPACKは実行プロファイルで対象カーネルを確認できず、未測定。"
                        modes = [mode for mode in modes if mode.key not in {"browser_xnnpack", "browser_mt_fixed", "browser_xnnpack_revectorize"}]
                    if not environment["revectorization_diagnostic"]["verified"]:
                        skipped["browser_revectorize"] = "V8再ベクトル化は基準との差を示す変換ログを確認できず、未測定。"
                        modes = [mode for mode in modes if mode.key != "browser_revectorize"]
                    if use_xnnpack and not environment["combined_diagnostic"]["verified"]:
                        skipped["browser_xnnpack_revectorize"] = "XNNPACKとV8の併用は同じ演算割当と実際のV8変換を確認できず、未測定。"
                        modes = [mode for mode in modes if mode.key != "browser_xnnpack_revectorize"]
                    environment["skipped_candidates"] = skipped
                    requested = environment["core_variants"]
                    measured_keys = {mode.key for mode in modes}
                    if skipped:
                        environment["requested_but_unmeasured"] = {key: value for key, value in requested.items() if key not in measured_keys}
                    environment["core_variants"] = {key: value for key, value in requested.items() if key in measured_keys}
                    atomic_write(args.output.with_suffix(".diagnostics.json"), json.dumps({key: environment[key] for key in ("xnnpack_diagnostic", "revectorization_diagnostic", "combined_diagnostic", "skipped_candidates")}, ensure_ascii=False, indent=2).encode())
                    if len(modes) < 2:
                        raise RuntimeError("Neither requested backend passed activation diagnostics; diagnostics saved, no purported optimized timings measured")
                    environment["block_order"] = [block.mode for block in make_schedule(modes, args.seed, balanced=True)]
                    progress("Balanced block order: " + " → ".join(environment["block_order"]))
                runners: dict[str, Any] = {}
                browsers: dict[str, Any] = {}
                cpu_roots: dict[str, int] = {}
                try:
                    if native_binary:
                        progress("Initializing native CORE")
                        started = time.monotonic()
                        runners["native"] = NativeRunner(native_binary, native_runtime, model, work / "query.json", args.threads, args.style_id, work / "native.wav")
                        cpu_roots["native"] = runners["native"].process.pid
                        log.append({"event": "initialized", "mode": "native", "seconds": round(time.monotonic() - started, 3)})
                    browser_modes = [("browser", "/st/voicevox_benchmark.js", 1, False)] if browser_st else []
                    if browser_mt:
                        browser_modes.append(("browser_mt", "/mt/voicevox_benchmark.js", args.threads, True))
                    if args.experiments:
                        browser_modes.extend([
                            ("browser_mt_threads", "/mt/voicevox_benchmark.js", args.experimental_threads, True),
                            ("browser_mt_o3", "/o3/voicevox_benchmark.js", args.threads, True),
                            ("browser_mt_graph3", "/graph3/voicevox_benchmark.js", args.threads, True),
                        ])
                    if args.backend_experiments:
                        browser_modes.extend([
                            ("browser_mt_fixed", "/mt/voicevox_benchmark.js", args.threads, True),
                            ("browser_xnnpack", "/xnnpack/voicevox_benchmark.js", args.threads, True),
                            ("browser_revectorize", "/mt/voicevox_benchmark.js", args.threads, True),
                            ("browser_xnnpack_revectorize", "/xnnpack/voicevox_benchmark.js", args.threads, True),
                        ])
                        browser_modes = [row for row in browser_modes if any(mode.key == row[0] for mode in modes)]
                    for key, module, threads, threaded in browser_modes:
                        progress(f"Initializing isolated {key} Chromium and CORE")
                        started = time.monotonic()
                        configured = next(mode for mode in modes if mode.key == key)
                        launch_options = dict(options)
                        if configured.revectorize:
                            launch_options["args"] = ["--js-flags=--wasm-revectorize"]
                        browser = playwright.chromium.launch(**launch_options)
                        browsers[key] = browser
                        cpu_roots[key] = chromium_process_id(browser)
                        if "browser" in environment and environment["browser"] != browser.version:
                            raise RuntimeError("Browser versions differ between modes")
                        environment["browser"] = browser.version
                        runners[key] = BrowserRunner(browser, url, module, threads, threaded, args.style_id, work / f"{key}.wav",
                                                     fixed_shape=configured.fixed_shape, spin_off=configured.spin_off,
                                                     xnn_threads=threads if configured.execution_provider == "XNNPACK" else 0)
                        if configured.revectorize:
                            info = browser_engine_info(browser)
                            info["requested_js_flags"] = launch_options["args"]
                            if info["requested_js_flags"] != ["--js-flags=--wasm-revectorize"]:
                                raise RuntimeError("Timed launch must use only the revectorization flag without trace flags")
                            combined = configured.execution_provider == "XNNPACK"
                            checked_engine = environment["combined_diagnostic" if combined else "revectorization_diagnostic"]["candidate"]
                            if (info["product"], info["js_version"]) != (checked_engine["product"], checked_engine["js_version"]):
                                raise RuntimeError("Timed V8 version differs from its activation diagnostic")
                            environment["timed_combined_engine" if combined else "timed_revectorization_engine"] = info
                        environment[f"{key}_pthreads_created"] = runners[key].pthreads_created
                        environment[f"{key}_global_spin_off"] = runners[key].spin_off
                        if configured.execution_provider == "XNNPACK":
                            environment[f"{key}_xnn_threads"] = runners[key].xnn_threads
                            environment[f"{key}_xnn_sessions"] = runners[key].xnn_sessions
                            environment[f"{key}_ort_global_threads"] = 1
                        if configured.fixed_shape:
                            environment[f"{key}_fixed_length"] = runners[key].fixed_length
                            environment[f"{key}_fixed_matches"] = runners[key].fixed_matches
                            environment[f"{key}_vocoder_sha256"] = VOCODER_MODEL_SHA256
                            environment[f"{key}_verified_input_shapes"] = {"spec": [runners[key].fixed_length, 80]}
                        log.append({"event": "initialized", "mode": key, "seconds": round(time.monotonic() - started, 3)})
                    if "browser_mt" in runners:
                        environment["browser_mt_pthreads_created"] = runners["browser_mt"].pthreads_created
                    environment.update(runners["browser_mt" if args.backend_experiments else "browser"].page.evaluate("() => ({browser_hardware_concurrency:navigator.hardwareConcurrency,cross_origin_isolated:crossOriginIsolated,user_agent:navigator.userAgent})"))
                    for mode in modes:
                        progress(f"Warmup: {mode.label} (excluded from latency and CPU measurements)")
                        elapsed, duration = runners[mode.key].synthesize(save=True)
                        if isinstance(runners[mode.key], BrowserRunner):
                            pthreads = runners[mode.key].thread_state()
                            environment[f"{mode.key}_pthreads_after_warmup"] = pthreads
                            if mode.execution_provider == "XNNPACK" and pthreads != mode.threads - 1:
                                raise RuntimeError("Timed XNNPACK process has unexpected thread-pool ownership")
                        if mode.fixed_shape:
                            environment[f"{mode.key}_warmup_with_ort_shape_validation"] = True
                        log.append({"event": "warmup_excluded", "mode": mode.key, "seconds": elapsed, "audio_s": duration})
                    durations = [runner.duration for runner in runners.values()]
                    if max(durations) - min(durations) > 1 / query["outputSamplingRate"]:
                        raise RuntimeError("Native/browser output lengths differ; refusing a misleading comparison")
                    output_checks = verify_outputs(runners, "browser_mt" if browser_mt else "browser", log)
                    spectrograms = output_checks.pop("spectrograms")
                    progress(f"Starting {len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK} serial trials with target-process CPU sampling")
                    def measured_synthesis(mode: Mode) -> tuple[float, float, CpuMeasurement]:
                        sampler = ProcessCpuSampler(cpu_roots[mode.key], mode.label)
                        return sampler.measure(runners[mode.key].synthesize)
                    result = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "modes": [asdict(mode) for mode in modes], "trials": [],
                              "measurement_status": "measuring", "environment": environment, "audio_s": durations[0], "style_id": args.style_id, "seed": args.seed, "log": log,
                              "output_checks": output_checks,
                              "spectrograms": spectrograms,
                              "schedule_method": "seeded position-balanced and pair-order-balanced design; randomized labels and round order" if args.backend_experiments else "seeded independent shuffle in each round",
                              "schedule": [asdict(block) for block in make_schedule(modes, args.seed, balanced=args.backend_experiments)],
                              "notes": ["出力検証は測定外。同じ実行のブラウザMT基準とPCM・PCM化前FP32を照合。非一致を精度維持とは判定しない。" if browser_mt else "出力検証は測定外。ブラウザST基準とPCM・PCM化前FP32を照合。"]}
                    if args.baseline_only:
                        result["notes"].append("この先行検証はnativeとブラウザ単一スレッドのみ。マルチスレッドは未実施。")
                    if args.backend_experiments:
                        if use_xnnpack:
                            result["notes"].append("XNNPACKの速度は同じ固定shapeのCPU対照と比較。")
                            result["notes"].append("併用の追加効果は同じXNNPACK単独と、全体の効果は固定shapeのCPU対照と比較。FP32形式の維持と数値の完全一致は別々に確認。")
                        else:
                            result["notes"].append("この実行はV8候補のみ。XNNPACKは測定対象外。")
                        result["notes"].append("V8再ベクトル化は同じ通常MT WASMとの比較。診断用プロファイル・トレースは時間測定とは別のブラウザで実行。")
                        result["notes"].extend(skipped.values())
                    def checkpoint(completed: list[Trial]) -> None:
                        result["trials"] = [asdict(trial) for trial in completed]
                        atomic_write(args.output.with_suffix(".json"), json.dumps(result, ensure_ascii=False, indent=2).encode())
                    checkpoint([])
                    run_schedule(modes, args.seed, measured_synthesis, log,
                                 balanced=args.backend_experiments, checkpoint=checkpoint)
                    result["measurement_status"] = "complete"
                    save_measurements_and_report(result, args.output)
                    print(f"Report: {args.output.resolve()}")
                finally:
                    for runner in runners.values():
                        with contextlib.suppress(Exception):
                            runner.close()
                    for browser in browsers.values():
                        with contextlib.suppress(Exception):
                            browser.close()
    finally:
        if previous_browser_path is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous_browser_path



def positive_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return number


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threads", type=positive_int, default=default_threads(), help="native + multithreaded-browser inference threads; default: max(1, available logical CPUs // 2)")
    parser.add_argument("--seed", type=int, default=20261007, help="reproducible block order")
    parser.add_argument("--audio-query", type=Path, help="optional prepared AudioQuery JSON; default: built-in approximately 10-second query")
    parser.add_argument("--style-id", type=int, default=302)
    parser.add_argument("--target-seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("voicevox-benchmark.html"))
    parser.add_argument("--cache-dir", type=Path, default=cache_root())
    parser.add_argument("--report-from", type=Path, help="regenerate HTML from a completed measurement JSON")
    parser.add_argument("--audio-dir", type=Path, help="with --report-from: make images from local hash-matched WAVs; audio is not embedded")
    parser.add_argument("--spectrogram-dir", type=Path, help="with --report-from: load PNG-only spectrograms and their manifest")
    parser.add_argument("--baseline-only", action="store_true", help="explicitly measure only native and browser single-thread (MT is not measured)")
    parser.add_argument("--experiments", action="store_true", help="add isolated MT thread-count, CORE O3, and ORT graph-Level3 candidates (90 trials total)")
    parser.add_argument("--backend-experiments", nargs="?", const="all", choices=("all", "v8"), help="all: MT/fixed-shape controls, XNNPACK and V8; v8: only MT control and V8; balanced blocks")
    parser.add_argument("--kernel-experiments", action="store_true", help="opt in to strict FP32 selector experiment on Linux/WSL x86-64; use --threads 2; three runtimes, 3 fresh sets x 5 paired rounds plus CPU reference")
    parser.add_argument("--kernel-revectorize", choices=("off", "on"), default="off", help="kernel experiment only; OFF default; ON requires independently verified V8 transformation")
    parser.add_argument("--kernel-profile-xnn", action="store_true", help="kernel experiment only; add untimed untouched-XNN operator placement after primary timings")
    parser.add_argument("--graph-experiments", action="store_true", help="opt in to original/combined FP32 graph trials on the same CPU CORE WASM; explicit V8 OFF by default; 30 trials")
    parser.add_argument("--graph-revectorize", choices=("off", "on"), default="off", help="with --graph-experiments: explicit V8 setting for this two-mode run; default off; rerun on separately with a different --output")
    parser.add_argument("--experimental-threads", type=positive_int, default=4, help="thread-count candidate used with --experiments; default: 4")
    parser.add_argument("--threaded-ort-archive", type=Path, help="local verified MT archive with adjacent .sha256 sidecar")
    parser.add_argument("--threaded-ort-url", help="verified unsigned threaded ORT archive from the fork release")
    parser.add_argument("--xnnpack-ort-archive", type=Path, help="local verified XNNPACK archive with adjacent .sha256 sidecar")
    parser.add_argument("--xnnpack-ort-url", help="verified strict-FP XNNPACK runtime archive from the fork release")
    parser.add_argument("--browser-path", type=Path, help="optional installed Chromium executable")
    parser.add_argument("--headed", action="store_true", help="show the otherwise identical browser runner")
    parser.add_argument("--prepare-only", action="store_true", help="build checked browser CORE assets and save private paths without measurements")
    parser.add_argument("--research-screen", action="store_true", help="research only: preliminary 1 trial per block, 3 blocks; JSON only, no speedup claim")
    parser.add_argument("--research-manifest", type=Path, help="same-host candidate manifest using cached validated CORE builds")
    parser.add_argument("--measurement-gate", type=Path, help="wait until this file exists before research timing")
    parser.add_argument("--self-test", action="store_true", help="check scheduling, validation and reporting without synthesizing")
    parser.add_argument("--diagnostic-config", type=Path, help=argparse.SUPPRESS)
    return parser


def self_test() -> None:
    progress("Self-test fixtures only; no real synthesis or CPU measurements")
    modes = [Mode("native", "Native", 2, "native CPU"), Mode("browser", "Browser", 1, "WebAssembly SIMD")]
    a = make_schedule(modes, 42)
    assert a == make_schedule(modes, 42)
    assert len(a) == 6 and all(sum(block.mode == mode.key for block in a) == 3 for mode in modes)
    assert max(1, logical_cpu_count() // 2) == default_threads()
    for count in (2, 3, 4, 5, 6, 7):
        candidates = [Mode(f"mode{i}", str(i), 2, "test") for i in range(count)]
        for seed in range(100):
            blocks = make_schedule(candidates, seed, balanced=True)
            rounds = [[block.mode for block in blocks[start:start + count]] for start in range(0, len(blocks), count)]
            for mode in candidates:
                positions = [row.index(mode.key) for row in rounds]
                counts = [positions.count(index) for index in range(count)]
                assert max(counts) - min(counts) <= 1
            for first in candidates:
                for second in candidates:
                    if first.key != second.key:
                        assert {row.index(first.key) < row.index(second.key) for row in rounds} == {False, True}
    # Explicit test fixture, confined to a temporary directory and never delivered
    # as benchmark measurements.
    log: list[dict[str, Any]] = []
    def fixture(mode: Mode) -> tuple[float, float, CpuMeasurement]:
        trace = (CpuInterval(-1.001, -0.001, 0.0, 0.0, 1, False, "pre"),
                 CpuInterval(-0.001, 0.119, 0.06 * mode.threads, 50.0 * mode.threads, 1, False),
                 CpuInterval(0.119, 0.199, 0.04 * mode.threads, 50.0 * mode.threads, 1, False),
                 CpuInterval(0.199, 1.199, 0.0, 0.0, 1, False, "post"))
        return 0.1 * mode.threads, 10.0, CpuMeasurement(0.1 * mode.threads, 0.2, 0.5 * mode.threads,
                                                     50.0 * mode.threads, 5, 1, False, trace)
    trials = run_schedule(modes, 42, fixture, log)
    result = {"schema_version": SCHEMA_VERSION, "created_at": "SELF-TEST FIXTURE", "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials], "environment": {"test": "<>&"}, "audio_s": 10.0, "style_id": 302, "seed": 42, "log": log}
    validate_results(result)
    assert len(list(csv.DictReader(io.StringIO(trial_csv(result["trials"]))))) == 30
    assert len(list(csv.DictReader(io.StringIO(cpu_trace_csv(result["trials"]))))) == 120
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "test.html"
        render_report(result, report)
        content = report.read_text()
        assert '<details><summary>生データ（CSV）</summary>' in content
        assert '<details open' not in content
        assert '<summary>CPU 時系列（CSV）</summary>' in content
        assert 'class="cpu-grid"' in content
        assert '&lt;&gt;&amp;' in content
        assert 'https://' not in content  # Standalone report requires no CDN.
        first = Path(folder) / "atomic.txt"
        atomic_write(first, b"first")
        atomic_write(first, b"second")
        assert first.read_bytes() == b"second"
    corrupt = json.loads(json.dumps(result))
    corrupt["trials"][0]["rtf"] = 999.0
    try:
        validate_results(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("Inconsistent RTF accepted")
    graph_regression_self_test()
    kernel_regression_self_test()
    print("Self-test passed. No benchmark measurements were made.")


# Graph changes are opt-in and source-pinned. Imports stay local so ordinary
# benchmark/report/help paths never import ONNX or transform the model.
GRAPH_FUSION_REFERENCE = (
    "https://raw.githubusercontent.com/microsoft/onnxruntime/v1.23.2/"
    "onnxruntime/core/optimizer/conv_activation_fusion.cc"
)
GRAPH_CONVTRANSPOSE_NAMES = (
    "ConvTranspose_4", "ConvTranspose_54", "ConvTranspose_104", "ConvTranspose_154"
)


def graph_require(condition: bool, message: str) -> None:
    """Fail closed even under python -O, unlike assertions in research scripts."""
    if not condition:
        raise ValueError("Graph experiment: " + message)


def graph_promote_convtranspose(model: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Promote pinned 1D deconvolutions to 2D with a size-one spatial axis.

    Initializer element order and FP32 bytes are unchanged; Unsqueeze/Squeeze
    preserve the graph-facing tensors and every consumer.
    """
    import copy
    import onnx
    from onnx import helper
    graph_require(next((op.version for op in model.opset_import if not op.domain), None) == 12,
                  "expected ONNX opset 12")
    graph_require([node.name for node in model.graph.node if node.op_type == "ConvTranspose"]
                  == list(GRAPH_CONVTRANSPOSE_NAMES), "unexpected ConvTranspose nodes")
    names = {value for node in model.graph.node for value in (node.name, *node.input, *node.output)}
    names.update(tensor.name for tensor in model.graph.initializer)
    graph_require(not any("__deconv2d" in name for name in names), "inserted-name collision")
    consumers = {
        node.output[0]: [(consumer.name, index) for consumer in model.graph.node
                         for index, value in enumerate(consumer.input) if value == node.output[0]]
        for node in model.graph.node if node.op_type == "ConvTranspose"
    }
    changed = copy.deepcopy(model)
    initializers = {tensor.name: tensor for tensor in changed.graph.initializer}
    nodes, edits = [], []
    for old in changed.graph.node:
        if old.op_type != "ConvTranspose":
            nodes.append(old)
            continue
        node = copy.deepcopy(old)
        attributes = {value.name: helper.get_attribute_value(value) for value in old.attribute}
        allowed = set("auto_pad dilations group kernel_shape pads strides output_padding output_shape".split())
        graph_require(not old.domain and set(attributes) <= allowed, "unsupported deconvolution attributes")
        weight = initializers[node.input[1]]
        graph_require(len(weight.dims) == 3 and weight.data_type == onnx.TensorProto.FLOAT,
                      "expected a rank-three FP32 deconvolution weight")
        promoted_weight = copy.deepcopy(weight)
        promoted_weight.name = weight.name + "__deconv2d"
        promoted_weight.dims.insert(2, 1)
        graph_require(promoted_weight.raw_data == weight.raw_data
                      and list(promoted_weight.dims) == [weight.dims[0], weight.dims[1], 1, weight.dims[2]],
                      "weight bytes or element order changed")
        changed.graph.initializer.append(promoted_weight)
        node.input[1] = promoted_weight.name
        promoted_attributes = copy.deepcopy(attributes)
        for key in ("kernel_shape", "strides", "dilations"):
            if key in promoted_attributes:
                graph_require(len(promoted_attributes[key]) == 1, "unexpected " + key)
                promoted_attributes[key] = [1, *promoted_attributes[key]]
        if "pads" in promoted_attributes:
            graph_require(len(promoted_attributes["pads"]) == 2, "unexpected pads")
            before, after = promoted_attributes["pads"]
            promoted_attributes["pads"] = [0, before, 0, after]
        for key, leading in (("output_padding", 0), ("output_shape", 1)):
            if key in promoted_attributes:
                graph_require(len(promoted_attributes[key]) == 1, "unexpected " + key)
                promoted_attributes[key] = [leading, *promoted_attributes[key]]
        del node.attribute[:]
        node.attribute.extend(helper.make_attribute(key, value) for key, value in promoted_attributes.items())
        incoming, outgoing = node.input[0], node.output[0]
        node.input[0] = incoming + "__deconv2d_input"
        node.output[0] = outgoing + "__deconv2d_output"
        nodes.extend([
            helper.make_node("Unsqueeze", [incoming], [node.input[0]], axes=[2], name=node.name + "__expand"),
            node,
            helper.make_node("Squeeze", [node.output[0]], [outgoing], axes=[2], name=node.name + "__squeeze"),
        ])
        def printable(values: dict[str, Any]) -> dict[str, Any]:
            return {key: value.decode() if isinstance(value, bytes) else value for key, value in values.items()}
        edits.append({"name": node.name, "before": printable(attributes), "after": printable(promoted_attributes)})
    del changed.graph.node[:]
    changed.graph.node.extend(nodes)
    used = {value for node in nodes for value in node.input}
    retained = [tensor for tensor in changed.graph.initializer if tensor.name in used]
    del changed.graph.initializer[:]
    changed.graph.initializer.extend(retained)
    graph_require(len(edits) == 4, "expected exactly four deconvolution promotions")
    for output, expected in consumers.items():
        actual = [(consumer.name, index) for consumer in changed.graph.node
                  for index, value in enumerate(consumer.input) if value == output]
        graph_require(actual == expected, "deconvolution consumer connectivity changed")
    outputs = [value for node in changed.graph.node for value in node.output if value]
    graph_require(len(outputs) == len(set(outputs)), "duplicate tensor producer")
    onnx.checker.check_model(changed)
    return changed, edits


def graph_fuse_conv_leakyrelu(model: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Selective ORT 1.23.2 ConvActivationFusion equivalent for the CPU EP."""
    import copy
    import struct
    import onnx
    from onnx import helper
    changed = copy.deepcopy(model)
    opsets = {op.domain: op.version for op in changed.opset_import}
    graph_require(opsets.get("") == 12 and opsets.get("com.microsoft") == 1, "unexpected fusion opsets")
    graph_require(not any(attribute.type in (onnx.AttributeProto.GRAPH, onnx.AttributeProto.GRAPHS)
                          for node in changed.graph.node for attribute in node.attribute), "subgraphs are unsupported")
    consumers, producers = {}, {}
    outputs = {value.name for value in changed.graph.output}
    initializers = {tensor.name: tensor for tensor in changed.graph.initializer}
    for index, node in enumerate(changed.graph.node):
        for value in node.input:
            consumers.setdefault(value, []).append(index)
        for value in node.output:
            graph_require(value not in producers, "duplicate tensor producer")
            producers[value] = index
    replacements, removed, edits = {}, set(), []
    for index, node in enumerate(changed.graph.node):
        if node.domain or node.op_type != "Conv" or len(node.output) != 1:
            continue
        output = node.output[0]
        following = consumers.get(output, [])
        if output in outputs or len(following) != 1:
            continue
        following_index = following[0]
        activation = changed.graph.node[following_index]
        if (activation.domain or activation.op_type != "LeakyRelu"
                or list(activation.input) != [output] or len(activation.output) != 1):
            continue
        graph_require(len(node.input) == 3
                      and initializers[node.input[1]].data_type == initializers[node.input[2]].data_type == onnx.TensorProto.FLOAT,
                      "expected FP32 convolution weights and bias")
        attributes = {value.name: value for value in activation.attribute}
        graph_require(set(attributes) == {"alpha"} and attributes["alpha"].type == onnx.AttributeProto.FLOAT,
                      "unexpected LeakyRelu attributes")
        fused = copy.deepcopy(node)
        fused.domain, fused.op_type = "com.microsoft", "FusedConv"
        fused.output[:] = activation.output
        fused.attribute.extend([
            helper.make_attribute("activation", "LeakyRelu"),
            helper.make_attribute("activation_params", [attributes["alpha"].f]),
        ])
        alpha_bytes = struct.pack("<f", attributes["alpha"].f)
        graph_require(struct.pack("<f", fused.attribute[-1].floats[0]) == alpha_bytes,
                      "LeakyRelu FP32 alpha changed")
        replacements[index] = fused
        removed.add(following_index)
        edits.append({"conv": node.name, "activation": activation.name, "removed_tensor": output,
                      "output": activation.output[0], "alpha_fp32_hex": alpha_bytes.hex()})
    nodes = [replacements.get(index, node) for index, node in enumerate(changed.graph.node) if index not in removed]
    del changed.graph.node[:]
    changed.graph.node.extend(nodes)
    dead = {edit["removed_tensor"] for edit in edits}
    retained = [value for value in changed.graph.value_info if value.name not in dead]
    del changed.graph.value_info[:]
    changed.graph.value_info.extend(retained)
    graph_require(len(edits) == 37, "expected exactly 37 selective Conv/LeakyRelu fusions")
    graph_require([tensor.SerializeToString() for tensor in model.graph.initializer]
                  == [tensor.SerializeToString() for tensor in changed.graph.initializer], "fusion changed weights")
    graph_require(model.opset_import == changed.opset_import and model.graph.input == changed.graph.input
                  and model.graph.output == changed.graph.output, "fusion changed the graph interface")
    onnx.checker.check_model(changed)
    return changed, edits


def graph_transform_vocoder_bytes(original: bytes) -> tuple[bytes, dict[str, Any]]:
    """Reject unknown weights before importing/decoding ONNX; never quantize."""
    graph_require(hashlib.sha256(original).hexdigest() == VOCODER_MODEL_SHA256,
                  "unrecognized source vocoder SHA-256; refusing model substitution")
    import onnx
    model = onnx.load_model_from_string(original)
    promoted, promotion_edits = graph_promote_convtranspose(model)
    combined, fusion_edits = graph_fuse_conv_leakyrelu(promoted)
    # The two local rewrites must commute, as in the reviewed source transform.
    fused, _ = graph_fuse_conv_leakyrelu(model)
    reverse, _ = graph_promote_convtranspose(fused)
    candidate = combined.SerializeToString()
    graph_require(candidate == reverse.SerializeToString(), "graph transforms do not commute")
    return candidate, {"source_vocoder_sha256": VOCODER_MODEL_SHA256,
                       "candidate_vocoder_sha256": hashlib.sha256(candidate).hexdigest(),
                       "onnx_version": onnx.__version__, "fusion_reference": GRAPH_FUSION_REFERENCE,
                       "convtranspose_edits": promotion_edits, "fusion_edits": fusion_edits,
                       "precision": "unchanged FP32 tensor bytes, strict FP flags; exactness checked on this host"}


def graph_prepare_candidate(source: Path, destination: Path) -> dict[str, Any]:
    """Write only a private temporary candidate; preserve every other VVM entry."""
    import copy
    import zipfile
    graph_require(source.resolve() != destination.resolve(), "never overwrite the source model")
    source_digest = sha256_file(source)
    with zipfile.ZipFile(source) as original:
        graph_require(original.namelist().count("vocoder.onnx") == 1, "expected exactly one vocoder.onnx")
        candidate, provenance = graph_transform_vocoder_bytes(original.read("vocoder.onnx"))
        with zipfile.ZipFile(destination, "w") as output:
            for item in original.infolist():
                output.writestr(copy.copy(item), candidate if item.filename == "vocoder.onnx" else original.read(item.filename))
        with zipfile.ZipFile(destination) as check:
            graph_require(check.namelist() == original.namelist(), "VVM entry list changed")
            for name in original.namelist():
                if name != "vocoder.onnx":
                    graph_require(check.read(name) == original.read(name), "non-vocoder VVM entry changed")
    graph_require(sha256_file(source) == source_digest, "source VVM was modified")
    return {**provenance, "source_vvm_sha256": source_digest, "candidate_vvm_sha256": sha256_file(destination)}


def require_graph_exactness(checks: dict[str, Any], output: Path) -> None:
    for key, check in checks["modes"].items():
        if not check["pcm_exact"] or not check["fp32_exact"]:
            atomic_write(output.with_suffix(".failed-checks.json"), json.dumps(checks, indent=2).encode())
            raise RuntimeError("Graph candidate differs from the same-run control: " + key
                               + "; numeric checks saved, no graph timings measured")


class V8VerificationError(RuntimeError):
    """Explicit V8 ON could not be verified; no silent fallback is permitted."""


def run_graph_experiments(args: argparse.Namespace) -> None:
    """Two same-host conditions at a time; OFF is independent of ON support."""
    graph_require(args.style_id == 302, "only pinned sample.vvm Style302 is supported")
    prepared = argparse.Namespace(**vars(args))
    prepared.prepare_only = True
    prepared.graph_experiments = False
    prepared.backend_experiments = None
    prepared.experiments = prepared.baseline_only = False
    progress("Preparing the unchanged CORE C API/static ORT pipeline for explicit graph experiments")
    assets = run_benchmark(prepared)
    graph_require(isinstance(assets, dict), "asset preparation did not return verified assets")
    binary, original = Path(assets["binaries"]["mt"]), Path(assets["model"])
    receipt = json.loads((binary.parent / "complete.json").read_text("utf-8"))
    # Candidate archives and internal manifests live only for this invocation.
    with tempfile.TemporaryDirectory(prefix="voicevox-graph-private-") as directory:
        work = Path(directory)
        candidate = work / "combined-private.vvm"
        provenance = graph_prepare_candidate(original, candidate)
        setting = args.graph_revectorize
        variants = []
        for key, label, model in (("original", "Original FP32 graph", original),
                                  ("combined", "Experimental ConvTranspose2D + selective fusion", candidate)):
            variants.append({"key": key, "label": f"{label} ×{args.threads}; V8 {setting.upper()}",
                             "threads": args.threads, "binary": str(binary), "build_identity": receipt["identity"],
                             "model": str(model), "model_sha256": sha256_file(model), "revectorize": setting == "on",
                             "description": "same CORE CPU WASM, dynamic shapes, ORT Level1, strict FP32; "
                                            "model-only comparison under explicit V8 " + setting.upper()})
        manifest = {"variants": variants, "graph_experiments": True,
                    "graph_revectorize": setting, "transform_provenance": provenance,
                    "fixture_query_sha256": assets["query_sha256"]}
        manifest_path = work / "manifest.json"
        atomic_write(manifest_path, json.dumps(manifest, indent=2).encode())
        research = argparse.Namespace(**vars(args))
        research.research_manifest = manifest_path
        progress("Experimental graph comparison with V8 " + setting.upper() + "; no gain is assumed")
        try:
            run_research(research)
        except V8VerificationError as error:
            unavailable = {"status": "unavailable", "graph_revectorize": setting,
                           "stage": "untimed V8 activation verification", "error": str(error),
                           "measurements_made": False, "silent_fallback_used": False}
            atomic_write(args.output.with_suffix(".unavailable.json"), json.dumps(unavailable, indent=2).encode())
            raise


def validate_arguments(args: argparse.Namespace) -> None:
    validate_kernel_arguments(args)
    requested = (args.experiments, args.baseline_only, args.backend_experiments,
                 args.graph_experiments, args.research_manifest)
    if sum(map(bool, requested)) > 1:
        raise ValueError("Choose one of --experiments, --baseline-only, --backend-experiments, "
                         "--graph-experiments, or --research-manifest")
    if args.graph_revectorize != "off" and not args.graph_experiments:
        raise ValueError("--graph-revectorize requires --graph-experiments")
    if args.graph_experiments and args.prepare_only:
        raise ValueError("--graph-experiments runs private model trials; use --prepare-only separately for CORE assets")
    if args.research_screen and not (args.graph_experiments or args.research_manifest):
        raise ValueError("--research-screen requires --graph-experiments or --research-manifest")
    if args.measurement_gate and not (args.graph_experiments or args.research_manifest):
        raise ValueError("--measurement-gate requires --graph-experiments or --research-manifest")


def graph_regression_self_test() -> None:
    """No model loading, compiler, browser, inference, or network is used."""
    import unittest.mock
    defaults = argument_parser().parse_args([])
    graph_require(not defaults.graph_experiments and defaults.graph_revectorize == "off",
                  "graph experiments must be explicitly opted into")
    validate_arguments(defaults)
    for options in (["--graph-experiments", "--backend-experiments"],
                    ["--graph-revectorize", "on"], ["--research-screen"],
                    ["--graph-experiments", "--prepare-only"]):
        try:
            validate_arguments(argument_parser().parse_args(options))
        except ValueError:
            pass
        else:
            raise AssertionError("Conflicting or unused option was accepted: " + str(options))
    for setting in ("off", "on"):
        options = argument_parser().parse_args(["--graph-experiments", "--graph-revectorize", setting])
        validate_arguments(options)
    try:
        graph_transform_vocoder_bytes(b"not the source-pinned model")
    except ValueError as error:
        graph_require("SHA-256" in str(error), "unrecognized model was not rejected by hash")
    else:
        raise AssertionError("Unrecognized model accepted")
    import zipfile
    with tempfile.TemporaryDirectory(prefix="voicevox-graph-selftest-") as directory:
        source, target = Path(directory) / "source.vvm", Path(directory) / "candidate.vvm"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("vocoder.onnx", b"fixture source")
            archive.writestr("manifest.json", b"fixture metadata")
            archive.writestr("predictor.onnx", b"fixture predictor")
        source_bytes = source.read_bytes()
        module = sys.modules[__name__]
        with unittest.mock.patch.object(module, "graph_transform_vocoder_bytes", return_value=(b"fixture changed", {})):
            info = graph_prepare_candidate(source, target)
        graph_require(source.read_bytes() == source_bytes, "source archive mutated")
        with zipfile.ZipFile(target) as archive:
            graph_require(archive.read("vocoder.onnx") == b"fixture changed", "candidate was not embedded")
            graph_require(archive.read("predictor.onnx") == b"fixture predictor", "predictor was modified")
        graph_require(info["source_vvm_sha256"] == hashlib.sha256(source_bytes).hexdigest(), "source provenance")
        try:
            graph_prepare_candidate(source, source)
        except ValueError:
            pass
        else:
            raise AssertionError("In-place model overwrite accepted")
    with tempfile.TemporaryDirectory(prefix="voicevox-graph-failure-selftest-") as directory:
        output = Path(directory) / "report.html"
        checks = {"modes": {"original": {"pcm_exact": True, "fp32_exact": True},
                            "combined": {"pcm_exact": True, "fp32_exact": False}}}
        try:
            require_graph_exactness(checks, output)
        except RuntimeError:
            pass
        else:
            raise AssertionError("FP32 mismatch accepted as a graph improvement")
        graph_require(json.loads(output.with_suffix(".failed-checks.json").read_text()) == checks,
                      "failed numeric output checks were not persisted")
    counts = []
    modes = [Mode("original", "Original", 1, "fixture"), Mode("combined", "Combined", 1, "fixture")]
    calls = 0
    def interrupted_fixture(mode: Mode) -> tuple[float, float, CpuMeasurement]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("intentional fixture interruption")
        return 0.1, 1.0, CpuMeasurement(0.0, 0.1, 0.0, 0.0, 1, 1, False, ())
    try:
        run_schedule(modes, 1, interrupted_fixture, [], balanced=True,
                     checkpoint=lambda completed: counts.append(len(completed)))
    except RuntimeError as error:
        graph_require("intentional fixture" in str(error), "unexpected checkpoint fixture error")
    else:
        raise AssertionError("Interruption fixture did not interrupt")
    graph_require(counts == [1, 2], "completed trials were not checkpointed before interruption")
    progress("Graph opt-in, input guards, source preservation, and private archive fixtures passed")


def verify_research_revectorization(entry: dict[str, Any], mode: Mode, options: dict[str, Any], url: str, work: Path, output: Path, style: int, require_on: bool = True) -> dict[str, Any]:
    records = {}
    for enable, name in (((False, 'off'), (True, 'on')) if require_on else ((False, 'off'),)):
        config_path = work / (mode.key + '-' + name + '-diagnostic.json')
        result_path = work / (mode.key + '-' + name + '-engine.json')
        config = {'options': options, 'url': url, 'threads': mode.threads, 'style': style, 'enable': enable,
                  'module': '/' + mode.key + '/voicevox_benchmark.js', 'model_url': '/' + mode.key + '/sample.vvm',
                  'fixed_shape': mode.fixed_shape, 'spin_off': mode.spin_off, 'model_target': entry.get('model_target', 'vocoder'),
                  'xnn_threads': mode.threads if mode.execution_provider == 'XNNPACK' else 0,
                  'profile': False, 'xnnpack': False, 'wav': str(work / (mode.key + '-diagnostic.wav')),
                  'result': str(result_path)}
        atomic_write(config_path, json.dumps(config).encode())
        env = dict(os.environ); env['DEBUG'] = 'pw:browser'
        progress('Untimed V8 transformation check: ' + mode.key + ' ' + name)
        captured = checked_run([sys.executable, str(Path(__file__).resolve()), '--diagnostic-config', str(config_path)], env=env)
        trace_path = output.parent / (mode.key + '-' + name + '-v8.log')
        atomic_write(trace_path, captured.encode())
        info = json.loads(result_path.read_text())
        nodes = [int(value) for value in re.findall(r'Decide(?:d)? to vectorize, ([1-9][0-9]*) revectorizable nodes', captured)]
        rejected = bool(re.search(r'(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|Error:.*(?:wasm-revectorize|trace-wasm-revectorize)', captured, re.I))
        expected = '--js-flags=' + ('--wasm-revectorize' if enable else '--no-wasm-revectorize') + ',--trace-wasm-revectorize'
        records[name] = {**info, 'transformed_groups': len(nodes), 'revectorizable_nodes': sum(nodes), 'flag_rejected': rejected,
                         'launch_configuration_matches': info['requested_js_flags'] == [expected],
                         'trace_sha256': sha256_file(trace_path), 'trace_file': trace_path.name}
    if 'on' not in records:
        off = records['off']
        records['off_verified'] = off['transformed_groups'] == 0 and not off['flag_rejected'] and off['launch_configuration_matches']
        records['on_verified'] = False
        records['verified'] = records['off_verified']
        return records
    off, on = records['off'], records['on']
    records['off_verified'] = off['transformed_groups'] == 0 and not off['flag_rejected'] and off['launch_configuration_matches']
    records['on_verified'] = records['off_verified'] and on['transformed_groups'] > 0 and not on['flag_rejected'] and on['launch_configuration_matches'] and (off['product'], off['js_version']) == (on['product'], on['js_version'])
    records['verified'] = (off['transformed_groups'] == 0 and on['transformed_groups'] > 0 and
        all(x['launch_configuration_matches'] and not x['flag_rejected'] for x in (off, on)) and
        (off['product'], off['js_version']) == (on['product'], on['js_version']))
    return records


def run_research(args: argparse.Namespace) -> None:
    """Compare manifest-declared cached CORE variants without changing any precision flags.

    Each entry identifies a cached validated build, model archive, threads, provider,
    shape policy, and an explicit V8 revectorization setting. Models/audio stay local.
    All timing records are checkpointed before validation, and no waveform is saved
    in the output directory. This is a browser CORE benchmark, not ORT Web JS.
    """
    research_trials_per_block = 1 if args.research_screen else TRIALS_PER_BLOCK
    from playwright.sync_api import sync_playwright
    import psutil
    manifest = json.loads(args.research_manifest.read_text('utf-8'))
    entries = manifest['variants']
    if not 2 <= len(entries) <= 7:
        raise ValueError('Research needs two to seven same-host modes')
    if any(not isinstance(entry.get('key'), str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', entry['key']) for entry in entries):
        raise ValueError('Unsafe or invalid research mode key')
    modes = [Mode(entry['key'], entry['label'], entry.get('threads', args.threads),
                  entry.get('description', 'isolated FP32 research variant'),
                  experimental=index > 0, fixed_shape=entry.get('fixed_shape', False),
                  spin_off=entry.get('spin_off', False),
                  execution_provider=entry.get('provider', 'CPU'),
                  revectorize=entry.get('revectorize', False)) for index, entry in enumerate(entries)]
    if len({m.key for m in modes}) != len(modes):
        raise ValueError('Duplicate research mode')
    query = json.loads(args.audio_query.read_text('utf-8')) if args.audio_query else prepared_query(args.target_seconds)
    if not isinstance(query, dict) or type(query.get('outputSamplingRate')) is not int or query['outputSamplingRate'] <= 0:
        raise ValueError('AudioQuery needs a positive integer outputSamplingRate')
    query_bytes = json.dumps(query, separators=(',', ':'), ensure_ascii=False).encode()
    if manifest.get('fixture_query_sha256', hashlib.sha256(query_bytes).hexdigest()) != hashlib.sha256(query_bytes).hexdigest():
        raise ValueError('AudioQuery differs from the graph preparation input')
    environment = environment_info()
    report_manifest = {key: value for key, value in manifest.items() if key != 'variants'}
    report_manifest['variants'] = [{key: value for key, value in entry.items() if key not in ('binary', 'model')}
                                   for entry in entries]
    environment.update({'research_manifest': report_manifest, 'audio_query_sha256': hashlib.sha256(query_bytes).hexdigest(),
                        'precision_flags': 'FP32; no quantization, FP16, relaxed SIMD or approximate fast-math',
                        'cpu_padding_seconds': CPU_PADDING_S, 'cpu_sample_interval_ms': CPU_SAMPLE_INTERVAL_S * 1000,
                        'cpu_metric': 'sum target Chromium process-tree user+system CPU seconds/action snapshot seconds; 100%=one logical CPU',
                        'browser_cpu_affinity': psutil.Process().cpu_affinity() if hasattr(psutil.Process(), 'cpu_affinity') else None,
                        'available_logical_cpus': logical_cpu_count(),
                        'core_variants': {}, 'wasm_bytes': {}})
    log = []
    trials = []
    results_path = args.output.with_suffix('.json')
    previous_browser_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(args.cache_dir.resolve() / 'playwright')
    try:
        if not args.browser_path:
            progress('Preparing cached Chromium for research')
            checked_run([sys.executable, '-m', 'playwright', 'install', 'chromium'], env=dict(os.environ))
        with tempfile.TemporaryDirectory(prefix='voicevox-research-private-') as temporary:
            work = Path(temporary)
            atomic_write(work / 'query.json', query_bytes)
            atomic_write(work / 'index.html', BROWSER_PAGE.encode())
            atomic_write(work / 'worker.js', BROWSER_WORKER.encode())
            for entry, mode in zip(entries, modes):
                binary = Path(entry['binary']).resolve()
                model = Path(entry['model']).resolve()
                receipt = json.loads((binary.parent / 'complete.json').read_text())
                if binary.name not in receipt['files'] or receipt['identity'] != entry['build_identity']:
                    raise ValueError('Binary is not the manifest-pinned validated CORE build')
                for name, digest in receipt['files'].items():
                    if sha256_file(binary.parent / name) != digest:
                        raise ValueError('Cached CORE build integrity mismatch')
                if sha256_file(model) != entry['model_sha256']:
                    raise ValueError('Model archive hash mismatch')
                folder = work / mode.key
                folder.mkdir()
                for path in binary.parent.glob('voicevox_benchmark.*'):
                    if path.suffix in {'.wasm', '.js'}:
                        shutil.copy2(path, folder / path.name)
                shutil.copyfile(model, folder / 'sample.vvm')
                environment['core_variants'][mode.key] = {'identity': receipt['identity'], 'files': receipt['files']}
                environment['wasm_bytes'][mode.key] = binary.with_suffix('.wasm').stat().st_size
            with serve_assets(work) as url, sync_playwright() as playwright:
                runners, browsers, cpu_roots = {}, {}, {}
                environment['v8_activation_diagnostics'] = {}
                diagnostic_cache = {}
                try:
                    for entry, mode in zip(entries, modes):
                        identity = (json.dumps(environment['core_variants'][mode.key], sort_keys=True), entry['model_sha256'], mode.threads,
                                    mode.fixed_shape, mode.spin_off, mode.execution_provider, entry.get('model_target', 'vocoder'), mode.revectorize)
                        if identity not in diagnostic_cache:
                            options = {'headless': not args.headed}
                            if args.browser_path: options['executable_path'] = str(args.browser_path)
                            try:
                                diagnostic_cache[identity] = verify_research_revectorization(entry, mode, options, url, work, args.output, args.style_id, require_on=mode.revectorize)
                            except RuntimeError as error:
                                if args.graph_experiments:
                                    raise V8VerificationError('V8 activation check failed for ' + mode.key + ': ' + str(error)) from error
                                raise
                        environment['v8_activation_diagnostics'][mode.key] = diagnostic_cache[identity]
                        atomic_write(args.output.with_suffix('.diagnostics.json'), json.dumps(environment['v8_activation_diagnostics'], indent=2).encode())
                        if not diagnostic_cache[identity]['on_verified' if mode.revectorize else 'off_verified']:
                            raise V8VerificationError('V8 activation could not be verified for research mode ' + mode.key)

                    for entry, mode in zip(entries, modes):
                        jsflag = '--wasm-revectorize' if mode.revectorize else '--no-wasm-revectorize'
                        options = {'headless': not args.headed, 'args': ['--js-flags=' + jsflag]}
                        if args.browser_path:
                            options['executable_path'] = str(args.browser_path)
                        progress('Initializing research ' + mode.key)
                        browser = playwright.chromium.launch(**options)
                        browsers[mode.key] = browser
                        cpu_roots[mode.key] = chromium_process_id(browser)
                        environment['browser'] = browser.version
                        environment[mode.key + '_engine'] = browser_engine_info(browser)
                        actual = environment[mode.key + '_engine']
                        checked = environment['v8_activation_diagnostics'][mode.key]['on' if mode.revectorize else 'off']
                        if (actual['product'], actual['js_version']) != (checked['product'], checked['js_version']):
                            raise RuntimeError('Timed browser engine differs from V8 diagnostic')
                        environment[mode.key + '_v8_flags'] = options['args']
                        runner = BrowserRunner(browser, url, '/' + mode.key + '/voicevox_benchmark.js', mode.threads, True,
                                               args.style_id, work / (mode.key + '.wav'), fixed_shape=mode.fixed_shape,
                                               spin_off=mode.spin_off, xnn_threads=mode.threads if mode.execution_provider == 'XNNPACK' else 0,
                                               model_url='/' + mode.key + '/sample.vvm', model_target=entry.get('model_target', 'vocoder'))
                        runners[mode.key] = runner
                        environment[mode.key + '_hardware_concurrency'] = runner.page.evaluate('navigator.hardwareConcurrency')
                        for _ in range(3):
                            elapsed, duration = runner.synthesize(save=True)
                            log.append({'event': 'warmup_excluded', 'mode': mode.key, 'seconds': elapsed, 'audio_s': duration})
                        environment[mode.key + '_pthreads'] = runner.thread_state()
                        environment[mode.key + '_runtime'] = {'browser': browser.version, 'fixed_length': runner.fixed_length,
                            'fixed_matches': runner.fixed_matches, 'xnn_threads': runner.xnn_threads,
                            'xnn_sessions': runner.xnn_sessions, 'spin_off': runner.spin_off, 'shared_memory': runner.shared_memory,
                            'model_target': runner.model_target, 'vocoder_fixed_matches': runner.vocoder_fixed_matches,
                            'vocoder_xnn_sessions': runner.vocoder_xnn_sessions, 'ort_global_threads': 1 if runner.xnn_threads else mode.threads}
                    reference = modes[0].key
                    output_checks = verify_outputs(runners, reference, log)
                    spectrograms = output_checks.pop('spectrograms')
                    for key, repeated in output_checks['per_mode_repeat'].items():
                        if not repeated['pcm_exact'] or not repeated['fp32_exact']:
                            atomic_write(args.output.with_suffix('.failed-checks.json'), json.dumps(output_checks, indent=2).encode())
                            raise RuntimeError('Research mode is not bitwise repeatable: ' + key)
                    if args.graph_experiments:
                        require_graph_exactness(output_checks, args.output)
                    if not output_checks['reference_deterministic']:
                        raise RuntimeError('Research control is not bitwise repeatable; investigate before timing')
                    durations = [runner.duration for runner in runners.values()]
                    if max(durations) - min(durations) > 1 / query['outputSamplingRate']:
                        raise RuntimeError('Research output durations differ')
                    result = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                              'modes': [asdict(mode) for mode in modes], 'trials': [], 'measurement_status': 'measuring', 'environment': environment,
                              'research_screen': args.research_screen, 'trials_per_block': research_trials_per_block,
                              'audio_s': durations[0], 'style_id': args.style_id, 'seed': args.seed, 'log': log,
                              'output_checks': output_checks, 'spectrograms': spectrograms,
                              'schedule_method': 'seeded position-balanced and pair-order-balanced design; randomized labels and round order',
                              'schedule': [asdict(block) for block in make_schedule(modes, args.seed, balanced=True)],
                              'notes': [f'Same-host interleaved control/candidate, {research_trials_per_block * BLOCKS_PER_MODE} trials in 3 blocks; explicit V8 OFF/ON. Preliminary screen only.' if args.research_screen else 'Same-host interleaved control/candidate, 15 trials in 3 blocks; explicit V8 OFF/ON.',
                                        'Raw FP32 and PCM differences are measured, not assumed acceptable. Only numeric metrics and PNGs are published.']}
                    atomic_write(results_path, json.dumps(result, ensure_ascii=False, indent=2).encode())
                    if args.measurement_gate:
                        progress('Ready for quiet measurement window; waiting for gate ' + str(args.measurement_gate))
                        while not args.measurement_gate.exists():
                            time.sleep(1)
                    def measured(mode):
                        return ProcessCpuSampler(cpu_roots[mode.key], mode.label).measure(runners[mode.key].synthesize)
                    # Write each completed block/trial before any whole-run validation.
                    order = 0
                    for block in make_schedule(modes, args.seed, balanced=True):
                        mode = next(m for m in modes if m.key == block.mode)
                        progress('Research block ' + mode.key + ' ' + str(block.number))
                        for number in range(1, research_trials_per_block + 1):
                            order += 1
                            elapsed, duration, cpu = measured(mode)
                            trial = Trial(order, mode.key, block.number, number, mode.threads, elapsed, duration,
                                          elapsed / duration, **asdict(cpu))
                            trials.append(trial)
                            result['trials'] = [asdict(t) for t in trials]
                            atomic_write(results_path, json.dumps(result, ensure_ascii=False, indent=2).encode())
                            progress(f'Research trial {order}: {mode.key} {elapsed:.6f}s')
                    result['measurement_status'] = 'complete'
                    atomic_write(results_path, json.dumps(result, ensure_ascii=False, indent=2).encode())
                    if args.research_screen:
                        validate_results(result)
                        progress('Preliminary research screen saved to ' + str(results_path) + '; no confirmed speedup claim')
                    else:
                        save_measurements_and_report(result, args.output)
                finally:
                    for runner in runners.values():
                        with contextlib.suppress(Exception): runner.close()
                    for browser in browsers.values():
                        with contextlib.suppress(Exception): browser.close()
    finally:
        if previous_browser_path is None: os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
        else: os.environ['PLAYWRIGHT_BROWSERS_PATH'] = previous_browser_path


def main() -> None:
    args = argument_parser().parse_args()
    if args.diagnostic_config:
        revectorization_diagnostic_child(args.diagnostic_config)
        return
    validate_arguments(args)
    if args.output.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("--output must end with .html or .htm")
    if args.self_test:
        self_test()
        return
    if args.report_from:
        result = json.loads(args.report_from.read_text("utf-8"))
        if result.get("research_screen"):
            raise ValueError("Preliminary screen JSON is not a final 15-trial report; run rigorous confirmation first")
        if "audio_outputs" in result:
            raise ValueError("Legacy embedded audio must be removed before rendering; use local WAVs or PNG-only spectrograms")
        if args.audio_dir and args.spectrogram_dir:
            raise ValueError("Choose either --audio-dir or --spectrogram-dir")
        if args.audio_dir:
            attach_saved_audio(result, args.audio_dir)
        if args.spectrogram_dir:
            attach_saved_spectrograms(result, args.spectrogram_dir)
        render_report(result, args.output)
        print(f"Report: {args.output.resolve()}")
        return
    if args.audio_dir or args.spectrogram_dir:
        raise ValueError("--audio-dir and --spectrogram-dir are only used with --report-from")
    if args.kernel_experiments:
        run_kernel_experiments(args)
    elif args.graph_experiments:
        run_graph_experiments(args)
    elif args.research_manifest:
        run_research(args)
    else:
        run_benchmark(args)


# ---------------------------------------------------------------------------
# Optional strict-FP32 runtime selector experiment (Linux / WSL, x86-64 only).
# This adapter does not participate in any original or graph benchmark mode.
# The readable embedded sources below are byte-pinned research provenance,
# materialized privately and removed after use. No helper-code download occurs.
# ---------------------------------------------------------------------------
KERNEL_GRAPH_BASE_SHA256 = "bd24cf78b68ff2db97dbd0c0b32e5d5678ced862c1403e15da2dd6f891dca62a"
KERNEL_BASE_SHA256 = "d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe"
KERNEL_BROWSER_SHA256 = "2ca8db10524e87156d5f18f7dfa94783c7f5726f53431b778723f1ef43063dee"
KERNEL_MODEL_SHA256 = "51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9"
KERNEL_MODES = ("original", "auto_roundtrip", "loadsplat")
_KERNEL_CLEANUP_UNVERIFIED = False  # Serial runtime commands only; fail-closed retention.


def kernel_require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError("Kernel experiment: " + message)


def validate_kernel_arguments(args: argparse.Namespace) -> None:
    enabled = args.kernel_experiments
    if not enabled:
        kernel_require(args.kernel_revectorize == "off" and not args.kernel_profile_xnn,
                       "--kernel-revectorize/--kernel-profile-xnn require --kernel-experiments")
        return
    kernel_require(__debug__, "Python optimization (-O/PYTHONOPTIMIZE) disables research guards; run without it")
    kernel_require(not any((args.experiments, args.baseline_only, args.backend_experiments,
                            args.graph_experiments, args.research_manifest, args.report_from,
                            args.research_screen, args.measurement_gate, args.headed,
                            args.audio_dir, args.spectrogram_dir)),
                   "choose --kernel-experiments separately from other experiment/report options; headless only")
    kernel_require(args.graph_revectorize == "off", "--graph-revectorize belongs to the graph option")
    kernel_require(args.threads == 2, "the validated runtime fixture requires --threads 2 (global ORT1 / XNN2)")
    kernel_require(args.style_id == 302 and args.target_seconds == 10 and args.audio_query is None,
                   "the runtime fixture requires Style302 and built-in prepared_query(10); no custom AudioQuery")
    kernel_require(not any((args.threaded_ort_archive, args.threaded_ort_url,
                            args.xnnpack_ort_archive, args.xnnpack_ort_url)),
                   "runtime archive overrides are unsupported; the selector requires the exact released archive")
    kernel_require(not (args.prepare_only and args.kernel_profile_xnn),
                   "provider profiling requires inference; omit --prepare-only")
    kernel_require(sys.platform.startswith("linux") and platform.machine().lower() in {"x86_64", "amd64"},
                   "runtime preparation currently supports x86-64 Linux/WSL only; ordinary modes are unchanged")


def kernel_materialize_sources(destination: Path) -> dict[str, Path]:
    """Verify every embedded byte string before executing any of its code."""
    kernel_require(set(KERNEL_EMBEDDED_SOURCES) == set(KERNEL_SOURCE_SHA256), "source catalogue differs from pins")
    result = {}
    for relative, source in KERNEL_EMBEDDED_SOURCES.items():
        data = source.encode("utf-8")
        kernel_require(hashlib.sha256(data).hexdigest() == KERNEL_SOURCE_SHA256[relative],
                       "embedded source SHA-256 mismatch: " + relative)
        path = destination / relative
        kernel_require(path.resolve().is_relative_to(destination.resolve()), "unsafe helper path")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
        kernel_require(sha256_file(path) == KERNEL_SOURCE_SHA256[relative], "materialized source changed")
        result[relative] = path
    return result


class KernelProcessCleanupError(RuntimeError):
    """Child processes could still own private files; retain their directory."""


@contextlib.contextmanager
def kernel_private_directory() -> Iterator[str]:
    path = Path(tempfile.mkdtemp(prefix="voicevox-kernel-private-"))
    remove = True
    try:
        yield str(path)
    except BaseException as error:
        if _KERNEL_CLEANUP_UNVERIFIED or isinstance(error, KernelProcessCleanupError):
            remove = False
            raise KernelProcessCleanupError("Subprocess cleanup is unverified; private files retained at " + str(path)) from error
        raise
    finally:
        if remove:
            shutil.rmtree(path)


def kernel_process_snapshot() -> dict[int, dict[str, Any]]:
    records = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            pid = int(path.parent.name)
            records[pid] = {'pid': pid, 'state': fields[0], 'parent': int(fields[1]),
                            'group': int(fields[2]), 'start': int(fields[19])}
        except (OSError, ValueError, IndexError):
            continue
    return records


@contextlib.contextmanager
def kernel_subprocess_owner() -> Iterator[set[tuple[int, int]]]:
    """Adopt orphaned descendants, including Chromium's detached Linux group.

    PR_SET_CHILD_SUBREAPER affects only this Python process, is restored afterward,
    and gives this serial command runner a verifiable cleanup ownership boundary.
    Pre-existing direct children are excluded by PID plus Linux start-time ticks.
    """
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot query child-subreaper state')
    baseline = {(row['pid'], row['start']) for row in kernel_process_snapshot().values()
                if row['parent'] == os.getpid()}
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot establish private subprocess ownership')
    try:
        yield baseline
    finally:
        if libc.prctl(36, previous.value, 0, 0, 0) != 0:
            raise KernelProcessCleanupError('Could not restore child-subreaper state')


def kernel_stop_process_tree(process: subprocess.Popen, baseline_children: set[tuple[int, int]],
                             grace_seconds: float = 5.0) -> None:
    """Terminate/reap owned live descendants, including separately detached groups."""
    import signal
    owned: dict[int, int] = {}
    owner = os.getpid()
    def capture() -> list[dict[str, Any]]:
        snapshot = kernel_process_snapshot()
        selected = set()
        for row in snapshot.values():
            identity = (row['pid'], row['start'])
            if (row['pid'] == process.pid or row['group'] == process.pid
                    or (row['parent'] == owner and identity not in baseline_children)
                    or owned.get(row['pid']) == row['start']):
                selected.add(row['pid'])
        while True:
            descendants = {row['pid'] for row in snapshot.values() if row['parent'] in selected}
            if descendants <= selected:
                break
            selected |= descendants
        for pid in selected:
            owned[pid] = snapshot[pid]['start']
        for pid, start in owned.items():
            if pid not in snapshot and Path('/proc', str(pid)).exists():
                raise KernelProcessCleanupError('An owned process exists but its identity cannot be read')
        rows = [snapshot[pid] for pid in selected]
        # Reap only adopted, already terminated direct children. Popen owns its leader.
        for row in rows:
            if row['parent'] == owner and row['pid'] != process.pid and row['state'] in {'Z', 'X'}:
                with contextlib.suppress(ChildProcessError, ProcessLookupError):
                    os.waitpid(row['pid'], os.WNOHANG)
        return [row for row in rows if row['state'] not in {'Z', 'X'}]
    def signal_rows(rows: list[dict[str, Any]], sig: int) -> None:
        for row in rows:
            current = kernel_process_snapshot().get(row['pid'])
            if current and current['start'] == row['start']:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(row['pid'], sig)
    try:
        sent_term = set()
        deadline = time.monotonic() + grace_seconds
        while rows := capture():
            newcomers = [row for row in rows if (row['pid'], row['start']) not in sent_term]
            signal_rows(newcomers, signal.SIGTERM)
            sent_term.update((row['pid'], row['start']) for row in newcomers)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        deadline = time.monotonic() + 5.0
        while rows := capture():
            signal_rows(rows, signal.SIGKILL)
            if time.monotonic() >= deadline:
                raise KernelProcessCleanupError('Owned subprocesses survived termination')
            time.sleep(0.05)
        process.wait(timeout=5)
        if capture():
            raise KernelProcessCleanupError('Subprocess-tree cleanup could not be verified')
    except BaseException as error:
        # Even a second Ctrl-C or unexpected shutdown error must preserve private
        # files until the user can confirm that the owned processes have stopped.
        if isinstance(error, KernelProcessCleanupError):
            raise
        raise KernelProcessCleanupError('Subprocess-tree cleanup did not finish') from error


def kernel_run_checked(command: list[str], log_path: Path, env: dict[str, str]) -> None:
    """Checkpoint output and verify all owned subprocesses exited before cleanup."""
    global _KERNEL_CLEANUP_UNVERIFIED
    kernel_require(not _KERNEL_CLEANUP_UNVERIFIED, "an earlier subprocess cleanup remains unverified")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('w', encoding='utf-8') as log, kernel_subprocess_owner() as baseline:
        # Retention is independent of the propagated exception: log flush/close
        # errors must not mask failed cleanup and allow private-file deletion.
        _KERNEL_CLEANUP_UNVERIFIED = True
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                   start_new_session=True)
        try:
            code = process.wait()
        except BaseException:
            kernel_stop_process_tree(process, baseline)
            _KERNEL_CLEANUP_UNVERIFIED = False
            raise
        else:
            # Chromium can detach; verify descendants even when its Python
            # helper exits normally or fails before orderly browser shutdown.
            kernel_stop_process_tree(process, baseline)
            _KERNEL_CLEANUP_UNVERIFIED = False
        finally:
            log.flush()
            os.fsync(log.fileno())
    if code:
        raise RuntimeError(f'Kernel experiment step failed (exit {code}); diagnostic log: {log_path}')


def kernel_check_report_privacy(value: Any) -> None:
    """Numerical evidence can contain traces, never waveform/model payloads."""
    forbidden = {"wav_base64", "audio_outputs", "raw_samples", "pcm_samples", "fp32_samples",
                 "model_bytes", "tensor_data", "weights", "audio_base64", "raw_wave", "wav_bytes"}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"fp32_samples", "pcm_samples"}:
                # waveform_comparison reports a scalar sample count here,
                # never an array. Permit the count while rejecting payloads.
                kernel_require(type(item) is int and item >= 0, "sample counts must be nonnegative integers, never arrays")
                continue
            kernel_require(str(key).lower() not in forbidden, "private payload key in report")
            kernel_check_report_privacy(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            kernel_check_report_privacy(item)
    elif isinstance(value, str):
        kernel_require("data:audio/" not in value.lower(), "embedded audio URL in report")
    elif isinstance(value, float):
        kernel_require(math.isfinite(value), "nonfinite report metric")


def kernel_check_artifacts(directory: Path) -> None:
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        kernel_require(path.suffix.lower() in {".json", ".log", ".html", ".png"}, "unexpected result file type")
        with path.open("rb") as stream:
            prefix = stream.read(12)
        kernel_require(not prefix.startswith((b"RIFF", b"fLaC", b"OggS")), "audio magic in result file")
        if path.suffix.lower() == ".json":
            kernel_check_report_privacy(json.loads(path.read_text("utf-8")))
        elif path.suffix.lower() in {".log", ".html"}:
            text = path.read_text("utf-8", errors="replace")
            kernel_require(not any(token in text.lower() for token in ("wav_base64", "data:audio/", '"audio_outputs"')),
                           "audio payload marker in result file")


def kernel_summary(result: dict[str, Any]) -> dict[str, Any]:
    confirmation = result["confirmation"]
    rows = confirmation["trials"]
    paired = {}
    for row in rows:
        paired.setdefault((row["process_set"], row["pair"]), {})[row["mode"]] = row["elapsed_s"]
    values = {}
    for mode in KERNEL_MODES:
        observations = [row["elapsed_s"] for row in rows if row["mode"] == mode]
        values[mode] = {"observations": len(observations), "median_seconds": statistics.median(observations)}
    comparisons = {}
    for control in KERNEL_MODES[:2]:
        reductions = [100 * (1 - row["loadsplat"] / row[control]) for row in paired.values()]
        clusters = []
        for process_set in (1, 2, 3):
            ratios = [row[control] / row["loadsplat"] for (group, _), row in paired.items() if group == process_set]
            clusters.append({"process_set": process_set, "paired_observations": len(ratios),
                             "geomean_speedup": statistics.geometric_mean(ratios)})
        comparisons[control] = {"median_paired_reduction_percent": statistics.median(reductions),
                                "process_set_effects": clusters}
    dispatch = [dict(process_set=group["id"], mode=mode, **runtime["dispatch"])
                for group in confirmation["process_sets"] for mode, runtime in group["runtime"].items()]
    companions = confirmation["cpu_companion"]
    cpu_complete = len(companions) == 9 and all(
        row.get("cpu_incomplete") is False and row.get("cpu_trace")
        and all(point.get("cpu_incomplete") is False for point in row["cpu_trace"])
        for row in companions)
    return {"timings": values, "loadsplat_vs_control": comparisons, "dispatch": dispatch,
            "cpu_companion_coverage_complete": cpu_complete,
            "selector_is_noop_on_this_worker": all(row["actual_dispatch"] == "loadsplat" for row in dispatch),
            "xnn_vs_cpu": result["cpu_reference"]["xnn_vs_cpu"]}


def render_kernel_report(result: dict[str, Any], output: Path) -> None:
    """Self-contained numerical report; no model, WAV, PCM or FP32 payloads."""
    kernel_check_report_privacy(result)
    summary = result["summary"]
    esc = lambda value: html.escape(str(value))
    rows = "".join(f"<tr><td>{esc(mode)}</td><td>{row['observations']}</td><td>{row['median_seconds']:.6f}</td></tr>"
                   for mode, row in summary["timings"].items())
    effects = "".join(f"<li>loadsplat vs {esc(control)}: median paired latency reduction "
                      f"{value['median_paired_reduction_percent']:.2f}%</li>"
                      for control, value in summary["loadsplat_vs_control"].items())
    dispatch = "".join(f"<tr><td>{row['process_set']}</td><td>{esc(row['mode'])}</td>"
                       f"<td>{esc(row['actual_dispatch'])}</td><td>{row['worker_hardware_concurrency']}</td>"
                       f"<td>{row['checked_pointers']}</td></tr>" for row in summary["dispatch"])
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=["process_set", "pair", "mode", "elapsed_s", "audio_s"])
    writer.writeheader()
    writer.writerows({key: row[key] for key in writer.fieldnames} for row in result["confirmation"]["trials"])
    # CPU measurements belong to separate instrumented companions, never the
    # primary latency comparison. Preserve their complete sampled traces.
    companions = result["confirmation"]["cpu_companion"]
    traces = esc(json.dumps(companions, indent=2))
    cpu_coverage = ("All nine companions have captured traces with no reported sampling gap."
                    if summary["cpu_companion_coverage_complete"] else
                    "CPU companion coverage is incomplete or unverified; inspect cpu_incomplete and captured trace fields. Primary latencies remain separate.")
    cpu_check = esc(json.dumps(summary["xnn_vs_cpu"], indent=2))
    cluster_check = esc(json.dumps(summary["loadsplat_vs_control"], indent=2))
    noop = "All actual workers already selected loadsplat; the forced selector is a no-op here." if summary["selector_is_noop_on_this_worker"] else "Actual worker pointer selections are shown below."
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOICEVOX strict FP32 selector experiment</title><style>body{{font:16px system-ui;max-width:1100px;margin:auto;padding:24px;color:#172334}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;border-bottom:1px solid #ccd;padding:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f7;padding:12px}}details{{margin:18px 0}}h1{{font-size:26px}}</style>
<h1>Strict FP32 XNN runtime selector experiment</h1><p>V8 revectorization: {esc(result['revectorization']).upper()}. Original model; fixed query-derived vocoder length; global ORT1 / XNN2; spinning off; Style302; prepared_query(10).</p>
<p>Experimental, host-specific evidence. No general speedup is assumed. Only the released ordinary-SIMD FP32 GEMM/IGEMM pointer selector changes; kernels and the real pthreadpool remain byte-identical. All candidates repeated exactly and matched untouched XNN in FP32 and PCM before timing and again afterward.</p>
<p>Three fresh process sets × five paired rounds per condition. Primary latency has no high-frequency CPU sampler. All observations, including faults and host swap, are retained. Warmups do not prove tiering convergence. Negative reduction means slower.</p>
<table><thead><tr><th>Runtime</th><th>Observations</th><th>Median seconds</th></tr></thead><tbody>{rows}</tbody></table><ul>{effects}</ul>
<p>{esc(noop)}</p><table><thead><tr><th>Process set</th><th>Runtime</th><th>Actual dispatcher</th><th>Worker hardwareConcurrency</th><th>Checked pointers</th></tr></thead><tbody>{dispatch}</tbody></table>
<h2>Same-run CPU reference</h2><p>Loadsplat exactness is relative to untouched XNN. CPU and XNN are separate execution providers and may differ. These are the measured same-run errors, not an assumption of CPU bit-exactness:</p><pre>{cpu_check}</pre>
<details><summary>Paired effects and process-set clusters</summary><pre>{cluster_check}</pre></details>
<details><summary>All primary observations (CSV)</summary><pre>{esc(csv_buffer.getvalue())}</pre></details>
<h2>CPU companion coverage</h2><p>{esc(cpu_coverage)}</p>
<details><summary>Separate instrumented CPU companions and all captured time series (JSON)</summary><p>These companion latencies are excluded from the primary comparison. CPU traces are process-tree samples, not per-kernel CPU attribution. 100% denotes one logical CPU.</p><pre>{traces}</pre></details>
<p>Full numeric checks, build receipts, actual worker evidence and diagnostic logs are saved alongside this report. Private temporary waveforms and generated helpers are removed. Do not publish the runtime/tool/model cache.</p></html>'''
    atomic_write(output, document.encode("utf-8"))


def run_kernel_experiments(args: argparse.Namespace) -> None:
    """Complete opt-in preparation, output gates, timing, and CPU-reference path."""
    validate_kernel_arguments(args)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = output.parent / (output.stem + ".kernel")
    # Never combine a new failed run with stale successful evidence.
    kernel_require(not evidence.exists(), f"result directory already exists: {evidence}; choose a new --output")
    kernel_require(not output.exists() and not output.with_suffix(".json").exists(), "choose an unused --output")
    cache = args.cache_dir.resolve()
    kernel_require(not cache.is_relative_to(evidence) and not evidence.is_relative_to(cache), "keep --output outside --cache-dir")
    evidence.mkdir()
    cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop("PYTHONOPTIMIZE", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache / "playwright")
    result = {"schema": "voicevox-standalone-kernel-experiment-v1", "status": "initializing",
              "revectorization": args.kernel_revectorize, "created_at": datetime.now(timezone.utc).isoformat(),
              "standalone_sha256": sha256_file(Path(__file__)), "embedded_source_sha256": dict(KERNEL_SOURCE_SHA256),
              "precision": "strict ordinary-SIMD FP32; no FP16, quantization, relaxed SIMD, approximate math, or FMA contraction",
              "fixture": {"style_id": 302, "target_seconds": 10, "global_ort_threads": 1, "xnn_threads": 2,
                          "spin_off": True, "model_target": "vocoder", "fixed_length": "derived from query; not hardcoded"}}
    receipt_path = evidence / "execution.json"
    def checkpoint():
        kernel_check_report_privacy(result)
        atomic_write(receipt_path, json.dumps(result, indent=2, allow_nan=False).encode())
    private = None
    checkpoint()
    try:
        with kernel_private_directory() as temporary:
            private = Path(temporary)
            # Child-created waveform/model/profile temporary directories must
            # remain inside our cleanup scope, including interrupted children.
            child_temporary = private / "temporary"
            child_temporary.mkdir(mode=0o700)
            for variable in ("TMPDIR", "TMP", "TEMP"):
                env[variable] = str(child_temporary)
            sources = kernel_materialize_sources(private)
            base = sources["voicevox_webcpu_research.py"]
            prepare = sources["runtime/prepare_vocoder_runtime.py"]
            browser = private / "browser_runtime.py"
            manifest_path = private / "runtime.json"
            # No inference occurs in either preparation command.
            result["stage"] = "prepare original checked CORE assets"; checkpoint()
            kernel_run_checked([sys.executable, str(base), "--prepare-only", "--backend-experiments", "all",
                                "--threads", "2", "--cache-dir", str(cache)], evidence / "prepare-original.log", env)
            assets = json.loads((cache / "research-assets.json").read_text("utf-8"))
            kernel_require(assets["model_sha256"] == KERNEL_MODEL_SHA256, "expected the pinned original model")
            result["stage"] = "prepare selector variants and verify native bundling"; checkpoint()
            kernel_run_checked([sys.executable, str(prepare), "--cache", str(cache), "--output", str(manifest_path),
                                "--harness", str(base), "--kernel-dir", str(private / "kernel"), "--threads", "2"],
                               evidence / "prepare-runtime.log", env)
            kernel_run_checked([sys.executable, str(sources["runtime/apply_browser_dispatch.py"]), str(base),
                                "--output", str(browser), "--helper", str(sources["runtime/core_dispatch_check.js"])],
                               evidence / "generate-browser.log", env)
            kernel_require(sha256_file(browser) == KERNEL_BROWSER_SHA256, "generated browser source pin mismatch")
            manifest = json.loads(manifest_path.read_text("utf-8"))
            kernel_require([row["key"] for row in manifest["variants"]] == list(KERNEL_MODES), "three runtime controls required")
            for flag in ("require_exact_fp32_pcm_to_reference", "require_actual_core_browser_dispatch",
                         "require_repeated_raw_fp32_and_pcm_checks"):
                kernel_require(manifest.get(flag) is True, "missing required runtime gate: " + flag)
            for row in manifest["variants"]:
                row["revectorize"] = args.kernel_revectorize == "on"
            atomic_write(manifest_path, json.dumps(manifest, indent=2).encode())
            # Paths to cached binaries/models are private. Preserve numerical/build
            # provenance, while the live manifest exists only for this invocation.
            result["manifest"] = {key: value for key, value in manifest.items() if key != "variants"}
            result["manifest"]["variants"] = [{key: value for key, value in row.items() if key not in ("model", "binary")}
                                                for row in manifest["variants"]]
            result["generated_browser_sha256"] = sha256_file(browser)
            if args.prepare_only:
                result["status"] = "prepared"
                result["note"] = "No browser pointer proof, inference, output equality, CPU reference or timing has run. Rerun without --prepare-only using a new --output."
                checkpoint()
            else:
                if not args.browser_path:
                    kernel_run_checked([sys.executable, "-m", "playwright", "install", "chromium"],
                                       evidence / "prepare-browser.log", env)
                result["stage"] = "actual CORE browser checks and three-set paired confirmation"; checkpoint()
                command = [sys.executable, str(sources["runtime/browser_runtime_confirmation.py"]),
                           "--harness", str(browser), "--manifest", str(manifest_path), "--cache", str(cache),
                           "--revec", args.kernel_revectorize, "--output", str(evidence / "confirmation.json"),
                           "--seed", str(args.seed)]
                if args.browser_path:
                    command += ["--browser-path", str(args.browser_path.resolve())]
                kernel_run_checked(command, evidence / "confirmation.log", env)
                confirmation = json.loads((evidence / "confirmation.json").read_text("utf-8"))
                kernel_require(confirmation["status"] == "complete" and confirmation.get("private_temporary_directory_deleted") is True,
                               "confirmation is incomplete or private waveform cleanup was not verified")
                diagnostics = evidence / "activation-evidence.json"
                atomic_write(diagnostics, json.dumps(confirmation["diagnostics"], indent=2).encode())
                result["stage"] = "sequential same-run CPU reference (excluded from timings)"; checkpoint()
                cpu = Path(assets["binaries"]["mt"])
                command = [sys.executable, str(sources["runtime/browser_reference_and_profile.py"]),
                           "--harness", str(browser), "--manifest", str(manifest_path), "--cpu-binary", str(cpu),
                           "--cpu-receipt", str(cpu.parent / "complete.json"), "--activation-evidence", str(diagnostics),
                           "--revec", args.kernel_revectorize, "--output", str(evidence / "cpu-reference.json")]
                if args.browser_path:
                    command += ["--browser-path", str(args.browser_path.resolve())]
                if args.kernel_profile_xnn:
                    command += ["--profile-xnn"]
                kernel_run_checked(command, evidence / "cpu-reference.log", env)
                quality = json.loads((evidence / "cpu-reference.json").read_text("utf-8"))
                kernel_require(quality.get("passed") is True and quality.get("private_temporary_directory_deleted") is True,
                               "same-run CPU reference failed or private waveform cleanup was not verified")
                result.update(confirmation=confirmation, cpu_reference=quality, status="validated")
                result["summary"] = kernel_summary(result)
                checkpoint()
        result["private_helpers_deleted"] = private is None or not private.exists()
        result["stage"] = "validate result privacy"
        checkpoint()
        kernel_check_artifacts(evidence)
        result["stage"] = "write final local deliverables"
        checkpoint()
        completed = dict(result, status="prepared_only" if args.prepare_only else "complete", stage="finished")
        if not args.prepare_only:
            render_kernel_report(completed, output)
        atomic_write(output.with_suffix(".json"), json.dumps(completed, indent=2, allow_nan=False).encode())
        result = completed
        checkpoint()
    except BaseException as error:
        result.update(status="failed", error_type=type(error).__name__)
        checkpoint()
        raise
    finally:
        result["private_helpers_deleted"] = private is None or not private.exists()
        checkpoint()
    if args.prepare_only:
        progress("Runtime builds prepared with native-bundle proof; no inference or timing. Receipt: " + str(output.with_suffix(".json")))
    else:
        progress("Experimental runtime report: " + str(output))
        progress("Same-run CPU errors and all raw numeric observations: " + str(output.with_suffix(".json")))


def kernel_regression_self_test() -> None:
    """Source/routing/privacy guards only. No downloads, compilers or inference."""
    import ast
    defaults = argument_parser().parse_args([])
    kernel_require(not defaults.kernel_experiments and defaults.kernel_revectorize == "off", "kernel opt-in defaults changed")
    validate_kernel_arguments(defaults)
    for options in (["--kernel-revectorize", "on"], ["--kernel-profile-xnn"],
                    ["--kernel-experiments", "--threads", "2", "--graph-experiments"],
                    ["--kernel-experiments", "--threads", "4"],
                    ["--kernel-experiments", "--threads", "2", "--audio-query", "private.json"]):
        try:
            validate_kernel_arguments(argument_parser().parse_args(options))
        except ValueError:
            pass
        else:
            raise AssertionError("Unsupported kernel combination accepted: " + str(options))
    with tempfile.TemporaryDirectory(prefix="voicevox-kernel-selftest-") as temporary:
        sources = kernel_materialize_sources(Path(temporary))
        for relative, path in sources.items():
            if relative.endswith(".py"):
                ast.parse(path.read_text("utf-8"))
        kernel_require(sha256_file(sources["voicevox_webcpu_research.py"]) == KERNEL_BASE_SHA256, "canonical base was repackaged incorrectly")
    for fixture in ({"wav_base64": "payload"}, {"nested": {"raw_samples": [0.1]}}, {"url": "data:audio/wav;base64,AA"}):
        try:
            kernel_check_report_privacy(fixture)
        except ValueError:
            pass
        else:
            raise AssertionError("Private report payload accepted")
    progress("Kernel opt-in, embedded-source hashes, syntax and privacy guards passed; no build or inference")


# BEGIN READABLE, EXACT-BYTE RESEARCH SOURCE CATALOGUE
# Each adjacent quoted line is source text, not compressed or downloaded code.
KERNEL_SOURCE_SHA256 = {
    "voicevox_webcpu_research.py": "d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe",
    "runtime/prepare_vocoder_runtime.py": "6b2a20253fc4b90a1d572e701d46c8cbab48870dfb12b9781cea047007c17227",
    "runtime/apply_browser_dispatch.py": "438f528947165594c5eb1485bc20df951a60de7a42395e47289063078f253c81",
    "runtime/core_dispatch_check.js": "abe7453d3feeaf6bc7f489452055a8ba0d02d22c7c158481112c51e0ada10c2b",
    "runtime/browser_runtime_confirmation.py": "0860d6b2e5ef91eea8fa4e3423bd022425ca890198f4f3705f30460b6d3bae3b",
    "runtime/validate_runtime_confirmation.py": "bb735ae25ab7c1338f7509e3967e5d430baa56386b3235875a912ca5cc739572",
    "runtime/browser_reference_and_profile.py": "d893514d2e7b41f1b5b46affd53b58831302370a30b77849d850e02d82e2037d",
    "runtime/test_prepare_vocoder_runtime.py": "93562441770aeda94f76344c22c4185fc2ccd197e301e74cea38a2648b778f1c",
    "runtime/test_runtime_confirmation.py": "19e26ae953ab1905096ab361e5df5bab25638cc88a43bee4d1ccda3e3c57e30c",
    "runtime/log_runtime_confirmation.py": "b1c93ba2635e66125dd52e98a98ed11c416344449083b153d9fb9cfc17ba62e5",
    "kernel/dispatch_probe.c": "c2753086a8d5fae75940a3edeee4d7f94c66c47be73c32459592f86eb1a0629b",
    "kernel/make_archive_variants.py": "183468c51a29746ae50871e2f637ab2fc4c941294358b4531f78c854ea067647"
}

KERNEL_EMBEDDED_SOURCES = {
    # SHA-256: d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe
    'voicevox_webcpu_research.py': (
        '#!/usr/bin/env -S uv run --script\n'
        '# /// script\n'
        '# requires-python = ">=3.12"\n'
        '# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]\n'
        '# ///\n'
        '"""Reproducible VOICEVOX CORE CPU/browser benchmark (one-file distribution).\n'
        '\n'
        'Run: uv run benchmark.py [--threads 4]\n'
        'Optimization experiment: uv run benchmark.py --experiments --threads 2\n'
        '\n'
        'The first run downloads pinned Rust/Emscripten, models, runtimes and Chromium,\n'
        'then builds CORE; allow several GB and several minutes. A native C/C++ compiler\n'
        'is required (Windows: MSVC Native Tools prompt; macOS: Xcode Command Line Tools;\n'
        "Linux: cc and c++). Linux also needs Chromium's system libraries; if missing,\n"
        'run: uv run --with playwright==1.63.0 python -m playwright install-deps chromium\n'
        'That system-package step may require administrator permission. Later runs reuse\n'
        'checked downloads and builds.\n'
        '\n'
        'Outputs: standalone HTML with CPU time-series/charts/collapsed CSV, plus JSON.\n'
        'No text analyzer or dictionary is used. No benchmark measurements are simulated.\n'
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        'import argparse\n'
        'import base64\n'
        'import contextlib\n'
        'import csv\n'
        'import hashlib\n'
        'import html\n'
        'import io\n'
        'import json\n'
        'import math\n'
        'import os\n'
        'from pathlib import Path\n'
        'import platform\n'
        'import random\n'
        'import re\n'
        'import shutil\n'
        'import statistics\n'
        'import subprocess\n'
        'import sys\n'
        'import tempfile\n'
        'import time\n'
        'import urllib.request\n'
        'import wave\n'
        'from dataclasses import asdict, dataclass, replace\n'
        'from datetime import datetime, timezone\n'
        'from typing import Any, Callable, Iterator\n'
        '\n'
        'CORE_COMMIT = "9b539761f3e152b966e08c2de0784129fe8cf68d"\n'
        'ORT_BUILDER_COMMIT = "117593885cd2a66e9cf17b4059e6424d7ea528c9"\n'
        'ORT_VERSION = "1.23.2"\n'
        'VOCODER_MODEL_SHA256 = "80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820"\n'
        'TRIALS_PER_BLOCK = 5\n'
        'BLOCKS_PER_MODE = 3\n'
        'SCHEMA_VERSION = 4\n'
        '\n'
        '\n'
        '@dataclass(frozen=True)\n'
        'class Mode:\n'
        '    key: str\n'
        '    label: str\n'
        '    threads: int\n'
        '    backend: str\n'
        '    core_optimization: str = "z"\n'
        '    graph_optimization: int = 1\n'
        '    experimental: bool = False\n'
        '    fixed_shape: bool = False\n'
        '    spin_off: bool = False\n'
        '    execution_provider: str = "CPU"\n'
        '    revectorize: bool = False\n'
        '\n'
        '\n'
        '@dataclass(frozen=True)\n'
        'class Trial:\n'
        '    order: int\n'
        '    mode: str\n'
        '    block: int\n'
        '    trial: int\n'
        '    threads: int\n'
        '    elapsed_s: float\n'
        '    audio_s: float\n'
        '    rtf: float\n'
        '    cpu_time_s: float\n'
        '    cpu_window_s: float\n'
        '    cpu_avg_cores: float\n'
        '    cpu_percent: float\n'
        '    cpu_samples: int\n'
        '    cpu_processes: int\n'
        '    cpu_incomplete: bool\n'
        '    cpu_trace: tuple[CpuInterval, ...]\n'
        '\n'
        '\n'
        '@dataclass(frozen=True)\n'
        'class Block:\n'
        '    mode: str\n'
        '    number: int\n'
        '\n'
        '\n'
        'CPU_SAMPLE_INTERVAL_S = 0.1\n'
        'CPU_PADDING_S = 1.0\n'
        'PROGRESS_INTERVAL_S = 15.0\n'
        '\n'
        '\n'
        'def progress(message: str) -> None:\n'
        '    print(f"[{time.strftime(\'%H:%M:%S\')}] {message}", file=sys.stderr, flush=True)\n'
        '\n'
        '\n'
        '@dataclass(frozen=True)\n'
        'class CpuInterval:\n'
        '    start_s: float\n'
        '    end_s: float\n'
        '    cpu_time_s: float\n'
        '    cpu_percent: float\n'
        '    cpu_processes: int\n'
        '    cpu_incomplete: bool\n'
        '    phase: str = "action"\n'
        '\n'
        '\n'
        '@dataclass(frozen=True)\n'
        'class CpuMeasurement:\n'
        '    cpu_time_s: float\n'
        '    cpu_window_s: float\n'
        '    cpu_avg_cores: float\n'
        '    cpu_percent: float\n'
        '    cpu_samples: int\n'
        '    cpu_processes: int\n'
        '    cpu_incomplete: bool\n'
        '    cpu_trace: tuple[CpuInterval, ...]\n'
        '\n'
        '\n'
        'class ProcessCpuSampler:\n'
        '    """Sum user+system CPU for one explicitly selected process tree.\n'
        '\n'
        '    The CPU window surrounds the request/response and counter reads, separately\n'
        '    from the existing internal synthesis timer. CPU times include all threads.\n'
        '    Never add children_user/system: descendant processes are counted directly.\n'
        '    Lost/late processes mark CPU data incomplete; those rows remain in CSV but\n'
        "    are excluded from the report's CPU average. Polling cannot observe a child\n"
        '    that starts and exits entirely between samples.\n'
        '    """\n'
        '    def __init__(self, root_pid: int, label: str, interval: float = CPU_SAMPLE_INTERVAL_S):\n'
        '        import psutil\n'
        '        import threading\n'
        '        if root_pid == os.getpid():\n'
        '            raise ValueError("Refusing to include the Python orchestrator in target CPU")\n'
        '        if interval <= 0 or not math.isfinite(interval):\n'
        '            raise ValueError("CPU sample interval must be positive")\n'
        '        self.psutil = psutil\n'
        '        self.root = psutil.Process(root_pid)\n'
        '        self.root_identity = self.root\n'
        '        self.label, self.interval = label, interval\n'
        '        self.stop = threading.Event()\n'
        '        self.sample_lock = threading.Lock()\n'
        '        self.phase = "pre"\n'
        '        self.phase_started_wall = 0.0\n'
        "        # Process equality/hash guards PID reuse using psutil's stable identity.\n"
        '        # A (pid, wall-clock create_time) tuple can duplicate a live Linux PID\n'
        '        # when /proc/stat btime shifts (for example after clock adjustments).\n'
        '        self.processes: dict[Any, Any] = {}\n'
        '        self.previous: dict[Any, float] = {}\n'
        '        self.lost: set[Any] = set()\n'
        '        self.total = 0.0\n'
        '        self.samples = 0\n'
        '        self.incomplete = False\n'
        '        self.error: Exception | None = None\n'
        '        self.started_epoch = 0.0\n'
        '        self.started_wall = 0.0\n'
        '        self.baseline_wall = 0.0\n'
        '        self.previous_sample_wall = 0.0\n'
        '        self.intervals: list[CpuInterval] = []\n'
        '\n'
        '    def _sample(self, initial: bool = False) -> None:\n'
        '        try:\n'
        '            before = self.total\n'
        '            if not self.root.is_running():\n'
        '                raise RuntimeError("Target CPU root exited or its PID was reused")\n'
        '            candidates = [self.root] + self.root.children(recursive=True)\n'
        '            for process in candidates:\n'
        '                try:\n'
        '                    process.create_time()  # Check existence; birth time is attribution only.\n'
        '                    identity = process\n'
        '                    self.processes.setdefault(identity, process)\n'
        '                except self.psutil.NoSuchProcess:\n'
        '                    self.incomplete = True\n'
        '            for identity, process in list(self.processes.items()):\n'
        '                if identity in self.lost:\n'
        '                    continue\n'
        '                try:\n'
        '                    # cpu_times alone does not guard against PID reuse.\n'
        '                    if not process.is_running():\n'
        '                        raise self.psutil.NoSuchProcess(process.pid)\n'
        '                    times = process.cpu_times()\n'
        '                    value = float(times.user + times.system)\n'
        '                    if not math.isfinite(value) or value < 0:\n'
        '                        raise RuntimeError("Invalid process CPU counter")\n'
        '                    if identity not in self.previous:\n'
        '                        if initial:\n'
        '                            self.previous[identity] = value\n'
        '                            continue\n'
        '                        phase_epoch = self.started_epoch + self.phase_started_wall - self.started_wall\n'
        '                        if process.create_time() < phase_epoch:\n'
        "                            # Never move a newly discovered child's earlier-phase\n"
        "                            # lifetime CPU into this phase's counters. Its late\n"
        '                            # discovery also invalidates earlier coverage where\n'
        '                            # it could already have consumed unobserved CPU.\n'
        '                            birth_wall = self.started_wall + process.create_time() - self.started_epoch\n'
        '                            affected = {point.phase for point in self.intervals if point.end_s > birth_wall}\n'
        '                            self.intervals = [replace(point, cpu_incomplete=True) if point.phase in affected else point\n'
        '                                              for point in self.intervals]\n'
        '                            self.previous[identity] = value\n'
        '                            self.incomplete = True\n'
        '                            continue\n'
        '                        previous_epoch = self.started_epoch + self.previous_sample_wall - self.started_wall\n'
        '                        if process.create_time() < previous_epoch:\n'
        '                            # Lifetime CPU is attributable to this trial, but a\n'
        '                            # late discovery cannot place it in the right interval.\n'
        '                            # Birth-time rounding is treated conservatively too.\n'
        '                            self.incomplete = True\n'
        '                        self.previous[identity] = 0.0\n'
        '                    delta = value - self.previous[identity]\n'
        '                    if delta < -1e-6:\n'
        '                        raise RuntimeError("Process CPU counter moved backwards")\n'
        '                    self.total += max(0.0, delta)\n'
        '                    self.previous[identity] = value\n'
        '                except self.psutil.NoSuchProcess:\n'
        '                    if identity == self.root_identity:\n'
        '                        raise RuntimeError("Target CPU root disappeared before the final sample")\n'
        '                    self.lost.add(identity)\n'
        '                    self.incomplete = True\n'
        '            sampled = time.perf_counter()\n'
        '            if initial:\n'
        '                self.baseline_wall = sampled\n'
        '            else:\n'
        '                span = sampled - self.previous_sample_wall\n'
        '                if span <= 0:\n'
        '                    raise RuntimeError("CPU snapshot clock did not advance")\n'
        '                delta = self.total - before\n'
        '                self.intervals.append(CpuInterval(self.previous_sample_wall, sampled, delta,\n'
        '                                                  100 * delta / span, len(self.processes), self.incomplete, self.phase))\n'
        '            self.previous_sample_wall = sampled\n'
        '            self.samples += 1\n'
        '        except Exception as error:\n'
        '            self.error = error\n'
        '            self.stop.set()\n'
        '\n'
        '    def _observe_padding(self, started: float) -> None:\n'
        '        """Require a full interval on the same clock used by the CPU trace.\n'
        '\n'
        '        A timed wait is not itself proof of elapsed perf_counter time (notably\n'
        '        across virtualized clock domains). Never invent or stretch samples.\n'
        '        """\n'
        '        deadline = started + CPU_PADDING_S\n'
        '        while True:\n'
        '            if self.error:\n'
        '                raise RuntimeError(f"CPU observation failed for {self.label}: {self.error}") from self.error\n'
        '            remaining = deadline - time.perf_counter()\n'
        '            if remaining <= 0:\n'
        '                return\n'
        '            if self.stop.wait(remaining):\n'
        '                raise RuntimeError(f"CPU observation interrupted for {self.label}: {self.error}") from self.error\n'
        '\n'
        '    def measure(self, action: Callable[[], tuple[float, float]]) -> tuple[float, float, CpuMeasurement]:\n'
        '        import threading\n'
        '        self.started_wall = time.perf_counter()\n'
        '        self.started_epoch = time.time()\n'
        '        with self.sample_lock:\n'
        '            self._sample(initial=True)\n'
        '            self.phase_started_wall = self.previous_sample_wall\n'
        '        if self.error:\n'
        '            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error\n'
        '        def sample_loop() -> None:\n'
        '            last_heartbeat = time.perf_counter()\n'
        '            while not self.stop.wait(self.interval):\n'
        '                with self.sample_lock:\n'
        '                    self._sample()\n'
        '                now = time.perf_counter()\n'
        '                if now - last_heartbeat >= 30:\n'
        '                    progress(f"{self.label}: synthesis still running ({now - self.started_wall:.0f}s)")\n'
        '                    last_heartbeat = now\n'
        '        worker = threading.Thread(target=sample_loop, name="benchmark-cpu-sampler", daemon=True)\n'
        '        worker.start()\n'
        '        try:\n'
        '            self._observe_padding(self.baseline_wall)\n'
        '            with self.sample_lock:\n'
        '                self._sample()\n'
        '                action_cpu_start, action_wall_start = self.total, self.previous_sample_wall\n'
        '                self.phase = "action"\n'
        '                self.phase_started_wall = action_wall_start\n'
        '                self.incomplete = False\n'
        '            if self.error:\n'
        '                raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error\n'
        '            request_started = time.perf_counter()\n'
        '            elapsed, duration = action()\n'
        '            with self.sample_lock:\n'
        '                self._sample()\n'
        '                action_cpu_end, action_wall_end = self.total, self.previous_sample_wall\n'
        '                self.phase = "post"\n'
        '                self.phase_started_wall = action_wall_end\n'
        '                self.incomplete = False\n'
        '            if self.error:\n'
        '                raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error\n'
        '            self._observe_padding(action_wall_end)\n'
        '        finally:\n'
        '            self.stop.set()\n'
        '            worker.join(timeout=5)\n'
        '            if worker.is_alive():\n'
        '                self.error = RuntimeError("CPU sampler did not stop")\n'
        '            elif self.error is None:\n'
        '                with self.sample_lock:\n'
        '                    self._sample()\n'
        '        if self.error:\n'
        '            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error\n'
        '        total, window = action_cpu_end - action_cpu_start, action_wall_end - action_wall_start\n'
        '        cores = total / window\n'
        '        trace = tuple(CpuInterval(item.start_s - request_started, item.end_s - request_started,\n'
        '                                  item.cpu_time_s, item.cpu_percent, item.cpu_processes, item.cpu_incomplete, item.phase)\n'
        '                      for item in self.intervals)\n'
        '        action_incomplete = any(item.cpu_incomplete for item in trace if item.phase == "action")\n'
        '        return elapsed, duration, CpuMeasurement(total, window, cores, 100 * cores,\n'
        '                                                self.samples, len(self.processes), action_incomplete, trace)\n'
        '\n'
        '\n'
        'def chromium_process_id(browser: Any) -> int:\n'
        '    """Use the public browser-level CDP API, never Playwright internals."""\n'
        '    session = browser.new_browser_cdp_session()\n'
        '    try:\n'
        '        response = session.send("SystemInfo.getProcessInfo")\n'
        '    except Exception as error:\n'
        '        raise RuntimeError("This Chromium does not provide browser process IDs for CPU sampling; use the bundled Chromium") from error\n'
        '    finally:\n'
        '        session.detach()\n'
        '    roots = [int(process["id"]) for process in response["processInfo"] if process.get("type") == "browser"]\n'
        '    if len(roots) != 1 or roots[0] <= 0 or roots[0] == os.getpid():\n'
        '        raise RuntimeError("Could not identify the benchmark Chromium process safely")\n'
        '    return roots[0]\n'
        '\n'
        '\n'
        'def weighted_cpu_percent(trials: list[dict[str, Any]]) -> float | None:\n'
        '    complete = [row for row in trials if not row["cpu_incomplete"]]\n'
        '    if not complete:\n'
        '        return None\n'
        '    return 100 * sum(row["cpu_time_s"] for row in complete) / sum(row["cpu_window_s"] for row in complete)\n'
        '\n'
        '\n'
        'def block_bootstrap_median_ci(samples: list[tuple[int, float]]) -> dict[str, Any] | None:\n'
        '    """Nominal percentile interval; three intact blocks, all 3**3 draws.\n'
        '\n'
        '    This preserves within-block dependence. Three clusters are too few to\n'
        '    guarantee reliable 95% coverage; the report labels the interval exploratory.\n'
        '    """\n'
        '    import itertools\n'
        '    groups: dict[int, list[float]] = {}\n'
        '    for block, value in samples:\n'
        '        groups.setdefault(block, []).append(value)\n'
        '    if len(groups) != BLOCKS_PER_MODE:\n'
        '        return None\n'
        '    keys = sorted(groups)\n'
        '    estimates = sorted(statistics.median(value for key in draw for value in groups[key])\n'
        '                       for draw in itertools.product(keys, repeat=BLOCKS_PER_MODE))\n'
        '    def quantile(probability: float) -> float:\n'
        '        position = (len(estimates) - 1) * probability\n'
        '        lo, hi = math.floor(position), math.ceil(position)\n'
        '        return estimates[lo] + (estimates[hi] - estimates[lo]) * (position - lo)\n'
        '    return {"low": quantile(0.025), "high": quantile(0.975), "nominal_level": 0.95,\n'
        '            "resampling_unit": "whole five-trial block", "blocks": len(groups), "draws": len(estimates),\n'
        '            "quantile_method": "linear interpolation at (n-1)*p", "exploratory": True}\n'
        '\n'
        '\n'
        'def make_schedule(modes: list[Mode], seed: int, *, balanced: bool = False) -> list[Block]:\n'
        '    """Three shuffled rounds, each containing one five-trial block per mode."""\n'
        '    if not modes or len({mode.key for mode in modes}) != len(modes):\n'
        '        raise ValueError("Mode keys must be nonempty and unique")\n'
        '    rng = random.Random(seed)\n'
        '    if balanced:\n'
        '        if len(modes) not in (2, 3, 4, 5, 6, 7) or BLOCKS_PER_MODE != 3:\n'
        '            raise ValueError("Balanced experiments require two to seven modes and three rounds")\n'
        '        base = list(modes)\n'
        '        rng.shuffle(base)\n'
        '        if len(modes) == 7:\n'
        '            indices = ((0, 1, 2, 3, 4, 5, 6), (3, 5, 4, 6, 2, 1, 0), (6, 4, 5, 2, 0, 3, 1))\n'
        '            rotations = [[base[index] for index in row] for row in indices]\n'
        '        elif len(modes) == 6:\n'
        '            indices = ((0, 1, 2, 3, 4, 5), (5, 4, 3, 2, 1, 0), (2, 3, 4, 5, 0, 1))\n'
        '            rotations = [[base[index] for index in row] for row in indices]\n'
        '        elif len(modes) == 5:\n'
        '            indices = ((0, 1, 2, 3, 4), (4, 3, 0, 2, 1), (2, 4, 1, 0, 3))\n'
        '            rotations = [[base[index] for index in row] for row in indices]\n'
        '        elif len(modes) == 4:\n'
        '            indices = ((0, 1, 2, 3), (1, 0, 3, 2), (2, 3, 0, 1))\n'
        '            rotations = [[base[index] for index in row] for row in indices]\n'
        '        elif len(modes) == 3:\n'
        '            rotations = [base[i:] + base[:i] for i in range(3)]\n'
        '        else:\n'
        '            rotations = [base[:], base[::-1], base[::rng.choice((1, -1))]]\n'
        '        rng.shuffle(rotations)\n'
        '        return [Block(mode.key, number) for number, row in enumerate(rotations, 1) for mode in row]\n'
        '    schedule = []\n'
        '    for block_number in range(1, BLOCKS_PER_MODE + 1):\n'
        '        round_modes = list(modes)\n'
        '        rng.shuffle(round_modes)\n'
        '        schedule.extend(Block(mode.key, block_number) for mode in round_modes)\n'
        '    return schedule\n'
        '\n'
        '\n'
        'def logical_cpu_count() -> int:\n'
        "    # Respect the process's available logical CPUs in containers/affinity masks.\n"
        '    process_count = getattr(os, "process_cpu_count", lambda: None)()\n'
        '    if process_count:\n'
        '        return process_count\n'
        '    if hasattr(os, "sched_getaffinity"):\n'
        '        return len(os.sched_getaffinity(0)) or 1\n'
        '    return os.cpu_count() or 1\n'
        '\n'
        '\n'
        'def default_threads() -> int:\n'
        '    return max(1, logical_cpu_count() // 2)\n'
        '\n'
        '\n'
        'def sha256_file(path: Path) -> str:\n'
        '    digest = hashlib.sha256()\n'
        '    with path.open("rb") as stream:\n'
        '        for chunk in iter(lambda: stream.read(1024 * 1024), b""):\n'
        '            digest.update(chunk)\n'
        '    return digest.hexdigest()\n'
        '\n'
        '\n'
        'def atomic_write(path: Path, data: bytes) -> None:\n'
        '    path.parent.mkdir(parents=True, exist_ok=True)\n'
        '    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)\n'
        '    temporary = Path(name)\n'
        '    try:\n'
        '        with os.fdopen(descriptor, "wb") as stream:\n'
        '            stream.write(data)\n'
        '            stream.flush()\n'
        '            os.fsync(stream.fileno())\n'
        '        os.replace(temporary, path)\n'
        '    finally:\n'
        '        temporary.unlink(missing_ok=True)\n'
        '\n'
        '\n'
        'def cached_download(url: str, root: Path, filename: str, *,\n'
        '                    expected_sha256: str | None = None) -> Path:\n'
        '    """A partial download is never reused. Every cache hit rechecks its digest.\n'
        '\n'
        '    An upstream SHA-256 is checked when supplied. Otherwise the manifest records\n'
        '    the digest seen on the first successful HTTPS download (integrity on reuse,\n'
        '    not a claim of independent supply-chain authentication).\n'
        '    """\n'
        '    if not url.startswith("https://"):\n'
        '        raise ValueError("Downloads require HTTPS")\n'
        '    if Path(filename).name != filename:\n'
        '        raise ValueError("Download filename must not contain a directory")\n'
        '    key = hashlib.sha256(url.encode()).hexdigest()[:24]\n'
        '    folder = root / "downloads" / key\n'
        '    folder.mkdir(parents=True, exist_ok=True)\n'
        '    target = folder / filename\n'
        '    receipt = folder / "complete.json"\n'
        '    if target.is_file() and receipt.is_file():\n'
        '        try:\n'
        '            saved = json.loads(receipt.read_text("utf-8"))\n'
        '            expected = expected_sha256 or saved["sha256"]\n'
        '            if saved["url"] == url and target.stat().st_size == saved["bytes"] and sha256_file(target) == expected:\n'
        '                progress(f"Cache hit: {filename}")\n'
        '                return target\n'
        '        except (OSError, ValueError, KeyError):\n'
        '            pass\n'
        '    descriptor, name = tempfile.mkstemp(prefix=".download-", dir=folder)\n'
        '    partial = Path(name)\n'
        '    try:\n'
        '        request = urllib.request.Request(url, headers={"User-Agent": "voicevox-core-benchmark/1"})\n'
        '        digest = hashlib.sha256()\n'
        '        total = 0\n'
        '        progress(f"Downloading {filename}")\n'
        '        last_progress = time.monotonic()\n'
        '        with os.fdopen(descriptor, "wb") as out, urllib.request.urlopen(request, timeout=120) as response:\n'
        '            if not response.geturl().startswith("https://"):\n'
        '                raise RuntimeError("Download redirected away from HTTPS")\n'
        '            length = response.headers.get("Content-Length")\n'
        '            for chunk in iter(lambda: response.read(1024 * 1024), b""):\n'
        '                out.write(chunk)\n'
        '                digest.update(chunk)\n'
        '                total += len(chunk)\n'
        '                now = time.monotonic()\n'
        '                if now - last_progress >= 5:\n'
        '                    size = f" / {int(length) / 2**20:.1f} MiB" if length else " MiB"\n'
        '                    progress(f"{filename}: {total / 2**20:.1f}{size} downloaded")\n'
        '                    last_progress = now\n'
        '            if length is not None and total != int(length):\n'
        '                raise RuntimeError(f"Incomplete download: {filename}")\n'
        '            out.flush()\n'
        '            os.fsync(out.fileno())\n'
        '        actual = digest.hexdigest()\n'
        '        progress(f"Downloaded {filename}: {total / 2**20:.1f} MiB; verifying checksum")\n'
        '        if expected_sha256 and actual != expected_sha256:\n'
        '            raise RuntimeError(f"SHA-256 mismatch: {filename}")\n'
        '        os.replace(partial, target)\n'
        '        atomic_write(receipt, json.dumps({"url": url, "sha256": actual, "bytes": total}).encode())\n'
        '        return target\n'
        '    finally:\n'
        '        partial.unlink(missing_ok=True)\n'
        '\n'
        '\n'
        'def cache_root() -> Path:\n'
        '    try:\n'
        '        from platformdirs import user_cache_path\n'
        '    except ImportError:\n'
        '        # Keep report-only and self-test usable with the Python standard library.\n'
        '        if sys.platform == "win32":\n'
        '            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))\n'
        '        elif sys.platform == "darwin":\n'
        '            base = Path.home() / "Library/Caches"\n'
        '        else:\n'
        '            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))\n'
        '        return base / "voicevox-core-benchmark"\n'
        '    return user_cache_path("voicevox-core-benchmark", appauthor=False)\n'
        '\n'
        '\n'
        'def checked_run(command: list[str], *, cwd: Path | None = None,\n'
        '                env: dict[str, str] | None = None) -> str:\n'
        '    """Capture output for errors/return values while keeping long work visible."""\n'
        '    import queue\n'
        '    import threading\n'
        '    started = time.monotonic()\n'
        '    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,\n'
        '                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")\n'
        '    messages: queue.Queue[str | None] = queue.Queue()\n'
        '    def read_output() -> None:\n'
        '        try:\n'
        '            for line in process.stdout:\n'
        '                messages.put(line)\n'
        '        finally:\n'
        '            messages.put(None)\n'
        '    reader = threading.Thread(target=read_output, daemon=True)\n'
        '    reader.start()\n'
        '    output: list[str] = []\n'
        '    last_line = ""\n'
        '    last_report = started\n'
        '    label = Path(command[0]).name\n'
        '    if len(command) > 1:\n'
        '        label += " " + Path(command[1]).name\n'
        '    try:\n'
        '        finished = False\n'
        '        while not finished:\n'
        '            try:\n'
        '                line = messages.get(timeout=1.0)\n'
        '                if line is None:\n'
        '                    finished = True\n'
        '                else:\n'
        '                    output.append(line)\n'
        '                    if line.strip():\n'
        '                        last_line = line.strip()[-180:]\n'
        '            except queue.Empty:\n'
        '                pass\n'
        '            now = time.monotonic()\n'
        '            if not finished and now - last_report >= PROGRESS_INTERVAL_S:\n'
        '                progress(f"{label}: running {now - started:.0f}s" + (f"; {last_line}" if last_line else ""))\n'
        '                last_report = now\n'
        '        code = process.wait()\n'
        '    except BaseException:\n'
        '        process.terminate()\n'
        '        try:\n'
        '            process.wait(timeout=5)\n'
        '        except subprocess.TimeoutExpired:\n'
        '            process.kill()\n'
        '            process.wait()\n'
        '        raise\n'
        '    finally:\n'
        '        reader.join(timeout=5)\n'
        '        process.stdout.close()\n'
        '    text_output = "".join(output)\n'
        '    duration = time.monotonic() - started\n'
        '    if code:\n'
        '        raise RuntimeError(f"Command failed ({code}): {\' \'.join(command)}\\n{text_output[-10000:]}")\n'
        '    if duration >= 5:\n'
        '        progress(f"{label}: completed in {duration:.1f}s")\n'
        '    return text_output.strip()\n'
        '\n'
        'def command_version(command: list[str]) -> str | None:\n'
        '    try:\n'
        '        return checked_run(command).splitlines()[0]\n'
        '    except (OSError, RuntimeError, IndexError):\n'
        '        return None\n'
        '\n'
        '\n'
        'def cpu_model() -> str:\n'
        '    try:\n'
        '        if sys.platform.startswith("linux"):\n'
        '            for line in Path("/proc/cpuinfo").read_text().splitlines():\n'
        '                if line.lower().startswith("model name"):\n'
        '                    return line.split(":", 1)[1].strip()\n'
        '        if sys.platform == "darwin":\n'
        '            return checked_run(["sysctl", "-n", "machdep.cpu.brand_string"])\n'
        '    except (OSError, RuntimeError):\n'
        '        pass\n'
        '    return platform.processor() or platform.machine() or "unknown"\n'
        '\n'
        '\n'
        'def environment_info() -> dict[str, Any]:\n'
        '    info: dict[str, Any] = {\n'
        '        "os": f"{platform.system()} {platform.release()}",\n'
        '        "architecture": platform.machine(),\n'
        '        "cpu": cpu_model(),\n'
        '        "available_logical_cpus": logical_cpu_count(),\n'
        '        "host_logical_cpus": os.cpu_count() or "unknown",\n'
        '        "python": platform.python_version(),\n'
        '        "core_commit": CORE_COMMIT,\n'
        '        "onnxruntime_version": ORT_VERSION,\n'
        '        "onnxruntime_builder_commit": ORT_BUILDER_COMMIT,\n'
        '    }\n'
        '    if sys.platform == "darwin":\n'
        '        info["os"] = f"macOS {platform.mac_ver()[0]} (Darwin {platform.release()})"\n'
        '    elif sys.platform.startswith("linux"):\n'
        '        try:\n'
        '            info["os"] = f"{platform.freedesktop_os_release().get(\'PRETTY_NAME\', \'Linux\')} (kernel {platform.release()})"\n'
        '        except OSError:\n'
        '            pass\n'
        '    for key, command in (("uv", ["uv", "--version"]), ("rust", ["rustc", "--version"]),\n'
        '                         ("emscripten", ["emcc", "--version"])):\n'
        '        if version := command_version(command):\n'
        '            info[key] = version\n'
        '    # CPU affinity / cgroup limits explain cloud results without exposing hostnames.\n'
        '    if hasattr(os, "sched_getaffinity"):\n'
        '        info["affinity_logical_cpus"] = len(os.sched_getaffinity(0))\n'
        '    limit_file = Path("/sys/fs/cgroup/cpu.max")\n'
        '    if sys.platform.startswith("linux") and limit_file.exists():\n'
        '        try:\n'
        '            quota, period = limit_file.read_text().strip().split()\n'
        '            if quota != "max":\n'
        '                info["cgroup_cpu_quota"] = int(quota) / int(period)\n'
        '        except (OSError, ValueError, ZeroDivisionError):\n'
        '            pass\n'
        '    if hasattr(os, "sysconf"):\n'
        '        try:\n'
        '            info["memory_gib"] = round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 2)\n'
        '        except (ValueError, OSError):\n'
        '            pass\n'
        '    elif sys.platform == "win32":\n'
        '        import ctypes\n'
        '        class MemoryStatus(ctypes.Structure):\n'
        '            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended")]\n'
        '        status = MemoryStatus()\n'
        '        status.length = ctypes.sizeof(status)\n'
        '        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):\n'
        '            info["memory_gib"] = round(status.total_phys / 2**30, 2)\n'
        '    return info\n'
        '\n'
        '\n'
        'def wav_duration(data: bytes) -> float:\n'
        '    with wave.open(io.BytesIO(data), "rb") as wav:\n'
        '        if wav.getnframes() < 1 or wav.getframerate() < 1:\n'
        '            raise ValueError("Generated WAV is empty")\n'
        '        return wav.getnframes() / wav.getframerate()\n'
        '\n'
        '\n'
        'def pcm_wav_info(data: bytes) -> tuple[dict[str, int], bytes]:\n'
        '    with wave.open(io.BytesIO(data), "rb") as stream:\n'
        '        info = {"channels": stream.getnchannels(), "sample_bytes": stream.getsampwidth(),\n'
        '                "sample_rate": stream.getframerate(), "frames": stream.getnframes()}\n'
        '        pcm = stream.readframes(info["frames"])\n'
        '    if info["sample_bytes"] != 2 or min(info.values()) <= 0 or len(pcm) != info["frames"] * info["channels"] * 2:\n'
        '        raise ValueError("Spectrogram requires complete nonempty PCM16 WAV audio")\n'
        '    return info, pcm\n'
        '\n'
        '\n'
        'SPECTROGRAM_METHOD = {"window": "periodic Hann", "nfft": 1024, "hop": 256,\n'
        '                      "channel": "first", "scale": "one-sided amplitude dBFS",\n'
        '                      "range_db": [-100, 0], "centered_zero_padding": True,\n'
        '                      "amplitude_normalization": "2/sum(window); DC and Nyquist not doubled"}\n'
        '\n'
        '\n'
        'def spectrogram_db(wav: bytes) -> tuple[Any, int, float]:\n'
        '    import numpy as np\n'
        '    info, pcm = pcm_wav_info(wav)\n'
        '    signal = np.frombuffer(pcm, dtype="<i2").reshape(-1, info["channels"])[:, 0].astype(np.float64) / 32768.0\n'
        '    nfft, hop = 1024, 256\n'
        '    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(nfft) / nfft)\n'
        '    frames = np.lib.stride_tricks.sliding_window_view(np.pad(signal, (nfft // 2, nfft // 2 + (-signal.size) % hop)), nfft)[::hop]\n'
        '    amplitude = np.abs(np.fft.rfft(frames * window, axis=1)) * (2.0 / window.sum())\n'
        '    amplitude[:, (0, -1)] *= 0.5\n'
        '    db = 20 * np.log10(np.maximum(amplitude, 1e-5))\n'
        '    return np.clip(db.T, -100, 0), info["sample_rate"], info["frames"] / info["sample_rate"]\n'
        '\n'
        '\n'
        'def png_dimensions(data: bytes) -> tuple[int, int]:\n'
        '    """Accept pixels only: no metadata chunks, appended data, or audio payload."""\n'
        '    from PIL import Image\n'
        '    if not data.startswith(b"\\x89PNG\\r\\n\\x1a\\n"):\n'
        '        raise ValueError("Expected a PNG spectrogram")\n'
        '    offset, chunks = 8, []\n'
        '    while offset < len(data):\n'
        '        if offset + 12 > len(data):\n'
        '            raise ValueError("Truncated PNG")\n'
        '        size = int.from_bytes(data[offset:offset + 4], "big")\n'
        '        kind = data[offset + 4:offset + 8]\n'
        '        if kind not in {b"IHDR", b"IDAT", b"IEND"} or offset + 12 + size > len(data):\n'
        '            raise ValueError("Spectrogram PNG must contain only image pixels")\n'
        '        chunks.append(kind)\n'
        '        offset += 12 + size\n'
        '        if kind == b"IEND":\n'
        '            break\n'
        '    if offset != len(data) or not chunks or chunks[0] != b"IHDR" or chunks[-1] != b"IEND":\n'
        '        raise ValueError("Malformed PNG or trailing payload")\n'
        '    with Image.open(io.BytesIO(data)) as image:\n'
        '        if image.format != "PNG" or image.mode != "RGB" or not (1 <= image.width <= 4096 and 1 <= image.height <= 4096):\n'
        '            raise ValueError("Unexpected spectrogram image format or dimensions")\n'
        '        size = image.size\n'
        '        image.verify()\n'
        '    return size\n'
        '\n'
        '\n'
        'def encode_spectrograms(wavs: dict[str, bytes], checks: dict[str, Any], provenance: str) -> list[dict[str, Any]]:\n'
        '    """Convert local diagnostics into image-only report data; never serialize audio."""\n'
        '    groups: dict[str, dict[str, Any]] = {}\n'
        '    previous_config = os.environ.get("MPLCONFIGDIR")\n'
        '    previous_cache = os.environ.get("XDG_CACHE_HOME")\n'
        '    with tempfile.TemporaryDirectory(prefix="voicevox-plot-") as config:\n'
        '        os.environ["MPLCONFIGDIR"] = config\n'
        '        os.environ["XDG_CACHE_HOME"] = config\n'
        '        try:\n'
        '            import matplotlib\n'
        '            from matplotlib.backends.backend_agg import FigureCanvasAgg\n'
        '            from matplotlib.figure import Figure\n'
        '            from PIL import Image\n'
        '            for key, wav in wavs.items():\n'
        '                info, pcm = pcm_wav_info(wav)\n'
        '                digest = hashlib.sha256(pcm).hexdigest()\n'
        '                if digest != checks[key]["pcm_sha256"] or info != checks[key]["wav_format"]:\n'
        '                    raise ValueError(f"Audio does not match the recorded output for {key}")\n'
        '                if digest not in groups:\n'
        '                    spectrum, rate, duration = spectrogram_db(wav)\n'
        '                    with matplotlib.rc_context({"font.family": "DejaVu Sans", "font.size": 8.5}):\n'
        '                        figure = Figure(figsize=(5.4, 2.25), dpi=130, layout="constrained")\n'
        '                        FigureCanvasAgg(figure)\n'
        '                        axes = figure.subplots()\n'
        '                        dt, df = 256 / rate, rate / 1024 / 1000\n'
        '                        image = axes.imshow(spectrum, origin="lower", aspect="auto", extent=(-dt / 2, (spectrum.shape[1] - .5) * dt, -df / 2, rate / 2000 + df / 2),\n'
        '                                            vmin=-100, vmax=0, cmap="magma", interpolation="nearest")\n'
        '                        axes.set(xlabel="Time (s)", ylabel="Frequency (kHz)", xlim=(0, duration), ylim=(0, rate / 2000))\n'
        '                        figure.colorbar(image, ax=axes, label="dBFS", ticks=(-100, -50, 0))\n'
        '                        rendered = io.BytesIO()\n'
        '                        figure.savefig(rendered, format="png")\n'
        '                    # Re-encode pixel data only, removing metadata and ancillary chunks.\n'
        '                    image_bytes = io.BytesIO()\n'
        '                    with Image.open(io.BytesIO(rendered.getvalue())) as source:\n'
        '                        pixels = Image.frombytes("RGB", source.size, source.convert("RGB").tobytes())\n'
        '                        pixels.save(image_bytes, format="PNG")\n'
        '                    png = image_bytes.getvalue()\n'
        '                    width, height = png_dimensions(png)\n'
        '                    groups[digest] = {"pcm_sha256": digest, "wav_format": info, "modes": [],\n'
        '                                      "png_base64": base64.b64encode(png).decode(), "png_sha256": hashlib.sha256(png).hexdigest(),\n'
        '                                      "width": width, "height": height, "method": SPECTROGRAM_METHOD.copy(), "provenance": provenance}\n'
        '                groups[digest]["modes"].append(key)\n'
        '        finally:\n'
        '            if previous_config is None:\n'
        '                os.environ.pop("MPLCONFIGDIR", None)\n'
        '            else:\n'
        '                os.environ["MPLCONFIGDIR"] = previous_config\n'
        '            if previous_cache is None:\n'
        '                os.environ.pop("XDG_CACHE_HOME", None)\n'
        '            else:\n'
        '                os.environ["XDG_CACHE_HOME"] = previous_cache\n'
        '    return list(groups.values())\n'
        '\n'
        '\n'
        'def attach_saved_audio(result: dict[str, Any], directory: Path) -> None:\n'
        '    checks = result.get("output_checks", {}).get("modes", {})\n'
        '    if not checks:\n'
        '        raise ValueError("Saved audio requires recorded PCM hashes and formats")\n'
        '    by_digest = {}\n'
        '    for path in sorted(directory.glob("*.wav")):\n'
        '        wav = path.read_bytes()\n'
        '        _, pcm = pcm_wav_info(wav)\n'
        '        by_digest.setdefault(hashlib.sha256(pcm).hexdigest(), wav)\n'
        '    missing = [key for key, check in checks.items() if check["pcm_sha256"] not in by_digest]\n'
        '    if missing:\n'
        '        raise ValueError("No hash-matched audio for: " + ", ".join(missing))\n'
        '    result["spectrograms"] = encode_spectrograms({key: by_digest[check["pcm_sha256"]] for key, check in checks.items()}, checks,\n'
        '        "Generated locally from retained untimed diagnostic WAVs after checking PCM hashes and formats against the measurement record. Only PNG pixels and provenance are distributed; CI verifies those images, not the private source WAVs.")\n'
        '\n'
        '\n'
        'def validate_spectrograms(result: dict[str, Any]) -> None:\n'
        '    groups = result.get("spectrograms", [])\n'
        '    if not groups:\n'
        '        return\n'
        '    labels = {mode["key"] for mode in result["modes"]}\n'
        '    fields = {"pcm_sha256", "wav_format", "modes", "png_base64", "png_sha256", "width", "height", "method", "provenance"}\n'
        '    represented = [key for group in groups for key in group["modes"]]\n'
        '    if set(represented) != labels or len(represented) != len(labels):\n'
        '        raise ValueError("Spectrogram groups must represent every mode exactly once")\n'
        '    for group in groups:\n'
        '        if set(group) != fields or group["method"] != SPECTROGRAM_METHOD:\n'
        '            raise ValueError("Unexpected spectrogram metadata or analysis method")\n'
        '        png = base64.b64decode(group["png_base64"], validate=True)\n'
        '        if hashlib.sha256(png).hexdigest() != group["png_sha256"] or png_dimensions(png) != (group["width"], group["height"]):\n'
        '            raise ValueError("Spectrogram image integrity check failed")\n'
        '        for key in group["modes"]:\n'
        '            check = result["output_checks"]["modes"][key]\n'
        '            if check["pcm_sha256"] != group["pcm_sha256"] or check["wav_format"] != group["wav_format"]:\n'
        '                raise ValueError("Spectrogram provenance does not match its measured mode")\n'
        '\n'
        '\n'
        'def attach_saved_spectrograms(result: dict[str, Any], directory: Path) -> None:\n'
        '    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))\n'
        '    if set(manifest) != {"schema_version", "spectrograms"} or manifest["schema_version"] != 1:\n'
        '        raise ValueError("Unexpected spectrogram manifest")\n'
        '    groups = []\n'
        '    for item in manifest["spectrograms"]:\n'
        '        group = dict(item)\n'
        '        filename = group.pop("file")\n'
        '        if not re.fullmatch(r"[a-z0-9-]+\\.png", filename):\n'
        '            raise ValueError("Invalid spectrogram filename")\n'
        '        group["png_base64"] = base64.b64encode((directory / filename).read_bytes()).decode()\n'
        '        groups.append(group)\n'
        '    result["spectrograms"] = groups\n'
        '    validate_spectrograms(result)\n'
        '\n'
        '\n'
        'def spectrograms_html(result: dict[str, Any]) -> str:\n'
        '    groups = result.get("spectrograms", [])\n'
        '    if not groups:\n'
        '        return ""\n'
        '    validate_spectrograms(result)\n'
        '    labels = {mode["key"]: mode["label"] for mode in result["modes"]}\n'
        '    figures = []\n'
        '    for group in groups:\n'
        '        title = " / ".join(labels[key] for key in group["modes"])\n'
        '        figures.append(f\'<figure class="spectrogram" data-pcm-sha256="{group["pcm_sha256"]}" data-png-sha256="{group["png_sha256"]}"><figcaption>{html.escape(title)}</figcaption><img alt="{html.escape(title, quote=True)} spectrogram" src="data:image/png;base64,{group["png_base64"]}"/></figure>\')\n'
        '    provenance = " / ".join(sorted({group["provenance"] for group in groups}))\n'
        '    return \'<h2>出力音声のスペクトログラム</h2><div class="spectrogram-grid">\' + \'\'.join(figures) + \'</div><p class="cpu-caption">PCMが完全一致するモードはまとめて表示。共通の時間・周波数・色スケール（−100〜0 dBFS）、先頭チャンネル、Hann窓1024点・hop256点。画像作成時にPCMハッシュと音声形式を実測記録と照合済み。</p><details><summary>音声の出典・解析条件</summary><p>\' + html.escape(provenance) + \'</p><p>PCM16を32768で割り、窓の総和で振幅を正規化した片側FFT（DC/Nyquistは倍化しない）。端は解析窓用にゼロpadding。1つの保存音声から作った画像であり、15試行の平均音声ではありません。配布するHTML・JSONにはスペクトログラム画像だけを含み、音声データは含みません。</p></details>\'\n'
        '\n'
        '\n'
        'def waveform_comparison(reference_wav: bytes, reference_raw: bytes, wav: bytes, raw: bytes) -> dict[str, Any]:\n'
        '    """Untimed exactness check, with error magnitudes but no perceptual tolerance."""\n'
        '    import array\n'
        '    def pcm(data: bytes) -> tuple[tuple[int, ...], bytes]:\n'
        '        with wave.open(io.BytesIO(data), "rb") as stream:\n'
        '            shape = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes())\n'
        '            if shape[1] != 2 or shape[3] < 1:\n'
        '                raise RuntimeError("Expected nonempty signed PCM16 output")\n'
        '            payload = stream.readframes(shape[3])\n'
        '            if len(payload) != shape[0] * shape[1] * shape[3]:\n'
        '                raise RuntimeError("Truncated PCM waveform")\n'
        '            return shape, payload\n'
        '    reference_shape, reference_pcm = pcm(reference_wav)\n'
        '    shape, values_pcm = pcm(wav)\n'
        '    if shape != reference_shape or len(raw) != len(reference_raw) or not raw or len(raw) % 4:\n'
        '        raise RuntimeError("Output shape/dtype differs from the same-run reference")\n'
        '    def values(data: bytes, typecode: str) -> Any:\n'
        '        result = array.array(typecode)\n'
        '        result.frombytes(data)\n'
        '        if sys.byteorder != "little":\n'
        '            result.byteswap()\n'
        '        return result\n'
        '    floats, ref_floats = values(raw, "f"), values(reference_raw, "f")\n'
        '    if not all(math.isfinite(value) for value in floats) or not all(math.isfinite(value) for value in ref_floats):\n'
        '        raise RuntimeError("FP32 output contains NaN or infinity")\n'
        '    float_errors = [float(a) - float(b) for a, b in zip(floats, ref_floats)]\n'
        '    pcm_errors = [int(a) - int(b) for a, b in zip(values(values_pcm, "h"), values(reference_pcm, "h"))]\n'
        '    rmse = math.sqrt(math.fsum(error * error for error in float_errors) / len(float_errors))\n'
        '    reference_rms = math.sqrt(math.fsum(float(value) ** 2 for value in ref_floats) / len(ref_floats))\n'
        '    return {"wav_format": {"channels": shape[0], "sample_bytes": shape[1], "sample_rate": shape[2], "frames": shape[3]},\n'
        '            "fp32_samples": len(floats), "finite": True, "pcm_exact": values_pcm == reference_pcm,\n'
        '            "pcm_sha256": hashlib.sha256(values_pcm).hexdigest(), "pcm_changed_samples": sum(error != 0 for error in pcm_errors),\n'
        '            "pcm_max_abs_lsb": max(map(abs, pcm_errors)), "pcm_rmse_lsb": math.sqrt(math.fsum(error * error for error in pcm_errors) / len(pcm_errors)),\n'
        '            "fp32_exact": raw == reference_raw, "fp32_sha256": hashlib.sha256(raw).hexdigest(),\n'
        '            "fp32_changed_samples": sum(a != b for a, b in zip(values(raw, "I"), values(reference_raw, "I"))),\n'
        '            "fp32_max_abs": max(map(abs, float_errors)), "fp32_rmse": rmse,\n'
        '            "fp32_relative_rmse": rmse / max(reference_rms, 1e-12), "relative_rms_floor": 1e-12}\n'
        '\n'
        '\n'
        'def verify_outputs(runners: dict[str, Any], reference: str, log: list[dict[str, Any]]) -> dict[str, Any]:\n'
        '    progress(f"Checking PCM and pre-PCM FP32 outputs against {reference} (untimed)")\n'
        '    reference_wav = runners[reference].wav.read_bytes()\n'
        '    reference_raw = runners[reference].raw_wave()\n'
        '    runners[reference].synthesize(save=True)\n'
        '    repeated = waveform_comparison(reference_wav, reference_raw, runners[reference].wav.read_bytes(), runners[reference].raw_wave())\n'
        '    comparisons = {}\n'
        '    raw_outputs = {reference: reference_raw}\n'
        '    wav_outputs = {key: runner.wav.read_bytes() for key, runner in runners.items()}\n'
        '    for key, runner in runners.items():\n'
        '        raw = reference_raw if key == reference else runner.raw_wave()\n'
        '        raw_outputs[key] = raw\n'
        '        comparisons[key] = waveform_comparison(reference_wav, reference_raw, runner.wav.read_bytes(), raw)\n'
        '        progress(f"Output {key}: PCM {\'exact\' if comparisons[key][\'pcm_exact\'] else \'DIFFERS\'}, FP32 {\'exact\' if comparisons[key][\'fp32_exact\'] else \'DIFFERS\'}")\n'
        '    result = {"reference_mode": reference, "reference_deterministic": repeated["pcm_exact"] and repeated["fp32_exact"],\n'
        '              "reference_repeat": repeated, "modes": comparisons}\n'
        '    if "browser_xnnpack" in runners:\n'
        '        fixed = "browser_mt_fixed"\n'
        '        runners[fixed].synthesize(save=True)\n'
        '        repeat_fixed = waveform_comparison(wav_outputs[fixed], raw_outputs[fixed], runners[fixed].wav.read_bytes(), runners[fixed].raw_wave())\n'
        '        result["fixed_control_repeat"] = repeat_fixed\n'
        '        result["reference_deterministic"] = result["reference_deterministic"] and repeat_fixed["pcm_exact"] and repeat_fixed["fp32_exact"]\n'
        '        result["xnnpack_vs_fixed_control"] = waveform_comparison(wav_outputs[fixed], raw_outputs[fixed], wav_outputs["browser_xnnpack"], raw_outputs["browser_xnnpack"])\n'
        '    if "browser_xnnpack_revectorize" in runners:\n'
        '        xnn, combined = "browser_xnnpack", "browser_xnnpack_revectorize"\n'
        '        runners[xnn].synthesize(save=True)\n'
        '        repeat_xnn = waveform_comparison(wav_outputs[xnn], raw_outputs[xnn], runners[xnn].wav.read_bytes(), runners[xnn].raw_wave())\n'
        '        result["xnnpack_reference_repeat"] = repeat_xnn\n'
        '        result["reference_deterministic"] = result["reference_deterministic"] and repeat_xnn["pcm_exact"] and repeat_xnn["fp32_exact"]\n'
        '        result["combined_vs_xnnpack"] = waveform_comparison(wav_outputs[xnn], raw_outputs[xnn], wav_outputs[combined], raw_outputs[combined])\n'
        '        result["combined_vs_fixed_control"] = waveform_comparison(wav_outputs["browser_mt_fixed"], raw_outputs["browser_mt_fixed"], wav_outputs[combined], raw_outputs[combined])\n'
        '    result["spectrograms"] = encode_spectrograms(wav_outputs, comparisons, "Generated locally from same-run untimed output-check WAVs before measured trials; only spectrogram PNG pixels are retained in this report.")\n'
        '    log.append({"event": "untimed_output_checks", "reference": reference, "reference_deterministic": result["reference_deterministic"]})\n'
        '    return result\n'
        '\n'
        '\n'
        'def trial_csv(trials: list[dict[str, Any]]) -> str:\n'
        '    stream = io.StringIO(newline="")\n'
        '    writer = csv.DictWriter(stream, fieldnames=[key for key in Trial.__dataclass_fields__ if key != "cpu_trace"], extrasaction="ignore")\n'
        '    writer.writeheader()\n'
        '    writer.writerows(trials)\n'
        '    return stream.getvalue()\n'
        '\n'
        '\n'
        'def cpu_trace_csv(trials: list[dict[str, Any]]) -> str:\n'
        '    stream = io.StringIO(newline="")\n'
        '    context = ("order", "mode", "block", "trial", "threads")\n'
        '    writer = csv.DictWriter(stream, fieldnames=[*context, "interval", *CpuInterval.__dataclass_fields__])\n'
        '    writer.writeheader()\n'
        '    for trial in trials:\n'
        '        for index, point in enumerate(trial.get("cpu_trace", ()), 1):\n'
        '            writer.writerow({**{key: trial[key] for key in context}, "interval": index, **point, "phase": point.get("phase", "action")})\n'
        '    return stream.getvalue()\n'
        '\n'
        '\n'
        'def validate_results(result: dict[str, Any]) -> None:\n'
        '    if result.get("schema_version") not in (2, 3, SCHEMA_VERSION):\n'
        '        raise ValueError("Unsupported result schema")\n'
        '    expected_trials = result.get("trials_per_block", TRIALS_PER_BLOCK)\n'
        '    if expected_trials != (1 if result.get("research_screen") is True else TRIALS_PER_BLOCK):\n'
        '        raise ValueError("Invalid research screen/trial count")\n'
        '    for field in ("style_id", "seed"):\n'
        '        if type(result[field]) is not int:\n'
        '            raise ValueError(f"Invalid {field}")\n'
        '    if not isinstance(result["audio_s"], (float, int)) or not math.isfinite(result["audio_s"]) or result["audio_s"] <= 0:\n'
        '        raise ValueError("Invalid reference audio duration")\n'
        '    for mode in result["modes"]:\n'
        '        if not isinstance(mode["key"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", mode["key"]):\n'
        '            raise ValueError("Invalid mode key")\n'
        '        if type(mode["threads"]) is not int or not 1 <= mode["threads"] <= 65535:\n'
        '            raise ValueError("Invalid inference thread count")\n'
        '        if not all(isinstance(mode[field], str) for field in ("label", "backend")):\n'
        '            raise ValueError("Invalid mode description")\n'
        '    modes = {mode["key"]: mode for mode in result["modes"]}\n'
        '    if not modes or len(modes) != len(result["modes"]):\n'
        '        raise ValueError("Missing or duplicate modes")\n'
        '    if checks := result.get("output_checks"):\n'
        '        if checks["reference_mode"] not in modes or set(checks["modes"]) != set(modes):\n'
        '            raise ValueError("Output checks do not match measured modes")\n'
        '        if type(checks["reference_deterministic"]) is not bool:\n'
        '            raise ValueError("Invalid output repeatability result")\n'
        '        optional_checks = [checks[key] for key in ("fixed_control_repeat", "xnnpack_vs_fixed_control", "xnnpack_reference_repeat", "combined_vs_xnnpack", "combined_vs_fixed_control") if key in checks]\n'
        '        for check in [checks["reference_repeat"], *checks["modes"].values(), *optional_checks, *checks.get("per_mode_repeat", {}).values()]:\n'
        '            if check["finite"] is not True or check["fp32_samples"] <= 0:\n'
        '                raise ValueError("Invalid FP32 output check")\n'
        '            for field in ("pcm_exact", "fp32_exact"):\n'
        '                if type(check[field]) is not bool:\n'
        '                    raise ValueError("Invalid output exactness result")\n'
        '            for field in ("pcm_changed_samples", "pcm_max_abs_lsb", "pcm_rmse_lsb", "fp32_changed_samples", "fp32_max_abs", "fp32_rmse", "fp32_relative_rmse"):\n'
        '                if not isinstance(check[field], (int, float)) or not math.isfinite(check[field]) or check[field] < 0:\n'
        '                    raise ValueError("Invalid output error statistic")\n'
        '    by_block: dict[tuple[str, int], list[dict[str, Any]]] = {}\n'
        '    orders = []\n'
        '    for trial in result["trials"]:\n'
        '        if trial["mode"] not in modes or trial["threads"] != modes[trial["mode"]]["threads"]:\n'
        '            raise ValueError("Trial does not match its configured mode")\n'
        '        for field in ("elapsed_s", "audio_s", "rtf"):\n'
        '            if not isinstance(trial[field], (int, float)) or not math.isfinite(trial[field]) or trial[field] <= 0:\n'
        '                raise ValueError(f"Invalid {field}")\n'
        '        if not math.isclose(trial["rtf"], trial["elapsed_s"] / trial["audio_s"], rel_tol=1e-9):\n'
        '            raise ValueError("RTF does not match measured duration")\n'
        '        for field in ("cpu_time_s", "cpu_window_s", "cpu_avg_cores", "cpu_percent"):\n'
        '            if not isinstance(trial[field], (float, int)) or not math.isfinite(trial[field]) or trial[field] < 0:\n'
        '                raise ValueError(f"Invalid {field}")\n'
        '        if trial["cpu_window_s"] <= 0 or type(trial["cpu_incomplete"]) is not bool:\n'
        '            raise ValueError("Invalid CPU observation window/coverage")\n'
        '        for field in ("cpu_samples", "cpu_processes"):\n'
        '            if type(trial[field]) is not int or trial[field] < 1:\n'
        '                raise ValueError(f"Invalid {field}")\n'
        '        if not math.isclose(trial["cpu_avg_cores"], trial["cpu_time_s"] / trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-12):\n'
        '            raise ValueError("CPU core equivalent does not match the observation window")\n'
        '        if not math.isclose(trial["cpu_percent"], 100 * trial["cpu_avg_cores"], rel_tol=1e-9, abs_tol=1e-12):\n'
        '            raise ValueError("CPU percent does not match the one-core normalization")\n'
        '        if result["schema_version"] >= 3:\n'
        '            trace = trial["cpu_trace"]\n'
        '            if not trace or len(trace) != trial["cpu_samples"] - 1:\n'
        '                raise ValueError("CPU trace does not match the number of snapshots")\n'
        '            previous_end = None\n'
        '            incomplete = False\n'
        '            phases = []\n'
        '            for point in trace:\n'
        '                phase = point.get("phase", "action")\n'
        '                if phase not in ("pre", "action", "post"):\n'
        '                    raise ValueError("Invalid CPU observation phase")\n'
        '                if phases and phase != phases[-1]:\n'
        '                    incomplete = False\n'
        '                for key in ("start_s", "end_s", "cpu_time_s", "cpu_percent"):\n'
        '                    if not isinstance(point[key], (int, float)) or not math.isfinite(point[key]):\n'
        '                        raise ValueError(f"Invalid CPU interval {key}")\n'
        '                span = point["end_s"] - point["start_s"]\n'
        '                if span <= 0 or point["cpu_time_s"] < 0 or point["cpu_percent"] < 0:\n'
        '                    raise ValueError("Invalid CPU interval duration/counter")\n'
        '                if previous_end is not None and not math.isclose(point["start_s"], previous_end, abs_tol=1e-9):\n'
        '                    raise ValueError("CPU intervals must be contiguous")\n'
        '                if not math.isclose(point["cpu_percent"], 100 * point["cpu_time_s"] / span, rel_tol=1e-8, abs_tol=1e-8):\n'
        '                    raise ValueError("CPU interval percent does not match its actual duration")\n'
        '                if type(point["cpu_processes"]) is not int or not 1 <= point["cpu_processes"] <= trial["cpu_processes"]:\n'
        '                    raise ValueError("Invalid CPU interval process count")\n'
        '                if type(point["cpu_incomplete"]) is not bool or incomplete and not point["cpu_incomplete"]:\n'
        '                    raise ValueError("CPU interval coverage cannot recover after a lost process")\n'
        '                incomplete = point["cpu_incomplete"]\n'
        '                previous_end = point["end_s"]\n'
        '                phases.append(phase)\n'
        '            action_trace = [point for point in trace if point.get("phase", "action") == "action"]\n'
        '            if not action_trace or action_trace[-1]["cpu_incomplete"] != trial["cpu_incomplete"]:\n'
        '                raise ValueError("Action CPU coverage does not match its headline result")\n'
        '            if not action_trace or not math.isclose(sum(point["cpu_time_s"] for point in action_trace), trial["cpu_time_s"], rel_tol=1e-9, abs_tol=1e-9):\n'
        '                raise ValueError("Action CPU intervals do not sum to headline CPU time")\n'
        '            if not math.isclose(sum(point["end_s"] - point["start_s"] for point in action_trace), trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-9):\n'
        '                raise ValueError("Action CPU intervals do not match headline CPU window")\n'
        '            if result["schema_version"] >= 4:\n'
        '                if phases != sorted(phases, key={"pre": 0, "action": 1, "post": 2}.get) or set(phases) != {"pre", "action", "post"}:\n'
        '                    raise ValueError("CPU trace must contain ordered pre/action/post phases")\n'
        '                for phase in ("pre", "post"):\n'
        '                    seconds = sum(point["end_s"] - point["start_s"] for point in trace if point["phase"] == phase)\n'
        '                    if seconds < CPU_PADDING_S - 1e-6:\n'
        '                        raise ValueError("CPU padding does not contain a full observed second")\n'
        '        by_block.setdefault((trial["mode"], trial["block"]), []).append(trial)\n'
        '        orders.append(trial["order"])\n'
        '    if sorted(orders) != list(range(1, len(orders) + 1)):\n'
        '        raise ValueError("Trial order must be unique and consecutive")\n'
        '    for mode in modes:\n'
        '        for block in range(1, BLOCKS_PER_MODE + 1):\n'
        '            trials = by_block.get((mode, block), [])\n'
        '            if sorted(row["trial"] for row in trials) != list(range(1, expected_trials + 1)):\n'
        '                raise ValueError(f"Incomplete block: {mode} / {block}")\n'
        '            positions = sorted(row["order"] for row in trials)\n'
        '            if positions != list(range(positions[0], positions[0] + expected_trials)):\n'
        '                raise ValueError("Trials within a block must be consecutive")\n'
        '    if len(by_block) != len(modes) * BLOCKS_PER_MODE:\n'
        '        raise ValueError("Unexpected block")\n'
        '    if "schedule" in result:\n'
        '        actual = [{"mode": row["mode"], "number": row["block"]} for row in sorted(result["trials"], key=lambda row: row["order"]) if row["trial"] == 1]\n'
        '        if actual != result["schedule"]:\n'
        '            raise ValueError("Recorded block schedule differs from actual trials")\n'
        '        if result.get("schedule_method", "").startswith("seeded position-balanced"):\n'
        '            for mode in modes:\n'
        '                positions = [index % len(modes) for index, block in enumerate(actual) if block["mode"] == mode]\n'
        '                counts = [positions.count(position) for position in range(len(modes))]\n'
        '                if max(counts) - min(counts) > 1:\n'
        '                    raise ValueError("Settings pass is not position-balanced")\n'
        '\n'
        '\n'
        'def run_schedule(modes: list[Mode], seed: int,\n'
        '                 synthesize: Callable[[Mode], tuple[float, float, CpuMeasurement]],\n'
        '                 log: list[dict[str, Any]], *, balanced: bool = False) -> list[Trial]:\n'
        '    """Only one callback runs at a time; all model initialization is external."""\n'
        '    schedule = make_schedule(modes, seed, balanced=balanced)\n'
        '    indexed = {mode.key: mode for mode in modes}\n'
        '    trials = []\n'
        '    for block in schedule:\n'
        '        mode = indexed[block.mode]\n'
        '        progress(f"{mode.label}: block {block.number}/{BLOCKS_PER_MODE}")\n'
        '        for repetition in range(1, TRIALS_PER_BLOCK + 1):\n'
        '            elapsed, duration, cpu = synthesize(mode)\n'
        '            if not (math.isfinite(elapsed) and elapsed > 0 and math.isfinite(duration) and duration > 0):\n'
        '                raise RuntimeError(f"{mode.label} returned an invalid measurement")\n'
        '            trials.append(Trial(len(trials) + 1, mode.key, block.number, repetition,\n'
        '                                mode.threads, elapsed, duration, elapsed / duration, **asdict(cpu)))\n'
        '            coverage = " (CPU incomplete)" if cpu.cpu_incomplete else ""\n'
        '            progress(f"Trial {len(trials)}/{len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK}: {mode.label}, block {block.number}, {repetition}/{TRIALS_PER_BLOCK}; synthesis {elapsed:.3f}s, CPU {cpu.cpu_percent:.1f}%{coverage}")\n'
        '        log.append({"event": "block_completed", "mode": mode.key,\n'
        '                    "block": block.number, "samples": TRIALS_PER_BLOCK})\n'
        '    return trials\n'
        '\n'
        '\n'
        'def svg_results(result: dict[str, Any]) -> str:\n'
        '    modes = result["modes"]\n'
        '    rows = result["trials"]\n'
        '    width, height = 880, max(230, 85 + 66 * len(modes))\n'
        '    left, right, top, bottom = 220, 30, 24, 45\n'
        '    maximum = max(row["elapsed_s"] for row in rows) * 1.12\n'
        '    plot_width = width - left - right\n'
        '    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"]\n'
        '    output = [f\'<svg viewBox="0 0 {width} {height}" role="img" aria-label="各モードの合成時間。点は全試行、太線は中央値">\']\n'
        '    for tick in range(6):\n'
        '        value = maximum * tick / 5\n'
        '        x = left + value / maximum * plot_width\n'
        '        output.append(f\'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height-bottom}" stroke="#e2e6ec"/>\')\n'
        '        output.append(f\'<text x="{x:.2f}" y="{height-20}" text-anchor="middle">{value:.2f}</text>\')\n'
        '    for index, mode in enumerate(modes):\n'
        '        y = top + 33 + index * 66\n'
        '        samples = [row for row in rows if row["mode"] == mode["key"]]\n'
        '        color = colors[index % len(colors)]\n'
        '        output.append(f\'<text x="{left-15}" y="{y+5}" text-anchor="end">{html.escape(mode["label"])}</text>\')\n'
        '        for number, sample in enumerate(samples):\n'
        '            x = left + sample["elapsed_s"] / maximum * plot_width\n'
        '            dy = ((number % 5) - 2) * 5\n'
        '            label = html.escape(f\'Block {sample["block"]}, trial {sample["trial"]}: {sample["elapsed_s"]:.6f} s, RTF {sample["rtf"]:.6f}\')\n'
        '            output.append(f\'<circle cx="{x:.2f}" cy="{y+dy}" r="3.5" fill="{color}" fill-opacity="0.65"><title>{label}</title></circle>\')\n'
        '        median = statistics.median(row["elapsed_s"] for row in samples)\n'
        '        x = left + median / maximum * plot_width\n'
        '        interval = block_bootstrap_median_ci([(row["block"], row["elapsed_s"]) for row in samples])\n'
        '        if interval:\n'
        '            low, high = (left + interval[key] / maximum * plot_width for key in ("low", "high"))\n'
        '            output.append(f\'<path class="timing-ci" d="M{low:.2f},{y}H{high:.2f}M{low:.2f},{y-9}V{y+9}M{high:.2f},{y-9}V{y+9}" stroke="{color}" stroke-width="1.8" fill="none"><title>中央値の参考95%区間: {interval["low"]:.6f}–{interval["high"]:.6f}秒（3ブロックbootstrap）</title></path>\')\n'
        '        output.append(f\'<line x1="{x:.2f}" x2="{x:.2f}" y1="{y-17}" y2="{y+17}" stroke="{color}" stroke-width="3"><title>Median: {median:.6f} s</title></line>\')\n'
        '    output.append(f\'<text x="{left+plot_width/2}" y="{height-2}" text-anchor="middle">合成時間（秒、短いほど速い）</text></svg>\')\n'
        '    return "".join(output)\n'
        '\n'
        '\n'
        'def svg_sequence(result: dict[str, Any]) -> str:\n'
        '    rows = sorted(result["trials"], key=lambda row: row["order"])\n'
        '    modes = result["modes"]\n'
        '    colors = {mode["key"]: color for mode, color in zip(modes, ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"] * len(modes))}\n'
        '    width, height, left, top = 880, 180, 65, 15\n'
        '    plot_width, plot_height = width - left - 25, height - top - 40\n'
        '    maximum = max(row["rtf"] for row in rows) * 1.1\n'
        '    output = [f\'<svg viewBox="0 0 {width} {height}" role="img" aria-label="測定順とRTF">\']\n'
        '    for tick in range(4):\n'
        '        value = maximum * tick / 3\n'
        '        y = top + plot_height - value / maximum * plot_height\n'
        '        output.append(f\'<line x1="{left}" y1="{y:.2f}" x2="{width-25}" y2="{y:.2f}" stroke="#e2e6ec"/><text x="{left-10}" y="{y+4:.2f}" text-anchor="end">{value:.2f}</text>\')\n'
        '    for row in rows:\n'
        '        x = left + (row["order"] - 1) / max(1, len(rows) - 1) * plot_width\n'
        '        y = top + plot_height - row["rtf"] / maximum * plot_height\n'
        '        output.append(f\'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{colors[row["mode"]]}"><title>#{row["order"]}: {html.escape(row["mode"])} / RTF {row["rtf"]:.6f}</title></circle>\')\n'
        '    output.append(f\'<text x="{left}" y="{height-18}" text-anchor="middle">1</text><text x="{width-25}" y="{height-18}" text-anchor="middle">{len(rows)}</text><text x="{width/2}" y="{height-2}" text-anchor="middle">測定順（縦軸：RTF）</text></svg>\')\n'
        '    return "".join(output)\n'
        '\n'
        '\n'
        'def aggregate_cpu_profiles(result: dict[str, Any], bin_seconds: float = 0.25) -> list[dict[str, Any]]:\n'
        '    """Actual request-relative pre/action/post; pointwise block-bootstrap CI."""\n'
        '    if not math.isfinite(bin_seconds) or bin_seconds <= 0:\n'
        '        raise ValueError("CPU profile bin duration must be positive")\n'
        '    profiles = []\n'
        '    padded = result["schema_version"] >= 4\n'
        '    minimum = -CPU_PADDING_S if padded else 0.0\n'
        '    for mode in result["modes"]:\n'
        '        trials = [row for row in result["trials"] if row["mode"] == mode["key"]]\n'
        '        phase_counts = {phase: 0 for phase in ("pre", "action", "post")}\n'
        '        trial_segments = []\n'
        '        response_ends = []\n'
        '        for trial in trials:\n'
        '            segments = []\n'
        '            for phase in phase_counts:\n'
        '                intervals = [point for point in trial.get("cpu_trace", []) if point.get("phase", "action") == phase]\n'
        '                if not intervals or any(point["cpu_incomplete"] for point in intervals):\n'
        '                    continue\n'
        '                phase_counts[phase] += 1\n'
        '                if phase == "action":\n'
        '                    response_ends.append(intervals[-1]["end_s"])\n'
        '                post_limit = intervals[0]["start_s"] + CPU_PADDING_S if phase == "post" else math.inf\n'
        '                for point in intervals:\n'
        '                    start, end = point["start_s"], min(point["end_s"], post_limit)\n'
        '                    rate = point["cpu_time_s"] / (point["end_s"] - point["start_s"])\n'
        '                    if padded and phase == "action":\n'
        '                        start = max(0.0, start)\n'
        '                    elif phase == "pre":\n'
        '                        end = min(0.0, end)\n'
        '                    if end > start:\n'
        '                        segments.append((start, end, rate))\n'
        '            trial_segments.append((trial["block"], segments))\n'
        '        maximum = max((end for _, segments in trial_segments for _, end, _ in segments), default=0.0)\n'
        '        count = max(0, math.ceil((maximum - minimum) / bin_seconds))\n'
        '        contributions: list[list[tuple[int, float, float]]] = [[] for _ in range(count)]\n'
        '        for block, segments in trial_segments:\n'
        '            cpu, observed = [0.0] * count, [0.0] * count\n'
        '            for start, end, rate in segments:\n'
        '                start, end = max(minimum, start), min(maximum, end)\n'
        '                index = max(0, math.floor((start - minimum) / bin_seconds))\n'
        '                while index < count and minimum + index * bin_seconds < end:\n'
        '                    left = minimum + index * bin_seconds\n'
        '                    overlap = max(0.0, min(end, left + bin_seconds) - max(start, left))\n'
        '                    cpu[index] += rate * overlap\n'
        '                    observed[index] += overlap\n'
        '                    index += 1\n'
        '            for index, seconds in enumerate(observed):\n'
        '                if seconds > 0:\n'
        '                    contributions[index].append((block, cpu[index], seconds))\n'
        '        points = []\n'
        '        for index, entries in enumerate(contributions):\n'
        '            if not entries:\n'
        '                continue\n'
        '            left, right = minimum + index * bin_seconds, min(maximum, minimum + (index + 1) * bin_seconds)\n'
        '            cpu, seconds = math.fsum(entry[1] for entry in entries), math.fsum(entry[2] for entry in entries)\n'
        '            samples = [(block, 100 * value / observed) for block, value, observed in entries]\n'
        '            full_count = sum(observed >= right - left - 1e-9 for _, _, observed in entries)\n'
        '            interval = block_bootstrap_median_ci(samples) if full_count == len(trials) else None\n'
        '            points.append({"start_s": left, "end_s": right, "cpu_percent": 100 * cpu / seconds,\n'
        '                           "median_percent": statistics.median(value for _, value in samples),\n'
        '                           "ci_low_percent": interval["low"] if interval else None,\n'
        '                           "ci_high_percent": interval["high"] if interval else None,\n'
        '                           "cpu_time_s": cpu, "observed_s": seconds, "trial_count": len(entries),\n'
        '                           "full_trial_count": full_count, "contributing_blocks": len({block for block, _ in samples})})\n'
        '        profiles.append({"mode": mode["key"], "label": mode["label"], "complete_trials": phase_counts["action"],\n'
        '                         "phase_complete_trials": phase_counts, "total_trials": len(trials), "bin_seconds": bin_seconds,\n'
        '                         "response_end_median_s": statistics.median(response_ends) if response_ends else None,\n'
        '                         "response_end_min_s": min(response_ends) if response_ends else None,\n'
        '                         "response_end_max_s": max(response_ends) if response_ends else None,\n'
        '                         "points": points})\n'
        '    return profiles\n'
        '\n'
        '\n'
        'def cpu_profile_html(result: dict[str, Any]) -> str:\n'
        '    if result["schema_version"] < 3:\n'
        '        return ""\n'
        '    profiles = aggregate_cpu_profiles(result)\n'
        '    padded = result["schema_version"] >= 4\n'
        '    xmin = -CPU_PADDING_S if padded else 0.0\n'
        '    xmax = max((point["end_s"] for row in profiles for point in row["points"]), default=0.25)\n'
        '    ymax = max(100, math.ceil(max((point["ci_high_percent"] if point["ci_high_percent"] is not None else point["median_percent"]\n'
        '                                  for row in profiles for point in row["points"]), default=0) / 100) * 100)\n'
        '    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"]\n'
        '    figures = []\n'
        '    for index, profile in enumerate(profiles):\n'
        '        left, top, width, height = 52, 30, 365, 140\n'
        '        def x(value: float) -> float:\n'
        '            return left + (value - xmin) / (xmax - xmin) * width\n'
        '        def y(value: float) -> float:\n'
        '            return top + height - value / ymax * height\n'
        '        color = colors[index % len(colors)]\n'
        '        parts = [f\'<svg class="cpu-profile" data-mode="{profile["mode"]}" viewBox="0 0 440 215" role="img" aria-label="{html.escape(profile["label"], quote=True)} CPU時系列、全試行の中央値と参考95%区間"><title>{html.escape(profile["label"])}: {profile["total_trials"]}回／3ブロック</title>\',\n'
        '                 f\'<text x="{left}" y="16">{html.escape(profile["label"])}</text>\']\n'
        '        if padded:\n'
        '            parts.append(f\'<rect x="{left}" y="{top}" width="{x(0)-left:.2f}" height="{height}" fill="#edf0f5"/>\')\n'
        '        for tick in range(3):\n'
        '            value = ymax * tick / 2\n'
        '            parts.append(f\'<line x1="{left}" y1="{y(value):.2f}" x2="{left+width}" y2="{y(value):.2f}" stroke="#e2e6ec"/><text x="{left-7}" y="{y(value)+4:.2f}" text-anchor="end">{value:.0f}%</text>\')\n'
        '        for tick in range(5):\n'
        '            value = xmax * tick / 4\n'
        '            parts.append(f\'<text x="{x(value):.2f}" y="190" text-anchor="middle">{value:.1f}</text>\')\n'
        '        if padded:\n'
        '            parts.append(f\'<text x="{left}" y="190" text-anchor="end">−1</text>\')\n'
        '        if profile["response_end_median_s"] is not None:\n'
        '            a, b, median = (x(profile[key]) for key in ("response_end_min_s", "response_end_max_s", "response_end_median_s"))\n'
        '            parts.append(f\'<rect class="response-end-range" x="{a:.2f}" y="{top}" width="{max(0.5,b-a):.2f}" height="{height}" fill="#8793a1" fill-opacity="0.16"/>\')\n'
        '            parts.append(f\'<line class="response-end-median" x1="{median:.2f}" x2="{median:.2f}" y1="{top}" y2="{top+height}" stroke="#647082" stroke-dasharray="3 3"><title>応答直後の採取境界: 中央値{profile["response_end_median_s"]:.3f}秒、範囲{profile["response_end_min_s"]:.3f}–{profile["response_end_max_s"]:.3f}秒</title></line>\')\n'
        '        previous, previous_end = None, None\n'
        '        for point in profile["points"]:\n'
        '            partial = point["full_trial_count"] < profile["total_trials"]\n'
        '            opacity, dash = ("0.45", \' stroke-dasharray="3 2"\') if partial else ("1", "")\n'
        '            a, b = x(point["start_s"]), x(point["end_s"])\n'
        '            if point["ci_low_percent"] is not None:\n'
        '                parts.append(f\'<rect class="cpu-ci" x="{a:.2f}" y="{y(point["ci_high_percent"]):.2f}" width="{b-a:.2f}" height="{y(point["ci_low_percent"])-y(point["ci_high_percent"]):.2f}" fill="{color}" fill-opacity="0.18"/>\')\n'
        '            path = f\'M{a:.2f},{y(point["median_percent"]):.2f}L{b:.2f},{y(point["median_percent"]):.2f}\'\n'
        '            if previous is not None and math.isclose(previous_end, point["start_s"], abs_tol=1e-9):\n'
        '                path = f\'M{a:.2f},{y(previous):.2f}L{a:.2f},{y(point["median_percent"]):.2f}\' + path\n'
        '            parts.append(f\'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" opacity="{opacity}"{dash}/>\')\n'
        '            interval = (f\'{point["ci_low_percent"]:.1f}–{point["ci_high_percent"]:.1f}%\' if point["ci_low_percent"] is not None else \'なし（全試行の区間全体を観測できず）\')\n'
        '            title = f\'{point["start_s"]:.2f}–{point["end_s"]:.2f}秒: 中央値{point["median_percent"]:.1f}%, 参考95%区間{interval}, 寄与n={point["trial_count"]}, 区間全体の観測n={point["full_trial_count"]}\'\n'
        '            parts.append(f\'<rect x="{a:.2f}" y="{top}" width="{max(1,b-a):.2f}" height="{height}" fill="transparent"><title>{html.escape(title)}</title></rect>\')\n'
        '            previous, previous_end = point["median_percent"], point["end_s"]\n'
        '        axis_caption = "合成要求開始からの秒数（前後も同じ軸）" if padded else "合成要求開始からの秒数"\n'
        '        parts.append(f\'<text x="{left+width/2}" y="211" text-anchor="middle">{axis_caption}</text></svg>\')\n'
        '        tail = profile["points"][-1]["trial_count"] if profile["points"] else 0\n'
        '        figures.append(\'<figure>\' + \'\'.join(parts) + f\'<figcaption>全{profile["total_trials"]}回を集計 · 末尾の寄与 n={tail}</figcaption></figure>\')\n'
        '    payload = json.dumps(profiles, ensure_ascii=False, separators=(",", ":")).replace("<", "\\\\u003c")\n'
        '    span_note = "開始前1秒から各試行の応答完了後1秒までを同じ実秒軸に表示。" if padded else "合成中の観測を要求開始からの実秒軸に表示。"\n'
        '    return \'<h2>CPU 使用率の時間推移（モード別集計）</h2><p>中央値・参考95%区間（15回／3ブロック、各時点）</p><div class="cpu-grid">\' + \'\'.join(figures) + \'</div><p class="cpu-caption">\' + span_note + \'縦の点線は応答直後の採取境界の中央値、灰色は最短〜最長。95%帯は全15回で区間全体を観測できた箇所だけに表示し、末尾は実測の中央値とnのみ（0埋めなし）。100%＝論理1コア。平均CPU表は合成中のみ。</p><script id="cpu-profile-data" type="application/json">\' + payload + \'</script>\'\n'
        '\n'
        '\n'
        'def render_report(result: dict[str, Any], target: Path) -> None:\n'
        '    validate_results(result)\n'
        '    raw_csv = trial_csv(result["trials"])\n'
        '    profile = cpu_profile_html(result)\n'
        '    spectra = spectrograms_html(result)\n'
        '    profile_csv = (f\'<details><summary>CPU 時系列（CSV）</summary><button data-csv="cpu-csv" data-filename="voicevox-cpu-timeseries.csv" type="button">CSV を保存</button><pre id="cpu-csv">{html.escape(cpu_trace_csv(result["trials"]))}</pre></details>\'\n'
        '                   if result["schema_version"] >= 3 else "")\n'
        '    summaries = []\n'
        '    checks = result.get("output_checks")\n'
        '    output_header = "<th>PCM / FP32</th>" if checks else ""\n'
        '    for mode in result["modes"]:\n'
        '        trials = [row for row in result["trials"] if row["mode"] == mode["key"]]\n'
        '        cpu_percent = weighted_cpu_percent(trials)\n'
        '        interval = block_bootstrap_median_ci([(row["block"], row["elapsed_s"]) for row in trials])\n'
        '        output_cell = ""\n'
        '        if checks:\n'
        '            check = checks["modes"][mode["key"]]\n'
        '            status = ("一致" if check["pcm_exact"] else "差分あり") + " / " + ("一致" if check["fp32_exact"] else "差分あり")\n'
        '            if not checks["reference_deterministic"]:\n'
        '                status = "基準が非決定的"\n'
        '            output_cell = f"<td>{status}</td>"\n'
        '        summaries.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (\n'
        '            mode["label"], mode["threads"], len(trials),\n'
        '            f\'{statistics.median(row["elapsed_s"] for row in trials):.3f}\',\n'
        '            f\'{statistics.median(row["rtf"] for row in trials):.3f}\',\n'
        '            "—" if cpu_percent is None else f"{cpu_percent:.1f}%",\n'
        '            f\'{interval["low"]:.3f}–{interval["high"]:.3f}\' if interval else "—",\n'
        '        )) + output_cell + "</tr>")\n'
        '    report_environment = {**result["environment"], "cpu_plot_aggregation": "actual request-start axis including post; 0.25s per-trial overlap-weighted rates, then median; nominal pointwise 95% whole-block percentile bootstrap, 27 draws; no tail CI without 15 fully observed trials",\n'
        '                          "report_script_sha256": sha256_file(Path(__file__)),\n'
        '                          "statistics": "15 trials in 3 intact blocks; all 27 draws of 3 blocks with replacement; median estimator; linearly interpolated 2.5/97.5 percentiles; exploratory, only 3 clusters"}\n'
        '    env_rows = "".join(f"<tr><th>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>" for key, value in report_environment.items())\n'
        '    mode_rows = "".join(f\'<li>{html.escape(mode["label"])}: {mode["threads"]} inference thread(s), {html.escape(mode["backend"])}</li>\' for mode in result["modes"])\n'
        '    note_values = list(result.get("notes", []))\n'
        '    incomplete = sum(row["cpu_incomplete"] for row in result["trials"])\n'
        '    if incomplete:\n'
        '        note_values.append(f"CPU追跡が不完全な{incomplete}試行はCPU平均から除外。試行のCPU総量は観測できた下限値で、時系列では区間への帰属にも不確実性あり。")\n'
        '    if checks and not checks["reference_deterministic"]:\n'
        '        note_values.append("基準の繰り返し出力が一致しないため、この実行では精度維持の判定を行わない。")\n'
        '    output_details = (f\'<details><summary>出力一致の検証</summary><p>同じ実行の {html.escape(checks["reference_mode"])} が基準。PCMは整数16bit、FP32はPCM化前。完全一致のみ一致と表示し、誤差の許容しきい値は設定しない。</p><pre>{html.escape(json.dumps(checks, ensure_ascii=False, indent=2))}</pre></details>\'\n'
        '                      if checks else "")\n'
        '    notes = "".join(f"<li>{html.escape(note)}</li>" for note in note_values)\n'
        '    duration = result["audio_s"]\n'
        '    log_lines = "\\n".join(json.dumps(event, ensure_ascii=False) for event in result.get("log", []))\n'
        '    ordering = ("乱数で基本順とラウンド順を決め、各位置の回数差を最大1に制限。各ペアの前後順は両方を含む" if result.get("schedule_method", "").startswith("seeded position-balanced")\n'
        '                else "各ラウンドでモード順をシャッフル")\n'
        "    document = f'''<!doctype html>\n"
        '<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<title>VOICEVOX CORE benchmark</title>\n'
        '<style>\n'
        ':root{{font-family:system-ui,-apple-system,sans-serif;color:#202936;background:#fff;font-size:15px;line-height:1.5}}body{{max-width:960px;margin:36px auto;padding:0 24px}}h1{{font-size:24px;letter-spacing:-.03em;margin:0 0 6px}}h2{{font-size:17px;margin:28px 0 10px}}p{{margin:6px 0 18px;color:#546170}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:9px 12px;border-bottom:1px solid #e2e6ec;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{font-weight:600;background:#f6f8fa}}figure{{margin:18px 0}}figcaption{{color:#546170;font-size:13px}}svg{{width:100%;height:auto}}svg text{{font-size:12px;fill:#546170}}details{{margin:18px 0;border-top:1px solid #d8dee7;padding-top:12px}}summary{{cursor:pointer;font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f6f8fa;padding:14px;max-height:420px;overflow:auto}}button{{font:inherit;background:#fff;border:1px solid #a8b4c4;border-radius:4px;padding:6px 12px;cursor:pointer;margin-top:12px}}.cpu-grid,.spectrogram-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 12px}}.cpu-grid figure,.spectrogram-grid figure{{margin:0}}.spectrogram figcaption{{min-height:3em}}.spectrogram img{{display:block;width:100%;height:auto}}.cpu-caption{{font-size:12px;margin-top:12px}}.scroll{{overflow-x:auto}}.environment th{{text-align:left;width:34%}}.environment td{{text-align:left;overflow-wrap:anywhere}}ul{{padding-left:22px}}@media(max-width:600px){{.cpu-grid,.spectrogram-grid{{grid-template-columns:1fr}}body{{margin:20px auto;padding:0 14px}}th,td{{padding:7px}}}}\n'
        '</style>\n'
        '<h1>VOICEVOX CORE benchmark</h1>\n'
        '<p>{html.escape(result["created_at"])} · 音声 {duration:.3f} 秒 · 各モード 5 回 × 3 ブロック</p>\n'
        '<p>合成時間：中央値・参考95%区間（15回／3ブロック）</p>\n'
        '<div class="scroll"><table><thead><tr><th>モード</th><th>スレッド</th><th>回数</th><th>中央値（秒）</th><th>中央値 RTF</th><th>平均 CPU</th><th>参考95%区間（秒）</th>{output_header}</tr></thead><tbody>{\'\'.join(summaries)}</tbody></table></div>\n'
        '<figure>{svg_results(result)}<figcaption>点は15回の各試行、太線は中央値、横の誤差棒は参考95%区間（3ブロックbootstrap）。RTF = 合成時間 ÷ 出力音声の長さ</figcaption></figure>\n'
        '<p class="cpu-caption">3ブロックしかないため、95%区間は参考値です。安定した95%被覆を保証するものではありません。</p>\n'
        '<details><summary>95%区間の計算方法</summary><p>5回連続のブロックを分割せず、3ブロックを3個復元抽出する全27通りで中央値を再計算。その分布の2.5%・97.5%分位を線形補間して表示する名目95%percentile区間です。15回を独立標本とは扱いません。CPUは5本の曲線をブロック単位で再抽出し、各時点の中央値について同じ計算を行います。点ごとの区間であり、曲線全体を同時に95%で覆う帯ではありません。以前の四分位帯（中央50%範囲）とは異なります。</p></details>\n'
        '<figure>{svg_sequence(result)}</figure>\n'
        '{profile}\n'
        '{spectra}\n'
        '<h2>測定条件</h2>\n'
        '<ul><li>同一 sample.vvm・Style ID {result["style_id"]}・準備済み AudioQuery JSON を使用。CPU 推論のみ、辞書・テキスト解析なし</li>\n'
        '<li>モデル初期化・ダウンロード・ビルド・AudioQuery 作成・ウォームアップ・音声保存は測定外。合成から WAV 生成までを測定</li>\n'
        '<li>{ordering}。1 ブロック内は 5 回連続。3 ラウンド、seed = {result["seed"]}。モード間の同時実行なし</li>\n'
        '<li>表示スレッド数は推論に設定した値。Web Worker 数ではない</li>\n'
        '<li>CPU は対象プロセスと子孫の user＋system 時間 ÷ カウンター採取間の観測時間。100%＝論理1コア、各試行の時間で重み付けした平均</li>\n'
        '<li>browser はモード別 Chromium 全体を集計（Python・制御用 Node は除外）。100ms ごとに追跡し、短命プロセスは取りこぼす場合あり</li>{mode_rows}{notes}</ul>\n'
        '<details><summary>実行環境・ログ</summary><table class="environment">{env_rows}</table><pre>{html.escape(log_lines)}</pre></details>\n'
        '{output_details}\n'
        '<details><summary>生データ（CSV）</summary><button id="save-csv" data-csv="raw-csv" data-filename="voicevox-benchmark.csv" type="button">CSV を保存</button><pre id="raw-csv">{html.escape(raw_csv)}</pre></details>\n'
        '{profile_csv}\n'
        '<script>\n'
        "document.querySelectorAll('button[data-csv]').forEach(button=>button.addEventListener('click',()=>{{\n"
        " const blob=new Blob([document.getElementById(button.dataset.csv).textContent],{{type:'text/csv;charset=utf-8'}});\n"
        " const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=button.dataset.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);\n"
        '}}));\n'
        "</script></html>'''\n"
        '    atomic_write(target, document.encode("utf-8"))\n'
        '\n'
        '\n'
        '# The generated Rust program uses no text analyzer or dictionary on either target.\n'
        "RUST_SOURCE = r'''\n"
        'use anyhow::{Context as _, ensure};\n'
        'use std::{io::{self, BufRead, Write}, sync::{Mutex, OnceLock}};\n'
        'use voicevox_core::{AccelerationMode, AudioQuery, StyleId, VoiceModelId, blocking::{Onnxruntime, Synthesizer, VoiceModelFile}};\n'
        'static SYNTH: OnceLock<Synthesizer<()>> = OnceLock::new();\n'
        'static QUERY: OnceLock<AudioQuery> = OnceLock::new();\n'
        'static WAV: Mutex<Vec<u8>> = Mutex::new(Vec::new());\n'
        'static RAW: Mutex<Vec<u8>> = Mutex::new(Vec::new());\n'
        'static SPIN_OFF: OnceLock<bool> = OnceLock::new();\n'
        'static FIXED_LENGTH: OnceLock<usize> = OnceLock::new();\n'
        'static MODEL_ID: OnceLock<VoiceModelId> = OnceLock::new();\n'
        "fn setup(runtime: &'static Onnxruntime, model: &str, query: &str, threads: u16, fixed_shape: bool, xnn_threads: u16, profile: bool) -> anyhow::Result<()> {\n"
        '    let query: AudioQuery = serde_json::from_slice(&std::fs::read(query)?)?;\n'
        '    query.validate()?;\n'
        '    let padded_length = if fixed_shape { Some(Synthesizer::<()>::benchmark_vocoder_length(&query)?) } else { None };\n'
        '    voicevox_core::__benchmark_fixed_shape::configure(padded_length, xnn_threads.into(), profile)?;\n'
        '    let synth = Synthesizer::builder(runtime).acceleration_mode(AccelerationMode::Cpu).cpu_num_threads(if xnn_threads > 0 { 1 } else { threads }).build()?;\n'
        '    let model = VoiceModelFile::open(model)?;\n'
        '    MODEL_ID.set(model.id()).map_err(|_| anyhow::anyhow!("Model ID already set"))?;\n'
        '    synth.load_voice_model(&model).perform()?;\n'
        '    voicevox_core::__benchmark_fixed_shape::verify_loaded()?;\n'
        '    FIXED_LENGTH.set(padded_length.unwrap_or(0)).map_err(|_| anyhow::anyhow!("Fixed-shape configuration already set"))?;\n'
        '    SYNTH.set(synth).map_err(|_| anyhow::anyhow!("Already initialized"))?;\n'
        '    QUERY.set(query).map_err(|_| anyhow::anyhow!("Already initialized"))?;\n'
        '    Ok(())\n'
        '}\n'
        'fn synthesize(style: u32) -> anyhow::Result<()> {\n'
        '    let synth = SYNTH.get().context("Not initialized")?;\n'
        '    let query = QUERY.get().context("No AudioQuery")?;\n'
        '    let wav = synth.synthesis(query, StyleId::new(style)).perform()?;\n'
        '    ensure!(wav.len() >= 44 && &wav[..4] == b"RIFF" && &wav[8..12] == b"WAVE", "Invalid WAV");\n'
        '    *WAV.lock().unwrap() = wav;\n'
        '    Ok(())\n'
        '}\n'
        'fn raw_wave(style: u32) -> anyhow::Result<()> {\n'
        '    let synth = SYNTH.get().context("Not initialized")?;\n'
        '    let wave = synth.benchmark_raw_synthesis(QUERY.get().context("No AudioQuery")?, StyleId::new(style))?;\n'
        '    ensure!(wave.iter().all(|sample| sample.is_finite()), "Nonfinite FP32 waveform");\n'
        '    *RAW.lock().unwrap() = wave.iter().flat_map(|sample| sample.to_le_bytes()).collect();\n'
        '    Ok(())\n'
        '}\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_init(threads: u16, spin_off: bool, fixed_shape: bool, xnn_threads: u16, profile: bool) -> i32 {\n'
        '    let result = browser_runtime(threads, spin_off, xnn_threads).and_then(|runtime| setup(runtime, "/sample.vvm", "/query.json", threads, fixed_shape, xnn_threads, profile));\n'
        '    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }\n'
        '}\n'
        '#[cfg(target_os="emscripten")]\n'
        "fn browser_runtime(threads: u16, spin_off: bool, xnn_threads: u16) -> anyhow::Result<&'static Onnxruntime> {\n"
        '    #[cfg(feature="threaded")]\n'
        '    {\n'
        "        // ORT's pthread WASM build requires one global pool. Register it before\n"
        '        // CORE creates its environment; the pinned ort crate automatically\n'
        '        // disables per-session pools when a global pool is present.\n'
        '        let base = unsafe { &*ort::sys::OrtGetApiBase() };\n'
        '        let api = unsafe { (base.GetApi)(ort::sys::ORT_API_VERSION) };\n'
        '        ensure!(!api.is_null(), "ORT API unavailable");\n'
        '        ensure!(ort::set_api(unsafe { api.read() }), "ORT API already initialized");\n'
        '        let pool = ort::environment::GlobalThreadPoolOptions::default()\n'
        '            .with_intra_threads(if xnn_threads > 0 { 1 } else { threads.into() })?.with_inter_threads(1)?.with_spin_control(!spin_off)?;\n'
        '        ensure!(ort::init().with_name("voicevox_benchmark").with_global_thread_pool(pool).commit(), "ORT environment already initialized");\n'
        '        SPIN_OFF.set(spin_off).map_err(|_| anyhow::anyhow!("Spin configuration already set"))?;\n'
        '    }\n'
        '    #[cfg(not(feature="threaded"))]\n'
        '    let _ = (threads, spin_off, xnn_threads);\n'
        '    Ok(Onnxruntime::init_once()?)\n'
        '}\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_synthesize(style: u32) -> i32 {\n'
        '    match synthesize(style) { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }\n'
        '}\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_wav_ptr() -> *const u8 { WAV.lock().unwrap().as_ptr() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_wav_len() -> usize { WAV.lock().unwrap().len() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_raw(style: u32) -> i32 {\n'
        '    match raw_wave(style) { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }\n'
        '}\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_raw_ptr() -> *const u8 { RAW.lock().unwrap().as_ptr() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_raw_len() -> usize { RAW.lock().unwrap().len() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_spin_off() -> i32 { i32::from(SPIN_OFF.get().copied().unwrap_or(false)) }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_fixed_length() -> usize { FIXED_LENGTH.get().copied().unwrap_or(0) }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_fixed_matches() -> usize { voicevox_core::__benchmark_fixed_shape::verified_sessions() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_xnn_threads() -> usize { voicevox_core::__benchmark_fixed_shape::xnn_threads() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_xnn_sessions() -> usize { voicevox_core::__benchmark_fixed_shape::xnn_sessions() }\n'
        '#[cfg(target_os="emscripten")]\n'
        '#[unsafe(no_mangle)]\n'
        'pub extern "C" fn bench_finish_profile() -> i32 {\n'
        '    let result = (|| -> anyhow::Result<()> {\n'
        '        ensure!(voicevox_core::__benchmark_fixed_shape::profiling_enabled(), "Profiling is disabled");\n'
        '        println!("BENCH_PROFILE_BEGIN"); io::stdout().flush()?;\n'
        '        SYNTH.get().context("Not initialized")?.unload_voice_model(*MODEL_ID.get().context("No model ID")?)?;\n'
        '        println!("BENCH_PROFILE_END"); io::stdout().flush()?;\n'
        '        Ok(())\n'
        '    })();\n'
        '    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }\n'
        '}\n'
        '#[cfg(target_os="emscripten")]\n'
        'fn main() {}\n'
        '#[cfg(not(target_os="emscripten"))]\n'
        'fn main() -> anyhow::Result<()> {\n'
        '    let args: Vec<String> = std::env::args().collect();\n'
        '    ensure!(args.len() == 7, "Expected runtime, model, query, threads, style, wav destination");\n'
        '    let runtime = Onnxruntime::load_once().filename(&args[1]).perform()?;\n'
        '    let threads = args[4].parse()?;\n'
        '    let style = args[5].parse()?;\n'
        '    setup(runtime, &args[2], &args[3], threads, false, 0, false)?;\n'
        '    println!("{}", serde_json::json!({"ready": true, "threads": threads}));\n'
        '    io::stdout().flush()?;\n'
        '    for line in io::stdin().lock().lines() {\n'
        '        let line = line?;\n'
        '        if line == "quit" { break; }\n'
        '        if line == "raw-save" {\n'
        '            raw_wave(style)?;\n'
        '            let raw = RAW.lock().unwrap();\n'
        '            std::fs::write(std::path::Path::new(&args[6]).with_extension("f32"), &*raw)?;\n'
        '            println!("{}", serde_json::json!({"raw_bytes": raw.len()}));\n'
        '            io::stdout().flush()?;\n'
        '            continue;\n'
        '        }\n'
        '        ensure!(line == "synthesize" || line == "synthesize-save", "Unknown request");\n'
        '        let start = std::time::Instant::now();\n'
        '        synthesize(style)?;\n'
        '        let elapsed_s = start.elapsed().as_secs_f64();\n'
        '        let wav = WAV.lock().unwrap();\n'
        '        if line == "synthesize-save" { std::fs::write(&args[6], &*wav)?; }\n'
        '        println!("{}", serde_json::json!({"elapsed_s": elapsed_s, "wav_bytes": wav.len()}));\n'
        '        io::stdout().flush()?;\n'
        '    }\n'
        '    Ok(())\n'
        '}\n'
        "'''\n"
        '\n'
        "BROWSER_WORKER = r'''\n"
        'let configuredThreads=1, threaded=false, ready=false, wasmStdout=[];\n'
        'function failure(error){postMessage({error:String(error && error.stack || error)});}\n'
        'onmessage=async ({data})=>{\n'
        ' try{\n'
        "  if(data.command==='init'){\n"
        '   configuredThreads=data.threads;threaded=data.threaded;\n'
        "   if(data.threaded && (!self.crossOriginIsolated || typeof SharedArrayBuffer==='undefined'))\n"
        "    throw new Error('Multithreaded WASM requires cross-origin isolation and SharedArrayBuffer');\n"
        '   globalThis.Module={noInitialRun:true,benchmarkPoolSize:Math.max(1,configuredThreads),mainScriptUrlOrBlob:new URL(data.module,self.location).href,\n'
        '    locateFile:(path)=>new URL(path,new URL(data.module,self.location)).href,\n'
        "    printErr:(...values)=>postMessage({log:values.join(' ')}),\n"
        "    print:(...values)=>wasmStdout.push(values.join(' ')),\n"
        '    onAbort:failure,\n'
        '    onRuntimeInitialized:async()=>{\n'
        '     try{\n'
        "      for(const file of ['sample.vvm','query.json']){\n"
        "       const response=await fetch(file==='sample.vvm' && data.model_url ? data.model_url : '/'+file);if(!response.ok)throw new Error('HTTP '+response.status+' '+file);\n"
        "       Module.FS.writeFile('/'+file,new Uint8Array(await response.arrayBuffer()));\n"
        '      }\n'
        "      if(Module._bench_init(configuredThreads,Number(data.spin_off),Number(data.fixed_shape),data.xnn_threads||0,Number(data.profile))!==0)throw new Error('CORE initialization failed');\n"
        "      Module.FS.unlink('/sample.vvm');Module.FS.unlink('/query.json');ready=true;\n"
        '      const pthreads=threaded ? Module.PThread.runningWorkers.length : 0;\n'
        '      if(threaded && configuredThreads>1 && pthreads<configuredThreads-1)\n'
        "       throw new Error('ORT did not create the requested inference pthreads');\n"
        "      postMessage({ready:true,threads:configuredThreads,shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads_created:pthreads,spin_off:Boolean(Module._bench_spin_off()),fixed_length:Module._bench_fixed_length(),fixed_matches:Module._bench_fixed_matches(),xnn_threads:Module._bench_xnn_threads(),xnn_sessions:Module._bench_xnn_sessions(),model_target:typeof Module._bench_vocoder_fixed_matches==='function'?'predictor':'vocoder',vocoder_fixed_matches:typeof Module._bench_vocoder_fixed_matches==='function'?Module._bench_vocoder_fixed_matches():Module._bench_fixed_matches(),vocoder_xnn_sessions:typeof Module._bench_vocoder_xnn_sessions==='function'?Module._bench_vocoder_xnn_sessions():Module._bench_xnn_sessions()});\n"
        '     }catch(error){failure(error);}\n'
        '    }};\n'
        '   importScripts(data.module);\n'
        "  }else if(data.command==='synthesize'){\n"
        "   if(!ready)throw new Error('CORE not ready');\n"
        '   const start=performance.now(),code=Module._bench_synthesize(data.style);\n'
        '   const elapsed_s=(performance.now()-start)/1000;\n'
        "   if(code!==0)throw new Error('Synthesis failed: '+code);\n"
        '   const ptr=Module._bench_wav_ptr(),length=Module._bench_wav_len();\n'
        "   if(!ptr||length<44)throw new Error('Invalid WAV');\n"
        '   const result={elapsed_s,wav_bytes:length};\n'
        '   if(data.save){result.wav=Module.HEAPU8.slice(ptr,ptr+length).buffer;postMessage(result,[result.wav]);}\n'
        '   else postMessage(result);\n'
        "  }else if(data.command==='raw'){\n"
        "   if(!ready || Module._bench_raw(data.style)!==0)throw new Error('Untimed FP32 synthesis failed');\n"
        '   const ptr=Module._bench_raw_ptr(),length=Module._bench_raw_len();\n'
        "   if(!ptr||length<4||length%4)throw new Error('Invalid FP32 output');\n"
        '   const raw=Module.HEAPU8.slice(ptr,ptr+length).buffer;postMessage({raw},[raw]);\n'
        "  }else if(data.command==='finish-profile'){\n"
        '   const start=wasmStdout.length;\n'
        "   if(Module._bench_finish_profile()!==0)throw new Error('Failed to flush untimed provider profile');\n"
        "   ready=false;postMessage({profile_text:wasmStdout.slice(start).join('\\n')});\n"
        "  }else if(data.command==='thread-state'){\n"
        "   if(!ready)throw new Error('CORE not ready');\n"
        '   postMessage({pthreads:threaded ? Module.PThread.runningWorkers.length : 0});\n'
        '  }\n'
        ' }catch(error){failure(error);}\n'
        '};\n'
        "'''\n"
        '\n'
        'BROWSER_PAGE = r\'\'\'<!doctype html><meta charset="utf-8"><title>VOICEVOX benchmark worker</title>\n'
        '<script>\n'
        'let worker=null, pending=null;\n'
        'window.logs=[];\n'
        'window.request=(data)=>new Promise((resolve,reject)=>{\n'
        " if(pending) return reject(new Error('Concurrent requests are forbidden'));\n"
        " if(data.command==='init'){\n"
        "  worker=new Worker('/worker.js');\n"
        '  worker.onerror=(event)=>{if(pending){clearTimeout(pending.timer);pending.reject(new Error(event.message));pending=null;worker.terminate();}};\n'
        '  worker.onmessage=({data})=>{\n'
        '   if(data.log){logs.push(data.log);return;}\n'
        '   if(!pending)return;\n'
        '   const {resolve,reject,timer}=pending;clearTimeout(timer);pending=null;\n'
        "   if(data.error)reject(new Error(data.error+'\\n'+logs.slice(-20).join('\\n')));\n"
        '   else {if(data.wav)data.wav=Array.from(new Uint8Array(data.wav));if(data.raw)data.raw=Array.from(new Uint8Array(data.raw));resolve(data);}\n'
        '  };\n'
        ' }\n'
        " const timer=setTimeout(()=>{worker.terminate();pending=null;reject(new Error('Worker timed out after 10 minutes'));},600000);\n"
        ' pending={resolve,reject,timer};worker.postMessage(data);\n'
        '});\n'
        "</script>'''\n"
        '\n'
        '\n'
        'def prepared_query(target_seconds: float) -> dict[str, Any]:\n'
        '    """Hand-authored benchmark phonemes, independent of text analysis/models.\n'
        '\n'
        '    Repeated /konnichiwa/ is intentionally a fixed synthetic query, not claimed\n'
        '    to represent a natural 10-second utterance. Measured WAV duration is used\n'
        '    for RTF rather than assuming the requested duration is exact.\n'
        '    """\n'
        '    if not math.isfinite(target_seconds) or not 1 <= target_seconds <= 60:\n'
        '        raise ValueError("--target-seconds must be between 1 and 60")\n'
        '    phrase = {"moras": [\n'
        '        {"text": "コ", "consonant": "k", "consonant_length": .07, "vowel": "o", "vowel_length": .12, "pitch": 5.4},\n'
        '        {"text": "ン", "consonant": None, "consonant_length": None, "vowel": "N", "vowel_length": .12, "pitch": 5.6},\n'
        '        {"text": "ニ", "consonant": "n", "consonant_length": .06, "vowel": "i", "vowel_length": .12, "pitch": 5.6},\n'
        '        {"text": "チ", "consonant": "ch", "consonant_length": .07, "vowel": "i", "vowel_length": .12, "pitch": 5.6},\n'
        '        {"text": "ワ", "consonant": "w", "consonant_length": .06, "vowel": "a", "vowel_length": .14, "pitch": 5.3}],\n'
        '        "accent": 5, "pause_mora": {"text": "、", "consonant": None, "consonant_length": None, "vowel": "pau", "vowel_length": .15, "pitch": 0.0}, "is_interrogative": False}\n'
        '    phrases = [json.loads(json.dumps(phrase)) for _ in range(8)]\n'
        '    total = .2 + sum(m["vowel_length"] + (m["consonant_length"] or 0) for p in phrases for m in p["moras"]) + 8 * .15\n'
        '    return {"accent_phrases": phrases, "speedScale": total / target_seconds, "pitchScale": 0.0, "intonationScale": 1.0, "volumeScale": 1.0, "prePhonemeLength": .1, "postPhonemeLength": .1, "outputSamplingRate": 24000, "outputStereo": False}\n'
        '\n'
        '\n'
        "CORE_BENCH_HELPER = '''        // Untimed diagnostic only: uses the pinned Talk/StreamingTalk domain dispatch\n"
        '        // up to decode(), before PCM conversion/resampling. Not called by synthesis().\n'
        '        #[doc(hidden)]\n'
        '        pub fn benchmark_raw_synthesis(&self, audio_query: &AudioQuery, style_id: StyleId) -> crate::Result<Vec<f32>> {\n'
        '            let audio_query = audio_query.to_validated()?;\n'
        '            let super::DecoderFeature { f0, phoneme } = audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);\n'
        '            self.0.decode(f0.len(), super::PhonemeCode::num_phoneme(), &f0, phoneme.as_flattened(), style_id, ()).block_on()\n'
        '        }\n'
        '\n'
        "'''\n"
        '\n'
        '\n'
        "CORE_FIXED_CONFIG = r'''\n"
        '#[doc(hidden)]\n'
        'pub mod __benchmark_fixed_shape {\n'
        '    use std::sync::{\n'
        '        OnceLock,\n'
        '        atomic::{AtomicUsize, Ordering},\n'
        '    };\n'
        '\n'
        '    pub const VOCODER_SHA256_HEX: &str =\n'
        '        "80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820";\n'
        '    pub(crate) const VOCODER_SHA256: [u8; 32] = [0x80, 0xa8, 0x1f, 0xd0, 0x59, 0x8b, 0x6e, 0x7d, 0x4e, 0x21, 0xfe, 0xf7, 0x4e, 0x03, 0xfe, 0xd1, 0x4e, 0x22, 0xf1, 0x11, 0xc6, 0xb0, 0xf4, 0xc4, 0x45, 0x45, 0x61, 0xba, 0xae, 0x07, 0x58, 0x20];\n'
        '    pub(crate) const VOCODER_BYTES: usize = 55_734_297;\n'
        '\n'
        '    static REQUESTED: OnceLock<Option<usize>> = OnceLock::new();\n'
        '    static VERIFIED_SESSIONS: AtomicUsize = AtomicUsize::new(0);\n'
        '    static XNN_THREADS: OnceLock<usize> = OnceLock::new();\n'
        '    static PROFILE: OnceLock<bool> = OnceLock::new();\n'
        '    static XNN_SESSIONS: AtomicUsize = AtomicUsize::new(0);\n'
        '\n'
        '    pub fn configure(padded_length: Option<usize>, xnn_threads: usize, profile: bool) -> anyhow::Result<()> {\n'
        '        anyhow::ensure!((xnn_threads == 0 && !profile) || padded_length.is_some(), "Provider diagnostics require verified fixed vocoder shape");\n'
        '        if let Some(n) = padded_length {\n'
        '            anyhow::ensure!(n > 0, "Fixed vocoder length must be positive");\n'
        '            let _ = i64::try_from(n)?;\n'
        '        }\n'
        '        REQUESTED\n'
        '            .set(padded_length)\n'
        '            .map_err(|_| anyhow::anyhow!("Benchmark configuration already initialized"))?;\n'
        '        XNN_THREADS.set(xnn_threads).map_err(|_| anyhow::anyhow!("XNNPACK already configured"))?;\n'
        '        PROFILE.set(profile).map_err(|_| anyhow::anyhow!("Profiling already configured"))?;\n'
        '        Ok(())\n'
        '    }\n'
        '\n'
        '    pub fn xnn_threads() -> usize { XNN_THREADS.get().copied().unwrap_or(0) }\n'
        '    pub fn profiling_enabled() -> bool { PROFILE.get().copied().unwrap_or(false) }\n'
        '    pub fn xnn_sessions() -> usize { XNN_SESSIONS.load(Ordering::SeqCst) }\n'
        '    pub(crate) fn record_xnn_session() -> anyhow::Result<()> {\n'
        '        anyhow::ensure!(XNN_SESSIONS.fetch_add(1, Ordering::SeqCst) == 0, "Multiple XNNPACK sessions registered");\n'
        '        Ok(())\n'
        '    }\n'
        '\n'
        '    pub(crate) fn requested_length() -> Option<usize> {\n'
        '        REQUESTED.get().copied().flatten()\n'
        '    }\n'
        '\n'
        '    pub(crate) fn record_verified_session() -> anyhow::Result<()> {\n'
        '        let old = VERIFIED_SESSIONS.fetch_add(1, Ordering::SeqCst);\n'
        '        anyhow::ensure!(old == 0, "More than one fixed-shape vocoder session matched");\n'
        '        Ok(())\n'
        '    }\n'
        '\n'
        '    pub fn verified_sessions() -> usize {\n'
        '        VERIFIED_SESSIONS.load(Ordering::SeqCst)\n'
        '    }\n'
        '\n'
        '    pub fn verify_loaded() -> anyhow::Result<()> {\n'
        '        let requested = REQUESTED\n'
        '            .get()\n'
        '            .ok_or_else(|| anyhow::anyhow!("Benchmark configuration was not initialized"))?;\n'
        '        let expected = usize::from(requested.is_some());\n'
        '        anyhow::ensure!(\n'
        '            verified_sessions() == expected,\n'
        '            "Expected {} verified fixed-shape vocoder session(s), got {}",\n'
        '            expected,\n'
        '            verified_sessions()\n'
        '        );\n'
        '        anyhow::ensure!(xnn_sessions() == usize::from(xnn_threads() > 0), "XNNPACK vocoder registration count mismatch");\n'
        '        Ok(())\n'
        '    }\n'
        '}\n'
        "'''\n"
        '\n'
        "CORE_FIXED_HELPER = r'''\n"
        '        #[doc(hidden)]\n'
        '        pub fn benchmark_vocoder_length(audio_query: &AudioQuery) -> anyhow::Result<usize> {\n'
        '            let audio_query = audio_query.to_validated()?;\n'
        '            let super::DecoderFeature { f0, phoneme } =\n'
        '                audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);\n'
        '            let length = f0.len();\n'
        '            let phoneme_size = super::PhonemeCode::num_phoneme();\n'
        '            anyhow::ensure!(length > 0 && phoneme_size == 45, "Unexpected decoder query shape");\n'
        '            anyhow::ensure!(phoneme.len() == length, "f0/phoneme frame count differs");\n'
        '\n'
        '            // Full-range streaming synthesis passes all intermediate rows,\n'
        '            // including MARGIN frames at each end, to the pinned vocoder.\n'
        '            let n = length.checked_add(2 * super::MARGIN)\n'
        '                .ok_or_else(|| anyhow::anyhow!("Vocoder length overflow"))?;\n'
        '            let _ = i64::try_from(n)?;\n'
        '            Ok(n)\n'
        '        }\n'
        '\n'
        "'''\n"
        '\n'
        "CORE_FIXED_OPTION = r'''\n"
        '        let benchmark_fixed_length =\n'
        '            match (crate::__benchmark_fixed_shape::requested_length(), model) {\n'
        '                (Some(n), ModelBytes::Onnx(bytes))\n'
        '                    if bytes.len() == crate::__benchmark_fixed_shape::VOCODER_BYTES =>\n'
        '                {\n'
        '                    use sha2::{Digest as _, Sha256};\n'
        '                    let digest: [u8; 32] = Sha256::digest(bytes).into();\n'
        '                    (digest == crate::__benchmark_fixed_shape::VOCODER_SHA256).then_some(n)\n'
        '                }\n'
        '                _ => None,\n'
        '            };\n'
        '        if let Some(n) = benchmark_fixed_length {\n'
        '            builder = builder\n'
        '                .with_dimension_override("length", i64::try_from(n)?)\n'
        '                .map_err(ort::Error::<()>::from)?\n'
        '                .with_dimension_override("feats", 80)\n'
        '                .map_err(ort::Error::<()>::from)?;\n'
        '            if let Some(threads) = std::num::NonZeroUsize::new(crate::__benchmark_fixed_shape::xnn_threads()) {\n'
        '                // Direct register calls the real C API and propagates errors;\n'
        "                // the pinned binding's platform hint omits WASM.\n"
        '                ort::ep::XNNPACK::default().with_intra_op_num_threads(threads).register(&mut builder)?;\n'
        '            }\n'
        '            if crate::__benchmark_fixed_shape::profiling_enabled() {\n'
        '                builder = builder.with_profiling("bench-ep").map_err(ort::Error::<()>::from)?;\n'
        '            }\n'
        '        }\n'
        '\n'
        "'''\n"
        '\n'
        "CORE_FIXED_VERIFY = r'''\n"
        '        if let Some(n) = benchmark_fixed_length {\n'
        '            let n = i64::try_from(n)?;\n'
        '            ensure!(sess.inputs().len() == 1, "Unexpected fixed vocoder input count");\n'
        '            for (name, expected_type, expected_shape) in [\n'
        '                ("spec", TensorElementType::Float32, vec![n, 80]),\n'
        '            ] {\n'
        '                let info = sess.inputs().iter()\n'
        '                    .find(|input| input.name() == name)\n'
        '                    .with_context(|| format!("Missing fixed vocoder input {name}"))?;\n'
        '                let ValueType::Tensor { ty, shape, .. } = info.dtype() else {\n'
        '                    bail!("Fixed vocoder input {name} is not a tensor");\n'
        '                };\n'
        '                ensure!(*ty == expected_type, "Unexpected fixed vocoder dtype for {name}");\n'
        '                ensure!(\n'
        '                    &shape[..] == expected_shape.as_slice(),\n'
        '                    "Fixed vocoder shape mismatch for {}: expected {:?}, got {:?}",\n'
        '                    name, expected_shape, shape\n'
        '                );\n'
        '            }\n'
        '            ensure!(\n'
        '                sess.outputs().len() == 1 && sess.outputs()[0].name() == "wave",\n'
        '                "Unexpected fixed vocoder output"\n'
        '            );\n'
        '            let ValueType::Tensor { ty, .. } = sess.outputs()[0].dtype() else {\n'
        '                bail!("Fixed vocoder output is not a tensor");\n'
        '            };\n'
        '            ensure!(*ty == TensorElementType::Float32, "Unexpected fixed vocoder output dtype");\n'
        '            crate::__benchmark_fixed_shape::record_verified_session()?;\n'
        '            if crate::__benchmark_fixed_shape::xnn_threads() > 0 {\n'
        '                crate::__benchmark_fixed_shape::record_xnn_session()?;\n'
        '            }\n'
        '        }\n'
        '\n'
        "'''\n"
        '\n'
        '\n'
        'def patch_fixed_shape(source: Path) -> None:\n'
        '    def insert(path: Path, marker: str, addition: str, token: str) -> None:\n'
        '        content = path.read_text("utf-8")\n'
        '        if token in content:\n'
        '            if addition not in content or content.count(token) != 1:\n'
        '                raise RuntimeError("Cached CORE fixed-shape patch differs from this script")\n'
        '            return\n'
        '        if content.count(marker) != 1:\n'
        '            raise RuntimeError("Pinned CORE does not match a fixed-shape patch marker")\n'
        '        path.write_text(content.replace(marker, addition + marker, 1), encoding="utf-8")\n'
        '    crate = source / "crates/voicevox_core"\n'
        '    manifest = crate / "Cargo.toml"\n'
        '    content = manifest.read_text("utf-8")\n'
        '    if "sha2.workspace = true" not in content:\n'
        '        if content.count("[dependencies]\\n") != 1 or re.search(r"^sha2\\s*[.=]", content, re.M):\n'
        '            raise RuntimeError("Unexpected CORE sha2 dependency declaration")\n'
        '        manifest.write_text(content.replace("[dependencies]\\n", "[dependencies]\\nsha2.workspace = true\\n", 1), encoding="utf-8")\n'
        '    content = manifest.read_text("utf-8")\n'
        '    ort_line = \'ort = { workspace = true, features = ["std", "ndarray", "tracing", "api-17", "alternative-backend"], default-features = false }\'\n'
        '    ort_xnn_line = ort_line.replace(\'"alternative-backend"\', \'"alternative-backend", "xnnpack"\')\n'
        '    if ort_xnn_line not in content:\n'
        '        if content.count(ort_line) != 1:\n'
        '            raise RuntimeError("Pinned CORE ort feature declaration differs")\n'
        '        manifest.write_text(content.replace(ort_line, ort_xnn_line, 1), encoding="utf-8")\n'
        '    library = crate / "src/lib.rs"\n'
        '    content = library.read_text("utf-8")\n'
        '    if "pub mod __benchmark_fixed_shape" not in content:\n'
        '        library.write_text(content + "\\n" + CORE_FIXED_CONFIG, encoding="utf-8")\n'
        '    elif CORE_FIXED_CONFIG not in content:\n'
        '        raise RuntimeError("Cached CORE fixed-shape configuration differs from this script")\n'
        '    insert(crate / "src/synthesizer.rs", "        // Untimed diagnostic only: uses the pinned Talk/StreamingTalk domain dispatch\\n",\n'
        '           CORE_FIXED_HELPER, "pub fn benchmark_vocoder_length")\n'
        '    runtime = crate / "src/core/infer/runtimes/onnxruntime.rs"\n'
        '    insert(runtime, "        let sess = match model {\\n", CORE_FIXED_OPTION, "let benchmark_fixed_length =")\n'
        '    insert(runtime, "        let input_param_infos = sess\\n", CORE_FIXED_VERIFY, "Unexpected fixed vocoder input count")\n'
        '\n'
        '\n'
        'def write_wrapper(source: Path) -> None:\n'
        '    synthesizer = source / "crates/voicevox_core/src/synthesizer.rs"\n'
        '    contents = synthesizer.read_text("utf-8")\n'
        '    if "pub fn benchmark_raw_synthesis" not in contents:\n'
        '        marker = "        /// AudioQueryから直接WAVフォーマットで音声波形を生成する。\\n"\n'
        '        if contents.count(marker) != 1:\n'
        '            raise RuntimeError("Pinned CORE synthesis source does not match the FP32 diagnostic patch")\n'
        '        synthesizer.write_text(contents.replace(marker, CORE_BENCH_HELPER + marker, 1), encoding="utf-8")\n'
        '    elif CORE_BENCH_HELPER not in contents:\n'
        '        raise RuntimeError("Cached CORE FP32 diagnostic patch differs from this script")\n'
        '    patch_fixed_shape(source)\n'
        '    crate = source / "crates/voicevox_benchmark"\n'
        '    (crate / "src").mkdir(parents=True, exist_ok=True)\n'
        '    (crate / "Cargo.toml").write_text(\'\'\'[package]\n'
        'name = "voicevox_benchmark"\n'
        'version = "0.0.0"\n'
        'edition = "2024"\n'
        '[features]\n'
        'native = ["voicevox_core/load-onnxruntime"]\n'
        'browser = ["voicevox_core/link-onnxruntime"]\n'
        'threaded = []\n'
        '[dependencies]\n'
        'anyhow.workspace = true\n'
        'serde_json.workspace = true\n'
        'voicevox_core.workspace = true\n'
        'ort.workspace = true\n'
        '\'\'\', encoding="utf-8")\n'
        '    (crate / "src/main.rs").write_text(RUST_SOURCE, encoding="utf-8")\n'
        '\n'
        '\n'
        'def prepare_model(source: Path, destination: Path) -> None:\n'
        '    import zipfile\n'
        '    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:\n'
        '        for path in sorted((source / "model/sample.vvm").iterdir()):\n'
        '            if path.is_file():\n'
        '                info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))\n'
        '                info.create_system = 3\n'
        '                info.external_attr = 0o100644 << 16\n'
        '                with path.open("rb") as input_file, archive.open(info, "w") as output_file:\n'
        '                    shutil.copyfileobj(input_file, output_file)\n'
        '\n'
        '\n'
        'class NativeRunner:\n'
        '    def __init__(self, executable: Path, runtime: Path, model: Path, query: Path, threads: int, style: int, wav: Path):\n'
        '        self.wav = wav\n'
        '        self.process = subprocess.Popen([str(executable), str(runtime), str(model), str(query), str(threads), str(style), str(wav)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)\n'
        '        import queue\n'
        '        import threading\n'
        '        self._lines: queue.Queue[str | None] = queue.Queue()\n'
        '        def read_lines() -> None:\n'
        '            for line in self.process.stdout:\n'
        '                self._lines.put(line)\n'
        '            self._lines.put(None)\n'
        '        self._reader = threading.Thread(target=read_lines, daemon=True)\n'
        '        self._reader.start()\n'
        '        try:\n'
        '            message = self._read()\n'
        '            if not message.get("ready") or message.get("threads") != threads:\n'
        '                raise RuntimeError("Native initialization did not confirm its thread setting")\n'
        '        except Exception:\n'
        '            self.close()\n'
        '            raise\n'
        '        self.duration = 0.0\n'
        '        self.bytes = 0\n'
        '\n'
        '    def _read(self) -> dict[str, Any]:\n'
        '        import queue\n'
        '        try:\n'
        '            line = self._lines.get(timeout=600)\n'
        '        except queue.Empty:\n'
        '            self.process.kill()\n'
        '            self.process.wait()\n'
        '            raise RuntimeError("Native runner timed out after 10 minutes") from None\n'
        '        if line is None:\n'
        '            raise RuntimeError(f"Native runner exited unexpectedly ({self.process.poll()})")\n'
        '        return json.loads(line)\n'
        '\n'
        '    def synthesize(self, save: bool = False) -> tuple[float, float]:\n'
        '        self.process.stdin.write("synthesize-save\\n" if save else "synthesize\\n")\n'
        '        self.process.stdin.flush()\n'
        '        message = self._read()\n'
        '        if save:\n'
        '            self.duration = wav_duration(self.wav.read_bytes())\n'
        '            self.bytes = message["wav_bytes"]\n'
        '        if not self.duration or message["wav_bytes"] != self.bytes:\n'
        '            raise RuntimeError("Native WAV length changed between trials")\n'
        '        return message["elapsed_s"], self.duration\n'
        '\n'
        '    def raw_wave(self) -> bytes:\n'
        '        self.process.stdin.write("raw-save\\n")\n'
        '        self.process.stdin.flush()\n'
        '        message = self._read()\n'
        '        data = self.wav.with_suffix(".f32").read_bytes()\n'
        '        if len(data) != message["raw_bytes"]:\n'
        '            raise RuntimeError("Native FP32 length mismatch")\n'
        '        return data\n'
        '\n'
        '    def close(self) -> None:\n'
        '        if self.process.poll() is None:\n'
        '            try:\n'
        '                self.process.stdin.write("quit\\n")\n'
        '                self.process.stdin.flush()\n'
        '                self.process.wait(timeout=10)\n'
        '            except (OSError, subprocess.TimeoutExpired):\n'
        '                self.process.kill()\n'
        '                self.process.wait()\n'
        '        self._reader.join(timeout=2)\n'
        '        self.process.stdin.close()\n'
        '        self.process.stdout.close()\n'
        '\n'
        '\n'
        'class BrowserRunner:\n'
        '    def __init__(self, browser: Any, url: str, module: str, threads: int, threaded: bool, style: int, wav: Path,\n'
        '                 *, fixed_shape: bool = False, spin_off: bool = False, xnn_threads: int = 0, profile: bool = False, model_url: str | None = None, model_target: str = "vocoder"):\n'
        '        self.page = browser.new_page()\n'
        '        self.page.set_default_timeout(600_000)\n'
        '        self.page.goto(url, wait_until="load")\n'
        '        message = self.page.evaluate("data => request(data)", {"command": "init", "module": module, "threads": threads, "threaded": threaded, "fixed_shape": fixed_shape, "spin_off": spin_off, "xnn_threads": xnn_threads, "profile": profile, "model_url": model_url})\n'
        '        if not message.get("ready") or message.get("threads") != threads or message.get("shared_memory") != threaded or message.get("spin_off") != spin_off:\n'
        '            self.page.close()\n'
        '            raise RuntimeError("Browser did not confirm the required thread/shared-memory configuration")\n'
        '        if message.get("model_target") != model_target:\n'
        '            raise RuntimeError("Compiled CORE target differs from manifest")\n'
        '        self.model_target = model_target\n'
        '        self.vocoder_fixed_matches = message["vocoder_fixed_matches"]\n'
        '        self.vocoder_xnn_sessions = message["vocoder_xnn_sessions"]\n'
        '        if model_target == "predictor" and (self.vocoder_fixed_matches or self.vocoder_xnn_sessions):\n'
        '            raise RuntimeError("Predictor-only variant changed vocoder registration")\n'
        '        self.style, self.wav, self.duration, self.bytes = style, wav, 0.0, 0\n'
        '        self.shared_memory = message["shared_memory"]\n'
        '        self.pthreads_created = message.get("pthreads_created", 0)\n'
        '        self.spin_off = message["spin_off"]\n'
        '        self.xnn_threads, self.xnn_sessions = message["xnn_threads"], message["xnn_sessions"]\n'
        '        if self.xnn_threads != xnn_threads or self.xnn_sessions != int(xnn_threads > 0):\n'
        '            self.page.close()\n'
        '            raise RuntimeError("XNNPACK was not registered for exactly the requested vocoder session")\n'
        '        self.fixed_length, self.fixed_matches = message["fixed_length"], message["fixed_matches"]\n'
        '        if (fixed_shape and (self.fixed_length <= 0 or self.fixed_matches != 1)) or (not fixed_shape and (self.fixed_length != 0 or self.fixed_matches != 0)):\n'
        '            self.page.close()\n'
        '            raise RuntimeError("Fixed vocoder shape was not applied to exactly the requested session")\n'
        '\n'
        '    def synthesize(self, save: bool = False) -> tuple[float, float]:\n'
        '        message = self.page.evaluate("data => request(data)", {"command": "synthesize", "style": self.style, "save": save})\n'
        '        if save:\n'
        '            data = bytes(message["wav"])\n'
        '            atomic_write(self.wav, data)\n'
        '            self.duration = wav_duration(data)\n'
        '            self.bytes = len(data)\n'
        '        if not self.duration or message["wav_bytes"] != self.bytes:\n'
        '            raise RuntimeError("Browser WAV length changed between trials")\n'
        '        return message["elapsed_s"], self.duration\n'
        '\n'
        '    def close(self) -> None:\n'
        '        self.page.close()\n'
        '\n'
        '    def raw_wave(self) -> bytes:\n'
        '        message = self.page.evaluate("data => request(data)", {"command": "raw", "style": self.style})\n'
        '        return bytes(message["raw"])\n'
        '\n'
        '    def thread_state(self) -> int:\n'
        '        return int(self.page.evaluate("() => request({command:\'thread-state\'})")["pthreads"])\n'
        '\n'
        '    def finish_profile(self, profile_path: Path | None = None) -> dict[str, Any]:\n'
        '        message = self.page.evaluate("() => request({command:\'finish-profile\'})")\n'
        '        text = message["profile_text"]\n'
        '        if text.count("BENCH_PROFILE_BEGIN") != 1 or text.count("BENCH_PROFILE_END") != 1:\n'
        '            raise RuntimeError("Provider profile stdout markers are missing or ambiguous")\n'
        '        events = json.loads(text.split("BENCH_PROFILE_BEGIN", 1)[1].split("BENCH_PROFILE_END", 1)[0].strip())\n'
        '        if profile_path:\n'
        '            atomic_write(profile_path, json.dumps(events, ensure_ascii=False, indent=2).encode())\n'
        '        providers: dict[str, set[str]] = {}\n'
        '        assignments: set[tuple[str, str, str]] = set()\n'
        '        for event in events:\n'
        '            if event.get("cat") == "Node" and event.get("name", "").endswith("_kernel_time") and event.get("args", {}).get("provider"):\n'
        '                providers.setdefault(event["args"]["provider"], set()).add(event["name"])\n'
        '                assignments.add((event["args"]["provider"], event["name"], event["args"].get("op_name", "")))\n'
        '        return {"verified": bool(providers.get("XnnpackExecutionProvider")),\n'
        '                "provider_kernel_counts": {key: len(nodes) for key, nodes in providers.items()},\n'
        '                "provider_assignment_sha256": hashlib.sha256(json.dumps(sorted(assignments), separators=(",", ":")).encode()).hexdigest(),\n'
        '                "profile_events": len(events), "profiling_scope": "separate untimed diagnostic browser only"}\n'
        '\n'
        '\n'
        'def browser_engine_info(browser: Any) -> dict[str, Any]:\n'
        '    session = browser.new_browser_cdp_session()\n'
        '    try:\n'
        '        version = session.send("Browser.getVersion")\n'
        '        return {"product": version["product"], "js_version": version["jsVersion"]}\n'
        '    finally:\n'
        '        session.detach()\n'
        '\n'
        '\n'
        'def revectorization_diagnostic_child(config_path: Path) -> None:\n'
        '    from playwright.sync_api import sync_playwright\n'
        '    config = json.loads(config_path.read_text("utf-8"))\n'
        '    with sync_playwright() as playwright:\n'
        '        options = dict(config["options"])\n'
        '        options["args"] = ["--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if config["enable"] else "--js-flags=--no-wasm-revectorize,--trace-wasm-revectorize"]\n'
        '        browser = playwright.chromium.launch(**options)\n'
        '        runner = None\n'
        '        try:\n'
        '            info = browser_engine_info(browser)\n'
        '            info["requested_js_flags"] = options["args"]\n'
        '            xnnpack = config.get("xnnpack", False)\n'
        '            runner = BrowserRunner(browser, config["url"], config.get("module", "/xnnpack/voicevox_benchmark.js" if xnnpack else "/mt/voicevox_benchmark.js"), config["threads"], True,\n'
        '                                   config["style"], Path(config["wav"]), fixed_shape=config.get("fixed_shape", xnnpack), spin_off=config.get("spin_off", xnnpack),\n'
        '                                   xnn_threads=config.get("xnn_threads", config["threads"] if xnnpack else 0), profile=config.get("profile", xnnpack),\n'
        '                                   model_url=config.get("model_url"), model_target=config.get("model_target", "vocoder"))\n'
        '            for _ in range(3):\n'
        '                runner.synthesize(save=True)\n'
        '            if xnnpack:\n'
        '                pthreads = runner.thread_state()\n'
        '                if pthreads != config["threads"] - 1:\n'
        '                    raise RuntimeError("Combined diagnostic has unexpected thread-pool ownership")\n'
        '                info["xnnpack_profile"] = {**runner.finish_profile(Path(config["profile_result"])),\n'
        '                                           "fixed_length": runner.fixed_length, "fixed_matches": runner.fixed_matches,\n'
        '                                           "pthreads": pthreads, "xnn_threads": runner.xnn_threads, "xnn_sessions": runner.xnn_sessions}\n'
        '            atomic_write(Path(config["result"]), json.dumps(info).encode())\n'
        '        finally:\n'
        '            if runner:\n'
        '                runner.close()\n'
        '            browser.close()\n'
        '\n'
        '\n'
        'def verify_revectorization(url: str, options: dict[str, Any], threads: int, style: int, work: Path,\n'
        '                          trace_dir: Path | None = None, *, xnnpack: bool = False) -> dict[str, Any]:\n'
        '    records = {}\n'
        '    prefix = "combined" if xnnpack else "revec"\n'
        '    for enable, key in ((False, "control"), (True, "candidate")):\n'
        '        progress(f"Checking {prefix} {key} vector transformations in a separate untimed browser")\n'
        '        config_path, result_path = work / f"{prefix}-{key}-config.json", work / f"{prefix}-{key}-result.json"\n'
        '        atomic_write(config_path, json.dumps({"options": options, "url": url, "threads": threads, "style": style, "enable": enable,\n'
        '                                            "xnnpack": xnnpack, "profile_result": str((trace_dir or work) / f"{key}-provider-profile.json"),\n'
        '                                            "wav": str(work / f"{prefix}-{key}.wav"), "result": str(result_path)}).encode())\n'
        '        env = dict(os.environ)\n'
        '        env["DEBUG"] = "pw:browser"\n'
        '        try:\n'
        '            output = checked_run([sys.executable, str(Path(__file__).resolve()), "--diagnostic-config", str(config_path)], env=env)\n'
        '        except RuntimeError as error:\n'
        '            if trace_dir:\n'
        '                atomic_write(trace_dir / f"{key}-failed-tail.log", str(error).encode())\n'
        '            raise\n'
        '        if trace_dir:\n'
        '            atomic_write(trace_dir / f"{key}.log", output.encode())\n'
        '        nodes = [int(value) for value in re.findall(r"Decide(?:d)? to vectorize, ([1-9][0-9]*) revectorizable nodes", output)]\n'
        '        rejected = bool(re.search(r"(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|Error:.*(?:wasm-revectorize|trace-wasm-revectorize)", output, re.I))\n'
        '        info = json.loads(result_path.read_text("utf-8"))\n'
        '        expected = "--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if enable else "--js-flags=--no-wasm-revectorize,--trace-wasm-revectorize"\n'
        '        records[key] = {**info, "transformed_groups": len(nodes), "revectorizable_nodes": sum(nodes),\n'
        '                        "flag_rejected": rejected, "launch_configuration_matches": info["requested_js_flags"] == [expected]}\n'
        '    control, candidate = records["control"], records["candidate"]\n'
        '    verified = (control["transformed_groups"] == 0 and candidate["transformed_groups"] > 0\n'
        '                and all(value["launch_configuration_matches"] and not value["flag_rejected"] for value in records.values())\n'
        '                and (control["product"], control["js_version"]) == (candidate["product"], candidate["js_version"]))\n'
        '    if xnnpack:\n'
        '        a, b = control["xnnpack_profile"], candidate["xnnpack_profile"]\n'
        '        verified = verified and a["verified"] and b["verified"] and all(\n'
        '            a[field] == b[field] for field in ("provider_assignment_sha256", "fixed_length", "fixed_matches", "pthreads", "xnn_threads", "xnn_sessions"))\n'
        '    return {**records, "verified": verified,\n'
        '            "evidence": "no transformations in trace-only control; positive nonempty V8 optimizer transformations with revectorization; actual CORE WASM, three untimed syntheses each; flags record supplied launch configuration, activation is established by traces"}\n'
        '\n'
        '\n'
        'def verify_xnnpack(playwright: Any, url: str, options: dict[str, Any], threads: int, style: int, work: Path,\n'
        '                   profile_path: Path | None = None) -> dict[str, Any]:\n'
        '    progress("Checking XNNPACK kernel execution in a separate untimed profiled browser")\n'
        '    browser = playwright.chromium.launch(**options)\n'
        '    runner = None\n'
        '    try:\n'
        '        runner = BrowserRunner(browser, url, "/xnnpack/voicevox_benchmark.js", threads, True, style,\n'
        '                               work / "xnnpack-diagnostic.wav", fixed_shape=True, spin_off=True, xnn_threads=threads, profile=True)\n'
        '        runner.synthesize(save=True)\n'
        '        pthreads = runner.thread_state()\n'
        '        if pthreads != threads - 1:\n'
        '            raise RuntimeError("XNNPACK process created an unexpected number of inference pthreads")\n'
        '        return {**runner.finish_profile(profile_path), "pthreads_after_warmup": pthreads, "fixed_length": runner.fixed_length, "fixed_matches": runner.fixed_matches,\n'
        '                "xnn_threads": runner.xnn_threads, "xnn_sessions": runner.xnn_sessions}\n'
        '    finally:\n'
        '        if runner:\n'
        '            runner.close()\n'
        '        browser.close()\n'
        '\n'
        '\n'
        '@contextlib.contextmanager\n'
        'def serve_assets(folder: Path) -> Iterator[str]:\n'
        '    import functools\n'
        '    import http.server\n'
        '    import threading\n'
        '    class Handler(http.server.SimpleHTTPRequestHandler):\n'
        '        def end_headers(self) -> None:\n'
        '            self.send_header("Cross-Origin-Opener-Policy", "same-origin")\n'
        '            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")\n'
        '            self.send_header("Cache-Control", "no-store")\n'
        '            super().end_headers()\n'
        '        def log_message(self, *_: Any) -> None:\n'
        '            pass\n'
        '    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(folder)))\n'
        '    thread = threading.Thread(target=server.serve_forever, daemon=True)\n'
        '    thread.start()\n'
        '    try:\n'
        '        yield f"http://127.0.0.1:{server.server_port}"\n'
        '    finally:\n'
        '        server.shutdown()\n'
        '        server.server_close()\n'
        '        thread.join()\n'
        '\n'
        'RUST_VERSION = "1.96.0"\n'
        'EMSDK_VERSION = "4.0.8"\n'
        'EMSDK_COMMIT = "419021fa040428bc69ef1559b325addb8e10211f"\n'
        'BASE_RELEASE = "https://github.com/yamachu/onnxruntime-builder/releases/download/onnxruntime-1.23.2"\n'
        "ORT_ARCHIVE_SHA256 = {'onnxruntime-linux-arm64-1.23.2.tgz': '121888dc9d8c6267f6373df150eed9cd2da5dfd4e277d99b22e092533790f61a', 'onnxruntime-linux-x64-1.23.2.tgz': '2e147a06354a4b75362d4a26e6c55b126d05e25dc19c609c1733aedb7156e8a2', 'onnxruntime-osx-arm64-1.23.2.tgz': 'a80514d3ecf04f8c7e8e8c2d0f1dd603bee0c383e84c1ca01e40bf7807c3d0ce', 'onnxruntime-osx-x86_64-1.23.2.tgz': '0c6489e161ea803e52e8153bdba4ea1541252c171776925f0f230091e1a5a949', 'onnxruntime-wasm-static-1.23.2.tgz': '2dc2c5073337b0e77e08314ca9936f5b9a355ff4e237c16294b969e7f2db6593', 'onnxruntime-win-arm64-1.23.2.tgz': '119b1e2fefde9b139c8a43d3e7ef1817b7ff8d551209916d5f1f8528dd451829', 'onnxruntime-win-x64-1.23.2.tgz': '9db1a87c4502e435319d24602ebd280c98d3d62f7c2b31f16e34de2143732bde'}\n"
        'THREADED_ORT_URL = "https://github.com/Hiroshiba/onnxruntime-builder/releases/download/onnxruntime-wasm-static-simd-threaded-1.23.2/onnxruntime-wasm-static-simd-threaded-1.23.2.tgz"\n'
        'XNNPACK_ORT_URL = "https://github.com/Hiroshiba/onnxruntime-builder/releases/download/onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2/onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2.tgz"\n'
        'ORT_ARCHIVE_SHA256["onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2.tgz"] = "76182743b15a31e0d361432d8db297a78e3da64aad356730fe93576993af5a2a"\n'
        'ORT_ARCHIVE_SHA256["onnxruntime-wasm-static-simd-threaded-1.23.2.tgz"] = "bdc024237b8303feb24c237bc7c8c07fdabd12a2bb7049684ed2894c580a63d1"\n'
        '\n'
        '\n'
        'def unpack_cached(archive: Path, destination: Path) -> Path:\n'
        '    import tarfile\n'
        '    receipt = destination / ".complete.json"\n'
        '    digest = sha256_file(archive)\n'
        '    if receipt.is_file():\n'
        '        try:\n'
        '            if json.loads(receipt.read_text())["sha256"] == digest:\n'
        '                return destination\n'
        '        except (OSError, ValueError, KeyError):\n'
        '            pass\n'
        '    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=destination.parent))\n'
        '    try:\n'
        '        with tarfile.open(archive) as tar:\n'
        '            tar.extractall(temporary, filter="data")\n'
        '        atomic_write(temporary / ".complete.json", json.dumps({"sha256": digest}).encode())\n'
        '        if destination.exists():\n'
        '            shutil.rmtree(destination)\n'
        '        os.replace(temporary, destination)\n'
        '        return destination\n'
        '    finally:\n'
        '        if temporary.exists():\n'
        '            shutil.rmtree(temporary)\n'
        '\n'
        '\n'
        'def find_one(folder: Path, name: str) -> Path:\n'
        '    matches = [p for p in folder.rglob(name) if p.is_file()]\n'
        '    if len(matches) != 1:\n'
        '        raise RuntimeError(f"Expected one {name}; found {len(matches)}")\n'
        '    return matches[0].resolve()\n'
        '\n'
        '\n'
        'def native_platform() -> tuple[str, str]:\n'
        '    machine = platform.machine().lower()\n'
        '    arm = machine in {"aarch64", "arm64"}\n'
        '    x64 = machine in {"x86_64", "amd64"}\n'
        '    if not (arm or x64):\n'
        '        raise RuntimeError(f"Unsupported architecture: {machine}; use 64-bit x86 or ARM")\n'
        '    if sys.platform == "win32":\n'
        '        return f"win-{\'arm64\' if arm else \'x64\'}", f"{\'aarch64\' if arm else \'x86_64\'}-pc-windows-msvc"\n'
        '    if sys.platform == "darwin":\n'
        '        return f"osx-{\'arm64\' if arm else \'x86_64\'}", f"{\'aarch64\' if arm else \'x86_64\'}-apple-darwin"\n'
        '    if sys.platform.startswith("linux"):\n'
        '        return f"linux-{\'arm64\' if arm else \'x64\'}", f"{\'aarch64\' if arm else \'x86_64\'}-unknown-linux-gnu"\n'
        '    raise RuntimeError(f"Unsupported operating system: {sys.platform}")\n'
        '\n'
        '\n'
        'def ensure_toolchains(root: Path) -> dict[str, str]:\n'
        '    _, triple = native_platform()\n'
        '    if sys.platform == "win32" and not shutil.which("cl.exe"):\n'
        '        raise RuntimeError("Windows requires Visual Studio C++ Build Tools and an x64/ARM64 Native Tools command prompt. Install/activate those tools, then rerun the same uv command.")\n'
        '    if sys.platform != "win32" and not (shutil.which("cc") and shutil.which("c++")):\n'
        '        raise RuntimeError("A native C/C++ compiler is required. Install Xcode Command Line Tools on macOS, or your distribution\'s C/C++ build tools on Linux, then rerun.")\n'
        '    import clang.cindex\n'
        '    env = dict(os.environ)\n'
        '    env["RUSTUP_HOME"] = str(root / "rustup")\n'
        '    env["CARGO_HOME"] = str(root / "cargo")\n'
        '    env["LIBCLANG_PATH"] = str(Path(clang.cindex.__file__).parent / "native")\n'
        '    cargo_bin = root / "cargo/bin"\n'
        '    env["PATH"] = str(cargo_bin) + os.pathsep + env["PATH"]\n'
        '    suffix = ".exe" if sys.platform == "win32" else ""\n'
        '    rustup = cargo_bin / ("rustup" + suffix)\n'
        '    if not rustup.is_file():\n'
        '        installer = cached_download(f"https://static.rust-lang.org/rustup/dist/{triple}/rustup-init{suffix}", root, "rustup-init" + suffix)\n'
        '        if sys.platform != "win32":\n'
        '            installer.chmod(0o755)\n'
        '        checked_run([str(installer), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", RUST_VERSION, "--target", "wasm32-unknown-emscripten"], env=env)\n'
        '    # rustup reuses installed components and repairs an interrupted installation.\n'
        '    checked_run([str(rustup), "toolchain", "install", RUST_VERSION, "--profile", "minimal", "--target", "wasm32-unknown-emscripten", "--component", "rust-src"], env=env)\n'
        '    emsdk_dir = root / f"emsdk-{EMSDK_VERSION}"\n'
        '    archive = cached_download(f"https://github.com/emscripten-core/emsdk/archive/{EMSDK_COMMIT}.tar.gz", root, f"emsdk-{EMSDK_VERSION}.tar.gz")\n'
        '    unpack_cached(archive, emsdk_dir)\n'
        '    emsdk = find_one(emsdk_dir, "emsdk.py")\n'
        '    installed = emsdk.parent / "upstream/emscripten"\n'
        '    if not (installed / "emcc.py").is_file():\n'
        '        checked_run([sys.executable, str(emsdk), "install", EMSDK_VERSION], env=env)\n'
        '    checked_run([sys.executable, str(emsdk), "activate", EMSDK_VERSION], env=env)\n'
        "    # Use the activated SDK's explicit paths without evaluating a shell script.\n"
        '    node = find_one(emsdk.parent / "node", "node.exe" if sys.platform == "win32" else "node")\n'
        '    env["EMSDK"] = str(emsdk.parent)\n'
        '    env["EM_CONFIG"] = str(emsdk.parent / ".emscripten")\n'
        '    env["EMSDK_NODE"] = str(node)\n'
        '    env["EMSDK_PYTHON"] = sys.executable\n'
        '    env["PATH"] = os.pathsep.join(map(str, [emsdk.parent, installed, node.parent])) + os.pathsep + env["PATH"]\n'
        '    return env\n'
        '\n'
        '\n'
        'def build_runner(source: Path, runtime: Path, root: Path, env: dict[str, str], *, threaded: bool | None, threads: int,\n'
        '                 optimization: str = "z", graph_level: int = 1, xnnpack: bool = False) -> Path:\n'
        '    if optimization not in ("z", "3") or graph_level not in (1, 3):\n'
        '        raise ValueError("Unsupported benchmark optimization variant")\n'
        '    write_wrapper(source)\n'
        '    kind = "native" if threaded is None else ("browser-mt" if threaded else "browser-st")\n'
        '    if optimization != "z":\n'
        '        kind += "-o3"\n'
        '    if graph_level != 1:\n'
        '        kind += "-graph3"\n'
        '    if xnnpack:\n'
        '        kind += "-xnnpack"\n'
        '    pool = \'Module["benchmarkPoolSize"]\' if threaded else 0\n'
        '    import inspect\n'
        '    build_adapter = inspect.getsource(build_runner) + inspect.getsource(write_wrapper) + inspect.getsource(patch_fixed_shape)\n'
        '    identity = {"build_adapter_sha256": hashlib.sha256(build_adapter.encode()).hexdigest(), "core": CORE_COMMIT, "rust": RUST_VERSION, "emscripten": EMSDK_VERSION,\n'
        '                "runtime": sha256_file(runtime), "wrapper": hashlib.sha256(RUST_SOURCE.encode()).hexdigest(),\n'
        '                "core_diagnostic_patch": hashlib.sha256(CORE_BENCH_HELPER.encode()).hexdigest(),\n'
        '                "core_fixed_shape_patch": hashlib.sha256((CORE_FIXED_CONFIG + CORE_FIXED_HELPER + CORE_FIXED_OPTION + CORE_FIXED_VERIFY + "sha2.workspace = true; ort/xnnpack").encode()).hexdigest(),\n'
        '                "kind": kind, "optimization": optimization, "graph_level": graph_level, "xnnpack": xnnpack,\n'
        '                "pool": pool, "rebuild_std": bool(threaded), "platform": native_platform()[1]}\n'
        '    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]\n'
        '    folder = root / "builds" / key\n'
        '    folder.mkdir(parents=True, exist_ok=True)\n'
        '    suffix = ".exe" if sys.platform == "win32" else ""\n'
        '    binary = folder / ("voicevox_benchmark" + (suffix if threaded is None else ".js"))\n'
        '    receipt = folder / "complete.json"\n'
        '    if binary.is_file() and receipt.is_file():\n'
        '        info = json.loads(receipt.read_text())\n'
        '        if info.get("identity") == identity and all((folder / name).is_file() and sha256_file(folder / name) == digest for name, digest in info["files"].items()):\n'
        '            print(f"Cached build: {kind}", flush=True)\n'
        '            return binary\n'
        '    build_env = dict(env)\n'
        "    # A caller's pre-existing optimization flags must not silently change this\n"
        '    # experiment or reuse a binary built under a different precision policy.\n'
        '    for name in list(build_env):\n'
        '        if (name.startswith("CARGO_PROFILE_") or name in {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "EMCC_CFLAGS", "EMMAKEN_CFLAGS", "CARGO_BUILD_TARGET"}\n'
        '                or name.startswith("CARGO_") and name.endswith("_RUSTFLAGS")\n'
        '                or re.search(r"(^|_)(CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS)(_|$)", name)):\n'
        '            build_env.pop(name)\n'
        '    build_env["CARGO_PROFILE_C_API_OPT_LEVEL"] = optimization\n'
        '    build_env["CARGO_TARGET_DIR"] = str(root / "cargo-target" / kind)\n'
        '    cargo = str(root / "cargo/bin" / ("cargo.exe" if sys.platform == "win32" else "cargo"))\n'
        '    command = [cargo, f"+{RUST_VERSION}", "build", "--manifest-path", str(source / "Cargo.toml"), "-p", "voicevox_benchmark", "--profile", "c-api"]\n'
        '    if threaded is None:\n'
        '        command += ["--features", "native"]\n'
        '        output_folder = Path(build_env["CARGO_TARGET_DIR"]) / "c-api"\n'
        '    else:\n'
        '        command += ["--features", "browser,threaded" if threaded else "browser", "--target", "wasm32-unknown-emscripten"]\n'
        '        features = "+simd128,+atomics,+bulk-memory,+mutable-globals" if threaded else "+simd128"\n'
        '        flags = ["-C", f"target-feature={features}", "-C", "link-arg=-msimd128", "-C", "link-arg=-fwasm-exceptions", "-C", "link-arg=-fno-fast-math", "-C", "link-arg=-ffp-contract=off", "-C", "link-arg=-sALLOW_MEMORY_GROWTH=1",\n'
        '                 "-C", "link-arg=-sINITIAL_MEMORY=1073741824", "-C", "link-arg=-sSTACK_SIZE=8388608",\n'
        '                 "-C", "link-arg=-sEXPORTED_RUNTIME_METHODS=FS,HEAPU8" + (",PThread" if threaded else ""),\n'
        '                 "-C", "link-arg=-sEXPORTED_FUNCTIONS=_main,_bench_init,_bench_synthesize,_bench_wav_ptr,_bench_wav_len,_bench_raw,_bench_raw_ptr,_bench_raw_len,_bench_spin_off,_bench_fixed_length,_bench_fixed_matches,_bench_xnn_threads,_bench_xnn_sessions,_bench_finish_profile",\n'
        '                 "-L", f"native={runtime.parent}"]\n'
        '        if optimization == "3":\n'
        '            flags += ["-C", "link-arg=-O3"]\n'
        '        if threaded:\n'
        '            # The distributed wasm std has no atomics. Rebuild the matching 1.96\n'
        '            # sources instead of mixing in a different nightly compiler.\n'
        '            command += ["-Z", "build-std=std,panic_abort"]\n'
        '            build_env["RUSTC_BOOTSTRAP"] = "1"\n'
        '            flags += ["-C", "link-arg=-pthread", "-C", f"link-arg=-sPTHREAD_POOL_SIZE={pool}"]\n'
        '            build_env["CFLAGS_wasm32_unknown_emscripten"] = "-pthread -msimd128"\n'
        '            build_env["CXXFLAGS_wasm32_unknown_emscripten"] = "-pthread -msimd128"\n'
        '        build_env["CARGO_ENCODED_RUSTFLAGS"] = "\\x1f".join(flags)\n'
        '        build_env["CARGO_TARGET_WASM32_UNKNOWN_EMSCRIPTEN_LINKER"] = "emcc.bat" if sys.platform == "win32" else "emcc"\n'
        '        output_folder = Path(build_env["CARGO_TARGET_DIR"]) / "wasm32-unknown-emscripten/c-api"\n'
        '    progress(f"Building {kind}: CORE opt-level={optimization}, ORT graph Level{graph_level} (first run may take several minutes)")\n'
        '    graph_source = source / "crates/voicevox_core/src/core/infer/runtimes/onnxruntime.rs"\n'
        '    original = graph_source.read_text("utf-8")\n'
        '    pattern = ".with_optimization_level(GraphOptimizationLevel::Level1)"\n'
        '    if original.count(pattern) != 1:\n'
        '        raise RuntimeError("Pinned CORE graph optimization source does not match Level1")\n'
        '    try:\n'
        '        if graph_level == 3:\n'
        '            graph_source.write_text(original.replace(pattern, ".with_optimization_level(GraphOptimizationLevel::Level3)"), encoding="utf-8")\n'
        '        checked_run(command, env=build_env)\n'
        '    finally:\n'
        '        if graph_level == 3:\n'
        '            graph_source.write_text(original, encoding="utf-8")\n'
        '    generated = [output_folder / binary.name]\n'
        '    if threaded is not None:\n'
        '        generated += list(output_folder.glob("voicevox_benchmark.wasm")) + list(output_folder.glob("voicevox_benchmark.worker.js"))\n'
        '        if not any(path.suffix == ".wasm" for path in generated):\n'
        '            raise RuntimeError("Build did not produce WASM")\n'
        '    hashes = {}\n'
        '    for path in generated:\n'
        '        shutil.copy2(path, folder / path.name)\n'
        '        hashes[path.name] = sha256_file(folder / path.name)\n'
        '    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())\n'
        '    return binary\n'
        '\n'
        '\n'
        'def verify_threaded_runtime(folder: Path, *, xnnpack: bool = False) -> dict[str, Any]:\n'
        '    info_path = find_one(folder, "BUILD_INFO.json")\n'
        '    info = json.loads(info_path.read_text("utf-8"))\n'
        '    expected = {"schema_version": 1, "library": "onnxruntime", "version": ORT_VERSION,\n'
        '                "source_commit": "a83fc4d58cb48eb68890dd689f94f28288cf2278",\n'
        '                "emscripten_version": EMSDK_VERSION, "target": "wasm32-unknown-emscripten",\n'
        '                "simd": True, "pthreads": True, "signed": False, "exception_abi": "wasm", "thread_pool_scope": "global"}\n'
        '    if xnnpack:\n'
        '        expected.update({"xnnpack": True, "relaxed_simd": False, "fast_math": False, "fp_contract": "off",\n'
        '                         "thread_pool_scope": "global_ort_plus_per_session_xnnpack", "pthreadpool_backend": "pthreads",\n'
        '                         "pthreadpool_execution_smoke_passed": True})\n'
        '    if any(info.get(key) != value for key, value in expected.items()):\n'
        '        raise RuntimeError("The threaded archive does not match the required generic ORT/SIMD/pthread build")\n'
        '    if not info.get("smoke_test", {}).get("passed"):\n'
        '        raise RuntimeError("The threaded runtime\'s build smoke test did not pass")\n'
        '    if xnnpack and not all(info["smoke_test"].get(key) for key in ("profile_verified", "numerical_reference_verified")):\n'
        '        raise RuntimeError("XNNPACK build lacks verified provider activity and numerical smoke checks")\n'
        '    runtime = find_one(folder, "libonnxruntime_webassembly.a")\n'
        '    sums = (info_path.parent / "SHA256SUMS").read_text("utf-8")\n'
        '    entry = next((line.split()[0] for line in sums.splitlines() if line.split() and line.split()[-1].lstrip("*") == "lib/libonnxruntime_webassembly.a"), None)\n'
        '    if entry != sha256_file(runtime):\n'
        '        raise RuntimeError("Threaded runtime library checksum mismatch")\n'
        '    return info\n'
        '\n'
        '\n'
        'def verify_archive_sidecar(archive: Path) -> str:\n'
        '    sidecar = archive.with_name(archive.name + ".sha256")\n'
        '    entries = [line.split() for line in sidecar.read_text("utf-8").splitlines() if line.strip()]\n'
        '    match = next((parts[0] for parts in entries if len(parts) == 2 and Path(parts[1].lstrip("*")).name == archive.name), None)\n'
        '    if not match or not re.fullmatch("[a-fA-F0-9]{64}", match) or sha256_file(archive) != match.lower():\n'
        '        raise RuntimeError("Threaded archive checksum mismatch")\n'
        '    return match.lower()\n'
        '\n'
        '\n'
        'def save_measurements_and_report(result: dict[str, Any], output: Path) -> None:\n'
        '    """Persist observations before validation or rendering can reject them."""\n'
        '    raw_path = output.with_suffix(".json")\n'
        '    atomic_write(raw_path, json.dumps(result, ensure_ascii=False, indent=2).encode())\n'
        '    progress(f"Raw measurements saved: {raw_path.resolve()}")\n'
        '    try:\n'
        '        validate_results(result)\n'
        '        render_report(result, output)\n'
        '    except Exception as error:\n'
        '        raise RuntimeError(f"Report generation failed; raw measurements remain at {raw_path.resolve()}: {error}") from error\n'
        '\n'
        '\n'
        'def run_benchmark(args: argparse.Namespace) -> None:\n'
        '    from playwright.sync_api import sync_playwright\n'
        '    import psutil\n'
        '    progress("Preparing benchmark inputs and cached tools")\n'
        '    if sum(map(bool, (args.experiments, args.baseline_only, args.backend_experiments))) > 1:\n'
        '        raise ValueError("Choose only one of --experiments, --baseline-only, or --backend-experiments")\n'
        '    use_xnnpack = args.backend_experiments == "all"\n'
        '    if use_xnnpack and args.style_id != 302:\n'
        '        raise ValueError("XNNPACK experiments target sample.vvm streaming Style302; use --style-id 302")\n'
        '    if args.experiments and args.experimental_threads > logical_cpu_count():\n'
        '        progress(f"Warning: experimental threads={args.experimental_threads} exceeds available logical CPUs={logical_cpu_count()}")\n'
        '    if not 0 <= args.style_id <= 2**32 - 1:\n'
        '        raise ValueError("--style-id must be an unsigned 32-bit integer")\n'
        '    query = json.loads(args.audio_query.read_text("utf-8")) if args.audio_query else prepared_query(args.target_seconds)\n'
        '    if not isinstance(query, dict) or not isinstance(query.get("outputSamplingRate"), int) or query["outputSamplingRate"] <= 0:\n'
        '        raise ValueError("AudioQuery must contain a positive integer outputSamplingRate")\n'
        '    query_bytes = json.dumps(query, ensure_ascii=False, separators=(",", ":")).encode("utf-8")\n'
        '    root = args.cache_dir.resolve()\n'
        '    root.mkdir(parents=True, exist_ok=True)\n'
        '    threaded_url = args.threaded_ort_url or THREADED_ORT_URL\n'
        '    xnnpack_url = args.xnnpack_ort_url or XNNPACK_ORT_URL\n'
        '    if use_xnnpack and not xnnpack_url and not args.xnnpack_ort_archive:\n'
        '        raise RuntimeError("The XNNPACK runtime is not configured until its validated release is available; no XNNPACK measurement was made")\n'
        '    if not args.baseline_only and not threaded_url and not args.threaded_ort_archive:\n'
        '        raise RuntimeError("The unsigned multithreaded ORT release is not yet configured. Supply its verified archive URL using --threaded-ort-url; single-thread results are not a substitute for this mode.")\n'
        '    begin = time.monotonic()\n'
        '    progress(f"Preparing Rust {RUST_VERSION} and Emscripten {EMSDK_VERSION}")\n'
        '    env = ensure_toolchains(root)\n'
        '    progress("Preparing CORE source and ONNX Runtime archives")\n'
        '    core_archive = cached_download(f"https://github.com/yamachu/voicevox_core/archive/{CORE_COMMIT}.tar.gz", root, f"core-{CORE_COMMIT}.tar.gz")\n'
        '    source_patch_key = hashlib.sha256((CORE_BENCH_HELPER + CORE_FIXED_CONFIG + CORE_FIXED_HELPER + CORE_FIXED_OPTION + CORE_FIXED_VERIFY).encode()).hexdigest()[:16]\n'
        '    core_tree = unpack_cached(core_archive, root / f"source-{CORE_COMMIT}-{source_patch_key}")\n'
        '    source = next(path.parent for path in core_tree.glob("*/Cargo.toml"))\n'
        '    platform_name, _ = native_platform()\n'
        '    runtime_archives = {}\n'
        '    urls = {} if args.backend_experiments == "v8" or args.prepare_only else {\n'
        '        "native": f"{BASE_RELEASE}/onnxruntime-{platform_name}-{ORT_VERSION}.tgz",\n'
        '        "browser": f"{BASE_RELEASE}/onnxruntime-wasm-static-{ORT_VERSION}.tgz",\n'
        '    }\n'
        '    if not args.baseline_only:\n'
        '        urls["browser_mt"] = threaded_url\n'
        '    if use_xnnpack:\n'
        '        urls["browser_xnnpack"] = xnnpack_url or "local-xnnpack-archive"\n'
        '    for name, url in urls.items():\n'
        '        local_archive = args.threaded_ort_archive if name == "browser_mt" else (args.xnnpack_ort_archive if name == "browser_xnnpack" else None)\n'
        '        if local_archive:\n'
        '            archive = local_archive.resolve()\n'
        '            verify_archive_sidecar(archive)\n'
        '        else:\n'
        '            from urllib.parse import urlparse\n'
        '            filename = Path(urlparse(url).path).name\n'
        '            archive = cached_download(url, root, filename, expected_sha256=ORT_ARCHIVE_SHA256.get(filename))\n'
        '        (root / "runtimes").mkdir(exist_ok=True)\n'
        '        extracted = unpack_cached(archive, root / "runtimes" / (name + "-" + sha256_file(archive)[:16]))\n'
        '        runtime_archives[name] = (archive, extracted)\n'
        '    library_name = "onnxruntime.dll" if sys.platform == "win32" else (f"libonnxruntime.{ORT_VERSION}.dylib" if sys.platform == "darwin" else f"libonnxruntime.so.{ORT_VERSION}")\n'
        '    threaded_info = verify_threaded_runtime(runtime_archives["browser_mt"][1]) if not args.baseline_only else None\n'
        '    xnnpack_info = verify_threaded_runtime(runtime_archives["browser_xnnpack"][1], xnnpack=True) if use_xnnpack else None\n'
        '    native_runtime = find_one(runtime_archives["native"][1], library_name) if "native" in runtime_archives else None\n'
        '    st_runtime = find_one(runtime_archives["browser"][1], "libonnxruntime_webassembly.a") if "browser" in runtime_archives else None\n'
        '    mt_runtime = find_one(runtime_archives["browser_mt"][1], "libonnxruntime_webassembly.a") if not args.baseline_only else None\n'
        '    native_binary = build_runner(source, native_runtime, root, env, threaded=None, threads=args.threads) if native_runtime else None\n'
        '    browser_st = build_runner(source, st_runtime, root, env, threaded=False, threads=1) if st_runtime else None\n'
        '    browser_mt = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads) if mt_runtime else None\n'
        '    browser_binaries = {"st": browser_st} if browser_st else {}\n'
        '    if browser_mt:\n'
        '        browser_binaries["mt"] = browser_mt\n'
        '    if use_xnnpack:\n'
        '        xnn_runtime = find_one(runtime_archives["browser_xnnpack"][1], "libonnxruntime_webassembly.a")\n'
        '        browser_binaries["xnnpack"] = build_runner(source, xnn_runtime, root, env, threaded=True, threads=args.threads, xnnpack=True)\n'
        '    if args.experiments:\n'
        '        browser_binaries["o3"] = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads, optimization="3")\n'
        '        browser_binaries["graph3"] = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads, graph_level=3)\n'
        '    model = root / f"sample-v1-{CORE_COMMIT}.vvm"\n'
        '    if not model.exists():\n'
        '        descriptor, temporary_name = tempfile.mkstemp(prefix=".sample-", dir=root)\n'
        '        os.close(descriptor)\n'
        '        temporary = Path(temporary_name)\n'
        '        try:\n'
        '            prepare_model(source, temporary)\n'
        '            os.replace(temporary, model)\n'
        '        finally:\n'
        '            temporary.unlink(missing_ok=True)\n'
        '    if args.prepare_only:\n'
        '        info = {"binaries": {key: str(binary) for key, binary in browser_binaries.items()}, "model": str(model),\n'
        '                "model_sha256": sha256_file(model), "source": str(source),\n'
        '                "runtimes": {key: str(folder) for key, (_, folder) in runtime_archives.items()},\n'
        '                "query_sha256": hashlib.sha256(query_bytes).hexdigest()}\n'
        '        atomic_write(root / "research-assets.json", json.dumps(info, indent=2).encode())\n'
        '        progress("Verified browser assets prepared; no performance measurements made")\n'
        '        return\n'
        '    modes = [] if args.backend_experiments == "v8" else [Mode("native", f"Native ×{args.threads}", args.threads, "native CPU, per-session thread pools"),\n'
        '             Mode("browser", "Browser ×1", 1, "WebAssembly SIMD, unshared memory")]\n'
        '    if not args.baseline_only:\n'
        '        modes.append(Mode("browser_mt", f"Browser pthreads ×{args.threads}", args.threads, "WebAssembly SIMD + pthreads, global thread pool"))\n'
        '    if args.experiments:\n'
        '        modes.extend([\n'
        '            Mode("browser_mt_threads", f"実験 MT ×{args.experimental_threads}", args.experimental_threads, "experimental: thread count only; global pool", experimental=True),\n'
        '            Mode("browser_mt_o3", f"実験 MT ×{args.threads} O3", args.threads, "experimental: CORE opt-level=3 + final Emscripten -O3; existing LTO retained", core_optimization="3", experimental=True),\n'
        '            Mode("browser_mt_graph3", f"実験 MT ×{args.threads} Graph L3", args.threads, "experimental: ORT GraphOptimizationLevel::Level3 (ORT_ENABLE_LAYOUT)", graph_optimization=3, experimental=True),\n'
        '        ])\n'
        '    if use_xnnpack:\n'
        '        modes.extend([\n'
        '            Mode("browser_mt_fixed", f"CPU ×{args.threads} fixed shape", args.threads, "matching CPU control for XNNPACK: query-derived vocoder-only fixed length", fixed_shape=True),\n'
        '            Mode("browser_xnnpack", f"実験 XNNPACK ×{args.threads}", args.threads, "XNNPACK vocoder-only pool; ORT global fallback intra=1/inter=1, spin disabled; compare with fixed-shape CPU control", experimental=True, fixed_shape=True, spin_off=True, execution_provider="XNNPACK"),\n'
        '        ])\n'
        '    if args.backend_experiments:\n'
        '        modes.append(Mode("browser_revectorize", f"実験 V8 revectorize ×{args.threads}", args.threads, "same dynamic-shape CPU WASM as MT control; only --js-flags=--wasm-revectorize", experimental=True, revectorize=True))\n'
        '    if use_xnnpack:\n'
        '        modes.append(Mode("browser_xnnpack_revectorize", f"実験 XNNPACK + V8 ×{args.threads}", args.threads,\n'
        '                          "same fixed-shape XNNPACK WASM and pools; add only --js-flags=--wasm-revectorize", experimental=True,\n'
        '                          fixed_shape=True, spin_off=True, execution_provider="XNNPACK", revectorize=True))\n'
        '    log: list[dict[str, Any]] = [{"event": "assets_ready", "seconds": round(time.monotonic() - begin, 3)}]\n'
        '    environment = environment_info()\n'
        '    environment["requested_backend_experiments"] = args.backend_experiments or "none"\n'
        '    environment.update({"cpu_metric": "sum target-tree user+system CPU seconds between action boundary snapshots / action snapshot seconds; excludes pre/post padding; 100%=one logical CPU", "cpu_sample_interval_ms": CPU_SAMPLE_INTERVAL_S * 1000,\n'
        '                        "cpu_padding_seconds": CPU_PADDING_S,\n'
        '                        "cpu_trace_clock": "actual snapshot-completion times relative to synthesis request start; synchronized pre/action/post boundary snapshots; no internal operator-phase attribution",\n'
        '                        "cpu_plot_aggregation": "common 0.25s request-relative pre/action bins and separate response-end-relative post bins; overlap-weighted rates per phase-complete trial, then median/inclusive quartiles; no zero padding",\n'
        '                        "cpu_browser_scope": "separate Chromium instance per mode; root and descendants; Python/Playwright Node excluded", "psutil": psutil.__version__})\n'
        '    environment.update({"rust": checked_run([str(root / "cargo/bin" / ("rustc.exe" if sys.platform == "win32" else "rustc")), f"+{RUST_VERSION}", "--version"], env=env),\n'
        '                        "emscripten": EMSDK_VERSION, "sample_vvm_sha256": sha256_file(model),\n'
        '                        "audio_query_sha256": hashlib.sha256(query_bytes).hexdigest(),\n'
        '                        "browser_thread_support": "single-threaded baseline" if args.baseline_only else "shared WASM memory + global ORT pool verified at initialization"})\n'
        '    if threaded_info:\n'
        '        environment.update({"threaded_builder_commit": threaded_info["builder_commit"],\n'
        '                            "threaded_validation_commit": threaded_info["validation_commit"],\n'
        '                            "threaded_validation_run_id": threaded_info["validation_run_id"],\n'
        '                            "threaded_rust_std": "Rust 1.96.0 rebuilt with atomics (build-std)",\n'
        '                            "threaded_pthread_pool_size": args.threads,\n'
        '                            "browser_mt_thread_pool": "global; intra-op=requested threads, inter-op=1"})\n'
        '    for name, (archive, _) in runtime_archives.items():\n'
        '        environment[f"{name}_ort_archive_sha256"] = sha256_file(archive)\n'
        '    environment["core_variants"] = {mode.key: {"opt_level": mode.core_optimization, "graph_level": mode.graph_optimization,\n'
        '                                               "threads": mode.threads, "experimental": mode.experimental,\n'
        '                                               "fixed_shape": mode.fixed_shape, "global_spin_off": mode.spin_off,\n'
        '                                               "execution_provider": mode.execution_provider, "revectorize": mode.revectorize} for mode in modes}\n'
        '    if xnnpack_info:\n'
        '        environment["xnnpack_builder_commit"] = xnnpack_info["builder_commit"]\n'
        '        environment["xnnpack_thread_ownership"] = f"XNNPACK intra={args.threads}; ORT global intra=1/inter=1; only one SHA-matched vocoder session registers XNNPACK"\n'
        '    environment["wasm_bytes"] = {key: binary.with_suffix(".wasm").stat().st_size for key, binary in browser_binaries.items()}\n'
        '    environment["precision_flags"] = "FP32 model; unchanged strict FP/SIMD flags; no fast-math, relaxed SIMD, FP16 or quantization"\n'
        '    environment["block_order"] = [block.mode for block in make_schedule(modes, args.seed, balanced=args.backend_experiments)]\n'
        '    # Browser cache is scoped to this script, including on a second run.\n'
        '    previous_browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")\n'
        '    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "playwright")\n'
        '    try:\n'
        '        if not args.browser_path:\n'
        '            progress("Preparing cached Chromium")\n'
        '            checked_run([sys.executable, "-m", "playwright", "install", "chromium"], env=dict(os.environ))\n'
        '        with tempfile.TemporaryDirectory(prefix="voicevox-benchmark-") as temporary:\n'
        '            work = Path(temporary)\n'
        '            # Only explicit benchmark assets are reachable from the loopback server.\n'
        '            shutil.copyfile(model, work / "sample.vvm")\n'
        '            atomic_write(work / "query.json", query_bytes)\n'
        '            atomic_write(work / "index.html", BROWSER_PAGE.encode())\n'
        '            atomic_write(work / "worker.js", BROWSER_WORKER.encode())\n'
        '            for mode_key, binary in browser_binaries.items():\n'
        '                (work / mode_key).mkdir()\n'
        '                for path in binary.parent.glob("voicevox_benchmark.*"):\n'
        '                    if path.suffix in {".js", ".wasm"}:\n'
        '                        shutil.copy2(path, work / mode_key / path.name)\n'
        '            with serve_assets(work) as url, sync_playwright() as playwright:\n'
        '                options: dict[str, Any] = {"headless": not args.headed}\n'
        '                if args.browser_path:\n'
        '                    options["executable_path"] = str(args.browser_path)\n'
        '                environment["browser_headless"] = not args.headed\n'
        '                skipped: dict[str, str] = {}\n'
        '                if args.backend_experiments:\n'
        '                    environment["xnnpack_diagnostic"] = {"requested": use_xnnpack, "verified": False}\n'
        '                    environment["combined_diagnostic"] = {"requested": use_xnnpack, "verified": False}\n'
        '                    if use_xnnpack:\n'
        '                        try:\n'
        '                            environment["xnnpack_diagnostic"] = verify_xnnpack(playwright, url, options, args.threads, args.style_id, work,\n'
        '                                                                            args.output.parent / "xnnpack-diagnostic-profile.json")\n'
        '                        except Exception as error:\n'
        '                            reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")\n'
        '                            environment["xnnpack_diagnostic"] = {"verified": False, "error": reason[-1500:]}\n'
        '                    try:\n'
        '                        environment["revectorization_diagnostic"] = verify_revectorization(url, options, args.threads, args.style_id, work,\n'
        '                                                                                          args.output.parent / "v8-diagnostic-traces")\n'
        '                    except Exception as error:\n'
        '                        reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")\n'
        '                        environment["revectorization_diagnostic"] = {"verified": False, "error": reason[-1500:]}\n'
        '                    if use_xnnpack and environment["xnnpack_diagnostic"]["verified"]:\n'
        '                        try:\n'
        '                            environment["combined_diagnostic"] = verify_revectorization(url, options, args.threads, args.style_id, work,\n'
        '                                                                                       args.output.parent / "combined-diagnostic-traces", xnnpack=True)\n'
        '                        except Exception as error:\n'
        '                            reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")\n'
        '                            environment["combined_diagnostic"] = {"verified": False, "error": reason[-1500:]}\n'
        '                    if use_xnnpack and not environment["xnnpack_diagnostic"]["verified"]:\n'
        '                        skipped["browser_xnnpack"] = "XNNPACKは実行プロファイルで対象カーネルを確認できず、未測定。"\n'
        '                        modes = [mode for mode in modes if mode.key not in {"browser_xnnpack", "browser_mt_fixed", "browser_xnnpack_revectorize"}]\n'
        '                    if not environment["revectorization_diagnostic"]["verified"]:\n'
        '                        skipped["browser_revectorize"] = "V8再ベクトル化は基準との差を示す変換ログを確認できず、未測定。"\n'
        '                        modes = [mode for mode in modes if mode.key != "browser_revectorize"]\n'
        '                    if use_xnnpack and not environment["combined_diagnostic"]["verified"]:\n'
        '                        skipped["browser_xnnpack_revectorize"] = "XNNPACKとV8の併用は同じ演算割当と実際のV8変換を確認できず、未測定。"\n'
        '                        modes = [mode for mode in modes if mode.key != "browser_xnnpack_revectorize"]\n'
        '                    environment["skipped_candidates"] = skipped\n'
        '                    requested = environment["core_variants"]\n'
        '                    measured_keys = {mode.key for mode in modes}\n'
        '                    if skipped:\n'
        '                        environment["requested_but_unmeasured"] = {key: value for key, value in requested.items() if key not in measured_keys}\n'
        '                    environment["core_variants"] = {key: value for key, value in requested.items() if key in measured_keys}\n'
        '                    atomic_write(args.output.with_suffix(".diagnostics.json"), json.dumps({key: environment[key] for key in ("xnnpack_diagnostic", "revectorization_diagnostic", "combined_diagnostic", "skipped_candidates")}, ensure_ascii=False, indent=2).encode())\n'
        '                    if len(modes) < 2:\n'
        '                        raise RuntimeError("Neither requested backend passed activation diagnostics; diagnostics saved, no purported optimized timings measured")\n'
        '                    environment["block_order"] = [block.mode for block in make_schedule(modes, args.seed, balanced=True)]\n'
        '                    progress("Balanced block order: " + " → ".join(environment["block_order"]))\n'
        '                runners: dict[str, Any] = {}\n'
        '                browsers: dict[str, Any] = {}\n'
        '                cpu_roots: dict[str, int] = {}\n'
        '                try:\n'
        '                    if native_binary:\n'
        '                        progress("Initializing native CORE")\n'
        '                        started = time.monotonic()\n'
        '                        runners["native"] = NativeRunner(native_binary, native_runtime, model, work / "query.json", args.threads, args.style_id, work / "native.wav")\n'
        '                        cpu_roots["native"] = runners["native"].process.pid\n'
        '                        log.append({"event": "initialized", "mode": "native", "seconds": round(time.monotonic() - started, 3)})\n'
        '                    browser_modes = [("browser", "/st/voicevox_benchmark.js", 1, False)] if browser_st else []\n'
        '                    if browser_mt:\n'
        '                        browser_modes.append(("browser_mt", "/mt/voicevox_benchmark.js", args.threads, True))\n'
        '                    if args.experiments:\n'
        '                        browser_modes.extend([\n'
        '                            ("browser_mt_threads", "/mt/voicevox_benchmark.js", args.experimental_threads, True),\n'
        '                            ("browser_mt_o3", "/o3/voicevox_benchmark.js", args.threads, True),\n'
        '                            ("browser_mt_graph3", "/graph3/voicevox_benchmark.js", args.threads, True),\n'
        '                        ])\n'
        '                    if args.backend_experiments:\n'
        '                        browser_modes.extend([\n'
        '                            ("browser_mt_fixed", "/mt/voicevox_benchmark.js", args.threads, True),\n'
        '                            ("browser_xnnpack", "/xnnpack/voicevox_benchmark.js", args.threads, True),\n'
        '                            ("browser_revectorize", "/mt/voicevox_benchmark.js", args.threads, True),\n'
        '                            ("browser_xnnpack_revectorize", "/xnnpack/voicevox_benchmark.js", args.threads, True),\n'
        '                        ])\n'
        '                        browser_modes = [row for row in browser_modes if any(mode.key == row[0] for mode in modes)]\n'
        '                    for key, module, threads, threaded in browser_modes:\n'
        '                        progress(f"Initializing isolated {key} Chromium and CORE")\n'
        '                        started = time.monotonic()\n'
        '                        configured = next(mode for mode in modes if mode.key == key)\n'
        '                        launch_options = dict(options)\n'
        '                        if configured.revectorize:\n'
        '                            launch_options["args"] = ["--js-flags=--wasm-revectorize"]\n'
        '                        browser = playwright.chromium.launch(**launch_options)\n'
        '                        browsers[key] = browser\n'
        '                        cpu_roots[key] = chromium_process_id(browser)\n'
        '                        if "browser" in environment and environment["browser"] != browser.version:\n'
        '                            raise RuntimeError("Browser versions differ between modes")\n'
        '                        environment["browser"] = browser.version\n'
        '                        runners[key] = BrowserRunner(browser, url, module, threads, threaded, args.style_id, work / f"{key}.wav",\n'
        '                                                     fixed_shape=configured.fixed_shape, spin_off=configured.spin_off,\n'
        '                                                     xnn_threads=threads if configured.execution_provider == "XNNPACK" else 0)\n'
        '                        if configured.revectorize:\n'
        '                            info = browser_engine_info(browser)\n'
        '                            info["requested_js_flags"] = launch_options["args"]\n'
        '                            if info["requested_js_flags"] != ["--js-flags=--wasm-revectorize"]:\n'
        '                                raise RuntimeError("Timed launch must use only the revectorization flag without trace flags")\n'
        '                            combined = configured.execution_provider == "XNNPACK"\n'
        '                            checked_engine = environment["combined_diagnostic" if combined else "revectorization_diagnostic"]["candidate"]\n'
        '                            if (info["product"], info["js_version"]) != (checked_engine["product"], checked_engine["js_version"]):\n'
        '                                raise RuntimeError("Timed V8 version differs from its activation diagnostic")\n'
        '                            environment["timed_combined_engine" if combined else "timed_revectorization_engine"] = info\n'
        '                        environment[f"{key}_pthreads_created"] = runners[key].pthreads_created\n'
        '                        environment[f"{key}_global_spin_off"] = runners[key].spin_off\n'
        '                        if configured.execution_provider == "XNNPACK":\n'
        '                            environment[f"{key}_xnn_threads"] = runners[key].xnn_threads\n'
        '                            environment[f"{key}_xnn_sessions"] = runners[key].xnn_sessions\n'
        '                            environment[f"{key}_ort_global_threads"] = 1\n'
        '                        if configured.fixed_shape:\n'
        '                            environment[f"{key}_fixed_length"] = runners[key].fixed_length\n'
        '                            environment[f"{key}_fixed_matches"] = runners[key].fixed_matches\n'
        '                            environment[f"{key}_vocoder_sha256"] = VOCODER_MODEL_SHA256\n'
        '                            environment[f"{key}_verified_input_shapes"] = {"spec": [runners[key].fixed_length, 80]}\n'
        '                        log.append({"event": "initialized", "mode": key, "seconds": round(time.monotonic() - started, 3)})\n'
        '                    if "browser_mt" in runners:\n'
        '                        environment["browser_mt_pthreads_created"] = runners["browser_mt"].pthreads_created\n'
        '                    environment.update(runners["browser_mt" if args.backend_experiments else "browser"].page.evaluate("() => ({browser_hardware_concurrency:navigator.hardwareConcurrency,cross_origin_isolated:crossOriginIsolated,user_agent:navigator.userAgent})"))\n'
        '                    for mode in modes:\n'
        '                        progress(f"Warmup: {mode.label} (excluded from latency and CPU measurements)")\n'
        '                        elapsed, duration = runners[mode.key].synthesize(save=True)\n'
        '                        if isinstance(runners[mode.key], BrowserRunner):\n'
        '                            pthreads = runners[mode.key].thread_state()\n'
        '                            environment[f"{mode.key}_pthreads_after_warmup"] = pthreads\n'
        '                            if mode.execution_provider == "XNNPACK" and pthreads != mode.threads - 1:\n'
        '                                raise RuntimeError("Timed XNNPACK process has unexpected thread-pool ownership")\n'
        '                        if mode.fixed_shape:\n'
        '                            environment[f"{mode.key}_warmup_with_ort_shape_validation"] = True\n'
        '                        log.append({"event": "warmup_excluded", "mode": mode.key, "seconds": elapsed, "audio_s": duration})\n'
        '                    durations = [runner.duration for runner in runners.values()]\n'
        '                    if max(durations) - min(durations) > 1 / query["outputSamplingRate"]:\n'
        '                        raise RuntimeError("Native/browser output lengths differ; refusing a misleading comparison")\n'
        '                    output_checks = verify_outputs(runners, "browser_mt" if browser_mt else "browser", log)\n'
        '                    spectrograms = output_checks.pop("spectrograms")\n'
        '                    progress(f"Starting {len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK} serial trials with target-process CPU sampling")\n'
        '                    def measured_synthesis(mode: Mode) -> tuple[float, float, CpuMeasurement]:\n'
        '                        sampler = ProcessCpuSampler(cpu_roots[mode.key], mode.label)\n'
        '                        return sampler.measure(runners[mode.key].synthesize)\n'
        '                    trials = run_schedule(modes, args.seed, measured_synthesis, log, balanced=args.backend_experiments)\n'
        '                    result = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),\n'
        '                              "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials],\n'
        '                              "environment": environment, "audio_s": durations[0], "style_id": args.style_id, "seed": args.seed, "log": log,\n'
        '                              "output_checks": output_checks,\n'
        '                              "spectrograms": spectrograms,\n'
        '                              "schedule_method": "seeded position-balanced and pair-order-balanced design; randomized labels and round order" if args.backend_experiments else "seeded independent shuffle in each round",\n'
        '                              "schedule": [asdict(block) for block in make_schedule(modes, args.seed, balanced=args.backend_experiments)],\n'
        '                              "notes": ["出力検証は測定外。同じ実行のブラウザMT基準とPCM・PCM化前FP32を照合。非一致を精度維持とは判定しない。" if browser_mt else "出力検証は測定外。ブラウザST基準とPCM・PCM化前FP32を照合。"]}\n'
        '                    if args.baseline_only:\n'
        '                        result["notes"].append("この先行検証はnativeとブラウザ単一スレッドのみ。マルチスレッドは未実施。")\n'
        '                    if args.backend_experiments:\n'
        '                        if use_xnnpack:\n'
        '                            result["notes"].append("XNNPACKの速度は同じ固定shapeのCPU対照と比較。")\n'
        '                            result["notes"].append("併用の追加効果は同じXNNPACK単独と、全体の効果は固定shapeのCPU対照と比較。FP32形式の維持と数値の完全一致は別々に確認。")\n'
        '                        else:\n'
        '                            result["notes"].append("この実行はV8候補のみ。XNNPACKは測定対象外。")\n'
        '                        result["notes"].append("V8再ベクトル化は同じ通常MT WASMとの比較。診断用プロファイル・トレースは時間測定とは別のブラウザで実行。")\n'
        '                        result["notes"].extend(skipped.values())\n'
        '                    save_measurements_and_report(result, args.output)\n'
        '                    print(f"Report: {args.output.resolve()}")\n'
        '                finally:\n'
        '                    for runner in runners.values():\n'
        '                        with contextlib.suppress(Exception):\n'
        '                            runner.close()\n'
        '                    for browser in browsers.values():\n'
        '                        with contextlib.suppress(Exception):\n'
        '                            browser.close()\n'
        '    finally:\n'
        '        if previous_browser_path is None:\n'
        '            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)\n'
        '        else:\n'
        '            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous_browser_path\n'
        '\n'
        '\n'
        '\n'
        'def positive_int(value: str) -> int:\n'
        '    number = int(value)\n'
        '    if not 1 <= number <= 65535:\n'
        '        raise argparse.ArgumentTypeError("must be between 1 and 65535")\n'
        '    return number\n'
        '\n'
        '\n'
        'def argument_parser() -> argparse.ArgumentParser:\n'
        '    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)\n'
        '    parser.add_argument("--threads", type=positive_int, default=default_threads(), help="native + multithreaded-browser inference threads; default: max(1, available logical CPUs // 2)")\n'
        '    parser.add_argument("--seed", type=int, default=20261007, help="reproducible block order")\n'
        '    parser.add_argument("--audio-query", type=Path, help="optional prepared AudioQuery JSON; default: built-in approximately 10-second query")\n'
        '    parser.add_argument("--style-id", type=int, default=302)\n'
        '    parser.add_argument("--target-seconds", type=float, default=10.0)\n'
        '    parser.add_argument("--output", type=Path, default=Path("voicevox-benchmark.html"))\n'
        '    parser.add_argument("--cache-dir", type=Path, default=cache_root())\n'
        '    parser.add_argument("--report-from", type=Path, help="regenerate HTML from a completed measurement JSON")\n'
        '    parser.add_argument("--audio-dir", type=Path, help="with --report-from: make images from local hash-matched WAVs; audio is not embedded")\n'
        '    parser.add_argument("--spectrogram-dir", type=Path, help="with --report-from: load PNG-only spectrograms and their manifest")\n'
        '    parser.add_argument("--baseline-only", action="store_true", help="explicitly measure only native and browser single-thread (MT is not measured)")\n'
        '    parser.add_argument("--experiments", action="store_true", help="add isolated MT thread-count, CORE O3, and ORT graph-Level3 candidates (90 trials total)")\n'
        '    parser.add_argument("--backend-experiments", nargs="?", const="all", choices=("all", "v8"), help="all: MT/fixed-shape controls, XNNPACK and V8; v8: only MT control and V8; balanced blocks")\n'
        '    parser.add_argument("--experimental-threads", type=positive_int, default=4, help="thread-count candidate used with --experiments; default: 4")\n'
        '    parser.add_argument("--threaded-ort-archive", type=Path, help="local verified MT archive with adjacent .sha256 sidecar")\n'
        '    parser.add_argument("--threaded-ort-url", help="verified unsigned threaded ORT archive from the fork release")\n'
        '    parser.add_argument("--xnnpack-ort-archive", type=Path, help="local verified XNNPACK archive with adjacent .sha256 sidecar")\n'
        '    parser.add_argument("--xnnpack-ort-url", help="verified strict-FP XNNPACK runtime archive from the fork release")\n'
        '    parser.add_argument("--browser-path", type=Path, help="optional installed Chromium executable")\n'
        '    parser.add_argument("--headed", action="store_true", help="show the otherwise identical browser runner")\n'
        '    parser.add_argument("--prepare-only", action="store_true", help="build checked browser CORE assets and save private paths without measurements")\n'
        '    parser.add_argument("--research-screen", action="store_true", help="research only: preliminary 1 trial per block, 3 blocks; JSON only, no speedup claim")\n'
        '    parser.add_argument("--research-manifest", type=Path, help="same-host candidate manifest using cached validated CORE builds")\n'
        '    parser.add_argument("--measurement-gate", type=Path, help="wait until this file exists before research timing")\n'
        '    parser.add_argument("--self-test", action="store_true", help="check scheduling, validation and reporting without synthesizing")\n'
        '    parser.add_argument("--diagnostic-config", type=Path, help=argparse.SUPPRESS)\n'
        '    return parser\n'
        '\n'
        '\n'
        'def self_test() -> None:\n'
        '    progress("Self-test fixtures only; no real synthesis or CPU measurements")\n'
        '    modes = [Mode("native", "Native", 2, "native CPU"), Mode("browser", "Browser", 1, "WebAssembly SIMD")]\n'
        '    a = make_schedule(modes, 42)\n'
        '    assert a == make_schedule(modes, 42)\n'
        '    assert len(a) == 6 and all(sum(block.mode == mode.key for block in a) == 3 for mode in modes)\n'
        '    assert max(1, logical_cpu_count() // 2) == default_threads()\n'
        '    for count in (2, 3, 4, 5, 6, 7):\n'
        '        candidates = [Mode(f"mode{i}", str(i), 2, "test") for i in range(count)]\n'
        '        for seed in range(100):\n'
        '            blocks = make_schedule(candidates, seed, balanced=True)\n'
        '            rounds = [[block.mode for block in blocks[start:start + count]] for start in range(0, len(blocks), count)]\n'
        '            for mode in candidates:\n'
        '                positions = [row.index(mode.key) for row in rounds]\n'
        '                counts = [positions.count(index) for index in range(count)]\n'
        '                assert max(counts) - min(counts) <= 1\n'
        '            for first in candidates:\n'
        '                for second in candidates:\n'
        '                    if first.key != second.key:\n'
        '                        assert {row.index(first.key) < row.index(second.key) for row in rounds} == {False, True}\n'
        '    # Explicit test fixture, confined to a temporary directory and never delivered\n'
        '    # as benchmark measurements.\n'
        '    log: list[dict[str, Any]] = []\n'
        '    def fixture(mode: Mode) -> tuple[float, float, CpuMeasurement]:\n'
        '        trace = (CpuInterval(-1.001, -0.001, 0.0, 0.0, 1, False, "pre"),\n'
        '                 CpuInterval(-0.001, 0.119, 0.06 * mode.threads, 50.0 * mode.threads, 1, False),\n'
        '                 CpuInterval(0.119, 0.199, 0.04 * mode.threads, 50.0 * mode.threads, 1, False),\n'
        '                 CpuInterval(0.199, 1.199, 0.0, 0.0, 1, False, "post"))\n'
        '        return 0.1 * mode.threads, 10.0, CpuMeasurement(0.1 * mode.threads, 0.2, 0.5 * mode.threads,\n'
        '                                                     50.0 * mode.threads, 5, 1, False, trace)\n'
        '    trials = run_schedule(modes, 42, fixture, log)\n'
        '    result = {"schema_version": SCHEMA_VERSION, "created_at": "SELF-TEST FIXTURE", "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials], "environment": {"test": "<>&"}, "audio_s": 10.0, "style_id": 302, "seed": 42, "log": log}\n'
        '    validate_results(result)\n'
        '    assert len(list(csv.DictReader(io.StringIO(trial_csv(result["trials"]))))) == 30\n'
        '    assert len(list(csv.DictReader(io.StringIO(cpu_trace_csv(result["trials"]))))) == 120\n'
        '    with tempfile.TemporaryDirectory() as folder:\n'
        '        report = Path(folder) / "test.html"\n'
        '        render_report(result, report)\n'
        '        content = report.read_text()\n'
        "        assert '<details><summary>生データ（CSV）</summary>' in content\n"
        "        assert '<details open' not in content\n"
        "        assert '<summary>CPU 時系列（CSV）</summary>' in content\n"
        '        assert \'class="cpu-grid"\' in content\n'
        "        assert '&lt;&gt;&amp;' in content\n"
        "        assert 'https://' not in content  # Standalone report requires no CDN.\n"
        '        first = Path(folder) / "atomic.txt"\n'
        '        atomic_write(first, b"first")\n'
        '        atomic_write(first, b"second")\n'
        '        assert first.read_bytes() == b"second"\n'
        '    corrupt = json.loads(json.dumps(result))\n'
        '    corrupt["trials"][0]["rtf"] = 999.0\n'
        '    try:\n'
        '        validate_results(corrupt)\n'
        '    except ValueError:\n'
        '        pass\n'
        '    else:\n'
        '        raise AssertionError("Inconsistent RTF accepted")\n'
        '    print("Self-test passed. No benchmark measurements were made.")\n'
        '\n'
        '\n'
        'def verify_research_revectorization(entry: dict[str, Any], mode: Mode, options: dict[str, Any], url: str, work: Path, output: Path, style: int, require_on: bool = True) -> dict[str, Any]:\n'
        '    records = {}\n'
        "    for enable, name in (((False, 'off'), (True, 'on')) if require_on else ((False, 'off'),)):\n"
        "        config_path = work / (mode.key + '-' + name + '-diagnostic.json')\n"
        "        result_path = work / (mode.key + '-' + name + '-engine.json')\n"
        "        config = {'options': options, 'url': url, 'threads': mode.threads, 'style': style, 'enable': enable,\n"
        "                  'module': '/' + mode.key + '/voicevox_benchmark.js', 'model_url': '/' + mode.key + '/sample.vvm',\n"
        "                  'fixed_shape': mode.fixed_shape, 'spin_off': mode.spin_off, 'model_target': entry.get('model_target', 'vocoder'),\n"
        "                  'xnn_threads': mode.threads if mode.execution_provider == 'XNNPACK' else 0,\n"
        "                  'profile': False, 'xnnpack': False, 'wav': str(work / (mode.key + '-diagnostic.wav')),\n"
        "                  'result': str(result_path)}\n"
        '        atomic_write(config_path, json.dumps(config).encode())\n'
        "        env = dict(os.environ); env['DEBUG'] = 'pw:browser'\n"
        "        progress('Untimed V8 transformation check: ' + mode.key + ' ' + name)\n"
        "        captured = checked_run([sys.executable, str(Path(__file__).resolve()), '--diagnostic-config', str(config_path)], env=env)\n"
        "        trace_path = output.parent / (mode.key + '-' + name + '-v8.log')\n"
        '        atomic_write(trace_path, captured.encode())\n'
        '        info = json.loads(result_path.read_text())\n'
        "        nodes = [int(value) for value in re.findall(r'Decide(?:d)? to vectorize, ([1-9][0-9]*) revectorizable nodes', captured)]\n"
        "        rejected = bool(re.search(r'(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|Error:.*(?:wasm-revectorize|trace-wasm-revectorize)', captured, re.I))\n"
        "        expected = '--js-flags=' + ('--wasm-revectorize' if enable else '--no-wasm-revectorize') + ',--trace-wasm-revectorize'\n"
        "        records[name] = {**info, 'transformed_groups': len(nodes), 'revectorizable_nodes': sum(nodes), 'flag_rejected': rejected,\n"
        "                         'launch_configuration_matches': info['requested_js_flags'] == [expected],\n"
        "                         'trace_sha256': sha256_file(trace_path), 'trace_file': trace_path.name}\n"
        "    if 'on' not in records:\n"
        "        off = records['off']\n"
        "        records['off_verified'] = off['transformed_groups'] == 0 and not off['flag_rejected'] and off['launch_configuration_matches']\n"
        "        records['on_verified'] = False\n"
        "        records['verified'] = records['off_verified']\n"
        '        return records\n'
        "    off, on = records['off'], records['on']\n"
        "    records['off_verified'] = off['transformed_groups'] == 0 and not off['flag_rejected'] and off['launch_configuration_matches']\n"
        "    records['on_verified'] = records['off_verified'] and on['transformed_groups'] > 0 and not on['flag_rejected'] and on['launch_configuration_matches'] and (off['product'], off['js_version']) == (on['product'], on['js_version'])\n"
        "    records['verified'] = (off['transformed_groups'] == 0 and on['transformed_groups'] > 0 and\n"
        "        all(x['launch_configuration_matches'] and not x['flag_rejected'] for x in (off, on)) and\n"
        "        (off['product'], off['js_version']) == (on['product'], on['js_version']))\n"
        '    return records\n'
        '\n'
        '\n'
        'def run_research(args: argparse.Namespace) -> None:\n'
        '    """Compare manifest-declared cached CORE variants without changing any precision flags.\n'
        '\n'
        '    Each entry identifies a cached validated build, model archive, threads, provider,\n'
        '    shape policy, and an explicit V8 revectorization setting. Models/audio stay local.\n'
        '    All timing records are checkpointed before validation, and no waveform is saved\n'
        '    in the output directory. This is a browser CORE benchmark, not ORT Web JS.\n'
        '    """\n'
        '    research_trials_per_block = 1 if args.research_screen else TRIALS_PER_BLOCK\n'
        '    from playwright.sync_api import sync_playwright\n'
        '    import psutil\n'
        "    manifest = json.loads(args.research_manifest.read_text('utf-8'))\n"
        "    entries = manifest['variants']\n"
        '    if len(entries) < 2:\n'
        "        raise ValueError('Research needs a same-host control and candidate')\n"
        "    modes = [Mode(entry['key'], entry['label'], entry.get('threads', args.threads),\n"
        "                  entry.get('description', 'isolated FP32 research variant'),\n"
        "                  experimental=index > 0, fixed_shape=entry.get('fixed_shape', False),\n"
        "                  spin_off=entry.get('spin_off', False),\n"
        "                  execution_provider=entry.get('provider', 'CPU'),\n"
        "                  revectorize=entry.get('revectorize', False)) for index, entry in enumerate(entries)]\n"
        '    if len({m.key for m in modes}) != len(modes):\n'
        "        raise ValueError('Duplicate research mode')\n"
        "    query = json.loads(args.audio_query.read_text('utf-8')) if args.audio_query else prepared_query(args.target_seconds)\n"
        "    query_bytes = json.dumps(query, separators=(',', ':'), ensure_ascii=False).encode()\n"
        '    environment = environment_info()\n'
        "    environment.update({'research_manifest': manifest, 'audio_query_sha256': hashlib.sha256(query_bytes).hexdigest(),\n"
        "                        'precision_flags': 'FP32; no quantization, FP16, relaxed SIMD or approximate fast-math',\n"
        "                        'cpu_padding_seconds': CPU_PADDING_S, 'cpu_sample_interval_ms': CPU_SAMPLE_INTERVAL_S * 1000,\n"
        "                        'cpu_metric': 'sum target Chromium process-tree user+system CPU seconds/action snapshot seconds; 100%=one logical CPU',\n"
        "                        'browser_cpu_affinity': psutil.Process().cpu_affinity() if hasattr(psutil.Process(), 'cpu_affinity') else None,\n"
        "                        'available_logical_cpus': logical_cpu_count(),\n"
        "                        'core_variants': {}, 'wasm_bytes': {}})\n"
        '    log = []\n'
        '    trials = []\n'
        "    results_path = args.output.with_suffix('.json')\n"
        "    previous_browser_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')\n"
        "    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(args.cache_dir.resolve() / 'playwright')\n"
        '    try:\n'
        "        with tempfile.TemporaryDirectory(prefix='voicevox-research-private-') as temporary:\n"
        '            work = Path(temporary)\n'
        "            atomic_write(work / 'query.json', query_bytes)\n"
        "            atomic_write(work / 'index.html', BROWSER_PAGE.encode())\n"
        "            atomic_write(work / 'worker.js', BROWSER_WORKER.encode())\n"
        '            for entry, mode in zip(entries, modes):\n'
        "                binary = Path(entry['binary']).resolve()\n"
        "                model = Path(entry['model']).resolve()\n"
        "                receipt = json.loads((binary.parent / 'complete.json').read_text())\n"
        "                if binary.name not in receipt['files'] or receipt['identity'] != entry['build_identity']:\n"
        "                    raise ValueError('Binary is not the manifest-pinned validated CORE build')\n"
        "                for name, digest in receipt['files'].items():\n"
        '                    if sha256_file(binary.parent / name) != digest:\n'
        "                        raise ValueError('Cached CORE build integrity mismatch')\n"
        "                if sha256_file(model) != entry['model_sha256']:\n"
        "                    raise ValueError('Model archive hash mismatch')\n"
        '                folder = work / mode.key\n'
        '                folder.mkdir()\n'
        "                for path in binary.parent.glob('voicevox_benchmark.*'):\n"
        "                    if path.suffix in {'.wasm', '.js'}:\n"
        '                        shutil.copy2(path, folder / path.name)\n'
        "                shutil.copyfile(model, folder / 'sample.vvm')\n"
        "                environment['core_variants'][mode.key] = {'identity': receipt['identity'], 'files': receipt['files']}\n"
        "                environment['wasm_bytes'][mode.key] = binary.with_suffix('.wasm').stat().st_size\n"
        '            with serve_assets(work) as url, sync_playwright() as playwright:\n'
        '                runners, browsers, cpu_roots = {}, {}, {}\n'
        "                environment['v8_activation_diagnostics'] = {}\n"
        '                diagnostic_cache = {}\n'
        '                try:\n'
        '                    for entry, mode in zip(entries, modes):\n'
        "                        identity = (json.dumps(environment['core_variants'][mode.key], sort_keys=True), entry['model_sha256'], mode.threads,\n"
        "                                    mode.fixed_shape, mode.spin_off, mode.execution_provider, entry.get('model_target', 'vocoder'), mode.revectorize)\n"
        '                        if identity not in diagnostic_cache:\n'
        "                            options = {'headless': not args.headed}\n"
        "                            if args.browser_path: options['executable_path'] = str(args.browser_path)\n"
        '                            diagnostic_cache[identity] = verify_research_revectorization(entry, mode, options, url, work, args.output, args.style_id, require_on=mode.revectorize)\n'
        "                        environment['v8_activation_diagnostics'][mode.key] = diagnostic_cache[identity]\n"
        "                        atomic_write(args.output.with_suffix('.diagnostics.json'), json.dumps(environment['v8_activation_diagnostics'], indent=2).encode())\n"
        "                        if not diagnostic_cache[identity]['on_verified' if mode.revectorize else 'off_verified']:\n"
        "                            raise RuntimeError('V8 activation could not be verified for research mode ' + mode.key)\n"
        '\n'
        '                    for entry, mode in zip(entries, modes):\n'
        "                        jsflag = '--wasm-revectorize' if mode.revectorize else '--no-wasm-revectorize'\n"
        "                        options = {'headless': not args.headed, 'args': ['--js-flags=' + jsflag]}\n"
        '                        if args.browser_path:\n'
        "                            options['executable_path'] = str(args.browser_path)\n"
        "                        progress('Initializing research ' + mode.key)\n"
        '                        browser = playwright.chromium.launch(**options)\n'
        '                        browsers[mode.key] = browser\n'
        '                        cpu_roots[mode.key] = chromium_process_id(browser)\n'
        "                        environment['browser'] = browser.version\n"
        "                        environment[mode.key + '_engine'] = browser_engine_info(browser)\n"
        "                        actual = environment[mode.key + '_engine']\n"
        "                        checked = environment['v8_activation_diagnostics'][mode.key]['on' if mode.revectorize else 'off']\n"
        "                        if (actual['product'], actual['js_version']) != (checked['product'], checked['js_version']):\n"
        "                            raise RuntimeError('Timed browser engine differs from V8 diagnostic')\n"
        "                        environment[mode.key + '_v8_flags'] = options['args']\n"
        "                        runner = BrowserRunner(browser, url, '/' + mode.key + '/voicevox_benchmark.js', mode.threads, True,\n"
        "                                               args.style_id, work / (mode.key + '.wav'), fixed_shape=mode.fixed_shape,\n"
        "                                               spin_off=mode.spin_off, xnn_threads=mode.threads if mode.execution_provider == 'XNNPACK' else 0,\n"
        "                                               model_url='/' + mode.key + '/sample.vvm', model_target=entry.get('model_target', 'vocoder'))\n"
        '                        runners[mode.key] = runner\n'
        "                        environment[mode.key + '_hardware_concurrency'] = runner.page.evaluate('navigator.hardwareConcurrency')\n"
        '                        for _ in range(3):\n'
        '                            elapsed, duration = runner.synthesize(save=True)\n'
        "                            log.append({'event': 'warmup_excluded', 'mode': mode.key, 'seconds': elapsed, 'audio_s': duration})\n"
        "                        environment[mode.key + '_pthreads'] = runner.thread_state()\n"
        "                        environment[mode.key + '_runtime'] = {'browser': browser.version, 'fixed_length': runner.fixed_length,\n"
        "                            'fixed_matches': runner.fixed_matches, 'xnn_threads': runner.xnn_threads,\n"
        "                            'xnn_sessions': runner.xnn_sessions, 'spin_off': runner.spin_off, 'shared_memory': runner.shared_memory,\n"
        "                            'model_target': runner.model_target, 'vocoder_fixed_matches': runner.vocoder_fixed_matches,\n"
        "                            'vocoder_xnn_sessions': runner.vocoder_xnn_sessions, 'ort_global_threads': 1 if runner.xnn_threads else mode.threads}\n"
        '                    reference = modes[0].key\n'
        '                    output_checks = verify_outputs(runners, reference, log)\n'
        "                    spectrograms = output_checks.pop('spectrograms')\n"
        "                    output_checks['per_mode_repeat'] = {}\n"
        '                    for key, runner in runners.items():\n'
        '                        first_wav, first_raw = runner.wav.read_bytes(), runner.raw_wave()\n'
        '                        runner.synthesize(save=True)\n'
        '                        repeated = waveform_comparison(first_wav, first_raw, runner.wav.read_bytes(), runner.raw_wave())\n'
        "                        output_checks['per_mode_repeat'][key] = repeated\n"
        "                        if not repeated['pcm_exact'] or not repeated['fp32_exact']:\n"
        "                            atomic_write(args.output.with_suffix('.failed-checks.json'), json.dumps(output_checks, indent=2).encode())\n"
        "                            raise RuntimeError('Research mode is not bitwise repeatable: ' + key)\n"
        "                    if not output_checks['reference_deterministic']:\n"
        "                        raise RuntimeError('Research control is not bitwise repeatable; investigate before timing')\n"
        '                    durations = [runner.duration for runner in runners.values()]\n'
        "                    if max(durations) - min(durations) > 1 / query['outputSamplingRate']:\n"
        "                        raise RuntimeError('Research output durations differ')\n"
        "                    result = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),\n"
        "                              'modes': [asdict(mode) for mode in modes], 'trials': [], 'environment': environment,\n"
        "                              'research_screen': args.research_screen, 'trials_per_block': research_trials_per_block,\n"
        "                              'audio_s': durations[0], 'style_id': args.style_id, 'seed': args.seed, 'log': log,\n"
        "                              'output_checks': output_checks, 'spectrograms': spectrograms,\n"
        "                              'schedule_method': 'seeded position-balanced and pair-order-balanced design; randomized labels and round order',\n"
        "                              'schedule': [asdict(block) for block in make_schedule(modes, args.seed, balanced=True)],\n"
        "                              'notes': [f'Same-host interleaved control/candidate, {research_trials_per_block * BLOCKS_PER_MODE} trials in 3 blocks; explicit V8 OFF/ON. Preliminary screen only.' if args.research_screen else 'Same-host interleaved control/candidate, 15 trials in 3 blocks; explicit V8 OFF/ON.',\n"
        "                                        'Raw FP32 and PCM differences are measured, not assumed acceptable. Only numeric metrics and PNGs are published.']}\n"
        '                    atomic_write(results_path, json.dumps(result, ensure_ascii=False, indent=2).encode())\n'
        '                    if args.measurement_gate:\n'
        "                        progress('Ready for quiet measurement window; waiting for gate ' + str(args.measurement_gate))\n"
        '                        while not args.measurement_gate.exists():\n'
        '                            time.sleep(1)\n'
        '                    def measured(mode):\n'
        '                        return ProcessCpuSampler(cpu_roots[mode.key], mode.label).measure(runners[mode.key].synthesize)\n'
        '                    # Write each completed block/trial before any whole-run validation.\n'
        '                    order = 0\n'
        '                    for block in make_schedule(modes, args.seed, balanced=True):\n'
        '                        mode = next(m for m in modes if m.key == block.mode)\n'
        "                        progress('Research block ' + mode.key + ' ' + str(block.number))\n"
        '                        for number in range(1, research_trials_per_block + 1):\n'
        '                            order += 1\n'
        '                            elapsed, duration, cpu = measured(mode)\n'
        '                            trial = Trial(order, mode.key, block.number, number, mode.threads, elapsed, duration,\n'
        '                                          elapsed / duration, **asdict(cpu))\n'
        '                            trials.append(trial)\n'
        "                            result['trials'] = [asdict(t) for t in trials]\n"
        '                            atomic_write(results_path, json.dumps(result, ensure_ascii=False, indent=2).encode())\n'
        "                            progress(f'Research trial {order}: {mode.key} {elapsed:.6f}s')\n"
        '                    if args.research_screen:\n'
        '                        validate_results(result)\n'
        "                        progress('Preliminary research screen saved to ' + str(results_path) + '; no confirmed speedup claim')\n"
        '                    else:\n'
        '                        save_measurements_and_report(result, args.output)\n'
        '                finally:\n'
        '                    for runner in runners.values():\n'
        '                        with contextlib.suppress(Exception): runner.close()\n'
        '                    for browser in browsers.values():\n'
        '                        with contextlib.suppress(Exception): browser.close()\n'
        '    finally:\n'
        "        if previous_browser_path is None: os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)\n"
        "        else: os.environ['PLAYWRIGHT_BROWSERS_PATH'] = previous_browser_path\n"
        '\n'
        '\n'
        'def main() -> None:\n'
        '    args = argument_parser().parse_args()\n'
        '    if args.diagnostic_config:\n'
        '        revectorization_diagnostic_child(args.diagnostic_config)\n'
        '        return\n'
        '    if args.output.suffix.lower() not in {".html", ".htm"}:\n'
        '        raise ValueError("--output must end with .html or .htm")\n'
        '    if args.self_test:\n'
        '        self_test()\n'
        '        return\n'
        '    if args.report_from:\n'
        '        result = json.loads(args.report_from.read_text("utf-8"))\n'
        '        if result.get("research_screen"):\n'
        '            raise ValueError("Preliminary screen JSON is not a final 15-trial report; run rigorous confirmation first")\n'
        '        if "audio_outputs" in result:\n'
        '            raise ValueError("Legacy embedded audio must be removed before rendering; use local WAVs or PNG-only spectrograms")\n'
        '        if args.audio_dir and args.spectrogram_dir:\n'
        '            raise ValueError("Choose either --audio-dir or --spectrogram-dir")\n'
        '        if args.audio_dir:\n'
        '            attach_saved_audio(result, args.audio_dir)\n'
        '        if args.spectrogram_dir:\n'
        '            attach_saved_spectrograms(result, args.spectrogram_dir)\n'
        '        render_report(result, args.output)\n'
        '        print(f"Report: {args.output.resolve()}")\n'
        '        return\n'
        '    if args.audio_dir or args.spectrogram_dir:\n'
        '        raise ValueError("--audio-dir and --spectrogram-dir are only used with --report-from")\n'
        '    if args.research_manifest:\n'
        '        run_research(args)\n'
        '    else:\n'
        '        run_benchmark(args)\n'
        '\n'
        '\n'
        'if __name__ == "__main__":\n'
        '    try:\n'
        '        main()\n'
        '    except KeyboardInterrupt:\n'
        '        raise SystemExit("Interrupted. A completed report was not written.")\n'
        '    except (RuntimeError, ValueError, OSError) as error:\n'
        '        raise SystemExit(f"Error: {error}") from error\n'
    ),
    # SHA-256: 6b2a20253fc4b90a1d572e701d46c8cbab48870dfb12b9781cea047007c17227
    'runtime/prepare_vocoder_runtime.py': (
        '#!/usr/bin/env python3\n'
        '"""Portable, source-pinned CORE vocoder-XNN runtime preparation.\n'
        'Run after voicevox_webcpu_research.py --prepare-only. No measurements or uploads.\n'
        '"""\n'
        'from __future__ import annotations\n'
        'import argparse, hashlib, importlib.util, json, os, re, shlex, shutil, subprocess, sys\n'
        'from pathlib import Path\n'
        '\n'
        "HARNESS_SHA='d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe'\n"
        "WRAPPER_SHA='76c21496eebbd122f41d43413878bbf3e2213a14bb8e86ae50a594bdcf54ed72'\n"
        "ORT_SHA='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd'\n"
        "PROBE_SHA='c2753086a8d5fae75940a3edeee4d7f94c66c47be73c32459592f86eb1a0629b'\n"
        "TRANSFORM_SHA='183468c51a29746ae50871e2f637ab2fc4c941294358b4531f78c854ea067647'\n"
        "XNN_COMMIT='fe98e0b93565382648129271381c14d6205255e3'\n"
        "XNN_TAR_SHA='639fa4fda5dbf0e501642db4a93ed1dba91aa4d9f2ce48ed5d01602adc0447cc'\n"
        "PTHREAD_SHA='546f40bfb687562e812ba226bf4afcb848e3a2bcc2fc8ca781047b37789f0d72'\n"
        '\n'
        '\n'
        'def sha(path):\n'
        '    h=hashlib.sha256()\n'
        "    with Path(path).open('rb') as f:\n"
        "        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)\n"
        '    return h.hexdigest()\n'
        '\n'
        '\n'
        'def load_module(path, name):\n'
        '    spec=importlib.util.spec_from_file_location(name,path)\n'
        '    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)\n'
        '    return module\n'
        '\n'
        '\n'
        'def replace_once(text, old, new):\n'
        "    if text.count(old)!=1:raise ValueError('Unexpected pinned harness marker: '+old[:100])\n"
        '    return text.replace(old,new,1)\n'
        '\n'
        '\n'
        "BUILD_AND_VERIFY = '''        # CORE bundles the native static library; cleaning only the final wrapper is unsafe.\n"
        '        clean_log = checked_run([cargo, f"+{RUST_VERSION}", "clean", "--manifest-path", str(source / "Cargo.toml"), "-p", "voicevox_core", "-p", "voicevox_benchmark", "--profile", "c-api", "--target", "wasm32-unknown-emscripten"], env=build_env)\n'
        '        link_log = checked_run(command + ["-vv"], env=build_env)\n'
        '        (folder / "cargo-link.log").write_text(clean_log + link_log)\n'
        '        rlibs = set(re.findall(r"--extern voicevox_core=([^\\\\s`]+)", link_log))\n'
        '        if len(rlibs) != 1: raise RuntimeError(f"Ambiguous actually linked CORE rlib: {rlibs}")\n'
        '        core_rlib = Path(next(iter(rlibs)))\n'
        '        llvm_ar = str(Path(build_env["EMSDK"]) / "upstream/bin/llvm-ar")\n'
        '        intended = subprocess.check_output([llvm_ar, "p", str(runtime), "gemm-config.c.o"], env=build_env)\n'
        '        bundled = subprocess.check_output([llvm_ar, "p", str(core_rlib), "gemm-config.c.o"], env=build_env)\n'
        '        if not intended or intended != bundled: raise RuntimeError("CORE rlib contains a stale or unexpected native dispatcher")\n'
        '        bundle_proof = {"verified": True, "core_rlib": str(core_rlib), "dispatcher_sha256": hashlib.sha256(intended).hexdigest(), "runtime_sha256": sha256_file(runtime), "invalidated_packages": ["voicevox_core", "voicevox_benchmark"]}\n'
        '        (folder / "native-bundle-verification.json").write_text(json.dumps(bundle_proof, indent=2))\n'
        "    finally:'''\n"
        '\n'
        '\n'
        'def generate_builder(harness, output):\n'
        '    harness=Path(harness);output=Path(output)\n'
        "    if sha(harness)!=HARNESS_SHA:raise ValueError('Research harness changed; review and update its source pin')\n"
        "    base=load_module(harness,'_dispatch_original_harness')\n"
        "    if hashlib.sha256(base.RUST_SOURCE.encode()).hexdigest()!=WRAPPER_SHA:raise ValueError('Original vocoder wrapper changed')\n"
        '    text=harness.read_text()\n'
        "    text=replace_once(text,'TRIALS_PER_BLOCK = 5','DISPATCH_DIR = Path(__file__).resolve().parent\\nTRIALS_PER_BLOCK = 5')\n"
        '    text=replace_once(text,\'    write_wrapper(source)\\n    kind =\', \'\'\'    if not xnnpack or threaded is not True: raise ValueError("Only threaded vocoder-XNN builds supported")\n'
        '    probe_info = json.loads((DISPATCH_DIR / "probe_compile.json").read_text())\n'
        '    probe_object = DISPATCH_DIR / "dispatch_probe.o"\n'
        '    if sha256_file(probe_object) != probe_info["object_sha256"] or sha256_file(DISPATCH_DIR / "dispatch_probe.c") != probe_info["source_sha256"]: raise RuntimeError("Probe compile provenance mismatch")\n'
        '    write_wrapper(source)\n'
        "    kind =''')\n"
        '    text=replace_once(text,\'identity = {"build_adapter_sha256":\',\'identity = {"native_bundle_policy": "v3: invalidate voicevox_core and voicevox_benchmark; inspect linked CORE rlib dispatcher; hash proof and link log", "dispatch_probe": probe_info, "build_adapter_sha256":\')\n'
        "    text=replace_once(text,'_bench_xnn_sessions,_bench_finish_profile','_bench_xnn_sessions,_bench_finish_profile,_probe_xnn_f32_dispatch')\n"
        '    text=replace_once(text,\'        if optimization == "3":\\n            flags +=\',\'        flags += ["-C", f"link-arg={probe_object}"]\\n        if optimization == "3":\\n            flags +=\')\n'
        "    text=replace_once(text,'        checked_run(command, env=build_env)\\n    finally:',BUILD_AND_VERIFY)\n"
        '    text=replace_once(text,\'    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())\',\'    for proof in ("native-bundle-verification.json", "cargo-link.log"):\\n        hashes[proof] = sha256_file(folder / proof)\\n    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())\')\n'
        '    text=replace_once(text,\'if __name__ == "__main__":\',\'if __name__ == "__main__":\\n    raise SystemExit("Generated builder module; use prepare_vocoder_runtime.py")\\n\\nif False:\')\n'
        '    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(text)\n'
        "    return load_module(output,'_dispatch_generated_harness')\n"
        '\n'
        '\n'
        'def archive_members(path):\n'
        '    """Ordered payload hashes retain duplicate member names, ignoring ar metadata."""\n'
        "    result=[];long_names=b''\n"
        "    with Path(path).open('rb') as f:\n"
        "        if f.read(8)!=b'!<arch>\\n':raise ValueError('Not a regular static archive')\n"
        '        while header:=f.read(60):\n'
        "            if len(header)!=60 or header[-2:]!=b'`\\n':raise ValueError('Malformed archive')\n"
        '            name=header[:16].decode().strip();size=int(header[48:58]);payload=f.read(size)\n'
        "            if len(payload)!=size:raise ValueError('Truncated archive member')\n"
        '            if size&1:f.read(1)\n'
        "            if name=='//':long_names=payload;continue\n"
        "            if name in ('/','/SYM64/'):continue\n"
        "            if name.startswith('/'):\n"
        "                offset=int(name[1:]);name=long_names[offset:].split(b'/\\n',1)[0].decode()\n"
        "            else:name=name.rstrip('/')\n"
        '            result.append((name,hashlib.sha256(payload).hexdigest()))\n'
        '    return result\n'
        '\n'
        '\n'
        'def verify_archive_change(original_members, candidate):\n'
        '    members=archive_members(candidate)\n'
        "    if len(members)!=len(original_members):raise ValueError('Archive member count changed')\n"
        '    changed=[(before,after) for before,after in zip(original_members,members) if before!=after]\n'
        "    if len(changed)!=1 or changed[0][0][0]!='gemm-config.c.o' or changed[0][1][0]!='gemm-config.c.o':\n"
        "        raise ValueError('Unexpected archive member changes')\n"
        '\n'
        '\n'
        'def verify_exports(binary):\n'
        '    """Follow Emscripten\'s JS mapping to minified WASM exports."""\n'
        "    binary=Path(binary);data=binary.with_suffix('.wasm').read_bytes()\n"
        "    if data[:8]!=b'\\0asm\\1\\0\\0\\0':raise ValueError('Invalid WASM')\n"
        '    def leb(pos):\n'
        '        value=shift=0\n'
        '        while True:\n'
        '            byte=data[pos];pos+=1;value|=(byte&127)<<shift\n'
        '            if not byte&128:return value,pos\n'
        '            shift+=7\n'
        '    names=[];pos=8\n'
        '    while pos<len(data):\n'
        '        section=data[pos];size,start=leb(pos+1);end=start+size\n'
        '        if section==7:\n'
        '            count,p=leb(start)\n'
        '            for _ in range(count):\n'
        '                n,p=leb(p);name=data[p:p+n].decode();p+=n+1;_,p=leb(p);names.append(name)\n'
        '        pos=end\n'
        '    js=binary.read_text();mapping={}\n'
        "    for name in ['bench_init','bench_synthesize','bench_raw','bench_raw_ptr','bench_raw_len','bench_fixed_length','bench_fixed_matches','bench_xnn_threads','bench_xnn_sessions','bench_finish_profile','probe_xnn_f32_dispatch']:\n"
        '        match=re.search(r\'Module\\["_\'+re.escape(name)+r\'"\\]=wasmExports\\["([^"]+)"\\]\',js)\n'
        "        if not match or match[1] not in names:raise ValueError('Missing actual export '+name)\n"
        '        mapping[name]=match[1]\n'
        '    return mapping\n'
        '\n'
        '\n'
        'def ensure_headers(base, cache, kernel):\n'
        "    archive=base.cached_download(f'https://codeload.github.com/google/XNNPACK/tar.gz/{XNN_COMMIT}',cache,'xnnpack-dispatch.tar.gz',expected_sha256=XNN_TAR_SHA)\n"
        "    tree=base.unpack_cached(archive,cache/'xnnpack-headers')\n"
        "    xnn=tree/f'XNNPACK-{XNN_COMMIT}'\n"
        "    if not (xnn/'src/xnnpack/config.h').is_file():raise ValueError('Pinned XNN headers unavailable')\n"
        "    pthread=base.cached_download('https://raw.githubusercontent.com/Maratyszcza/pthreadpool/4e80ca24521aa0fb3a746f9ea9c3eaa20e9afbb0/include/pthreadpool.h',cache,'pthreadpool.h',expected_sha256=PTHREAD_SHA)\n"
        '    return xnn,pthread\n'
        '\n'
        '\n'
        'def compile_probe(base, kernel, directory, env, xnn, pthread):\n'
        "    source=kernel/'dispatch_probe.c'\n"
        "    if sha(source)!=PROBE_SHA:raise ValueError('Dispatch probe source changed')\n"
        "    target=directory/'dispatch_probe.c';shutil.copyfile(source,target)\n"
        "    sdk=Path(env['EMSDK']);cc=sdk/'upstream/emscripten/emcc';obj=directory/'dispatch_probe.o';deps=directory/'dispatch_probe.d'\n"
        "    flags=['-O2','-pthread','-msimd128','-fwasm-exceptions','-fno-fast-math','-ffp-contract=off','-I',str(pthread.parent),'-I',str(xnn/'src'),'-I',str(xnn/'include'),'-MMD','-MF',str(deps),'-c',str(target),'-o',str(obj)]\n"
        '    base.checked_run([str(cc),*flags],env=env)\n'
        "    headers=shlex.split(deps.read_text().replace('\\\\\\n','').split(':',1)[1])\n"
        "    proof={'source_sha256':sha(target),'object_sha256':sha(obj),'compile_flags':flags,'dependency_sha256':{p:sha(p) for p in sorted(set(headers))},'compiler_version':subprocess.check_output([str(cc),'--version'],env=env,text=True),'compiler_sha256':sha(cc),'xnn_commit':XNN_COMMIT}\n"
        "    (directory/'probe_compile.json').write_text(json.dumps(proof,indent=2))\n"
        '\n'
        '\n'
        'def prepare(args):\n'
        '    cache=args.cache.resolve();kernel=args.kernel_dir.resolve();harness=args.harness.resolve()\n'
        "    base=load_module(harness,'_portable_research')\n"
        "    if sha(harness)!=HARNESS_SHA:raise ValueError('Research harness source pin mismatch')\n"
        "    assets=json.loads((cache/'research-assets.json').read_text())\n"
        "    expected_query=hashlib.sha256(json.dumps(base.prepared_query(10),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()\n"
        "    if assets['query_sha256']!=expected_query:raise ValueError('Prepared assets are not the fixed 10-second query fixture')\n"
        "    original=base.find_one(Path(assets['runtimes']['browser_xnnpack']),'libonnxruntime_webassembly.a')\n"
        "    if sha(original)!=ORT_SHA:raise ValueError('Unexpected original runtime archive')\n"
        "    model=Path(assets['model']);model_hash=sha(model)\n"
        "    if model_hash!=assets['model_sha256']:raise ValueError('Original private model changed')\n"
        "    directory=cache/'vocoder-dispatch-v3';directory.mkdir(parents=True,exist_ok=True)\n"
        '    env=base.ensure_toolchains(cache)\n'
        "    env['PATH']=str(Path(sys.executable).parent)+os.pathsep+env['PATH']\n"
        '    xnn,pthread=ensure_headers(base,directory,kernel)\n'
        '    compile_probe(base,kernel,directory,env,xnn,pthread)\n'
        "    transform=kernel/'make_archive_variants.py'\n"
        "    if sha(transform)!=TRANSFORM_SHA:raise ValueError('IR transform source changed')\n"
        "    archives=directory/'archives'\n"
        "    base.checked_run([sys.executable,str(transform),'--archive',str(original),'--sdk',env['EMSDK'],'--out',str(archives)],env=env)\n"
        "    generated=directory/'runtime_builder.py';builder=generate_builder(harness,generated)\n"
        "    core_archive=base.cached_download(f'https://github.com/yamachu/voicevox_core/archive/{base.CORE_COMMIT}.tar.gz',cache,f'core-{base.CORE_COMMIT}.tar.gz')\n"
        "    tree=base.unpack_cached(core_archive,directory/('source-'+sha(generated)[:16]));source=tree/f'voicevox_core-{base.CORE_COMMIT}'\n"
        "    original_members=archive_members(original);rows=[];staged=directory/'staged/lib/libonnxruntime_webassembly.a';staged.parent.mkdir(parents=True,exist_ok=True)\n"
        '    for variant in args.variants:\n'
        "        runtime=original if variant=='original' else archives/variant/'libonnxruntime_webassembly.a'\n"
        '        digest=sha(runtime)\n'
        "        if variant!='original':\n"
        "            provenance=json.loads((runtime.parent/'PROVENANCE.json').read_text())\n"
        "            if provenance['original_archive_sha256']!=ORT_SHA or provenance['variant_archive_sha256']!=digest:raise ValueError('Variant archive provenance mismatch')\n"
        '            verify_archive_change(original_members,runtime)\n'
        '        shutil.copyfile(runtime,staged)\n'
        "        if sha(staged)!=digest:raise ValueError('Staging changed archive')\n"
        '        binary=builder.build_runner(source,staged,cache,env,threaded=True,threads=args.threads,xnnpack=True)\n'
        "        if sha(staged)!=digest or sha(runtime)!=digest:raise ValueError('Archive changed during link')\n"
        "        receipt=json.loads((binary.parent/'complete.json').read_text())\n"
        "        for name,expected in receipt['files'].items():\n"
        "            if sha(binary.parent/name)!=expected:raise ValueError('Compiled output integrity mismatch')\n"
        "        bundle=json.loads((binary.parent/'native-bundle-verification.json').read_text())\n"
        "        if not bundle['verified'] or bundle['runtime_sha256']!=digest:raise ValueError('Native bundling proof mismatch')\n"
        '        export_map=verify_exports(binary)\n'
        '        dispatch_smoke=None\n'
        '        if args.dispatch_smoke_script:\n'
        "            if not args.node:raise ValueError('No Node executable for explicit smoke')\n"
        "            expected='loadsplat' if variant=='loadsplat' else 'splat'\n"
        "            smoke=subprocess.run([str(args.node),'--no-experimental-wasm-revectorize',str(args.dispatch_smoke_script.resolve()),str(binary),expected],text=True,capture_output=True,check=True)\n"
        '            dispatch_smoke=json.loads(smoke.stdout.strip())\n'
        "            if not dispatch_smoke.get('passed') or dispatch_smoke.get('actual')!=expected:raise ValueError('Actual CORE dispatch smoke mismatch')\n"
        '            dispatch_smoke.update(node_executable=str(args.node),script_sha256=sha(args.dispatch_smoke_script))\n'
        "        rows.append({'actual_core_node_smoke':dispatch_smoke,'key':variant.replace('-','_'),'label':'Vocoder XNN '+variant,'threads':args.threads,'binary':str(binary),'build_identity':receipt['identity'],'model':str(model),'model_sha256':model_hash,'provider':'XNNPACK','fixed_shape':True,'spin_off':True,'model_target':'vocoder','revectorize':False,'dispatch_expected':'auto' if variant in ('original','auto-roundtrip') else variant,'runtime_archive_sha256':digest,'native_bundle':bundle,'actual_export_map':export_map,'description':'Strict FP32; original model; vocoder-XNN only; global ORT1; actual browser dispatch gate required'})\n"
        "        (directory/('prepared-'+variant+'.json')).write_text(json.dumps(rows[-1],indent=2))\n"
        "        print('PORTABLE_VARIANT_READY',variant,str(binary),json.dumps(dispatch_smoke),flush=True)\n"
        "    result={'preparer_sha256':sha(Path(__file__)),'schema':'voicevox-vocoder-runtime-manifest-v1','variants':rows,'fixture_query_sha256':assets['query_sha256'],'research_harness_sha256':HARNESS_SHA,'builder_source_sha256':sha(generated),'require_exact_fp32_pcm_to_reference':True,'require_actual_core_browser_dispatch':True,'require_repeated_raw_fp32_and_pcm_checks':True,'note':'Build/native-bundle proof is not actual browser dispatch or performance evidence. No model/audio upload.'}\n"
        '    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2))\n'
        "    print('Prepared runtime manifest; actual browser dispatch and output gates still required:',args.output)\n"
        '\n'
        '\n'
        'def main():\n'
        '    root=Path(__file__).resolve().parent\n'
        "    parser=argparse.ArgumentParser();parser.add_argument('--cache',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--harness',type=Path,default=root/'voicevox_webcpu_research.py');parser.add_argument('--kernel-dir',type=Path,default=root/'kernel');parser.add_argument('--threads',type=int,default=2);parser.add_argument('--dispatch-smoke-script',type=Path);parser.add_argument('--node',type=Path,default=shutil.which('node'));parser.add_argument('--variants',nargs='+',choices=['original','auto-roundtrip','loadsplat','splat'],default=['original','auto-roundtrip','loadsplat']);args=parser.parse_args()\n"
        "    if args.threads<1 or len(set(args.variants))!=len(args.variants) or 'original' not in args.variants:parser.error('Require original control, unique variants, and positive threads')\n"
        '    import fcntl\n'
        '    args.cache.mkdir(parents=True,exist_ok=True)\n'
        "    with (args.cache/'vocoder-dispatch.lock').open('w') as lock:\n"
        '        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\n'
        '        prepare(args)\n'
        "if __name__=='__main__':main()\n"
    ),
    # SHA-256: 438f528947165594c5eb1485bc20df951a60de7a42395e47289063078f253c81
    'runtime/apply_browser_dispatch.py': (
        '#!/usr/bin/env python3\n'
        '"""Create a patched COPY of the pinned research harness; never edit it in place."""\n'
        'import argparse,ast,hashlib,pathlib,json\n'
        "BASE_SHA='d35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe'\n"
        "p=argparse.ArgumentParser();p.add_argument('source',type=pathlib.Path);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--helper',type=pathlib.Path,default=pathlib.Path(__file__).with_name('core_dispatch_check.js'));a=p.parse_args()\n"
        "assert a.source.resolve()!=a.output.resolve(),'Refuse in-place edits'\n"
        "s=a.source.read_text();assert hashlib.sha256(a.source.read_bytes()).hexdigest()==BASE_SHA,'Unexpected research harness revision'\n"
        "helper=a.helper.read_text();tree=ast.parse(s);assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='BROWSER_WORKER' for t in n.targets));worker=ast.literal_eval(assignment.value)\n"
        'def once(text,needle,replacement):\n'
        ' assert text.count(needle)==1,needle\n'
        ' return text.replace(needle,replacement)\n'
        'worker=once(worker,"  }else if(data.command===\'synthesize\'){","""  }else if(data.command===\'dispatch\'){\n'
        "   if(!ready)throw new Error('CORE must be initialized before dispatch proof');\n"
        "   const dispatch=captureActualCoreDispatch(Module,wasmStdout,data.expected_dispatch===undefined?'auto':data.expected_dispatch);\n"
        '   postMessage({dispatch});\n'
        '  }else if(data.command===\'synthesize\'){""")\n'
        'worker=helper+\'\\n\'+worker;assert \'"""\' not in worker\n'
        'lines=s.splitlines(keepends=True);s=\'\'.join(lines[:assignment.lineno-1])+\'BROWSER_WORKER = r"""\'+worker+\'"""\\n\'+\'\'.join(lines[assignment.end_lineno:])\n'
        's=once(s,"    entries = manifest[\'variants\']\\n","""    entries = manifest[\'variants\']\n'
        "    runtime_dispatch_matrix = any('dispatch_expected' in entry for entry in entries)\n"
        '    if runtime_dispatch_matrix:\n'
        "        if manifest.get('require_exact_fp32_pcm_to_reference') is not True:\n"
        "            raise ValueError('Runtime manifest must require exact FP32/PCM equality to untouched XNN')\n"
        "        if not entries or entries[0].get('key') not in ('original', 'untouched') or entries[0].get('provider') != 'XNNPACK' or entries[0].get('dispatch_expected') != 'auto' or entries[0].get('build_identity', {}).get('runtime') != '407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd':\n"
        "            raise ValueError('First runtime condition must be pinned untouched XNN')\n"
        "        if any(entry.get('dispatch_expected') not in ('auto', 'splat', 'loadsplat') for entry in entries):\n"
        "            raise ValueError('Every runtime condition requires a dispatcher expectation')\n"
        '""")\n'
        's=once(s,"                        runners[mode.key] = runner\\n","""                        runners[mode.key] = runner\n'
        "                        if 'dispatch_expected' in entry:\n"
        "                            proof = runner.page.evaluate('data => request(data)', {'command': 'dispatch', 'expected_dispatch': entry['dispatch_expected']})['dispatch']\n"
        "                            if proof.get('verified'):\n"
        '                                for prior_key, prior in environment.items():\n'
        "                                    if prior_key.endswith('_dispatch'):\n"
        "                                        if prior['worker_hardware_concurrency'] != proof['worker_hardware_concurrency']:\n"
        "                                            proof.update(verified=False, error_type='Error', error_message='CORE Worker hardwareConcurrency changed across variants')\n"
        "                                        elif prior['expected_dispatch'] == proof['expected_dispatch'] == 'auto' and prior['actual_dispatch'] != proof['actual_dispatch']:\n"
        "                                            proof.update(verified=False, error_type='Error', error_message='Untouched/roundtrip auto dispatcher differs')\n"
        "                            environment[mode.key + '_dispatch'] = proof\n"
        "                            atomic_write(args.output.with_suffix('.dispatch.json'), json.dumps({key: value for key, value in environment.items() if key.endswith('_dispatch')}, indent=2).encode())\n"
        "                            if not proof.get('verified'):\n"
        "                                raise RuntimeError('Actual CORE Worker dispatch failed; bounded checkpoint saved')\n"
        '""")\n'
        's=once(s,"                    spectrograms = output_checks.pop(\'spectrograms\')\\n","""                    spectrograms = output_checks.pop(\'spectrograms\')\n'
        "                    output_checks['exact_reference_required'] = runtime_dispatch_matrix\n"
        "                    output_checks['pre_timing_gate_passed'] = False\n"
        "                    atomic_write(args.output.with_suffix('.output-checks.json'), json.dumps(output_checks, indent=2).encode())\n"
        "                    if runtime_dispatch_matrix and (not output_checks['reference_deterministic'] or any(not check['finite'] or not check['fp32_exact'] or not check['pcm_exact'] for check in output_checks['modes'].values())):\n"
        "                        raise RuntimeError('Candidate differs from untouched XNN; numeric checks saved before timing')\n"
        '""")\n'
        's=once(s,"                        output_checks[\'per_mode_repeat\'][key] = repeated\\n","""                        output_checks[\'per_mode_repeat\'][key] = repeated\n'
        "                        atomic_write(args.output.with_suffix('.output-checks.json'), json.dumps(output_checks, indent=2).encode())\n"
        '""")\n'
        's=once(s,"                    result = {\'schema_version\': SCHEMA_VERSION, \'created_at\': datetime.now(timezone.utc).isoformat(),\\n","""                    output_checks[\'pre_timing_gate_passed\'] = True\n'
        "                    atomic_write(args.output.with_suffix('.output-checks.json'), json.dumps(output_checks, indent=2).encode())\n"
        "                    result = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),\n"
        '""")\n'
        "s+='\\n# Actual CORE Worker dispatch helper SHA256: '+hashlib.sha256(a.helper.read_bytes()).hexdigest()+'\\n'\n"
        'ast.parse(s);a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(s)\n'
        "print(json.dumps({'source_sha256':BASE_SHA,'helper_sha256':hashlib.sha256(a.helper.read_bytes()).hexdigest(),'output_sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),'output':str(a.output),'actual_core_worker_check':True,'exact_reference_gate':True}))\n"
    ),
    # SHA-256: abe7453d3feeaf6bc7f489452055a8ba0d02d22c7c158481112c51e0ada10c2b
    'runtime/core_dispatch_check.js': (
        '// Read-only proof from the actual initialized CORE instance in its dedicated Worker.\n'
        '// This helper never supplies, replaces, or overrides self/navigator.\n'
        "function verifyActualCoreDispatch(module, stdout, expected = 'auto') {\n"
        "  if (!['auto', 'splat', 'loadsplat'].includes(expected)) throw new Error('Invalid expected dispatcher');\n"
        "  if (typeof DedicatedWorkerGlobalScope === 'undefined' || !(self instanceof DedicatedWorkerGlobalScope)) {\n"
        "    throw new Error('Dispatch proof must run inside the actual dedicated CORE Worker');\n"
        '  }\n'
        '  const hc = self.navigator.hardwareConcurrency;\n'
        "  if (!Number.isInteger(hc) || hc < 1) throw new Error('Worker hardwareConcurrency unavailable');\n"
        "  if (self.crossOriginIsolated !== true || typeof SharedArrayBuffer === 'undefined' ||\n"
        "      !(module.HEAPU8.buffer instanceof SharedArrayBuffer)) throw new Error('Shared isolated CORE required');\n"
        "  if (!Array.isArray(stdout) || typeof module._probe_xnn_f32_dispatch !== 'function') {\n"
        "    throw new Error('Actual CORE dispatcher export/stdout capture missing');\n"
        '  }\n'
        '  const begin = stdout.length;\n'
        '  const code = module._probe_xnn_f32_dispatch();\n'
        "  const prefix = 'XNN_DISPATCH ';\n"
        "  const lines = stdout.slice(begin).filter(line => typeof line === 'string' && line.startsWith(prefix));\n"
        "  if (lines.length !== 1) throw new Error('Expected exactly one actual CORE dispatch record');\n"
        '  const record = JSON.parse(lines[0].slice(prefix.length));\n'
        "  const keys = ['is_x86', 'navigatorHardwareConcurrency', 'mr', 'nr', 'loadsplatPointers', 'splatPointers', 'checkedPointers', 'relaxedSimd'];\n"
        "  if (Object.keys(record).sort().join(',') !== keys.sort().join(',') ||\n"
        "      keys.some(key => !Number.isInteger(record[key]))) throw new Error('Unexpected dispatcher record schema');\n"
        '  if (record.is_x86 !== 1 || record.relaxedSimd !== 0 || record.mr !== 4 || record.nr !== 8 ||\n'
        '      record.checkedPointers !== 12 || record.navigatorHardwareConcurrency !== hc) {\n'
        "    throw new Error('Actual CORE architecture/precision/tile/context mismatch');\n"
        '  }\n'
        "  const actual = code === 0 ? 'splat' : code === 1 ? 'loadsplat' : null;\n"
        "  if (actual === null || record[actual + 'Pointers'] !== 12 ||\n"
        "      record.loadsplatPointers + record.splatPointers !== 12) throw new Error('Inconsistent actual CORE pointer family');\n"
        "  const required = expected === 'auto' ? (hc > 4 ? 'loadsplat' : 'splat') : expected;\n"
        "  if (actual !== required) throw new Error('Actual CORE dispatcher does not match manifest/heuristic');\n"
        '  return {\n'
        "    schema_version: 1, source: 'actual_CORE_module', context: 'dedicated_worker',\n"
        '    expected_dispatch: expected, actual_dispatch: actual,\n'
        '    worker_hardware_concurrency: hc, probe_hardware_concurrency: record.navigatorHardwareConcurrency,\n'
        '    is_x86: true, relaxed_simd: false, mr: 4, nr: 8,\n'
        '    splat_pointers: record.splatPointers, loadsplat_pointers: record.loadsplatPointers,\n'
        '    checked_pointers: 12, probe_return_code: code,\n'
        '    cross_origin_isolated: true, shared_memory: true, verified: true\n'
        '  };\n'
        '}\n'
        '\n'
        '// Return bounded failed evidence rather than discarding it. No arbitrary stdout\n'
        '// or error payloads survive this wrapper; the caller checkpoints then asserts.\n'
        "function captureActualCoreDispatch(module, stdout, expected = 'auto') {\n"
        '  const start = Array.isArray(stdout) ? stdout.length : 0;\n'
        '  try { return verifyActualCoreDispatch(module, stdout, expected); }\n'
        '  catch (error) {\n'
        '    let hc = null;\n'
        "    let context = 'not_dedicated_worker';\n"
        '    try {\n'
        "      if (typeof DedicatedWorkerGlobalScope !== 'undefined' && self instanceof DedicatedWorkerGlobalScope) context = 'dedicated_worker';\n"
        '      if (Number.isInteger(self.navigator.hardwareConcurrency)) hc = self.navigator.hardwareConcurrency;\n'
        '    } catch (_) {}\n'
        '    const records = [];\n'
        "    const prefix = 'XNN_DISPATCH ';\n"
        '    for (const line of (Array.isArray(stdout) ? stdout.slice(start) : [])) {\n'
        '      if (records.length === 2) break;\n'
        "      if (typeof line !== 'string' || !line.startsWith(prefix) || line.length > 1024) continue;\n"
        '      try {\n'
        '        const value = JSON.parse(line.slice(prefix.length));\n'
        '        const numeric = {};\n'
        "        for (const key of ['is_x86','navigatorHardwareConcurrency','mr','nr','loadsplatPointers','splatPointers','checkedPointers','relaxedSimd']) {\n"
        '          if (Number.isInteger(value[key]) && Math.abs(value[key]) <= 2147483647) numeric[key] = value[key];\n'
        '        }\n'
        '        records.push(numeric);\n'
        '      } catch (_) {}\n'
        '    }\n'
        '    const allowed = new Set([\n'
        "      'Invalid expected dispatcher','Dispatch proof must run inside the actual dedicated CORE Worker',\n"
        "      'Worker hardwareConcurrency unavailable','Shared isolated CORE required',\n"
        "      'Actual CORE dispatcher export/stdout capture missing','Expected exactly one actual CORE dispatch record',\n"
        "      'Unexpected dispatcher record schema','Actual CORE architecture/precision/tile/context mismatch',\n"
        "      'Inconsistent actual CORE pointer family','Actual CORE dispatcher does not match manifest/heuristic'\n"
        '    ]);\n'
        '    return {\n'
        "      schema_version: 1, source: 'actual_CORE_module', context,\n"
        "      expected_dispatch: ['auto','splat','loadsplat'].includes(expected) ? expected : 'invalid',\n"
        '      worker_hardware_concurrency: hc, numeric_records: records, verified: false,\n'
        "      error_type: ['Error','TypeError','RangeError','SyntaxError','RuntimeError'].includes(error?.name) ? error.name : 'Error',\n"
        "      error_message: allowed.has(error?.message) ? error.message : 'Unexpected dispatcher exception'\n"
        '    };\n'
        '  }\n'
        '}\n'
    ),
    # SHA-256: 0860d6b2e5ef91eea8fa4e3423bd022425ca890198f4f3705f30460b6d3bae3b
    'runtime/browser_runtime_confirmation.py': (
        '#!/usr/bin/env -S uv run --script\n'
        '# /// script\n'
        '# requires-python = ">=3.12"\n'
        '# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]\n'
        '# ///\n'
        '"""Fresh-process paired CORE browser confirmation; no high-frequency sampling in primary trials."""\n'
        'import argparse,contextlib,hashlib,importlib.util,json,os,random,shutil,sys,tempfile,time\n'
        'from pathlib import Path\n'
        'from dataclasses import asdict\n'
        'from validate_runtime_confirmation import validate,pressure_during_call,resource_observations,balanced_schedule,MODES,ARCHIVES\n'
        'import psutil\n'
        'from playwright.sync_api import sync_playwright\n'
        "p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--harness',type=Path,required=True);p.add_argument('--cache',type=Path);p.add_argument('--browser-path',type=Path);p.add_argument('--seed',type=int,default=1729);p.add_argument('--output',type=Path,required=True);p.add_argument('--revec',choices=['off','on'],default='off');p.add_argument('--process-sets',type=int,default=3);p.add_argument('--pairs',type=int,default=5);p.add_argument('--warmups',type=int,default=5);a=p.parse_args();assert a.process_sets >= 1 and a.pairs >= 1 and a.warmups >= 1\n"
        'root=Path(__file__).resolve().parent\n'
        "assert hashlib.sha256(a.harness.read_bytes()).hexdigest()=='2ca8db10524e87156d5f18f7dfa94783c7f5726f53431b778723f1ef43063dee', 'Unexpected patched research harness revision'\n"
        "spec=importlib.util.spec_from_file_location('bench',a.harness);b=importlib.util.module_from_spec(spec);sys.modules[spec.name]=b;spec.loader.exec_module(b)\n"
        "man=json.loads(a.manifest.read_text());entries=man['variants'];assert [e['key'] for e in entries]==list(MODES)\n"
        "assert man.get('require_exact_fp32_pcm_to_reference') is True\n"
        "assert all(e['build_identity']['runtime']==ARCHIVES[e['key']] for e in entries), 'Unexpected runtime archive digest'\n"
        "assert [e['dispatch_expected'] for e in entries]==['auto','auto','loadsplat']\n"
        "assert all(e['provider']=='XNNPACK' and e['fixed_shape'] and e['spin_off'] and e.get('model_target','vocoder')=='vocoder' and e['threads']==2 and e.get('revectorize',False)==(a.revec=='on') for e in entries)\n"
        "assert len({e['model_sha256'] for e in entries})==1\n"
        "assert entries[0]['model_sha256']=='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9'\n"
        'schedule=balanced_schedule(a.process_sets,a.pairs,a.seed)\n'
        "if a.cache:os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(a.cache.resolve()/'playwright')\n"
        "modes=[b.Mode(e['key'],e['label'],e['threads'],'Strict FP32 runtime confirmation',experimental=i>0,revectorize=a.revec=='on',fixed_shape=e['fixed_shape'],spin_off=e['spin_off'],execution_provider=e['provider']) for i,e in enumerate(entries)]\n"
        "result={'schema':'voicevox-browser-runtime-confirmation-v1','seed':a.seed,'schedule':schedule,'status':'initializing','revectorization':a.revec,'manifest_sha256':b.sha256_file(a.manifest),'harness_sha256':b.sha256_file(Path(__file__)),'validator_sha256':b.sha256_file(root/'validate_runtime_confirmation.py'),'research_harness_sha256':b.sha256_file(a.harness),'environment':b.environment_info(),'provenance':{},'diagnostics':{},'process_sets':[],'trials':[],'cpu_companion':[],'notes':['Primary latency is unsampled; separate CPU companion retains process-tree time series.','All requested warmups are recorded, not a proof of tiering convergence.','Fifteen paired observations in three fresh browser-process sets; analyze paired effects and process-set clusters.','One complete predeclared matrix: retain and annotate all majorfault and host-swap observations without exclusions. Abort only allocation/execution failure or available memory below1GiB. PSI/VmSwap provide context, not correction factors.', 'Post-call resource counters include checkpoint file IO between synthesis return and snapshot; reported synthesis latency excludes snapshots and checkpoint IO.','Raw FP32 and PCM remain private and are removed after checks.']}\n"
        'def save():b.atomic_write(a.output,json.dumps(result,indent=2).encode())\n'
        'def host():\n'
        ' m=psutil.virtual_memory();s=psutil.swap_memory();psi={}\n'
        ' try:\n'
        "  for line in Path('/proc/pressure/memory').read_text().splitlines():\n"
        "   name,*fields=line.split();psi[name]={k:float(v) for k,v in (f.split('=') for f in fields)}\n"
        ' except (OSError,ValueError):pass\n'
        " return {'memory_psi':psi,'available_bytes':m.available,'total_bytes':m.total,'swap_used':s.used,'swap_in':s.sin,'swap_out':s.sout,'loadavg':list(os.getloadavg())}\n"
        'def usage(pid):\n'
        ' out=[]\n'
        ' for q in [psutil.Process(pid),*psutil.Process(pid).children(recursive=True)]:\n'
        '  try:\n'
        "   stat=Path(f'/proc/{q.pid}/stat').read_text().rsplit(')',1)[1].split() if sys.platform.startswith('linux') else None\n"
        "   status=Path(f'/proc/{q.pid}/status').read_text() if stat else ''\n"
        "   swap=next((int(line.split()[1])*1024 for line in status.splitlines() if line.startswith('VmSwap:')),None)\n"
        "   out.append({'pid':q.pid,'created':q.create_time(),'rss':q.memory_info().rss,'cpu':q.cpu_times()._asdict(),'major_faults':int(stat[9]) if stat else None,'minor_faults':int(stat[7]) if stat else None,'swap_bytes':swap})\n"
        '  except (psutil.NoSuchProcess,psutil.AccessDenied,FileNotFoundError,PermissionError):pass\n'
        ' return out\n'
        "initial_host=host();result['initial_host']=initial_host;save()\n"
        'def require_memory():\n'
        ' snapshot=host()\n'
        " if snapshot['available_bytes']<1024**3:\n"
        "  result['low_memory_stop']=snapshot;save();raise RuntimeError('Available memory below1GiB')\n"
        "def exact(metrics):return metrics['finite'] and metrics['pcm_exact'] and metrics['fp32_exact']\n"
        "launch={'headless':True}\n"
        "if a.browser_path:launch['executable_path']=str(a.browser_path)\n"
        'worker_hc=None\n'
        'try:\n'
        " with tempfile.TemporaryDirectory(prefix='voicevox-runtime-confirm-private-') as td:\n"
        "  work=Path(td);(work/'index.html').write_text(b.BROWSER_PAGE);(work/'worker.js').write_text(b.BROWSER_WORKER);query=json.dumps(b.prepared_query(10),separators=(',',':'),ensure_ascii=False).encode();(work/'query.json').write_bytes(query);result['query_sha256']=hashlib.sha256(query).hexdigest()\n"
        '  for e in entries:\n'
        "   binary=Path(e['binary']);model=Path(e['model']);receipt=json.loads((binary.parent/'complete.json').read_text());assert receipt['identity']==e['build_identity'] and binary.name in receipt['files'] and binary.with_suffix('.wasm').name in receipt['files'];assert b.sha256_file(model)==e['model_sha256']\n"
        "   for name,sha in receipt['files'].items():assert b.sha256_file(binary.parent/name)==sha\n"
        "   folder=work/e['key'];folder.mkdir()\n"
        "   for f in binary.parent.glob('voicevox_benchmark.*'):\n"
        "    if f.suffix in ['.js','.wasm']:shutil.copyfile(f,folder/f.name)\n"
        "   shutil.copyfile(model,folder/'sample.vvm');result['provenance'][e['key']]={'receipt':receipt,'receipt_sha256':b.sha256_file(binary.parent/'complete.json'),'model_sha256':e['model_sha256']}\n"
        '  with b.serve_assets(work) as url,sync_playwright() as pw:\n'
        '   for e,m in zip(entries,modes):\n'
        "    require_memory();diagnostic=b.verify_research_revectorization(e,m,launch,url,work,a.output,302,require_on=a.revec=='on');result['diagnostics'][m.key]=diagnostic;save();assert diagnostic['on_verified' if m.revectorize else 'off_verified']\n"
        '   reference=None\n'
        '   for replica in range(1,a.process_sets+1):\n'
        "    browsers={};runners={};pids={};group={'id':replica,'warmups':[],'checks':{},'runtime':{},'sentinels':[]};result['process_sets'].append(group);save()\n"
        '    try:\n'
        '     for e,m in zip(entries,modes):\n'
        "      require_memory();browser=pw.chromium.launch(**launch,args=['--js-flags='+('--wasm-revectorize' if m.revectorize else '--no-wasm-revectorize')]);browsers[m.key]=browser;pids[m.key]=b.chromium_process_id(browser);engine=b.browser_engine_info(browser);checked=result['diagnostics'][m.key]['on' if m.revectorize else 'off'];assert (engine['product'],engine['js_version'])==(checked['product'],checked['js_version'])\n"
        "      r=b.BrowserRunner(browser,url,'/'+m.key+'/voicevox_benchmark.js',m.threads,True,302,work/(m.key+'.wav'),model_url='/'+m.key+'/sample.vvm',fixed_shape=m.fixed_shape,spin_off=m.spin_off,xnn_threads=m.threads,model_target=e.get('model_target','vocoder'));runners[m.key]=r\n"
        "      group['runtime'][m.key]={'engine':engine,'browser_pid':pids[m.key],'browser_created':psutil.Process(pids[m.key]).create_time(),'browser':browser.version,'launch_args':['--js-flags='+('--wasm-revectorize' if m.revectorize else '--no-wasm-revectorize')],'hardware_concurrency':r.page.evaluate('navigator.hardwareConcurrency'),'threads_requested':m.threads,'pthreads_running':r.thread_state(),'fixed_matches':r.fixed_matches,'xnn_sessions':r.xnn_sessions,'shared_memory':r.shared_memory}\n"
        "      proof=r.page.evaluate('data => request(data)',{'command':'dispatch','expected_dispatch':e['dispatch_expected']})['dispatch'];group['runtime'][m.key]['dispatch']=proof;save()\n"
        "      assert proof.get('verified'), 'Actual CORE dispatcher failed; evidence saved'\n"
        "      if worker_hc is None:worker_hc=proof['worker_hardware_concurrency']\n"
        "      assert proof['worker_hardware_concurrency']==worker_hc, 'Worker concurrency differs across conditions/process sets'\n"
        '      for iteration in range(a.warmups):\n'
        "       require_memory();elapsed,duration=r.synthesize(save=True);group['warmups'].append({'mode':m.key,'iteration':iteration+1,'elapsed_s':elapsed,'audio_s':duration});save()\n"
        "      wav,raw=r.wav.read_bytes(),r.raw_wave();r.synthesize(save=True);repeat=b.waveform_comparison(wav,raw,r.wav.read_bytes(),r.raw_wave());group['checks'][m.key]={'repeat':repeat};save();assert exact(repeat)\n"
        '      if reference is None:reference=(wav,raw)\n'
        "      diff=b.waveform_comparison(reference[0],reference[1],wav,raw);group['checks'][m.key]['versus_initial_control']=diff;group['runtime'][m.key]['pthreads_after_warmup']=r.thread_state();save();assert exact(diff)\n"
        "     assert len(set(pids.values()))==len(entries), 'Conditions must use separate Chromium processes'\n"
        "     require_memory();elapsed,duration=runners['original'].synthesize();group['sentinels'].append({'position':'before','elapsed_s':elapsed,'audio_s':duration});save()\n"
        '     for pair in range(1,a.pairs+1):\n'
        "      order=next(block['order'] for block in schedule if block['process_set']==replica and block['pair']==pair)\n"
        '      for key in order:\n'
        "       require_memory();before={'host':host(),'processes':usage(pids[key])};elapsed,duration=runners[key].synthesize()\n"
        "       row={'process_set':replica,'pair':pair,'mode':key,'pair_order':order,'elapsed_s':elapsed,'audio_s':duration,'before':before};result['trials'].append(row);save()\n"
        "       after={'host':host(),'processes':usage(pids[key])};row.update(after=after,resource_observations=resource_observations(before,after));save();print('PRIMARY_TRIAL '+json.dumps({k:row[k] for k in ['process_set','pair','mode','elapsed_s','audio_s','resource_observations']}),flush=True)\n"
        '       pressure=pressure_during_call(before,after)\n'
        "       if pressure:raise RuntimeError(pressure+'; saved trial retained')\n"
        "     require_memory();elapsed,duration=runners['original'].synthesize();group['sentinels'].append({'position':'after','elapsed_s':elapsed,'audio_s':duration});save()\n"
        '     # Separate instrumented companion, excluded from primary latency comparison.\n'
        '     for key in (list(MODES)[replica-1:]+list(MODES)[:replica-1]):\n'
        "      require_memory();before={'host':host(),'processes':usage(pids[key])};elapsed,duration,cpu=b.ProcessCpuSampler(pids[key],key).measure(runners[key].synthesize);result['cpu_companion'].append({'process_set':replica,'mode':key,'elapsed_s':elapsed,'audio_s':duration,**asdict(cpu)});save()\n"
        "      after={'host':host(),'processes':usage(pids[key])};result['cpu_companion'][-1].update(before=before,after=after,resource_observations=resource_observations(before,after));save()\n"
        "      if pressure_during_call(before,after):raise RuntimeError('Available memory below1GiB; companion retained')\n"
        '     for key,r in runners.items():\n'
        "      require_memory();r.synthesize(save=True);wav,raw=r.wav.read_bytes(),r.raw_wave();diff=b.waveform_comparison(reference[0],reference[1],wav,raw);group['checks'][key]['after']=diff;save();assert exact(diff)\n"
        "      r.synthesize(save=True);repeat=b.waveform_comparison(wav,raw,r.wav.read_bytes(),r.raw_wave());group['checks'][key]['after_repeat']=repeat;save();assert exact(repeat)\n"
        '    finally:\n'
        '     for r in runners.values():\n'
        '      with contextlib.suppress(Exception):r.close()\n'
        '     for browser in browsers.values():\n'
        '      with contextlib.suppress(Exception):browser.close()\n'
        "   validate(result,a.process_sets,a.pairs,a.warmups);result['status']='complete';save()\n"
        "except BaseException as exc:result['status']='failed';result['error_type']=type(exc).__name__;save();raise\n"
        'finally:\n'
        " result['private_temporary_directory_deleted']='work' not in globals() or not work.exists();save()\n"
    ),
    # SHA-256: bb735ae25ab7c1338f7509e3967e5d430baa56386b3235875a912ca5cc739572
    'runtime/validate_runtime_confirmation.py': (
        '"""Strict structural validation independent of browser availability."""\n'
        'import math,itertools,random\n'
        'MODES=("original","auto_roundtrip","loadsplat")\n'
        'ARCHIVES={"original":"407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd","auto_roundtrip":"da6c857b4bfc3f651f96b95a3fcf225fbf28e2abbb0b2b277d5bcb586020ebd2","loadsplat":"0ac03839bdabb9b8113fb6db16a757eefe1e9435850f11194423864df18f8aaf"}\n'
        '\n'
        'def balanced_schedule(sets=3,pairs=5,seed=1729):\n'
        ' if sets!=3 or pairs!=5:raise ValueError("Predeclared runtime confirmation requires 3 sets x 5 rounds")\n'
        ' orders=list(itertools.permutations(MODES))*2+[MODES,MODES[1:]+MODES[:1],MODES[2:]+MODES[:2]]\n'
        ' random.Random(seed).shuffle(orders)\n'
        ' return [{"process_set":i//pairs+1,"pair":i%pairs+1,"order":list(order)} for i,order in enumerate(orders)]\n'
        '\n'
        '\n'
        'def validate(result,sets,pairs,warmups):\n'
        ' def positive(row):\n'
        "  for key in ['elapsed_s','audio_s']:\n"
        "   if type(row[key]) not in (int,float) or not math.isfinite(row[key]) or row[key]<=0:raise ValueError('Invalid timing '+key)\n"
        " schedule=balanced_schedule(sets,pairs,result['seed'])\n"
        " if result['schedule']!=schedule:raise ValueError('Schedule differs from predeclared matrix')\n"
        ' actual_orders={}\n'
        " for row in result['trials']:\n"
        "  actual_orders.setdefault((row['process_set'],row['pair']),[]).append(row['mode'])\n"
        ' for block in schedule:\n'
        "  if actual_orders.get((block['process_set'],block['pair']))!=block['order']:raise ValueError('Trial order differs from schedule')\n"
        " if len(result['process_sets'])!=sets or [x['id'] for x in result['process_sets']]!=list(range(1,sets+1)):raise ValueError('Process sets incomplete')\n"
        ' seen=set()\n'
        " for row in result['trials']:\n"
        "  positive(row);key=(row['process_set'],row['pair'],row['mode'])\n"
        "  if key in seen:raise ValueError('Duplicate trial')\n"
        '  seen.add(key)\n'
        ' expected={(s,p,m) for s in range(1,sets+1) for p in range(1,pairs+1) for m in MODES}\n'
        " if seen!=expected:raise ValueError('Incomplete or unexpected trial matrix')\n"
        " all_hc={v['dispatch']['worker_hardware_concurrency'] for group in result['process_sets'] for v in group['runtime'].values()}\n"
        " if len(all_hc)!=1:raise ValueError('Worker concurrency differs across sets')\n"
        " identities=[(v['browser_pid'],v['browser_created']) for group in result['process_sets'] for v in group['runtime'].values()]\n"
        " if len(set(identities))!=sets*len(MODES):raise ValueError('Browser process identity reused')\n"
        " for group in result['process_sets']:\n"
        "  if len(group['sentinels'])!=2 or {x['position'] for x in group['sentinels']}!={'before','after'}:raise ValueError('Missing sentinels')\n"
        "  for row in group['sentinels']+group['warmups']:positive(row)\n"
        "  keys=[(x['mode'],x['iteration']) for x in group['warmups']]\n"
        "  if len(keys)!=len(MODES)*warmups or set(keys)!={(m,i) for m in MODES for i in range(1,warmups+1)}:raise ValueError('Warmup matrix incomplete')\n"
        "  if set(group['runtime'])!=set(MODES):raise ValueError('Runtime conditions incomplete')\n"
        "  if len({v['dispatch']['worker_hardware_concurrency'] for v in group['runtime'].values()})!=1:raise ValueError('Worker concurrency differs')\n"
        '  for mode in MODES:\n'
        "   proof=group['runtime'][mode]['dispatch']\n"
        "   expected=('loadsplat' if proof['worker_hardware_concurrency']>4 else 'splat') if mode!='loadsplat' else 'loadsplat'\n"
        "   if not proof['verified'] or proof['actual_dispatch']!=expected or proof['checked_pointers']!=12:raise ValueError('Actual dispatcher not verified')\n"
        "   for check in ['repeat','versus_initial_control','after','after_repeat']:\n"
        "    metrics=group['checks'][mode][check]\n"
        "    if not (metrics['pcm_exact'] and metrics['fp32_exact'] and metrics['finite']):raise ValueError('Output gate failed')\n"
        " if len(result['cpu_companion'])!=sets*len(MODES):raise ValueError('Missing CPU companion')\n"
        " for row in result['cpu_companion']:positive(row)\n"
        " if {(x['process_set'],x['mode']) for x in result['cpu_companion']}!={(s,m) for s in range(1,sets+1) for m in MODES}:raise ValueError('Duplicate or incorrect CPU companion matrix')\n"
        '\n'
        '\n'
        'def resource_observations(before,after):\n'
        " old={(x['pid'],x['created']):x for x in before['processes']}\n"
        " major=0;minor=0;swap_growth=0;unknown=len(set(old)-{(x['pid'],x['created']) for x in after['processes']})\n"
        " for x in after['processes']:\n"
        "  previous=old.get((x['pid'],x['created']))\n"
        "  if previous is None or x.get('major_faults') is None or previous.get('major_faults') is None:unknown+=1;continue\n"
        "  major+=max(0,x['major_faults']-previous['major_faults'])\n"
        "  if x.get('minor_faults') is not None and previous.get('minor_faults') is not None:minor+=max(0,x['minor_faults']-previous['minor_faults'])\n"
        "  if x.get('swap_bytes') is not None and previous.get('swap_bytes') is not None:swap_growth+=max(0,x['swap_bytes']-previous['swap_bytes'])\n"
        " return {'target_major_fault_delta':major,'target_minor_fault_delta':minor,'target_swap_growth_bytes':swap_growth,'host_swap_in_delta':max(0,after['host']['swap_in']-before['host']['swap_in']),'host_swap_out_delta':max(0,after['host']['swap_out']-before['host']['swap_out']),'unmatched_or_unknown_processes':unknown,'fault_exposed':major>0}\n"
        '\n'
        'def pressure_during_call(before,after):\n'
        ' # Faults and host swap counters are context, not a reason to remove observations.\n'
        " if min(before['host']['available_bytes'],after['host']['available_bytes']) < 1024**3:return 'Available memory below1GiB'\n"
        ' return None\n'
    ),
    # SHA-256: d893514d2e7b41f1b5b46affd53b58831302370a30b77849d850e02d82e2037d
    'runtime/browser_reference_and_profile.py': (
        '#!/usr/bin/env python3\n'
        '"""Sequential untimed browser references/profiling; private waveforms never escape.\n'
        '\n'
        'Run before or after the research timing processes, never during timing. Uses\n'
        'provided matching diagnostics or obtains one private untimed diagnostic first.\n'
        'This file prepares no external publication and is separate from timing code.\n'
        '"""\n'
        'import argparse,collections,hashlib,importlib.util,json,pathlib,shutil,sys,tempfile,os\n'
        'p=argparse.ArgumentParser()\n'
        "p.add_argument('--harness',type=pathlib.Path,required=True)\n"
        "p.add_argument('--manifest',type=pathlib.Path,required=True)\n"
        "p.add_argument('--cpu-binary',type=pathlib.Path,required=True)\n"
        "p.add_argument('--cpu-receipt',type=pathlib.Path,required=True)\n"
        "p.add_argument('--activation-evidence',type=pathlib.Path)\n"
        "p.add_argument('--output',type=pathlib.Path,required=True)\n"
        "p.add_argument('--browser-path',type=pathlib.Path)\n"
        "p.add_argument('--audio-query',type=pathlib.Path)\n"
        "p.add_argument('--target-seconds',type=float,default=10)\n"
        "p.add_argument('--style-id',type=int,default=302)\n"
        "p.add_argument('--profile-xnn',action='store_true')\n"
        "p.add_argument('--revec',choices=['off','on'],default='off')\n"
        'a=p.parse_args();a.output.parent.mkdir(parents=True,exist_ok=True)\n'
        "enable_revec=a.revec=='on';js_flag='--wasm-revectorize' if enable_revec else '--no-wasm-revectorize'\n"
        "spec=importlib.util.spec_from_file_location('core_browser_reference_harness',a.harness);h=importlib.util.module_from_spec(spec);sys.modules[spec.name]=h;spec.loader.exec_module(h)\n"
        'if "data.command===\'dispatch\'" not in h.BROWSER_WORKER:raise ValueError(\'Patched actual-CORE Worker dispatch support required\')\n'
        'sha=lambda path:hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()\n'
        "manifest=json.loads(a.manifest.read_text());entry=manifest['variants'][0]\n"
        "if entry['provider']!='XNNPACK' or not entry['fixed_shape'] or not entry['spin_off'] or entry.get('model_target','vocoder')!='vocoder':raise ValueError('First variant must be the strict-FP32 vocoder XNN control')\n"
        "if entry.get('revectorize',False)!=enable_revec:raise ValueError('Manifest revectorization must match --revec')\n"
        "if entry.get('dispatch_expected')!='auto' or entry.get('threads',2)!=2 or entry.get('key') not in ['original','untouched'] or entry.get('build_identity',{}).get('runtime')!='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd':raise ValueError('Pinned untouched XNN2 control required')\n"
        "xnn=pathlib.Path(entry['binary']);model=pathlib.Path(entry['model'])\n"
        "if sha(model)!=entry['model_sha256']:raise ValueError('Model hash mismatch')\n"
        "if entry['model_sha256']!='51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9':raise ValueError('Expected pinned original model only')\n"
        'receipts={}\n'
        "for label,binary,receipt_path in [('cpu',a.cpu_binary,a.cpu_receipt),('xnn',xnn,xnn.parent/'complete.json')]:\n"
        ' receipt=json.loads(receipt_path.read_text());receipts[label]=receipt\n'
        " if binary.name not in receipt['files'] or binary.with_suffix('.wasm').name not in receipt['files']:raise ValueError(label+' JS/WASM absent from receipt')\n"
        " for filename,digest in receipt['files'].items():\n"
        "  if sha(binary.parent/filename)!=digest:raise ValueError(label+' artifact receipt mismatch')\n"
        "if receipts['cpu']['identity'].get('xnnpack',False) or receipts['cpu']['identity'].get('kind')!='browser-mt':raise ValueError('Ordinary CPU-MT reference artifact required')\n"
        "if receipts['xnn']['identity']!=entry['build_identity']:raise ValueError('XNN manifest/build identity mismatch')\n"
        'expected_engine=None;activation=None\n'
        'if a.activation_evidence:\n'
        " diagnostics=json.loads(a.activation_evidence.read_text());activation=diagnostics[entry['key']]\n"
        " if not activation.get(a.revec+'_verified') or activation[a.revec]['flag_rejected'] or not activation[a.revec]['launch_configuration_matches'] or (activation[a.revec]['transformed_groups']>0)!=enable_revec:raise ValueError('Successful matching explicit activation evidence required')\n"
        " expected_engine={key:activation[a.revec][key] for key in ['product','js_version']}\n"
        'query=json.loads(a.audio_query.read_text()) if a.audio_query else h.prepared_query(a.target_seconds)\n'
        "query_bytes=json.dumps(query,separators=(',',':'),ensure_ascii=False).encode()\n"
        "report={'schema_version':1,'scope':'sequential untimed actual browser CORE numerical reference and optional vocoder-only profile','engine':expected_engine,'revectorization':a.revec,'requested_v8_flags':['--js-flags='+js_flag],'sequential_browser_processes':True,'inputs':{'harness_sha256':sha(a.harness),'manifest_sha256':sha(a.manifest),'cpu_js_sha256':sha(a.cpu_binary),'cpu_wasm_sha256':sha(a.cpu_binary.with_suffix('.wasm')),'xnn_js_sha256':sha(xnn),'xnn_wasm_sha256':sha(xnn.with_suffix('.wasm')),'model_sha256':sha(model),'query_sha256':hashlib.sha256(query_bytes).hexdigest(),'activation_evidence_sha256':sha(a.activation_evidence) if a.activation_evidence else None},'style_id':a.style_id,'references':{}}\n"
        'private_root=None;samples={}\n'
        'try:\n'
        ' from playwright.sync_api import sync_playwright\n'
        " with tempfile.TemporaryDirectory(prefix='voicevox-browser-reference-private-') as temporary:\n"
        '  work=pathlib.Path(temporary);private_root=work\n'
        "  (work/'query.json').write_bytes(query_bytes);(work/'index.html').write_text(h.BROWSER_PAGE);(work/'worker.js').write_text(h.BROWSER_WORKER);shutil.copyfile(model,work/'sample.vvm')\n"
        "  for label,binary in [('cpu',a.cpu_binary),('xnn',xnn)]:\n"
        '   folder=work/label;folder.mkdir()\n'
        "   for suffix in ['.js','.wasm']:shutil.copyfile(binary.with_suffix(suffix),folder/('voicevox_benchmark'+suffix))\n"
        "   os.link(work/'sample.vvm',folder/'sample.vvm')\n"
        "  options={'headless':True,'args':['--js-flags='+js_flag]}\n"
        "  if a.browser_path:options['executable_path']=str(a.browser_path)\n"
        '  with h.serve_assets(work) as url,sync_playwright() as playwright:\n'
        '   if activation is None:\n'
        "    mode=h.Mode('xnn','Untimed XNN reference',2,'private diagnostic',experimental=True,fixed_shape=True,spin_off=True,execution_provider='XNNPACK',revectorize=enable_revec)\n"
        "    activation=h.verify_research_revectorization(entry,mode,options,url,work,work/'activation.json',a.style_id,require_on=enable_revec)\n"
        "    if not activation.get(a.revec+'_verified'):raise RuntimeError('Matching explicit activation was not verified')\n"
        "    expected_engine={key:activation[a.revec][key] for key in ['product','js_version']}\n"
        "   report['engine']=expected_engine\n"
        "   report['activation']={key:activation[a.revec][key] for key in ['transformed_groups','revectorizable_nodes','flag_rejected','launch_configuration_matches','trace_sha256']}\n"
        '   def run_case(label,profile=False):\n'
        '    # Browser is fully closed before the next case starts; only one full heap.\n'
        '    browser=playwright.chromium.launch(**options);runner=None\n'
        '    try:\n'
        "     if h.browser_engine_info(browser)!=expected_engine:raise RuntimeError('Actual browser/V8 differs from matching diagnostic')\n"
        "     is_xnn=label=='xnn'\n"
        "     runner=h.BrowserRunner(browser,url,'/'+label+'/voicevox_benchmark.js',2,True,a.style_id,work/(label+('-profile' if profile else '')+'.wav'),fixed_shape=is_xnn,spin_off=is_xnn,xnn_threads=2 if is_xnn else 0,profile=profile,model_url='/sample.vvm',model_target='vocoder')\n"
        "     info={'threads':2,'configured_ort_global_threads':1 if is_xnn else 2,'configured_xnn_threads':2 if is_xnn else 0,'pthreads_after_init':runner.pthreads_created,'shared_memory':runner.shared_memory,'fixed_length':runner.fixed_length,'fixed_matches':runner.fixed_matches,'xnn_sessions':runner.xnn_sessions}\n"
        "     if not profile:report['references'][label]=info\n"
        "     else:report['vocoder_profile']=info\n"
        '     if is_xnn:info[\'dispatch\']=runner.page.evaluate("data => request(data)",{\'command\':\'dispatch\',\'expected_dispatch\':\'auto\'})[\'dispatch\']\n'
        "     if is_xnn and not info['dispatch'].get('verified'):raise RuntimeError('Actual CORE dispatch proof failed')\n"
        '     if profile:\n'
        '      for index in range(3):runner.synthesize(save=index==0)  # durations intentionally discarded\n'
        "      private_profile=work/'vocoder-profile.json';profile_info=runner.finish_profile(private_profile)\n"
        "      events=json.loads(private_profile.read_text());groups=collections.defaultdict(lambda:{'names':set(),'calls':0,'kernel_duration_us':0})\n"
        '      for event in events:\n'
        "       if event.get('cat')!='Node' or not event.get('name','').endswith('_kernel_time'):continue\n"
        "       args=event.get('args',{});provider=args.get('provider','unknown');op=args.get('op_name','unknown');group=groups[(provider,op)];group['names'].add(event['name']);group['calls']+=1;group['kernel_duration_us']+=event.get('dur',0)\n"
        "      rows=[{'provider':provider,'operator':op,'unique_nodes':len(value['names']),'calls':value['calls'],'kernel_duration_us':value['kernel_duration_us']} for (provider,op),value in sorted(groups.items())]\n"
        "      counts={row['operator']:row['unique_nodes'] for row in rows if row['provider']=='XnnpackExecutionProvider'}\n"
        "      info.update(profile_scope='first untouched-XNN control only; placement evidence across 3 cold+warm syntheses; summed kernel durations are not warm performance or latency',syntheses=3,profile_events=len(events),operator_counts=rows,xnn_conv_nodes=counts.get('Conv',0),xnn_convtranspose_nodes=counts.get('ConvTranspose',0),profile_assignment_sha256=profile_info['provider_assignment_sha256'])\n"
        "      info['all_expected_convolutions_observed']=info['xnn_conv_nodes']==74 and info['xnn_convtranspose_nodes']==4\n"
        "      report['vocoder_profile']=info\n"
        "      if not info['all_expected_convolutions_observed']:raise RuntimeError('Expected74 Conv+4 ConvTranspose were not all observed on XNN')\n"
        '     else:\n'
        '      first=None\n'
        '      for index in range(2):\n'
        '       runner.synthesize(save=True)  # all elapsed times discarded\n'
        '       current=(runner.wav.read_bytes(),runner.raw_wave())\n'
        '       if first is None:first=current\n'
        '       else:\n'
        "        repeat=h.waveform_comparison(*first,*current);info['repeat_comparison']=repeat\n"
        "        if not repeat['finite'] or not repeat['fp32_exact'] or not repeat['pcm_exact']:raise RuntimeError('Repeated browser outputs are not exact')\n"
        '        samples[label]=current\n'
        "      info['pthreads_after_checks']=runner.thread_state();report['references'][label]=info\n"
        '    finally:\n'
        '     if runner:runner.close()\n'
        '     browser.close()\n'
        "   run_case('cpu');run_case('xnn')\n"
        "   report['xnn_vs_cpu']=h.waveform_comparison(*samples['cpu'],*samples['xnn'])\n"
        "   if not report['xnn_vs_cpu']['finite']:raise RuntimeError('Nonfinite browser reference output')\n"
        '   samples.clear()\n'
        "   if a.profile_xnn:run_case('xnn',profile=True)\n"
        " report['passed']=True\n"
        'except BaseException as error:\n'
        " report['passed']=False;report['error_type']=type(error).__name__\n"
        ' raise\n'
        'finally:\n'
        " samples.clear();report['private_temporary_directory_deleted']=private_root is None or not private_root.exists();a.output.write_text(json.dumps(report,indent=2,allow_nan=False)+'\\n')\n"
    ),
    # SHA-256: 93562441770aeda94f76344c22c4185fc2ccd197e301e74cea38a2648b778f1c
    'runtime/test_prepare_vocoder_runtime.py': (
        '"""Lightweight tests: no downloads, compilers, models, inference, or browser."""\n'
        'import hashlib,json,tempfile,unittest\n'
        'from pathlib import Path\n'
        'import prepare_vocoder_runtime as p\n'
        '\n'
        'def ar(members):\n'
        "    data=b'!<arch>\\n'\n"
        '    for name,payload in members:\n'
        '        header=f\'{name+"/":<16}{0:<12}{0:<6}{0:<6}{644:<8}{len(payload):<10}`\\n\'.encode()\n'
        '        assert len(header)==60\n'
        "        data+=header+payload+(b'\\n' if len(payload)&1 else b'')\n"
        '    return data\n'
        '\n'
        'def leb(n):\n'
        '    out=[]\n'
        '    while True:\n'
        '        part=n&127;n>>=7;out.append(part|(128 if n else 0))\n'
        '        if not n:return bytes(out)\n'
        '\n'
        'class Tests(unittest.TestCase):\n'
        '    def test_ordered_archive_invariant(self):\n'
        '        with tempfile.TemporaryDirectory() as td:\n'
        "            a=Path(td)/'a.a';b=Path(td)/'b.a'\n"
        "            original=[('gemm-config.c.o',b'old'),('pthreadpool.o',b'actual pool'),('same.o',b'a'),('same.o',b'b')]\n"
        '            a.write_bytes(ar(original));baseline=p.archive_members(a)\n'
        "            b.write_bytes(ar([(name,b'new' if name=='gemm-config.c.o' else payload) for name,payload in original]))\n"
        '            p.verify_archive_change(baseline,b)\n'
        '            self.assertEqual(len(baseline),4)\n'
        "            b.write_bytes(ar([(name,b'wrong' if name=='pthreadpool.o' else payload) for name,payload in original]))\n"
        '            with self.assertRaises(ValueError):p.verify_archive_change(baseline,b)\n'
        '            with self.assertRaises(ValueError):p.verify_archive_change(baseline,a)\n'
        '    def test_export_mapping(self):\n'
        "        required=['bench_init','bench_synthesize','bench_raw','bench_raw_ptr','bench_raw_len','bench_fixed_length','bench_fixed_matches','bench_xnn_threads','bench_xnn_sessions','bench_finish_profile','probe_xnn_f32_dispatch']\n"
        '        with tempfile.TemporaryDirectory() as td:\n'
        "            js=Path(td)/'x.js';payload=leb(len(required));lines=[]\n"
        '            for i,name in enumerate(required):\n'
        '                key=\'x\'+str(i);raw=key.encode();payload+=leb(len(raw))+raw+b\'\\0\'+leb(i);lines.append(f\'Module["_{name}"]=wasmExports["{key}"];\')\n'
        "            js.write_text('\\n'.join(lines));js.with_suffix('.wasm').write_bytes(b'\\0asm\\1\\0\\0\\0'+b'\\7'+leb(len(payload))+payload)\n"
        '            self.assertEqual(len(p.verify_exports(js)),len(required))\n'
        "            js.write_text('\\n'.join(lines[:-1]))\n"
        '            with self.assertRaises(ValueError):p.verify_exports(js)\n'
        '    def test_pin_rejection(self):\n'
        '        with tempfile.TemporaryDirectory() as td:\n'
        '            src=Path(td)/\'bad.py\';src.write_text(\'print("not the pinned harness")\')\n'
        "            with self.assertRaises(ValueError):p.generate_builder(src,Path(td)/'out.py')\n"
        '    def test_repair_contract(self):\n'
        '        self.assertIn(\'"-p", "voicevox_core", "-p", "voicevox_benchmark"\',p.BUILD_AND_VERIFY)\n'
        "        self.assertIn('--extern voicevox_core=',p.BUILD_AND_VERIFY)\n"
        "        self.assertIn('intended != bundled',p.BUILD_AND_VERIFY)\n"
        "        self.assertNotIn('/workspace',Path(p.__file__).read_text())\n"
        '\n'
        "if __name__=='__main__':unittest.main()\n"
    ),
    # SHA-256: 19e26ae953ab1905096ab361e5df5bab25638cc88a43bee4d1ccda3e3c57e30c
    'runtime/test_runtime_confirmation.py': (
        '"""Synthetic tests only: no browser, model, timer, or inference is executed."""\n'
        'import collections,copy,itertools,unittest\n'
        'from validate_runtime_confirmation import MODES,balanced_schedule,validate,pressure_during_call,resource_observations\n'
        'from log_runtime_confirmation import summarize\n'
        '\n'
        'def fixture():\n'
        " schedule=balanced_schedule();exact={'pcm_exact':True,'fp32_exact':True,'finite':True}\n"
        " result={'schema':'voicevox-browser-runtime-confirmation-v1','revectorization':'off','status':'complete','seed':1729,'schedule':schedule,'process_sets':[],'trials':[],'cpu_companion':[]}\n"
        ' for s in (1,2,3):\n'
        "  runtime={m:{'browser_pid':s*10+i,'browser_created':100+s,'dispatch':{'verified':True,'worker_hardware_concurrency':4,'actual_dispatch':'loadsplat' if m=='loadsplat' else 'splat','checked_pointers':12}} for i,m in enumerate(MODES)}\n"
        "  result['process_sets'].append({'id':s,'runtime':runtime,'sentinels':[{'position':p,'elapsed_s':1,'audio_s':10} for p in ('before','after')],'warmups':[{'mode':m,'iteration':i,'elapsed_s':1,'audio_s':10} for m in MODES for i in range(1,6)],'checks':{m:{k:exact.copy() for k in ('repeat','versus_initial_control','after','after_repeat')} for m in MODES}})\n"
        "  result['cpu_companion'] += [{'process_set':s,'mode':m,'elapsed_s':1,'audio_s':10,'cpu_trace':[{'cpu_percent':100}]} for m in MODES]\n"
        ' for block in schedule:\n'
        "  for m in block['order']:result['trials'].append({'process_set':block['process_set'],'pair':block['pair'],'mode':m,'pair_order':block['order'],'elapsed_s':1,'audio_s':10})\n"
        ' return result\n'
        '\n'
        'class Tests(unittest.TestCase):\n'
        ' def test_schedule_balance(self):\n'
        '  for seed in range(20):\n'
        "   schedule=balanced_schedule(seed=seed);orders=[x['order'] for x in schedule]\n"
        '   for m in MODES:self.assertEqual(collections.Counter(o.index(m) for o in orders),{0:5,1:5,2:5})\n'
        '   for x,y in itertools.combinations(MODES,2):self.assertIn(sum(o.index(x)<o.index(y) for o in orders),(7,8))\n'
        ' def test_valid(self):validate(fixture(),3,5,5)\n'
        ' def test_reject_bad_matrix(self):\n'
        "  x=fixture();x['trials'].pop()\n"
        '  with self.assertRaises(ValueError):validate(x,3,5,5)\n'
        ' def test_reject_bad_order(self):\n'
        "  x=fixture();x['trials'][0],x['trials'][1]=x['trials'][1],x['trials'][0]\n"
        '  with self.assertRaises(ValueError):validate(x,3,5,5)\n'
        ' def test_reject_failed_repeat(self):\n'
        "  x=fixture();x['process_sets'][0]['checks']['loadsplat']['after_repeat']['fp32_exact']=False\n"
        '  with self.assertRaises(ValueError):validate(x,3,5,5)\n'
        ' def test_reject_wrong_dispatch(self):\n'
        "  x=fixture();x['process_sets'][0]['runtime']['loadsplat']['dispatch']['actual_dispatch']='splat'\n"
        '  with self.assertRaises(ValueError):validate(x,3,5,5)\n'
        ' def test_reject_nonfresh(self):\n'
        "  x=fixture();x['process_sets'][1]['runtime']=copy.deepcopy(x['process_sets'][0]['runtime'])\n"
        '  with self.assertRaises(ValueError):validate(x,3,5,5)\n'
        ' def test_pressure_policy(self):\n'
        "  before={'host':{'available_bytes':2*1024**3,'swap_in':1,'swap_out':2},'processes':[{'pid':1,'created':1,'major_faults':0,'swap_bytes':0}]};after=copy.deepcopy(before);after['processes'][0].update(major_faults=100,swap_bytes=999);after['host']['swap_in']=10000\n"
        "  self.assertIsNone(pressure_during_call(before,after));self.assertEqual(resource_observations(before,after)['target_major_fault_delta'],100)\n"
        "  after['host']['available_bytes']=1024**3-1;self.assertIsNotNone(pressure_during_call(before,after))\n"
        ' def test_log_allowlist(self):\n'
        "  x=fixture();x['raw_audio']=[.1]*1000;x['cpu_companion'][0]['private']='secret'\n"
        "  safe=summarize(x);self.assertNotIn('raw_audio',str(safe));self.assertNotIn('secret',str(safe));self.assertEqual(safe['paired_effects']['loadsplat']['paired_observations'],15);self.assertIn('cpu_trace',x['cpu_companion'][0])\n"
        ' def test_no_different_design(self):\n'
        '  with self.assertRaises(ValueError):balanced_schedule(sets=1,pairs=15)\n'
        "if __name__=='__main__':unittest.main()\n"
    ),
    # SHA-256: b1c93ba2635e66125dd52e98a98ed11c416344449083b153d9fb9cfc17ba62e5
    'runtime/log_runtime_confirmation.py': (
        '#!/usr/bin/env python3\n'
        '"""Allowlisted numeric logs and CPU usage traces; full evidence also remains in JSON."""\n'
        'import argparse,json,math,statistics\n'
        'from pathlib import Path\n'
        'from validate_runtime_confirmation import MODES\n'
        '\n'
        'def number(value):\n'
        " if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('Invalid number')\n"
        ' return value\n'
        '\n'
        'def summarize(result):\n'
        " if result['schema']!='voicevox-browser-runtime-confirmation-v1' or result['revectorization'] not in ('off','on') or result['status'] not in ('initializing','failed','complete'):raise ValueError('Unknown result schema')\n"
        ' rows=[];paired={}\n'
        " for row in result['trials']:\n"
        "  if row['mode'] not in MODES or row['process_set'] not in (1,2,3) or row['pair'] not in range(1,6):raise ValueError('Unexpected trial')\n"
        "  safe={k:row[k] for k in ('process_set','pair','mode')}\n"
        "  for key in ('elapsed_s','audio_s'):safe[key]=number(row[key])\n"
        "  safe['resource_observations']={k:number(v) for k,v in row.get('resource_observations',{}).items() if k in ('target_major_fault_delta','target_minor_fault_delta','target_swap_growth_bytes','host_swap_in_delta','host_swap_out_delta','unmatched_or_unknown_processes')}\n"
        "  rows.append(safe);paired.setdefault((row['process_set'],row['pair']),{})[row['mode']]=safe['elapsed_s']\n"
        ' dispatch=[]\n'
        " for group in result.get('process_sets',[]):\n"
        "  if group['id'] not in (1,2,3):raise ValueError('Unexpected process set')\n"
        "  for mode,value in group.get('runtime',{}).items():\n"
        "   if mode not in MODES:raise ValueError('Unexpected runtime mode')\n"
        "   proof=value.get('dispatch')\n"
        '   if not proof:continue\n'
        "   row={'process_set':group['id'],'mode':mode,'verified':proof.get('verified') is True}\n"
        "   if proof.get('actual_dispatch') in ('splat','loadsplat'):row['actual_dispatch']=proof['actual_dispatch']\n"
        "   for key in ('worker_hardware_concurrency','checked_pointers','splat_pointers','loadsplat_pointers'):\n"
        '    if proof.get(key) is not None:row[key]=number(proof[key])\n'
        '   dispatch.append(row)\n'
        ' companions=[]\n'
        " for row in result.get('cpu_companion',[]):\n"
        "  if row['mode'] not in MODES or row['process_set'] not in (1,2,3):raise ValueError('Unexpected companion')\n"
        "  safe={'mode':row['mode'],'process_set':row['process_set'],'trace_samples':len(row.get('cpu_trace',[]))}\n"
        "  for key in ('elapsed_s','audio_s','cpu_time_s','cpu_window_s','cpu_avg_cores','cpu_percent','cpu_samples','cpu_processes'):\n"
        '   if key in row:safe[key]=number(row[key])\n'
        "  safe['cpu_incomplete']=row.get('cpu_incomplete') is not False;companions.append(safe)\n"
        ' effects={}\n'
        ' for mode in MODES[1:]:\n'
        "  ratios=[x['original']/x[mode] for x in paired.values() if 'original' in x and mode in x and x[mode]>0]\n"
        '  clusters=[]\n'
        '  for process_set in (1,2,3):\n'
        "   values=[x['original']/x[mode] for (s,p),x in paired.items() if s==process_set and 'original' in x and mode in x and x[mode]>0]\n"
        "   if values:clusters.append({'process_set':process_set,'pairs':len(values),'geomean_speedup':statistics.geometric_mean(values)})\n"
        "  effects[mode]={'paired_observations':len(ratios),'geomean_speedup':statistics.geometric_mean(ratios) if ratios else None,'process_set_effects':clusters}\n"
        " return {'schema':'runtime-confirmation-numeric-v1','status':result['status'],'revectorization':result['revectorization'],'primary_unsampled':True,'actual_core_dispatch':dispatch,'trials':rows,'paired_effects':effects,'cpu_companion_count':len(result.get('cpu_companion',[])),'cpu_companion':companions,'cpu_coverage_complete':len(companions)==9 and all(not c['cpu_incomplete'] and c['trace_samples']>0 for c in companions),'full_cpu_traces_retained_in_result_json':True,'private_temporary_directory_deleted':result.get('private_temporary_directory_deleted') is True}\n"
        '\n'
        'def main():\n'
        " p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);a=p.parse_args();result=json.loads(a.input.read_text());safe=summarize(result)\n"
        " print('RUNTIME_CONFIRMATION_NUMERIC_BEGIN');print(json.dumps(safe,separators=(',',':'),allow_nan=False));print('RUNTIME_CONFIRMATION_NUMERIC_END')\n"
        " meta={k:result.get(k) for k in ['schema','status','revectorization','seed','schedule','manifest_sha256','harness_sha256','validator_sha256','research_harness_sha256','environment','diagnostics','query_sha256','initial_host','notes','error_type','private_temporary_directory_deleted']}\n"
        " print('RUNTIME_CONFIRMATION_METADATA '+json.dumps(meta,allow_nan=False))\n"
        " for mode,proof in result.get('provenance',{}).items():\n"
        "  if mode not in MODES:raise ValueError('Unknown provenance mode')\n"
        "  print('RUNTIME_CONFIRMATION_BUILD '+json.dumps({'mode':mode,'proof':proof},allow_nan=False))\n"
        " for group in result.get('process_sets',[]):\n"
        "  print('RUNTIME_CONFIRMATION_PROCESS_SET '+json.dumps({k:group.get(k) for k in ['id','warmups','checks','runtime','sentinels']},allow_nan=False))\n"
        " for row in result.get('trials',[]):\n"
        "  print('RUNTIME_CONFIRMATION_TRIAL '+json.dumps({k:row.get(k) for k in ['process_set','pair','mode','pair_order','elapsed_s','audio_s','before','after','resource_observations']},allow_nan=False))\n"
        " for row in result.get('cpu_companion',[]):\n"
        "  print('RUNTIME_CONFIRMATION_CPU_TRACE '+json.dumps({k:row.get(k) for k in ['process_set','mode','elapsed_s','audio_s','cpu_time_s','cpu_window_s','cpu_avg_cores','cpu_percent','cpu_samples','cpu_processes','cpu_incomplete','cpu_trace','before','after','resource_observations']},allow_nan=False))\n"
        "if __name__=='__main__':main()\n"
    ),
    # SHA-256: c2753086a8d5fae75940a3edeee4d7f94c66c47be73c32459592f86eb1a0629b
    'kernel/dispatch_probe.c': (
        '// Link this diagnostic helper against the actual ORT bundled archive.\n'
        "// Never infer browser dispatch from Node's absent self.navigator.\n"
        '#include <math.h>\n'
        '#include <stdio.h>\n'
        '#include <emscripten/emscripten.h>\n'
        '#include "xnnpack/config.h"\n'
        '#include "xnnpack/gemm.h"\n'
        '#include "xnnpack/igemm.h"\n'
        'EM_JS(int, probe_navigator_concurrency, (void), {\n'
        " return typeof self !== 'undefined' && self.navigator ? self.navigator.hardwareConcurrency : -1;\n"
        '});\n'
        'EMSCRIPTEN_KEEPALIVE int probe_xnn_f32_dispatch(void) {\n'
        ' const struct xnn_hardware_config* hw=xnn_init_hardware_config();\n'
        ' const struct xnn_gemm_config* config=xnn_init_f32_gemm_config();\n'
        ' if(!hw||!config)return -1;\n'
        ' int loadsplat=0,splat=0,total=0;\n'
        '#define TEST(kind,activation,prefix,m,arch) do { \\\n'
        ' xnn_##kind##_ukernel_fn f=config->activation.kind[((m)-1)].function[0]; \\\n'
        ' loadsplat+=(f==(xnn_##kind##_ukernel_fn)xnn_f32_##kind##_##prefix##ukernel_##m##x8__wasmsimd##arch##_loadsplat); \\\n'
        ' splat+=(f==(xnn_##kind##_ukernel_fn)xnn_f32_##kind##_##prefix##ukernel_##m##x8__wasmsimd##arch##_splat); \\\n'
        ' total++; \\\n'
        '} while(0)\n'
        '#define BOTH(kind,m) TEST(kind,linear,,m,); TEST(kind,minmax,minmax_,m,_x86); TEST(kind,relu,relu_,m,)\n'
        ' BOTH(gemm,1);BOTH(gemm,4);BOTH(igemm,1);BOTH(igemm,4);\n'
        '#undef BOTH\n'
        '#undef TEST\n'
        ' printf("XNN_DISPATCH {\\"is_x86\\":%d,\\"navigatorHardwareConcurrency\\":%d,\\"mr\\":%u,\\"nr\\":%u,\\"loadsplatPointers\\":%d,\\"splatPointers\\":%d,\\"checkedPointers\\":%d,\\"relaxedSimd\\":%d}\\n",hw->is_x86,probe_navigator_concurrency(),config->mr,config->nr,loadsplat,splat,total,XNN_ARCH_WASMRELAXEDSIMD);\n'
        ' return loadsplat==total ? 1 : splat==total ? 0 : -2;\n'
        '}\n'
        '#ifdef DISPATCH_PROBE_MAIN\n'
        'int main(void){return probe_xnn_f32_dispatch()<0?1:0;}\n'
        '#endif\n'
    ),
    # SHA-256: 183468c51a29746ae50871e2f637ab2fc4c941294358b4531f78c854ea067647
    'kernel/make_archive_variants.py': (
        '#!/usr/bin/env python3\n'
        '"""Isolated experimental IR edit; not a source rebuild or published runtime.\n'
        'Requires the matching LLVM toolchain from Emscripten 4.0.8.\n'
        '"""\n'
        'import argparse,difflib,hashlib,json,pathlib,re,shutil,subprocess\n'
        "p=argparse.ArgumentParser();p.add_argument('--archive',type=pathlib.Path,required=True);p.add_argument('--sdk',type=pathlib.Path,required=True);p.add_argument('--out',type=pathlib.Path,required=True);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)\n"
        "clang=str(a.sdk/'upstream/bin/clang'); ar=str(a.sdk/'upstream/bin/llvm-ar')\n"
        'def run(*args,**kw):return subprocess.run(args,check=True,**kw)\n'
        'def digest(b):return hashlib.sha256(b).hexdigest()\n'
        'def filehash(p):\n'
        ' h=hashlib.sha256()\n'
        " with p.open('rb') as f:\n"
        "  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)\n"
        ' return h.hexdigest()\n'
        'def members(path):\n'
        " result=[];names=b''\n"
        " with path.open('rb') as f:\n"
        "  assert f.read(8)==b'!<arch>\\n'\n"
        '  while header:=f.read(60):\n'
        "   assert len(header)==60 and header[-2:]==b'`\\n'\n"
        '   name=header[:16].decode().strip();size=int(header[48:58]);data=f.read(size)\n'
        '   assert len(data)==size\n'
        '   if size%2:f.read(1)\n'
        "   if name=='//':names=data;continue\n"
        "   if name in ('/','/SYM64/'):continue\n"
        "   if name.startswith('/'):\n"
        "    offset=int(name[1:]);name=names[offset:names.index(b'/\\n',offset)].decode()\n"
        "   else:name=name.removesuffix('/')\n"
        "   result.append({'name':name,'size':size,'sha256':digest(data)})\n"
        ' return result\n'
        "expected_archive_sha256='407990bee0eb36e4da3500b2cd8d7738b6a6a99de464147ad6fae175969145bd'\n"
        "assert filehash(a.archive)==expected_archive_sha256,'Archive is not the verified original release'\n"
        'sdk_version=(a.sdk/\'upstream/emscripten/emscripten-version.txt\').read_text().strip().strip(\'"\')\n'
        "assert sdk_version=='4.0.8',f'Unexpected Emscripten: {sdk_version}'\n"
        "toolchain={'emscripten_version':sdk_version}\n"
        "for tool,path in [('clang',clang),('llvm_ar',ar)]:\n"
        " toolchain[tool]={'version':subprocess.check_output([path,'--version'],text=True).strip(),'sha256':filehash(pathlib.Path(path))}\n"
        "original_members=members(a.archive);indices=[i for i,m in enumerate(original_members) if m['name']=='gemm-config.c.o'];assert len(indices)==1;idx=indices[0]\n"
        "raw=a.out/'original-gemm-config.c.o'\n"
        "with raw.open('wb') as f:run(ar,'p',str(a.archive),'gemm-config.c.o',stdout=f)\n"
        "assert filehash(raw)==original_members[idx]['sha256']\n"
        "assert filehash(raw)=='a8fdbbe614d51cc201068c79bcfbcaeab215ef8202396bcdaf8b4b0acc50b7ff','Unexpected original dispatcher'\n"
        "source=a.out/'original.ll';run(clang,'-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',str(raw),'-o',str(source))\n"
        "s=source.read_text();assert toolchain['clang']['version'].splitlines()[0] in s,'LLVM producer/toolchain mismatch'\n"
        "func=re.search(r'define internal void @init_f32_gemm_config\\(\\).*?\\n}',s,re.S);assert func\n"
        "anchor='  %7 = icmp sgt i32 %6, 4';assert s.count(anchor)==1 and anchor in func.group()\n"
        "assert re.search(r'%6 = tail call i32 @hardware_concurrency\\(\\)',func.group())\n"
        "selects=re.findall(r'  %\\d+ = select i1 %7, ptr @(\\w+), ptr @(\\w+)',func.group());assert len(selects)==12\n"
        "for left,right in selects:assert left.endswith('_loadsplat') and right==left.removesuffix('_loadsplat')+'_splat'\n"
        "assert '+simd128' in s and '+atomics' in s and '+exception-handling' in s\n"
        "assert '+relaxed-simd' not in s\n"
        "normalize=lambda t:'\\n'.join(t.splitlines()[1:])\n"
        'reports=[]\n'
        "for name,replacement in [('auto-roundtrip',anchor),('loadsplat','  %7 = icmp eq i32 0, 0'),('splat','  %7 = icmp eq i32 0, 1')]:\n"
        " dest=a.out/name;dest.mkdir(exist_ok=True);ll=dest/'gemm-config.ll';bc=dest/'gemm-config.c.o';roundtrip=dest/'verified.ll';archive=dest/'libonnxruntime_webassembly.a'\n"
        ' text=s.replace(anchor,replacement);ll.write_text(text)\n'
        " run(clang,'-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm-bc','-x','ir',str(ll),'-o',str(bc))\n"
        " run(clang,'-cc1','-triple','wasm32-unknown-emscripten','-emit-llvm','-x','ir',str(bc),'-o',str(roundtrip))\n"
        " assert normalize(roundtrip.read_text())==normalize(text),'Unexpected IR changes during assembly'\n"
        " diff=''.join(difflib.unified_diff(s.splitlines(True),text.splitlines(True),fromfile='original.ll',tofile=name+'.ll'));(dest/'dispatch-only.diff').write_text(diff)\n"
        " shutil.copyfile(a.archive,archive);run(ar,'rs',str(archive),str(bc))\n"
        ' new=members(archive);assert len(new)==len(original_members)\n'
        ' changed=[i for i,(m,n) in enumerate(zip(original_members,new)) if m!=n]\n'
        ' assert changed==[idx],changed\n'
        " assert [m['name'] for m in new]==[m['name'] for m in original_members]\n"
        " report={'variant':name,'toolchain':toolchain,'archive_symbol_index_rebuilt':True,'experimental_method':'LLVM IR dispatch-only edit; not source rebuild','original_archive':str(a.archive),'original_archive_sha256':filehash(a.archive),'variant_archive_sha256':filehash(archive),'archive_member_count':len(new),'changed_member_count':1,'changed_member':{'index':idx,'before':original_members[idx],'after':new[idx]},'condition_before':anchor.strip(),'condition_after':replacement.strip(),'selected_pointer_pairs':selects,'other_member_payloads_byte_identical':True,'roundtrip_ir_exact_except_module_id':True,'target_features_preserved':True,'cpuinfo_and_pthreadpool_unchanged':True,'math_operations_unchanged':True,'source_revision':'fe98e0b93565382648129271381c14d6205255e3','original_builder_revision':'72a65470c9873e073d137cef11bb6c9389e62d6a'}\n"
        " (dest/'PROVENANCE.json').write_text(json.dumps(report,indent=2)+'\\n');reports.append(report);print(name,report['variant_archive_sha256'])\n"
        "(a.out/'original-member-manifest.json').write_text(json.dumps(original_members,indent=2)+'\\n')\n"
        "(a.out/'VARIANTS.json').write_text(json.dumps(reports,indent=2)+'\\n')\n"
    ),
}
# END READABLE RESEARCH SOURCE CATALOGUE


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Interrupted. A completed report was not written.")
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(f"Error: {error}") from error
