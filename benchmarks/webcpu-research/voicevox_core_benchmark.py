#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.2", "cmake==4.4.3", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2", "numpy==2.3.5", "matplotlib==3.10.8", "pillow==12.3.0"]
# ///
"""Reproducible VOICEVOX CORE CPU/browser benchmark (one-file distribution).

Run: uv run benchmark.py [--threads 4]
Four confirmed modes: native CPU, browser single-thread, browser multi-thread,
and the accepted optimized browser configuration (same --threads N).
Run: uv run voicevox_core_benchmark.py --threads 2 --seed 1729
Supported complete comparison: Linux x86_64, including x86_64 WSL.
--plan and --self-test perform no downloads, builds, browser or model execution.

The first run downloads pinned Rust/Emscripten, models, runtimes and Chromium,
then builds CORE; allow several GB and several minutes. A native C/C++ compiler (cc and c++), git, and patch are required. Linux also needs Chromium's system libraries; if missing,
run: uv run --with playwright==1.63.0 python -m playwright install-deps chromium
That system-package step may require administrator permission. Later runs reuse
checked downloads and builds.

Outputs: standalone HTML with CPU time-series/charts/collapsed CSV, plus raw scalar JSON.
The optimized mode is a complete configuration comparison, not an isolated kernel effect.
Matched pristine-XNN correctness controls are untimed. No audio/model/tensors are published.
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
        for check in [checks["reference_repeat"], *checks["modes"].values(), *optional_checks]:
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
            if sorted(row["trial"] for row in trials) != list(range(1, TRIALS_PER_BLOCK + 1)):
                raise ValueError(f"Incomplete block: {mode} / {block}")
            positions = sorted(row["order"] for row in trials)
            if positions != list(range(positions[0], positions[0] + TRIALS_PER_BLOCK)):
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
                 log: list[dict[str, Any]], *, balanced: bool = False) -> list[Trial]:
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
       const response=await fetch('/'+file);if(!response.ok)throw new Error('HTTP '+response.status+' '+file);
       Module.FS.writeFile('/'+file,new Uint8Array(await response.arrayBuffer()));
      }
      if(Module._bench_init(configuredThreads,Number(data.spin_off),Number(data.fixed_shape),data.xnn_threads||0,Number(data.profile))!==0)throw new Error('CORE initialization failed');
      Module.FS.unlink('/sample.vvm');Module.FS.unlink('/query.json');ready=true;
      const pthreads=threaded ? Module.PThread.runningWorkers.length : 0;
      if(threaded && configuredThreads>1 && pthreads<configuredThreads-1)
       throw new Error('ORT did not create the requested inference pthreads');
      postMessage({ready:true,threads:configuredThreads,shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads_created:pthreads,spin_off:Boolean(Module._bench_spin_off()),fixed_length:Module._bench_fixed_length(),fixed_matches:Module._bench_fixed_matches(),xnn_threads:Module._bench_xnn_threads(),xnn_sessions:Module._bench_xnn_sessions()});
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
  }else if(data.command==='resource-state'){
   if(!ready)throw new Error('CORE not ready');
   postMessage({wasm_bytes:Module.HEAPU8.buffer.byteLength,pthreads:threaded ? Module.PThread.runningWorkers.length : 0});
  }else if(data.command==='dispatch'){
   if(!ready)throw new Error('CORE not ready');
   postMessage(verifyActualCoreDispatch(Module,wasmStdout,data.expected));
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
                 *, fixed_shape: bool = False, spin_off: bool = False, xnn_threads: int = 0, profile: bool = False):
        self.page = browser.new_page()
        self.page.set_default_timeout(600_000)
        self.page.goto(url, wait_until="load")
        message = self.page.evaluate("data => request(data)", {"command": "init", "module": module, "threads": threads, "threaded": threaded, "fixed_shape": fixed_shape, "spin_off": spin_off, "xnn_threads": xnn_threads, "profile": profile})
        if not message.get("ready") or message.get("threads") != threads or message.get("shared_memory") != threaded or message.get("spin_off") != spin_off:
            self.page.close()
            raise RuntimeError("Browser did not confirm the required thread/shared-memory configuration")
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
        options["args"] = ["--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if config["enable"] else "--js-flags=--trace-wasm-revectorize"]
        browser = playwright.chromium.launch(**options)
        runner = None
        try:
            info = browser_engine_info(browser)
            info["requested_js_flags"] = options["args"]
            xnnpack = config.get("xnnpack", False)
            runner = BrowserRunner(browser, config["url"], "/xnnpack/voicevox_benchmark.js" if xnnpack else "/mt/voicevox_benchmark.js", config["threads"], True,
                                   config["style"], Path(config["wav"]), fixed_shape=xnnpack, spin_off=xnnpack,
                                   xnn_threads=config["threads"] if xnnpack else 0, profile=xnnpack)
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
        nodes = [int(value) for value in re.findall(r"Decided to vectorize, ([1-9][0-9]*) revectorizable nodes", output)]
        rejected = bool(re.search(r"(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|Error:.*(?:wasm-revectorize|trace-wasm-revectorize)", output, re.I))
        info = json.loads(result_path.read_text("utf-8"))
        expected = "--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if enable else "--js-flags=--trace-wasm-revectorize"
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


def run_benchmark(args: argparse.Namespace) -> None:
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
    urls = {} if args.backend_experiments == "v8" else {
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
                    trials = run_schedule(modes, args.seed, measured_synthesis, log, balanced=args.backend_experiments)
                    result = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials],
                              "environment": environment, "audio_s": durations[0], "style_id": args.style_id, "seed": args.seed, "log": log,
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
    parser.add_argument("--experiments", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--backend-experiments", nargs="?", const="all", choices=("all", "v8"), help=argparse.SUPPRESS)
    parser.add_argument("--experimental-threads", type=positive_int, default=4, help=argparse.SUPPRESS)
    parser.add_argument("--threaded-ort-archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--threaded-ort-url", help=argparse.SUPPRESS)
    parser.add_argument("--xnnpack-ort-archive", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--xnnpack-ort-url", help=argparse.SUPPRESS)
    parser.add_argument("--browser-path", type=Path, help="optional installed Chromium executable")
    parser.add_argument("--headed", action="store_true", help="show the otherwise identical browser runner")
    parser.add_argument("--plan", action="store_true", help="show the exact offline run plan; no download/build/browser/model execution")
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
    print("Self-test passed. No benchmark measurements were made.")


def main() -> None:
    args = argument_parser().parse_args()
    if args.diagnostic_config:
        raise ValueError("Exploratory revectorization diagnostics are closed in this final comparison")
    if args.plan:
        print(json.dumps(final_plan(args), ensure_ascii=False, indent=2))
        return
    if args.output.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("--output must end with .html or .htm")
    if args.self_test:
        self_test()
        return
    if args.report_from:
        result = json.loads(args.report_from.read_text("utf-8"))
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
    run_benchmark(args)

# Embedded accepted source bundle; file digests are checked before imports.
RECIPE_MANIFEST = {'source-proof/vendor/proof_helpers.py': 'a82e8a22ddfa42b37d4799905b192f51f0ed8daa5b68f5b2e811727f7b2e0ff1', 'source-proof/tools/cumulative_source_proof.py': '2a486dadb0fc1ce4cab607779cc94d7dd5a5ca737bfbda15da089d445ef40052', 'source-proof/tools/cumulative-contract.json': '2e00a2ba5bcf91046d5bb7200eeb312dcfcbae33fab886f2711b63505c1f061b', 'source-proof/tools/input-pins.json': 'd211b06176d6052d7c9d6c98cc18a05e152668c2d5ad330b1b0d6e04a616c9f2', 'source-proof/tools/tokenized-compile-commands.json': 'b8068214e073eb2757f9a6ca2bde87145834e7985e0e8742e2661ce87ee5cf1d', 'vendor/source/prepare_inplace_leaky_source.py': '455315f69283a223538a4d8f6a3c624011008a3bb0e5cb53ab5aa787a2a79afe', 'vendor/source/source_proof.py': '5209cad45e355afb42eba8f199e1c1639aaf7834ce0127558bc7447e611d9fdd', 'vendor/source/callback_discovery.py': 'e9472d5115159ce9e3293a1ac82352a8e24841c1fdae709efa96315935027977', 'vendor/source/source_pins.json': '8cae1f6e8870e22cf29ce45bd539227e3bf48ad64dcfb41d7ed2a947c0e871cc', 'vendor/source/inplace_leaky.patch': '154698ab9012a18c8183e0d92a9feac0d39da53f484dd19eaf00a20ad3cf77cd', 'vendor/source/xnnpack-static-dependencies.patch': '5af16bc0cd746b6bc666b225972cbae3ad524d4ae22b36302bdfbf1270458b2a', 'vendor/source/base/voicevox_webcpu_research.py': 'd35499049d49bd8bdfef62f9a4ba788da6b36066ef324ce722833d41c1fb5dfe', 'vendor/source/base/runtime/prepare_vocoder_runtime.py': '6b2a20253fc4b90a1d572e701d46c8cbab48870dfb12b9781cea047007c17227', 'vendor/source/base/kernel/dispatch_probe.c': 'c2753086a8d5fae75940a3edeee4d7f94c66c47be73c32459592f86eb1a0629b', 'vendor/toolchain/affine_toolchain.py': 'a1650fe45ab15e5c9eb3e7883b296d5d72b44c1ae05ffeb37c9d0ef4ae972efd', 'accepted_builder.py': '201a070617b51ed8d54f4f61aaa2e8ea5bbef690396ec455f4e6d666a2da3de8'}
RECIPE_PAYLOAD_SHA256 = '4c4258a07615a9b1594599afae6716bf0321d856373a7410303382829d455e6e'
RECIPE_PAYLOAD = (
    'ABzY8000000t4*5d3Pf>k}ukS>r-%Q-W*akMdiMzpYBY#EZdr??Wp8lo~)Y(nTeFxMUfmPS&QxZ?)Mj1GLZ`<%69dsd6zofE-4cU1c1OV76bw;'
    '%lLB5^CaC&XE|R@mLLE0NB>y(2|Q2yi=XDdwiEicub;{1wjJ2kp9a>S{s}*ua?Ms~{!jmR{ycf|<js0D&DH~^IAL&cd^uS0Y`WwFmNPtB4T{BT'
    'u)gI3jMY46v&EbbzMq!sLAJPCvemR)%qIs2msEhyct({hH|cCz-r~1gK3notN#A}Pq#xJ(Xqxl+dYZA>WN>*qEvr^md{gq08agN!n^ne-ss8DV'
    'ua3_zj@c@^oxbCP?3QP5%kkj-bbY(ntOwJ(<zls-&Tj@812xl|`Cz%2O|y?<RD3rr%jsf1K0pUn8@yR9-tjq`XM8evIzZbiPK>`6J(P`7nME;J'
    '%x53}EdDD7gsu2sHl4ra`Q+g29bbJM@DHGQ&So?ScNmtz%|Z~rT5N7^#b}|2pnX^l(#2+;V<-gaSZknUa)6OPC{~NRL6Q`k^=8GBMD=CB=JUmx'
    'twDHspdXcM{cE<EuhIByn$}O2ANB9Ith_Z|$$#)*a>v&g=r!xU*{r9t>gl{(Vg~h}|G?Dh-xsB%cgfb&f+qGg{?tX5Gq%P^-s!(rT>l25_=oxr'
    'v&r&f)yp-%TNapb+0gpq5+kZ!y}`dT{*vACa>+7&aB%VJ)#c!H@Dj{;@cQEX&E@&av*hW;vmeg?a+bXL;i=;Xcs=y2FpjO1bBm{Lz`dNgzGbCO'
    'mPdIQx=|XifW>jny}+}*kmW(axE=a&5Zj)g=EH+m7w6xfzf4{~efD2Zzc;J)11}GpA`LlbMUm(DI1S?>_CmB3xEbTQm%Bj}Gb_wdnZ=6C_g&Ak'
    'xRv@js=oN~<>mQ{GgS7=!2mzQ&1yD084PdN>t%Uze0&2Q-=q_8*zph3)uOzertEkzpML-ct*3YVNTK%e3hRzx@f_!i_w(6;<&9GBS$TI<5=Ui6'
    '<@7E;0+(_Ijy(D>pJO!N9@!JeojAYgqsjW_=iykAPjffN6j2|3Z(Zdr!#Gn-VPHp2=((xwGuP%8#?<y)2NRe&amd1myEzLy#&XvWLf0yI?D}C4'
    '$1W>;<}h6&flrAFJtqpSDDcJypANqH@#VMA&yw%XpP#*<X&6cnI1(Um46Mi(t7Gv;qJRt%5_SAy(8nT&N6c~ZykMS_x?%2t=VHrG?bs=NyRbND'
    'Wz0{5sPI!1w(Zafix9tBMPU!eHEmHGhoA6wgN*fJF)NR=&D{p;3mBNlw~2V$rN?1j6y#t=tH|t(dl^duD-6Rp%VIAN^W0~C#zHqti!^7ppEE0p'
    'bI<d6;aQgNnDoSW{2)C?1W7BFttW&RZECn>F(*auvm&-FFUb8g4IRtkSdg}pXGNAW?z#m_qbMkx(6+I7e9O-;aDkng)VOx!ZlPv6UvAb%OW;=@'
    '5xIj}QCk?~0eYB+S)2!19A%juG0W$+?*u`VIT)JUb*&VI^MG3(3v3iC8be}xf#YnUV!e3F=hL6T*h+LADe2HlRT^1A<k%j!LYJpdgu)_bL59)F'
    'c@zRMBG=<#9QgpRDD)f-A~B&60LRY?J2$Dq*Pi3WM3>@PB}11h4jIi84A2a~nE)u>epDRP<sA<IW&1@CJCO^Pc6~QuULF+zbF;wlpqg1$#N0Hs'
    'Ktk%fEcF>yV2EWF#;o9t*$RWm?-F!B^ZJezXDkOZy1vhf6unMaRM>IM?aU6`n6V<nxMW<Yj((J8q34A>u<bks2sdWRb)%?D0LkSPpp)d&GFy-^'
    '?~=iR;@}3`hH@HbJm#(gII&q4VN4tramR~15LD!hhgQrB76%wO0FUK_aTuCp0B^kQ!?1;8BZv>X2zUgn;*OIQ4#*&Z<h!xsgxpOFFM_o4uz*s}'
    '&O@F%3`A$dDt4ANcq4M+!0wXJV2KIjdUo5Q!uGu&j#wIFu$diY5f&f-I7U+iXBnV4&YACm6fe*17;=ynyM;xR6=9e)XTuAFE)7}$DCv;%C0_G+'
    'Hsxh6Mc@fLNHZ(TLoZ12TMz`P<M?st2pnO#?|8Y#FlLbVu7$N!qy<>T!g5MU7#Ls+<)BMZ3PF1OZZXaHyTym(Jx{acCRy>4lLp%56bKy;VDAAz'
    '<1~-Z950|1#LP>vFo633=&*u-7p?=Y4IQj3Hy6AEC7`)t?AVQBZwpCEm{tq=-6C5+_9yCjmokhC=EA}BWFBUa*|{G&;8r{Lf-J&lr5<q?#>aNj'
    'IE(--%!2Y_g&E|N)8Jt{@SSK2VQ--a^Vu<0lbA4C*(sl7Eow5XRu^KIMY#_Z3mTSZF_-6@Ln#CBd6>z}3sWAt8Ai&FeF)De;Hk~5z=_i)HL+z7'
    'L)e?$vg!O7df0T%lj=c_utrayA$UH6IspueA%!^R$%QTf(UgZy>Uo*XxaAiG-jSrRAQ%}3hw-Ap(U|z?)2D-joEK1a|9dlC@zLV#cmN%>WH<cq'
    'MCcvUVgPl1uy`x~q@NX=mVEFRHrw#C)oQUC=`VC$EKdj%Mx<-2YG5y1@P9xzALN@Q>>IY`gSY%+c|2dt(djAvgcv3ZZ4HZqLk;JHcWgF=&QHdX'
    'Fvv1tfRz)nJ*ZA<3?|d69IY1b%fp($cSPFhFQ2LhWFMimF`Bu9kuae0b#c)rH2^3j`inxM7L3=o6~X`c=G9B8XL#87RusI#^XpS;pdKd<!${Rr'
    '5j>rXZY8?6Bk}t1p3xn?tcFMspubd9s8(peC!{-<BeH4a*iXb<h*29Iju$CSKq7`Lxm_&Yo{C?`SdS&I2s&+3sHPQ6Xin%Y^i5^0O(cJgBt2x='
    '4h#|&fW*;ol@1SK2EvX8Np~lWnMr4h>@AHry0{w6*d6q`6ID|5U}Rg4H~4Bm{~V47>2P@1oW0x0W(iK=BT-djTy7_~{6jvy;pKXydZPX^2VNI$'
    'c1VnAzPVeLBgG6QjJbsP_VjWk{w6SE>&2=(9Sz5{x=x0NhZ9HufZGVabs8rl19+S*R=GKxiU4D+SL1CgSz}m9;*98@q=d$%mhs&p-^{ouF&>a9'
    'qiTndI?18sChu1mW5EzaAf}MIA&bCp#@|i5dO}YVGG`M(H%3<&_9Lo$ENp2x*u;1|?|v@{k~L6WBl^!_MU#A8UQu<|_*zy|(IfbouO|Ehs8D^A'
    'MH*AAz7iuKJ6NI5cwo*lJwYq=C{|A=B$MXr$=%z0x*Ewp^c?dKQ;afJ3;n4ZmS{mpAwHj$N%`@PTr4A+uM1vmO2RGmaIn~{q2LdV1|^~73fY0p'
    'rleG?Po2ZT*Ms3t^P%D;R+fBSj?(Fztv;$g%0F~3)hm-|nx!lGe5^`OXw4?&2IA+#XgDEzcX)W+=qZ%W(UQ7Jd@#aNp63E5myNEA<-8OKs~V%x'
    'c=qbz4A6D_SJbOUVjx^w^BXlH1-Y0&S4HC2VhDJyI-sZLX0}L2!>=aq7b}QuD4tks9Ww7DOEg3@y-^cGthHIQbjIaqHbM25<%C%Cq`7L_^{K_D'
    'H5;;-6#J2*52S*J=I_SJLpPQ#x-oh5OyMY;A%myKmsq%DmV!+_3OpBMEnI)XFIW2}*Y6iccl<8ps{t&5C9eb{koz4*Q>_@`YC0b5<{QA!B!;Q3'
    '+?Kk88XeJE{y+;4n*FV&WTo=8ZYRgkfe*%zoXe8<wpzO7)!S<lZ{=e4j;k3KGGsKwSHsG_rQhh|ik}yYbtzpXD0U*7&}C0#V?$Go$3yy_K$s@T'
    '1mW^(sO;oK+sQ+dD8;o>4FRq|_%6iKB%Yx0lALlxYUr?oE7b=R0YDJopiZngHvc%P-;&TI&^i=sZ90KfZRMcAe{Khh_j62I1BvQdB=Ge>-ExIT'
    'q?NY86TMg5(dcxeA3}zi6&wyuQN=cY=c!sElh@}j-&_r=FO`i@aifxv*XZ!8)%0dcer)AK9Mq0>HoxJy2AI|2XFgAao23*$qjl7Kc(?78(S2jN'
    'aIAxgprf?<*Mf@1yH5vN<B{T%e^|=x1_Bb3z^h?p0tmbXThTP%fd^{6N6iNq801rh2W?%pqiWCy>qaqlucve3h^j3?x*j+f8$e;9Ll_myu3w3M'
    'Q;j6Fb&buLHz#s7dv_<6$aFot7SdreA6-exhnuC;^o{}b$Jq>x)0#axdI!9LssZN0pU<EF<;6ffX-#c!ix{i?ZBkXSwM~@ER@3F0&++TmU-z_p'
    'R+V_5eL(!=hObA%vlq{vCC|Ql{`C7d!@~|!qu6w{nBM^y67t@xra8^;RpT?_>@yf}nGgh}+~U&PNrPg>Zpt=;(-zU<J$1Cv+zp>|D5wC|TXI>i'
    'l6ntmc->(pvAEUum{vynK4e^BE#eK4CGhjmWC>y~eA;2Q4m;|iST|5@PKaD#G->OMXus0>Z>98JlcoH<?!`d%f{;-448Ms*(;AQM>?+A<U>lw`'
    'R%*j+5+xTJW=@50ooFNi=uZpM?)CRgo|wV((Vq%dust_IfBp&%xY2T6iSP`n#Ja^(qAmHpYGFL^Z4Y%YVh*{@Hp#NdVkng|-Q1OLyVuolUU&FV'
    'mrLq;5@jIv!E0S=AU8nQA6p|{+x3vs#EJ$>R>L<f#Iz5^Hj5yt+mGwc5LQ)-F?2*ft7ljCech~z-QUmJ#(%h@m9IpPbm#}yjV|5ryF1!dD5f_P'
    '>QQrIWbp>YLiL$Ed-d}2;_0(XP_3L%d_~AmP1QZ~Q-85_epCrePpyjp3-$A%S`t!;t?M%wPbf?J%~-62u_C_KZb_{bRsHMWf*;XJ8bB+jtz|Ao'
    'cLQqE=~~#Im5M8z212`)GU<+5AOnhkf1;{;$x#X&l)5X_h*y6(d-?R`voj%{p&Fs@knU^fF2@C%K^cHJnckIya+9J5>kX!tnDYE$(ijn-BDq1#'
    'zvV~^MG^S(OD(x)Eafw4`3k%m4KIfEmo%PwwvO6da5!%GEX<$L@M)_SwplNZR%BfO{Fld%s_U{<ml$H&-0oImhycyj+MaD2dOc?D3u(le%%{Q+'
    '#PAtVjCC(YPArcLDAA49Ot_^I{jeAM;kdiDh>maT_E)^%P`aT5zh~yen{r`BFfNVohTSVIliAD^Ay66=o+zFLt7vV1B-Vp;Gt6d&P;4^RmFT%}'
    'Z_H#@h1K6%T=8A~NxxV%z7WfAP+^D=@8;@l(SpIh9=8bu_w_uygaRam-%M){XosR1j++>(3_Dpw!MBDQtzJL8b<;5%>ShnOORq-T6*3anHWr9-'
    're(L?s<c@uoc{*2KCK0T-is7{0z!2Jf}v*^RJZ5PcqPXb>felgOh~6{qjD{Vj#RN><C}Uu%u05cm}Ui!^n0~FuDyw+(YeNQRwG2Z_waaAu7s8^'
    'CtT>-Y5i-l`T6HlOZ>hRQ3XfS;aD;f<|BKbpPGH`8k$Ywx)Ys&DQG`|-@`QR9yYzKRAm(SQ4@T@DJu5tIkEEvpKWwCEk@B#ADnAxZ%TK1?a)*&'
    'A&-XIL^&B)7^qFjk90uS5$Pf1wfK%CAYKZM9e-a{ME^3R*d*FcBJQM-R#7i4{>6)`RuQ%}7Zr(8&9VM;{Ye26TQhQJv>XNZx=F-`sCzZ#P19Ug'
    '*0aj0SM4>{{6_zk(1s7`rPq}prv`<|p<3<=LK>uzD0WD(cDV>M1dEg~^3-FpWro#U(nzWv(yw}OmB-~%2bAcC9G0S?ccKyWO;GnvQ}%HpT*9Sv'
    '@BBEgd<JSr>k$VVV^R&NV)lK)mgLeJ{qogU()bc0R>ym2x3e_vs@*c_JFh8GE(j9lWUl?oY2DpV+w~E(NZ9ITbEi1ys?~a<WzlsJa5sU{GF4-J'
    'RW)^esF_WVMPhK>PL7?2;#`{8(qf|1o_2{XgrQ;<l?I>hM4gFOJ3`&k9WRigGtmpE`os`a&lVJ0C^k@AEYU<Cp;L<`b|v*-Zm>U1s#vl7N;2-X'
    'AZWX^*THg1eHvbO%{JAb90!u9cj@$Iv)Gh_XRm)W4J}Q$nCW2+2!xcXk}K<4xwd7v>goL~(z2q(<B71aN5iAVP-&UX$Jwx=)L3UdWa?&`t%%W6'
    'uRyKooJOsqhdfV4c{QiP*R#c#6S24hvk%LUqr*ST=U3vt6WTQTpt}3@z$T;|{vs93Pg>h5moU5KWY+$5HJUZ9fftSkSio2IiS*Xscj6sA8Q>i}'
    'Az`^%m!wmVhX2vtKq7`{w)enj>%s%+0qqe2&FW~(+tQ$0LdG=Av)Lw>J#In~y3qh5)lrkzI(3>l{b9wWV!5`Ia~06Q^>QBV*1DL^>v2W4Hm_f>'
    'Y_(9~;sYIgKB(f;1r;VRD9_<>HL}VbzL={D8Q|b<31++ek?i>a%X2dCsMa^<FTT~zw@gHMi%4#E2Q7RpH>RpqWCV@E9FUk~cX@|bRY1h7E^<}<'
    'R<SOY<Vo*SFtl@Rw4nT#4Yf+sGZ96~VUwVq){KQh>{Zu%*y_vI;G5x5{z53JXX3vfgz%Rh<l~&bJDzV~NM2XkZ}qX%WV=fYCgkGgw6_CCvI7$o'
    '7@DMP<1Sg;pufSPq5|c=%(tte+Qpeq(rnoHrlIyf&O>=vg~8+^0AU0;GPX2?VU9;FyZo^hIcjJb<T_BGYkyb%g0Tn+!XjG{EGY7Hmk-CLA1Gs3'
    'k=klgi205j_{p=U&we=TDz}E?=<Rf#E5{MJe1_@-2e~n%<EA!K8r~VvC$`C_ax)YSkIac1nKMTlH=>=d2XYK0n#kYf9w|*K+M0+O^&k1r7_z=K'
    '`)7K3b<dKa>FoFUTte-um(KKEQEIbxY#I~_KlMaEYQ1b5gP~kFHDRLDNv%mxhja%U@2(nuZR@lsw5@j8-xuEska+Q(>Mdrl)zijTe?*_<%(i-7'
    'xvsIs+D<q5K_g&nF#J~Ugnw-AI+=pH>+CaqUdBQ<n7QPuccefY8|}4uX6}+28(K0<?T)?WjqhkM8vxf1>|2UR5>zkiQ+lr0*(8T1OR-cpYtqv='
    '`HX3P`ZOR6>apGuZM0DJ#@s~H%{C(imjd66;w_-4cu2a*PuuRE){>HuAwo|#y)Rnr6x~_2Mx$rDPPW#hfSmVh+7n)}_k+CVf^x}cGbjb6I$cJB'
    'Uj`NkR213^#M8$?%3Jl_kqJt>&8DCK&A0!hK4b+Qq67C%-%WGsyx5E;P8@(~A==g-Cyg3079z@L#Z>^wdNHbzxy1z%w&#nvP>l4hd9OAu>KNiJ'
    'aaC{CotpbL+^g2a;*t+(!^Rk^d|DKAwosl+YYoV-ubGOKiAn1gdP*8<a$T;q{b`J(Vz_^4bKFU@D#a;V>giq9mFGcjA)n~g48BS#`?gvxTKydl'
    'zWPeG@Tu-yZFe_egRpy&bG{TH-rW8(BYCd3%ce#@jE&3QCcCVqw>9N1xv2CPNyjIF8TWZ*#YVV}Uv0RHn?APYEccJa*IQklq{<Uovsuy{la6dq'
    '&WYXz4tsYiYgs_MmCfpvAh;TK>-(5Y=kI{Dz*p0qai(oS19d0{r)PuTs7mfTRo%zXO^c6aTXIm#Q7@B@`^QsrV?9Pt5o^)*%TS5FX3!6bI&%B<'
    'HAEq1y##D?pACBnLp56n9=69s#;-LVZEs9j9J$_Uu58IfXyiskLK{Hcv^OBNVq<^3cTcK*KQff=di$xl=cF`mngTBNiiGb_3_5KeHo`S}cW6lw'
    's`s>#ZF={(z17;QiI0s<)YhhHO`O3PuzyZ=Q_zJbe%K1sf54YYJE5ZbYKyno&EuY3(nbTH8ct#z)1vy4bga3aPF|sh*W2y9UFxS-{dJ>z*IT!H'
    '4GnQ7cfgtktl}FI;G()etAagST!zlBf(B1@t+Z=CO^rjqbdrh!gzwVX#|az$S%1<nhN(+UOSL!1jl$uM*h&Pc;y<dU>kc^vN2w>~(x@S?vC7n^'
    '#`OInYB?Enx7vFG8ua(t+5f3IMcruY-XKGzW{NOru|2LHh`?~EPv{0@xID{`ggU8qTI(ZGh7jlr^ZFzA|42J)Zrcs=%}PY^8&S1Y!x@LZC?koX'
    'uH7-(PrJdGAL&#SM=HEmZ9u#edz$<mVJaG@l{!2b$`~+RMur#@U#!`B1AK)>0jA53<i>!AMHP^xx%{Nb4Pq-#XsmLPB{Hb&M8=&RboOLw!F{6K'
    'AHz0`QxmO6<H4{UfIw2F^{CriH_oB?nR+;`<WN<2<8Qn-{9kx${MBuA+s?^J|1OLW4U$9EHq5ZrfqO=9HcMLNlg(W7s1%Q+3y{{INE2ecNTjZx'
    'P>8Bniixmrbn#9jtzh6pC^u8H{&tintlD{=R1uPzvi66amYNHL>QHAQ!FN5)Vomcz<`pqEoNuuR7Av7OXyz2TQK~YZ+8f(#ucr=*mz#ESPj0N2'
    'T;qU-sV0cG>a5zFEz8@*S`atVhh1_`h$tAj4p%c`%Inh@->o?RnYQ)t`c5B5WFnjhQw1O#l<bb{O>vQ0MdyE^2sCNl3B1)uGJ0reozWS~zl;Li'
    'vqr|tb}W`Y(85;hX@M16je!pTGd}$dr<}x@1G9DW)WOqsn~iQxOA$aOb{6#ZmD-sY4-)*ZqqghU)y$cbbkKGWU45m?%dN{&K)HS<xu+-2&F}(='
    'nodtuSpM+GdC3Z1Z=YzaukwS`lA0VkjU!6iV(}k7J7vrvI;YM@sjNK}3Fa9k8P_C^*^%KZDql7^?AQq4mm)-X<jew!`ikf+Ga<k3nF+NJ<$JaI'
    '*6gOzM6?gRItf*sKWN8L*URr2x-;NdM3~++7s-dThPGY|D1X!R?oOWCI?@g)9Zf9{x;GbJg9N?o)I+`Q=#@Ia%I1Wm=%I1Ap*rkRsS6$bLsv+g'
    'RM&xNJ#TkHs}~AbXuRsPa8;L2J0ql<=!>v2wpn!zRm8arn3m$M@v`n6v@@7wdD2iH%33#SMm$wLQlE8>tlitgzN1irXY%__K|OTDQngWkvHKY('
    '*?8NOxYqy>-%-M+#Y!Hx-1*bV`fl0nrzD|yYEk1v)y&Rg&^YAP#)xV)RP}as>H1ZNo8QR6s(6{!YQvLRF|a(qao8LxZ3d2Hk5yot@`?2Eyc(J9'
    'EMI?oTD9G!Q=PI9(Q#wRw*z6GD2|m~XdeHRO;fy`{H^viD_^F1eta*h^qccTO=|m8+eve{Rm*ZA34JDM91&N;xTO)jto3Miv|l}Vhg(etafYQT'
    'b#T9vN{Xv3C{i_gHCpNO5>VXgvk=lEU5OJTs>SO=b-GpwBsCmj2~>=%=Si{u*w8Q1%{-s+P7(@~q!YhT?v!Mer);6+C^OSAdbJfu+ZNPrB~}`O'
    'LbZLhbzbim7{*j?TXEu~(-ujhNF?$)M~Jj;q(h0wNvk+j`l{s^RSy&MXo^8dQv^JhiitYTo$}dYep8ZoW@;%3Em^O@yB1Zt$;zg<CN+@|S<Hie'
    'tx{14%ZBpopte%dg->;k8?r*kD6Sr)FtBg$*y?RZZZ(O<1ivi@bUOX`{ff3JRw|)l-ZJ%@G?UG@MO+%)F+^{J23^Yo0$1>aW~tql*ZrNnWu-AO'
    'ExD~~Y8`(6>Z@u&X&4f_bi1O^P?6i>tCOt^M?tf6R5_tJ5$30w7ulj_<-GDc3uJ&!o{Ah3^d%K6N3;`MuX&mAk{0{jrlh4!nxzPpn%{D=Lgi6<'
    '#bASM#>wyl8_ILjV$C0j%8c!B$tS|nqdc5<Vy_e?Aki`zph;9AGuSk3MO3+&ts6chK@|ylbdGG!r<5c{W!e~|ixurfQD_!h&8Fbd3qpdYm7zD$'
    '-PC6_D9&#<RNGQYy>A*Hs^R)+Z5>VL#R3%=*<^I#P7IN1fxZw2U!Q1zc_JXJid+=HI-89~m2b7$dpBs3JRqMm3sPlLa2RK&)dNZzgZ8hU2*dA5'
    'bxgXo{ksG@u1Cd_XEa^8oV%03FXV&!^kh6JW}EW1<q|d)f*eDPX#);uC{*ky$gP4I&nuoXIOIsxM4&_gbPXRSRfHiI^=ah@HIDTx{;SM0E3y<W'
    'W-}H0Efd$)XZ+b*WC@Y{xi(mLK)43x*Qe;tD@@fp_#%-kaiwOAKetwgToF2?g|Pm;s!Y5o2MsJ1)7b@M%6q{{s0#&E9OP^46Prt7vY87BudLuo'
    'TgQm#05~y_;B+~?iyPEkVurt=L8lhv<emt4A1s#ZqZxn4XQ#j5*XiB#XR)|GjR&tTE(bRNncKm0djE^~JrQL;4MxRuMS!uHi;#vjdrS5u(1guk'
    'pwBl@Mh~AHZe<HeWW^cZ(Ucq+>0spOZI=CPp<kZXd|;eKFR{Eb29=vq`w9mQov~ao%FqkN8*I0_S;#sjgJ<|>RnNAXJB6*oQU!3V7fU%v$CL(%'
    '=SL#xMms@Dm7R+VRF&YUT{0Sz5?Dhb&`fqTkqde}X#9!w{e+r$a@b4<Re7+)c0^sF%>MGE%UNGNX)Sl!j^~qNGn<L%@zJX1qtU;g99>yQF*_=*'
    'zdrnT{Q1*QlYf-I;=h#tM*J(X%v6v6QQ|+aw9>^4(>|^N*hsY04y({bOvCL&rTDs#(oZJ&dQ#bP%Ht#XR`QAZ^vU5vZ74LapQ&|pcu=F40HOxi'
    'NcJ079QU<i$U~jiW+C&2i5zSr>7Ny;gJ_`{>A+e^jz?-qoT`5{XrIb|4z=8RE$asdlZg2!Q{6xyWNZemrEb7rn-jYYHcPC}9E<Sd900POW&^#S'
    'QYq!M#Z`y>FXmTzdqy42esG6Ip3RqU`gVTW4alSq&3zrQ=6m;cdJX}Z)u??R`F;Q1&r7MYH3<~Ecx5ZA(ozL*(jauUb*}$B$c^`%4V~-OroPze'
    'DmL>>)B(PrjCAy?wiB0PAOGr}GKPXWe45M`t2+oHI^8l|)w-ds#&~^x(WBxHFYl@0a;F-eCZx){p<|F*Cj0~84Jn^m-!vOi3C+}oO;gEGlvAZW'
    'gP>~$E#m7$rGT$$R}nNas87{Zkx+dlHE@7G76+LE&5s-7Q+=$6mfKFo2uTJLdiSNAb~#g}QARyz)~vI!Clu620y>@kLnbRRzPzW<wXi5Cs|j_?'
    '*jU21J<rX?cip0@R{u`r%@*ptNxgI4T|`D;lZpYlu6NUAA`=m2f`Rzd3QcLgJuzd8de>{!6c#M4Xc1`EiSnuAxau$EcNApOiutNCw%-$F)DxX`'
    '0_;;B96>~XXwr4t3apAz$e;A<um*C7qd||Tup2?O1F`+#SUtqj8m&Sm6KFgMsY7a`)?l!FdDSUx5VY;Xwv}FgjAhJ9`3<oteU~ukwZaM2Zu!xl'
    'b=rM&sFxVV$9{JGsg<x`pws{4J;{HnWBs>mmA{wC^yb-SwGuVnti8HFgdpm=j%2wY$Jk0_HdS2JfcY+p@(-i|BrgOoo{Q-G%ZH1J0ba}nRWh8?'
    'Y=>}PQzA(ds88QuuBuNo8cV)VHj(U5uR!aE%Fejz2-5y!F{eFpUgO-gND5JVVe2mS4zTt&omT5Bzv`cg=JHa_owh->mm(GTv<*@Z8>j4aAe27Y'
    'EbDE?kX-k0o09aan|ET8)bYBcI^fsfH8}->sXMdKAVg0@#Z9fqlhZbV-7~$_n5oE?=67cBi#cs7PgtVPeTid%6{%alzULd0lKR}ZsHSJ>?iAuO'
    'z9*XHH^i}MPN=izZVAPbw#$)u7F?<OI?}3N-L6-6D{!l=-AgH4rIG{_6*9UhTaT2bbk%rgE)Bi&BD$yDx^zxn#8=92$Su1wdRqJ5Hq<7e-2qbP'
    'WFLV1SL6+7x_>F0^BAouM})d#hz-mxRvvN@!#8-n;z!RfU&$E6K~8rFiO2?#t<>;G%|2?o7MylFos&N_=e3WJ_BP7JjGR&`$&=})nHU4BFY7S8'
    '$*tjSmoSIH2%M`lE&b?v+=_^+|7b+KHG<ZRU^N|X-T1pDAMT44s|dQcoY|ZEBCT|7+sj;cBDiW~s-K$er4%AZ-grjzG=jdSa~0B^NQY75CHPBd'
    'W{iA)C76_zn^mI(%{459_M1-!jm<wb(`qNiJm&-J{~HQ@uFfP?S9w*DBI+tHxht(53gj+VyAUc_Smj7C&v%*)HoSfGgd8s-6sp>=*H8O$Wzj)Q'
    '9jc}C#wa6RwxTy-Wb>k`?rBo#1h1oN&5MAxrV3Rlf_meVdQ*H!Q?#28ta);+eF#g8Qg@@+906WtY$;9=su7TLGd%32i(=1Io}zA6XBl;cAu!T-'
    'i)<F^&A+HIlz;Zo^`<=?ic#bjJv7}D@;o-O0jX-#<Zel;`k>^}-XqESLTI(DA))Z;?a>+*doTWKeOsw*3Ocuj37GtK_&_Cv%=A;+cK3a(ZqIA('
    'CML3uq|?$i5ugpdD*20I^+#R&bhxECCCYypQc`x6#a7&#MK_b=ew3wN#M2;(Vk^&sh^|m8oXClytZ<w#8s2}LW2ml>F={S&9C0g+T|e;M$O*j2'
    'c48;A-N?>ty0p#m9pC5fLp94JxOKgr9b1lFFw3(%CrYy*=7sJ1aq2og%kwniHs0ICL-m^JeCdiTm%CX?7lS!z;P_EM_dMA_7O^x8Gaf|Tvz@Gn'
    'hMzQ?Kb&MzI=;ts<X&Hv$qM@E?nV{fi20nCbcx@PG?AmlQf@!$HJRcyrLGkDCg+QRIMFkxP6kxqFWbLAP*zB&PI_47vcE48WaE>@0TLbGw`=ah'
    'o<k63w%OmBSG2hd`f`lQFWMPK#Zm9PcMvFv!@;ik8`L*rUE^V6qf|ZYq;im3-w*V;HpAsMRx2!L%cic8V6v*9f7ebC2VLt<W)H1Iwmgtle=ZNB'
    'wGN++sTbk|T7BMZynCPM_}ukL_lQ(AA4Ej!wPJEJJ?6VRl|)18U5#T=CJ)ZYpzD7{>}kH!CR@b^x+U`>jP4eA)wS`!KHTAGRh>UP%VZ8g8djNy'
    'jkfh%=V&v3JBP7)R3G_n<J}PNrt2edOiQN5km+5fbYx484kdvQV|4xa%p`J9d$Ka9X|B9LCO*oK8bKJw3h(_yjIQ}XkM3hIwvJe=V#;X$bZ~yr'
    'IB#1s_Z6`cd6K)78Fxf<^N~`KakHlOBDik9uh#;G```eRT$Y1x<h8V~SBs2;^s<r$Pf4W^MnJ*e)0vH>EFx~SEot3V)>L)9?|M;Pb9=ie*Msxp'
    'S1RP^8+o4!ThW!dq%5c-4NRO0lBd>mG@i&%(TH=P><RMHDpA>wlJ^FhqNlIV<qm$%XVX-;K2Y6eUT&81JbW{_hh%4x(0)>qjG%TEV?k{tCJmLJ'
    '-cIC8@oRm;vGK)0Rj)`gl}MmabywL)z7iLhoYG~rV}Zg-_SMbR$yBZu<)on5z8$H&GarmpVb_fl_|Og2bHU7>kky?$YWj4>P@kI=#dHZ<?&~gx'
    '{r7pv_-SG}GtC%1Il3bcgz|vYbKA<hU?j~`p~8m!m5g=#u=ri%hY<-&j1jL=#j8Yn%{+0=^<htBbI*?mw4&;Tjj2XN*WzAZ_jdmNBz&sthE(4='
    '3RU=0BNty=a5h3X+mq|`&02leLJv0mSTBRhmZ}#s&}GG1r0TG3&HEGM%IZ<Y&BV(1|6$Gh0=YXZPdRub%h${gvsGMnE!RnB8R*dv$ufG_|LO!&'
    'CBs*5rt<Llayk1b^WO?$)&6g@o!-Hzm|2aDaLI`9A2meAz^qpvo6AEf1gBfZ>VZWp#|Hm52D45~L8^8mxaFjem%70s^q5fp)FxMU4iZn(p-7kn'
    '9b!elcV_yYO0239rOWF(*_ss|9Q;RpBl$%AicZ3mUdh8u+PYPDm5rKSr~l%7IVh$dV5!UVXyX2}azHnw&2+LGt^8DM%-5BrQ8JwiXe@uHJmFJ1'
    '_wZ3%AxihZN!Pn^VVVdQ!NWt-2R|B`H!R5n2JNe2hwALVu6#0`LsYFtmds0~&E`h7Zf#~Xm`Lxrs2iYqKRoWa@u@*#J)ieh(|_abmj2ZeQ2JdS'
    'An~F5h3S$ia>yjwgiQ3HR1K-jv-dXh;H|9PcEqhv4ee5RDR*gThr3q0?dc-{25M9n18r;c{;Ny7ZB(y~Rz6PoEU)Z(qJOMAJFZDkx28#Jij2dV'
    '!Hk-luTDfxwrWLn?*F7fU~!B>7V3IRbz&)5tQ-*PcO(1EmAG6iN1|#f+RD)b;c)NAavSYbHrI4@OIN%YzL6Xg0%=8QW6_IP`Dd?Qyf_z0OD=wV'
    'bD8|*?BdP&tC#fm*^4*d{#W-w>w`D1e!O^g)+dEU_1~PmeD*`~!`buKXLQ|#eE8k@-_E{8MK8ZQ|GxFGx8$qWmx=`AAqYGF?(f4M848GFYhaS>'
    'y#PB1PW@DBvGkO#2f`>d{X8OiQlahLv=oYqbiL9i-9Z$4TC@b_T;%1DMyZaF9n<C@1q}(HPgJIDBX1=yNR~24QM$%)M861IU<7z2DNT4SIlQM^'
    'bc9;(J=3dcN`b|udr1t-^yc=_jnt#GV<VN~NF6$rjrI<(+yuOTge%{+*iSN#bI9)!+I*%Bp+@=xGbBb%i!##g$&xVC71zN(&6`|lv?m6q_utT}'
    '{gX8T#%*0A4N+o`TOVg86?F#Dt}j~6tMkSB&6Qkn_gs>fzwOOqR~J7tw-qQuoH#Ke&s=F8gD$=mx*u&>@<t9h>5{3l-c1bGN9Q1_^4~mk(p2QB'
    '6Gb--HQY?H_e3f~gGKq%T>7HbM$|$nANh1eO4Dc|6dH{6NF-5}h$50ANmtPDC;W)ay2Hbt<}Y7;dzSp?8#<g_S4!C#=a|uB$C}vD#7Y7$ovwvX'
    'h#n7rn$t}}iAW0H`OtJBq2xE`FP~og{p{rfrBM<y`|RTU_2q+QjRb@E>`5bi{cpaGu;-e<tH$IueVeT%QO#8UT4){*H`C2mGu+{U)?QbG{hC>W'
    'yKNtAO{6G3Z{J;b9~B~LBwf=|FbY*uWW41vZSL@gS1&})=I2je3c2<CS@QJxbNcPgo9D^1vx`fyRn>U#?YWR<&(FVkMxQ0GpI-h@C8~c{#U52_'
    'z8xPU8dvuwkoKX<No9O4uVPlK^FA(?4c^Hx8{_mmzdMo(zJ1^u%*RI$Cc7M0byi){QB2g)Q?*`8zMTAHF`Y~ER)QRqnfiptVYSPu5>yhEl+uL`'
    'qlm@zaBefHBjM}qR1a1CG$eJ4)%7w0DCpnoZfgRHPgV8!sDc~uOtG4LOsSm3(`szS^7DiM=u{K1rH$nE-!Ff7_42eiK;waY{Ozlkmw&xDzdZZq'
    '@0VxKsJbC}tDil+`2JP$&5!5LzfJ!0)i-ZWhfY@|BPsUd>x8)Yv@yyn2!)NvUnlG7mJst$?;1&Gi8OV@*^h>}lP(f-zs?MUkk{ZUVOaeEIj?`m'
    '=Q8|hEUtcId3i#|mxX7=P-W%$k9SjY0v!uy@v)FE^8DJx@ufPjC}`EfO&vs9&NijI4mp$7fpXc>b|9ICv`_VZF0<mxF(4sHW_DqE{Kl2**KmQ1'
    'tAiS<0ZyI_5R%J}CeZiEDB?49LP}&p&a%y_gjTfT6YT*Ryd*n9#+FGl`gXP0+}sXS_`m4PYE8104eDSWaUxX?+(3oh=subrVLB3p!Xz~gI(OHo'
    'aTgcMVCcn2+Jpc1R8I)~YFmycPA$r#s@lp_E%)6?DU2<sDzwSkxbgw>w)voLp{xq1aIewfWVU$EDM&`>+aIC;*d`occR7AJ+;*i>b*X>`^->cu'
    'cuwsNWKDySRBmY)|N7>+bJ#<)ay2I6i0l7?Byh(A+iB=l5INH&uMbte{)jo658R3Eh|8Dx`zlC92A_zSi*a=`>{MvGl%iVfw@<u@E8=>m^M8oY'
    '4trv|6Gx<zoThTGik?OjycmA!nxmS0sYsFV)`u!|Z$^p+As^K<vqw@-H?rtyQky!vu0`jn5(9bKj=So_Ldo*PeU(})PJs4=O_|r5NEf)k)b3+^'
    'zfDu7cfG2zWIFMlaPAr5++@ADn+=U)#`-3DrKD`vp-tUcjGss{tU<Jj%#p9U8nk@AH9uE#W@v00O>NIoV<s=6bSl&<DowwS0MRf`Pw7bgk+}YG'
    'bt>yqb0h*DI=)PrIgDL7813kPF?dsWcB%l*(+3<EwYN-|bW&-0gq_v!G5_n}nYjOk=8W23)!`qN%SxS6ZEA)pHj%QlO**OOn-_BbVJdmQlc`VB'
    'Rcl6#Pi;4v2jPFu_1$`_<DpFsx@16jvzjEU@QdHMSy0dav=F+p^0LWn^^IxP+k}f7okOnQ9Vf;Or4HM%1ihPB^rGg*)il2$ylU?zE7yr+`j$_5'
    'BJ>0GyRjv8IuI{poelT!q@h{JCn6O=l0!q08;IiRM#X~3JO$ms9;+wqkHyo*$8E=Plh788RX8h6XIE`MCe=~|LVRsc;$UL6?@4dy0iy6g795EC'
    'SuY4`+k&Sjuu@(VDdlIU1-g6*2ui!8SC-s96eX*QJBilC>-rM-KIi5Os=zqnelL+wk%gzfZ~jIXKfb&?e{ps-+^l9~CoA--9#_c&uZ?8m>SRY%'
    's7i!%Z_KHp<gmU<U02XHJ4c=C)G49wP^+f8OI7BR?iK1PLyL+=?((Q6W?b2#>VbJ5h|DP>K2}mf>X-Gd!^muCJWw%_t&|*<Hl#C-NVhFt>0rce'
    '&R@Ox-YR(AdlzF}ua3#A0#57d^>X^#%a_WD-ZP;3R5sX^#<ho=>Uaz>EX7owENhL!{W<7*B8Xa9rCwj0zqverd6qoAc=p5jU#ciz2oDunSZVyN'
    'j8Z14<{5B3%f>;8o`Rj2vK*G1h(xP5h_qzpiF~QFg_iU;&tO!dV_e6R(E&P$-!++g;<%AHDwMpxkzcfKQ#s0IoA<SKXvVlmQ^crFK1qb(I%o4t'
    '?vRze-wPLtOc)I^Pb7I);*aZUXViEu|Gt(v{mc4A^8jm)L#2D&YW1y5LU3)|E|xT}zO0i=*SV!XbrO@0b^3wRDwbD1kQcscrOlWyJ*_HDb#+O6'
    'ZDA+XhV26hCp!0L8*E7yz)|x?d$VMRFZF6R&g`U`EBddl78|T@`D)H*#x?6^Umx&r_0FOQpIj_%W?VtfQMt;7zqzWV#MdPj8&!0yc7BeH=Mxi+'
    'lJTHAdRMczexzYgf#|5R4(=JD`}ex*VXM=w3Y$U`VQO+c`Mv6LW_)AI$Pg=4?ORtLboZoXz3Pi$r;e8DrXDtwsjiaNQm=G679%3KlM-MwypX3`'
    'TUxiVOVB;Brq0PWuR51G+Y;l$Uxu9=lRd8e2R*Y7<g7dCNhjM*QTM4ss*$%*Wl`*;4%YV|RC{i%9x7CsbP@_T6Yt4D&eLiR%{^b?I;ay#DuH0&'
    'msVKRApqq;J5~Q~(^#6huBAJqN>#2CN8X?Mx;s&aebZeV;5`{K>j8W?Q)crfB%L$5KcRZ13~2tYZr!K>{~`J*)LL?>^y;ci(}%s*vbcI%ejv49'
    '5p!iG1Mj2)Ckv8ZPJmI;Kp71o>coYI6cIi=Jlt}5b$zjNI|mj8a5Zz@swo}zlPx#E>4cX!`9Bo4>{KVsHZe?QzkHy4^L9+xi5)JGF=GaG-79p1'
    '-OK@C)2!6vr&7z2v#ix29dK0|I*^XR5^J!^6K-6d)48=ySo+n;kYVRyD-l3fYubvh)r(&KV5_@23&2ibS0{*1jO8ec8xQUQ#Rt)F$4sC1HL**o'
    '(JhyI7_~emr{;b2sC1r!Iajbux?SB?;@(bb<3?d=LaPSoEl8^HwJJrrOPyYW+~+bo$E3>a_8KMLOn>IXhx(G}wB#qsjF$J^SHWWP&*@yFrS|0M'
    'jog1Z9rP!L{&er03Ya6Vlt5<0G13uiy~4&KaSn03WMKO3Q;X>p>Y6u_sa%yOgK8l*PFAYLvxBS`utKLERjTWIu!k<|GyN0y6g~X3L(^>YVLF@2'
    'Zd|nqXk4=(t_fVc|74ztRu}il!8NC>DFucqy&uVf!(EggeZ0TInok|`d;5JzAj-H5Ns_45x%PcCbrRDakJWBhCAHE*i){~~IrsPsT6;WlOW^cW'
    'n_K2)l0K)d_J>-nNK3FCldrZ<@4F{Ss9DXV9LnI34b?dw`le~v*oZ*c#^3Fb<CCVfITrIOx1UL+l&%OmQ7nGxZBJYMVs$jn!s=I7jprn7WL%K;'
    'MVcj>Zyx5vA(<N@lD5X*y0*^C9V)~KtQT2QYdL)tZ!L7)yQB@Bu+F0<@5q$-%FN5h)Pkr!sV^TpxK|9(jSF+wU{!``Ixa?_p>3(_*7PT0ol!b+'
    'FyYf)0kNYvx+mX7U#0hC&VVSUopN#>_2j?MH5j)gX{}r3v@uqXcHzi3ozoJ(eDzf|#N!&!Wa&=_fBK*I^N8d9zqt(yKY{0ofAQ1)H?aLs|1O`~'
    'f#>*t8d(2~0oc&S3!4Aa|D8XS#qde(wtaG<@)|y=@A~V@sfKb-xEqAAN7)LzB6g$DFC3maPUic55xOi)V>fWUA_y}(P4kR-Ru+3{?m3Rf(u{>q'
    '#_g8O1ez5kC{bHjq;hkCL3r<Ipm<W<81bAApIq?SMkgT^sr{swZG2Is);#|f_%z>;Ng(oM%c?`SDoHsP&oAmUeN;{Ci?^S;J?Oa?k;W=7m|Zw_'
    'Xa^AwvcPiPl!a01=BXVsCwHx=@G>5x+|MGbuyZHN1KahOTf}i5$G#pb(@|-T&{N|N{JBdqN->3Dk*3VcqC7>jDf9E(WxnG?JZBE~nV+Rm<`te}'
    'J64(%VPHp@A33h=M+FECo{W3i>`hXM)@;{`Rq;wJJ-PgCgvViShi)7=cE+sGi^IT*L1Yne*A1h<<GGcEexA90YG*;<hehOsw#~dEv%)-nEaA4L'
    '32)5%)<nJd$PE><Jy99O#NkuCpp0aV7o=5FpW2te>N&5HOl(_-cAm4$vRPXAmLFOXw?md&1(qT!vNW^9h<i?y6*hjg(l||{xWJER+g9NgS=MQ2'
    '8y8zVDsn3g<H*YMm@_9R?9h)xKg@Vm#6eudykMXLG~`a?g>hinerh?spV^j`>V~vJAx?xC{qVgYj#wI7w!^?_ksZ0*%43Jc1xF8aH_n;w7GC7#'
    'xsCP<%VLhjau=)_X1T5&fPTZ*RR&>NC|8=_lK=1Jf7!ONBN&Ndi&Ys{*l}K*WwW!{tYQ-S>do3s634cZs@_2Udj4{U`p#ayNn#i`bSK4Pb^gXr'
    '&d$DjH=CVNORr{co+dIHksQ&2yPlAdB!lVdg)Wx)6P?pUMsL<9@s(|%(#wI(R5mS2;c|X4Xr>6joay1Z5J*69SzQWUZNn-@kS==JZNG}fn$C~J'
    'S%ZP-H#OfRNZtpsz?1x_N=HCsR9)_*bEE^`AM0i4JHkoxq&{!m8ky?8!7ZI`bb}&h>RckmZAE_;Hv!1|73AiJOh8Z%wXTOI^nE*pbTyi=Fhv{c'
    'OtDUZD)zKRnzc#**4ans<H6wHLRfp#>~1;W|2>$x&cGTEtZr!;pw<4QyjhEH{w){}yeIt_M9FY^F{qV*q=O8NJU0TgfCy>I1KR_~Wtn5yRuE-j'
    '3~2!w&spl&J_q<(4$wNya#lpCms;Q&A3#zR6(sbgKc!iBD*mX#gcG@;rB9Ng)LJ>-o*2pcbaGPZ%4E4n?Jnl0&{oD1mDx`F1S_PdKpG>3e}xJ7'
    'q$fk<liGKPb-|P8m#>m+wjdKkZfTeL#vv8Po5-vt`koh)mg+|KruUUb*|Wkpwo=Y5p1J||a^?bLQ)mfM9)@m|1}tFE6u1|7wg>bN3dRYVgV^@`'
    'R5I)n={*n&rB?B2p%F(Ot=L+pO4Dlp(K}mxS?BSpmz<d#OAxtZax6XNhJjC?@0tXw=E=43^{%<Z^w-UtVN}DeIm2qaQkh?s8z`5lZ7UXfa#WDV'
    '?noZz7jkH<UjUy9R<4ik*!s49S}d!PKV8&I<EWUetCe_kS5EJ8+o?Koq;{mMzh4OoCC9E8Z~0s(3|ms=ijG^HyLk|L5oD|F@qjy?o3o(Ez07lc'
    '7sgm>W!$#y%yJ_r{ZZ)pJd7;M4dc9UC5u$xm<Z(TStY^2|40k@|KC6N+W$!_{lI6j{~gEm{FeP6TE4rt|Nj|3bb4sB((!i3=NHFnx9muVv)wS#'
    'eaP1+P61WSF}OH>dVFbYcTNrt#5G$=XHt$j3Om;MRCqZVe7Bj+j#LqG7)4xN2Kk+>H*_nMP?t)X|M*~OCP)7B4(X7^0cVP)SK-c--Gp+c9nf(f'
    'vCm8!2(xLb(u|16=L7Y-I^L=OtdES)VL<(qJYo9x6+bvor&dJ-b(M9~NbRapA6_}v@+6oFr;%GNVtYfKdxjB8DqN${2|Z1SZdvn8-Dziju1-&g'
    '+s8WJt4#T#2C8dB|2eE06c^O-l{jCQ=mZs_s>TU7b>rT_Z5<9YObnRN3X3{sKCI)K8t|fmoTMLFm8EE=!-GbEWN!=g<vThzOMmE8J7LP<LF?|M'
    'G40~XZGH+onpCfKGJg84Dsn(Uu+7{2M)x;dpJZs>_oq`2ZmH~1X;zyxU%dKj^5)qOXD^=8O=o)f3RPcZ)H#x({z$A;q0|izUOzp*==sPjE)?*_'
    'N0NbScZ||vry`}#`%*D-;{cs_PK$A-^B<_IO3LbLZQ34d-Y{&QnIqhp)_aok&7Fus6ID<~!|q8h-L7bsj!#O2;cNMjVqS(~aKyPXiYKqPct6R5'
    'e6oL{QMKcCxV4!pRoQjpkf8ZOT;<rP7{g80l!V%Q>z48EqH18IjBNpPG+s8UHZEztx8nN3V592Bm2X3Rx7#m)_h~pDG(T%Js`I)LfVOTx9U9&{'
    'y?I$P?0B#(&aKW!OuW)M+bC`+>8aS@>Q0Jh*@}7nhH;znRsU_u*Y$16M8@@Doql&w!(M4*nXSWn4TnNmpVWOJqHU$SnQ~32yR#b^59PIh`38~;'
    'XwPL<EO9VaY?rAcy02G&NtsKL&EB(*<zNH(DrB_0Iim96y!qj&PS(zd^5vrB`Z9ZYhU*f)m6~F8bLEjtIX}?QBNM~FzoipWQ1tT|ky@~HC5X(;'
    '9F0}}cO&LZ4~$xXVs}wpM)Ij%pyrs#g1Ss~t(;a%#71Q&d#{S$(i0*osgu5Lv9QG^XBEz0rJ|g64zW~G3OXo|u6>~<Y6ciy=wp$CEiN_lPIn{g'
    '@ogvgvhKx!JX_I&yH&E{=6&w!>6LH0*P63M>{#2!EHgn?<wHMcmhUE{RnwqSA4*rNLY-kt7x|^<-mXz&>JJ;2ca{Ca&<epcjuR-yCw--jPX!~R'
    'Is>u*kb}NBNcGJjo#OsQUlj7mkVmpWNxA;$worY`hq~lr*to=3_K*UaF194nY;(Bok6?7O`7`A`T)$t`P+@+ltIS)Ge?nN2&>2_JoL3`Jb9dq?'
    'Me>%M*w@_yf>j0-omjgQ7i+YuviWy<dL_zS3(=v@n^dBs5eusBuT=NK^$fENgX|yXiYrc=x4%5on{;x4%3_eF>s!sm8jX7dC&eXwpejGg<C)`u'
    'I34!$blK)KBJx8m`&HcONK+y*5xrc@kD6`v9UK$O$c#)g<ItMv&MRJ`aIWnN+I>NFir15OWpY7bE0P`x8<Z27o44v*H>cu4MWh<zeP7h4@DGtg'
    'DA6M>qADp=lh~(}0h^7RUF2ouJtm3FFex2z)0KH`c@;YRHH8*ZdP(w}Rgu8rEG~iHFiFTym#9(+l}eIEIg!3eydAtbfAK9nrd&uZsUYqx;S+gE'
    'W3(Fnl#c%Wq+ogeYXL?6*P`5{zZUuQ-LJ)*-~3u+cgtUk6~FtH0u~Q{D!&$`%k{5N?#ptK{i-GJudJB=I-fKAXF4x_otHl!{*?auALKs$X+j^}'
    'G0_5AI{b+)`VoU5()Efw9pAk^{_e%o7SY7T#fshBsk=X}hEF!4_))QxZpqUp<Wzq`K>cLCfEF?@x4i>dJ@5KiKzvY6Z=j(Z{mfU3{z};r81UhU'
    '_R@B)xE1oPv*eLJ`0bC+&%_gP=8*EzBxf(5e)IgSaebsdD5{60$pJ>}w|TSDaUGl>FB|I{XwE1XtsLjCl>=8C24?HU-82)i^j}ww;v=nJ^-@>Y'
    'r3llv!pK#i!AzH<Grj6}os34`Uap<OBf7w?N^VEWD2&)svV2=hiOLCe2k)=i$KPaXE4>3}SoEq~mEpPxb=9UeWo>15Y0d`kP6qF;PL3RbGVdBE'
    'qmRy2dX#rY9P04|4+Q|dYmh6Fz}~UtsA{9`q)h(aS3-8K%JjFo*$5d1o9EE%peiuXcil;2u9LXyE}hQh*$f)HudDVCI#I7PcB5ii<tKu1)WceZ'
    '?cH;b_E?J#^srNj9;Bixf!=i*d?$JIsHnCl#4++$^r!gkt^ua>q_J&Lm1}Nh%=;JZkQTTA9ct;wTvny5MIhl+IzcLG3b8M~qWr+};kAKw9C}2i'
    '?{|J^0O<cB)xx_v>%vtRBzmDFNxpmf<}!Kl^zw&9J?N7o*N64M>Y+Bs)kF&DXxq*^U$0mte(b+wu{YskYx#DJVruqK#A&uxLZ@W4rarX+M)ayQ'
    'rLu9GbIU>?8%cp$BUM2c0_jBq75-J!%4Mnq1E(b(Q?5@@y`fxH+2`d6g;VkVDmu9)f9+b(HyRH5Zqp*YS_uZDFLc9+HrREu)!WAX57j4ERF$~?'
    'fk<eur%_6trdO<`BnP-kUT-cUz4fvenn{z5w$}FD`OEXmGs(NJUOxYORinsDp;%LV#bu~GEwkRr^-9wAhB?3Hsj{nG*Gr#}?ySFW&!2w(^3|Km'
    '^Jj0iEdJj0)Mi|R*u#49^xx#Eb<l<bs1kC96`(iN$MvO1M$%!K<sSyYE+42wex`0Kp4Txyr)J6kxfP>s_gc()n-Eg{plLn#RiP;v^-yQl@~<LS'
    'oOFfPB~)^AvA<)a7?PfSErf|_m9mm9au_LNRijMFR*E}JX6ify{;HGi3Jsvj<3)MopgR!+<+{T6(^;OemAd_-Vt}WZ#%l0{2g0;;dhb*3LC#iu'
    'S&fG}N?#nRY?x{F2v<>Qay}&rw32wXZF;?kN^zBgS=;5)i|@}a-yD824+Cf(yBY~Ui=IJoDH0{DuG#Fmx=^h5{u=|g1sGHIi55K9r*(~cKBn4j'
    'uR*=`+l#M+k=#M?Ki45rK%&ED>ebFg9FhmB>on+@)pcU8o-}K{`rkjzufL)MvXVIZv?Vl*+dQhvts6Mfs?yk|(VwcjY|#p(upP>jpRzgo<od9I'
    'A;xgm>y^URk}7O%titB*hmm`~*@h%_NGZ+Rb|!daqjK&Tcn{o$n(Jm{5A+&nf=|6PSX0Oh#f3eE>m=l>N~Eu9D9-chPYgl${n?8b<}3<`7p-d#'
    'CE!L}$)!KO>Tlp$Uy4TV^Y;VOll;(?Xyd_7SB*8TfY!BXL}kxSYSm?O`iA#s^#$*P|K%AmjZIs`TuCdLFNf@o)$o502LBe&(q_|{I6W!C2mhn*'
    '0{NdtKOK(#{Y0*!!~ciC1w_(j$K=J~b_&fZWD8Bw#IB4;;@l$Zr~GSq!wOxMH?;@<7Q#MTuLl1y`sw}Ghvx5Fsp|AyG$v_v>K#e5<)6|<i>QyO'
    '&N;$6tAIlK_t)?Fta%5JieMbnH`uqg56}wbhc`^vq`sg_+P5lM{i(=|4`Y~!Y?QhtXi4NB68uqctiBVMxl%Wq&{lnZEN^Y>P}(L`upv3b8)3AS'
    '>QKeo8oie?zEx316|v^UbXZcffs5L|6QE&MqP@W>|F{r$5*wkq9i?uo)UHp+Cw-il+^oGm6RI~7mAsz9TR?y5i*u)|YNW5XUFmApQr+n)80l$s'
    'bexD6kt>Px0VS2->D50jAO29-iwhBMs)wz&s=gxy`qFiBZnw5;#shMeC^t=MYB*)B!i){LKkQryyY<Y3y6Uz9W#SBS3F`r@my_t+91~eVsS0xu'
    'q$Fx9)zI1ZVSbN^|7)e>{QYg*AOGiio*T5{|2*H@$N&BF&j0Ot@Z#YB@|t(5*ztd2400=TY#pj@T<Kt*niF_I=lR0=U|>yC7H}44Q2^$RLOU;F'
    '&y9VayAJ3}^C0KUcfr@O7ce(Vtu&4;%PI2EPYWkycKW1oR_vZj3HnHiBhPVMyN|9Yh|_|lRva?h4hpXbQyaq;_$>6TFvyq_`nKx?VOj(jMDE0X'
    'Xgh8Y*-;d8J8#m}z5KP09NYCh$3`PP^tfrBN1kijxs`j2GcOLKBE`g{jLxCu0gvo3jlJBlnVpBR%eiAQJ1#;y!0dVt(ew4!Ul2v=2F+fIEXxmr'
    'xR0Pbcl^-L1FtApRKx*j%6!*#vWz=+o(5r%`Zk@930<cM892|eLWg-y=3)|ke+xmzH5B}A%P?7i-$M`<Q|$Y85!=3BfXgi>;stX&Kekzx7rAQ{'
    ';AIe#I$oF+u@eM^Us#31a~}Fpyc<E2<;Omne5-GaEITZ)s6m(O+EI}+#ynO8Q5NN1TG(#v1`NwS&(qw`Ssqzl?z=z*ER!_IoMIP}beKawU9r<Q'
    'R6d<=W|@b9vF!p2EVcZYyIv5aV4nbw1!F}(2JRM79D>ub!nV^m$S`2`E_AWg7Lq)xclMZ*gQ_r!=~OOrqs(z4(3x9~pL<1|#R0g64rqe$BO3_D'
    '^1_N)5aem*Sk5ja=^SrchANEv2(zLh%G^AQflXoLx)!$>5VUY343>wPV}Z}p*ezJj)6~mzHw!Ytd<UyBMIAd3mStZUpJ&;YK?-cj6x##f%+KhI'
    '9Jh)PZF`a9hD88$aGl6uk>k5|>c&xQ2l!z*#9mn7t!+hd>IQbW6H%18c?(?*ghL-l*{)Z(9#G5!vPK0)!oy4gm%`NZ{M@%WdIV;|$P}3av70-X'
    'U`!^Rz>U(K$kOR!w~%IsQQtJi;FcmUJO{IwhhQ4dw|&cI4hW064|c;^EFhIr7hGg#PU@tE<riUIq&CFmE~MQvV0PS#>Y2k_Ob_HS&oE<dYFR}J'
    'ta5WF3k#k>J8%l02cVa6U=-GB6y$jrgm#>#d?#*Su{j0vZv)b>Z?Nnj&q9nuWO3gPTtHG-08i62w=75;uw5Re5OZMo4Co3(2pr4KpgqJ9Ms+8$'
    'HuEhsg|QP`Jyd~jQZJ(n2m$n(f`iAs!goEWO{{Rh<XMEF3yRFMY@fRsNXalzS(?VF7x?ZjRFxT<A5p5`ZR6$lQI<P}<8dAr5I&e8C~<yK_@R|@'
    'KXNz_6^P-bP&{KRb2tmq$jh?Kjx!J#*=f2PWwiUYZNz%VE04Gr=9v#&H?>3N<{?ibU_fa5In+%aU_}BDiZu3Z3+gfVUBFinr9PC?INOaX`K!(n'
    'KUsfR_fZ)2(C5)vQz)Mhz0ArZH0`AU)|?wbRHp$4ZrF@NUOT?US!h8H#aZfd&*h;PT00?Y3A14vhV{=S1Kh_v051X6Bft{3p*zH`6+%h2LsA?t'
    'JXjW92L0Xj!^{Wa9*ZNCV(xBpNj|V`^4hlhq)<jDTcLYH08v^Bo))3cSPHT-3#$}@(scvwGDuI)avknj8FW`KfO6=@!ER&q@h)8mC-)Z8B7kNe'
    'i1`rZp~E>qie;e}$GIIlnGLIp!!q}RyvR6KZRqkihxUW11_Q!8fr;Znh(1i4^eQBY@UbUb=d*7*c^=2WOhzd0raX#(s!<#{8Nh+E1O;Ah0a&2_'
    'un1@Zw2vdK0)SW!Ov!g9PAEH+pKuFq*nZr%20{me0<+X*KET$FqcHUg2xx~FJjmlL_aew`*eIzVreSV-Idl-Hh%tm_0g<{ZdGa#LElP&l%W|>j'
    'MUm^UFmzIw)Go#(faGCVu^B9oB6eZ<KpO&Wut{t$gl6l3CqoRN2a{-L(!{N2l%g5{x0r2}pso}5fk+e)xW_a;PM*fh_EQ^};(N>j<3p)~<wOVA'
    'i!8@<M{WiU5)cwHfH8A}ovB>R78ETtTio1iT_A4MtF0A&8irspjHHvL;K&rq*oiT+Jjy8tP0XMn1(D-~8TltdsGK2`T~g60!^-Y7-ea2B;PzhQ'
    'BywVpfzz$P&106wzE?oMfD~{djB6fdA@qG4qQnYfXhjx>BFrqy#TpNtD1RJr30;&lW9zN6964Sek&eg^!Sk#Lu>{i8%5%?mI5}6`48s<{qH?S('
    'z#}UTIH@SH<K#Zf-`ok<u0-a1%~yATner}?SvR&#wGHd7Z>m9_<);N<8<ql;BkqQ<Pe2*Vc<Mm{Gr)J~*jOr*pdfVf!i7?kL3I!Fo!QaI7`$y&'
    'd%a3Qh_Oqppn%HYMK;Ej7a>+Nh=fHI=27T`K<fyy4>CCgO<30S36B|zA>(!>kQ8<31ng$3_yvgdsZ!htcn*B#enwU^3yK(qGidc3H?wF=1BN28'
    'hiQ+vV|g5G!F?X%QNa1`^ifdPwsj2SuFr1<)55oWC-lA4W0+*Ch%HJo;qxMgxP?^%K!o8JWiT?L6lS#_@enqdUjV0~=n=%dUvAYvd#%*igF448'
    '2bL);<k-gg&1~Qtkl6*P&{-hlU@6C$=LbRPc_0{TCd3-C!ot~|viaRs!rVS7;@dXQ(l9L|7KI^11T+$i8F!&%rC76t<zcj-f07l%U<Bnhih(*F'
    '2f^+F4zRLXh}+n<Pifz{LDkINoH9_cFbV=I%$a37&^u$FXN++UsvymPJuo<+KBri=scl(tXz^lq^s66iQ~SKo?^_RUgf(EnqJbsGpvI&r)MhUV'
    'I2No26((g4OjyXqz=d$L3cw(X>>^;112%MbMn2l%T}>%Elg*A-d@0Tba!Po)4FMrI`TzzxJVY=wD44534B!Lmxq%(0gdXG;@H{K#SPqQT1TRdj'
    'j0G^DVAcAbms-0MEFy!GDu`<fPCK^MH?ABMg8O3_ZV?Pv$YBPN>|4wS$A&>>!+wJ*gFmr&ax&&XbQn?gy*Ph_-k<{KlRE~T|HHPS44hsw94a7`'
    'h14l1F-_=GJ|s6HtaEHg7S97FT6SbZxduN%$%%3sQm4q=)Mie$^W2NX0P6m!t!_jQaN9$plf@~sQr{!%!*f8clZMc{u&xRR3oCbhAG~Sj9yk`F'
    'JMtL}Vvy>%v<neGhDN#?a=Uie>&NF<!d?c`tzeeprf%p#LBM$O+_N1R=AjP*JcKThVU$y-VNf%e1GNm&seqllD|t73p2!pw;%d{a%ow?S6U|&V'
    '&7BlBh=)}kLuG+<bg<AvI|74-Hk8?*D1hWpdSI_ZGGZJtz%Z5}`*t=`)Fn;ZAkuOB7PJG~$Y!o%I~j~nj2w3%_JG)wB8qvTXGd8U1I{t?nD7i-'
    '$TCWW1dg=)VCMz>k5U}iT#8-6#WtJ_Lc4ba!x-xp&<#^B2yEzqo*Tsg&!_;Iz(qHALyj2^<1q3v=&oc)W1Y|*0F<&v5UI`>Y}X83w_mft!o>7r'
    'i9#~Nm`@$@Ev6|<zAWbCHcnY#xsWit$WlNOOi;*c@|$A9!<|<%B{kVfnjd?8tJw0=&<hIMLxMe$Lhpl3msvnQau=nk?bFT$%#{dY%1!}4JP4T`'
    ';g2lMLVH)*?%2nai=b`iAKMgHJLugQfEf%U&UUe4Im<IxAUvXEk}#2ND62(UaNoz@4*FOWkclxlB$*pWDVBG*^GYV||BlV7bk^l9-@0O=DD0c*'
    'FtdP`5P-hrLfeA&n#MM`GYHd&gA?iCVj8-k8=)l6T$g7BmNRg%fQk|CtR{;42p)8cSTI%kR!ot_!W@oM&+?%n@&ZD`ccD*blscA$4g_KbM2Ff2'
    '`vmyoMp;o1Xp=mD9FggQa-z$0pxY-t=u^6x6;VdDoa}nf;uaQil<|;5nn2}s!Ki)_1Q3lmgOUr~(X%rr!=i=;#{rj*By=;I@%2_OzSXDWdcGIN'
    'DGcP;fsW{guvk1B@B{k}651ne$7gmNbF6MCJb94ikl_@Vz;io|yhl>_^K|KKBhdEy&=G=y$0;S&%ZtEH3)n9<OGEC(k!R(U!_^_{$>Uj0tI92C'
    'n>!~b4}dhc+{Y1^EwV-F<lF6+-kp#fm<RGg3roZIZE~qYhjAg7V;)ArVMp^BW>IDZnC!^)UDt{u2%WSD!pD+0r;`8=t({(nq6>``+7PtajMDnW'
    '1U)cb;uru9p$(+wo=vW8S}0z|yx5OpJB97&q>=CcHbQS^#d~*~YUcF%OroL?UOF%HAPrdv)hpl*tgAdO$gJU(Wz!~(>qo5c{S4|Fhxmrog;fJ+'
    '|80cMHf%et_bqSOtQn8vF!V9ev_lyAkSGPFJ@CM+e&J*CF~Uw^`w=vtfTDE*Oh1Mm)ISo3iWTRS!e*<L*bC^e>0Q7j6rjQi{3tF`moZq3d1^r`'
    '&V2xYpT`#FJR~~~6K#jwF070*%Y|0*I2v!J#r6$5i10qH#5#!}jUpbxtYT0m;?#Fw!8lHA2cZ+f)}~#bz@jbL9KavsHbyv4(I?0C9=XDA*{Vo4'
    'KmWXS3nS{AX+Mr~Fcu47xLRr8ve<z{Ps1PxiyYQ9mXHHg-6hcjogKsH!I<?Tur8FQEPEV{(`>VKy+nN=39B`L9_~Zm4a3X{SZL*`PYI|=Fk(?-'
    'tnfbzCIoq=e6V5>foJ{5;;HS}k0TE={EsEy=B#yHzt;^4n?H^$C^ZnwP!eh51&aix7Go(HIKXP!Ed#m(Ct-QnA?+u}(83{jdHQG)?-u!Tw%JBy'
    '7zMridgO!+EIaixmj=(FjrIZ<7^O5$j72cBJPtJ+nyy1`WM~~8?7`TF1rocDrgFJ;+r{=hyEn=NGT6@j2xh&*uoeJ#7|aYj=~)4+h7^!$Wf6nI'
    '0hKebJn%(k7b#gE&^;ar;&;m%+u4TRuv>cf;6rdDsgFMFEr$ZpyvVU>tJX^c@L+1Ec1#XXEF%CtcRbn{$6GIhA)W<~TrG>6twV@D_pO$U1C^m~'
    '6dt!C&vV0+M;XJD0?Q*LBP+zvLUenu`=N5AfTS>jz2!M5|H##{;x}xY67I!zA0k4j!77Hb5i^)8HdGs|QrIrGWy73?Dnap&SQI&JS$Rc-rJq6}'
    'W0b2mhsyRi0?T~1O(*kU$n`mQV&R@m^T;9F6vh>ybOC_^dm2ia!-3tLF&03yWI(})JgF=S$paMz?&Il8Zi)|E^~R`gC8r!K!s6tRXVADEEA_3+'
    '&zY5y`<TOmj;-AD0xJ$<ia7F8=DV0}D?~xhW50n$*j`(=3|zNYI!90peA>H+(XyAqs&|73(ztLTrJ>V6#mR#_aPvHj9ZYxTGTV;>pZO5o&>epR'
    'm3Pw*+cyFIUh|)O1=NL{tZ4^k3q{k=o<Woc<P<6jE2I&H1>=$(Q;<bYaUxM{=a83?{~M^BZ#QK6RYu=(A{#0-?C65Qu;x(59k68zT@2c-ox((L'
    'x#v^Jt6y-?>lQJKGTLZE=Y9)~>vgdeczS7s8Uxu3M0TCr^{milKp@ZsbtVssz{^9M+>UWZyIhcwaY&vupG;fdg30h`8r`j1m0{F3(U>|bf{CBU'
    'Eb~|v0&!r@P}qj;K^*5E>;^y)Y(NHej6;X@T_=OR2tduE$I6!Szh~1m-$o;h&tBV~p<(jh7mykm%V5w)7J(&n8yjmS_bD7Uh1Br@Mj?mZ$WscN'
    '1M6lyeWcF)Zm~+ITaA85|2}gt!|=K=<$dl!8_Qsw=H&eh{WSMO#|<4j$Y6Lu@PQ$iYgrMNN$N8gL(qjDIfy^sqi=_h{(S_7Ss0UpF<{`tf^p`1'
    '0mJ-JoEN8M!)(vC+|0(P`V@c$MvT3}j)J&=a<(ghRi=)6_O?9e34JyJCu1eDnQP~H>O%kXSZrkh)-j7xP@g-F6U4BiQOZwahS5&_A_gG&k5O_&'
    'WZH2#mDexnU)%d$582voucDUwE^K~Ge(vRk6Xm?HY&s<2bNmmQQ5vVY&HWq;oIykd<c|bTWLRWb0gLhv?!$xIc#hW_B9&VdTjWI^U2+2&q4L=t'
    'Ebcr=q0RF&1S5fi><BOmo5zaDhd>J;h0+(KJ1g!bU$3Y0n{s;$NUv-nM+Fm+*0II8696Y|#|JlnGctfY6m_~I!X|ew<O0Ri0DEIt#0>9tw;s{r'
    'if?laIdrF0A8}BMV0>E))<Kk~A+5Jah)*aXP&m`bjXlh_57e@G0BsO@6NMCJSssV>&VG#YW4Y#c37}xx@Ig-ZdMG0cq8zf4Jg6>&BKHfYpl}M9'
    '5TQjHaX?H8E6m~CPVt(D<jL@gB8RcIvwMJIXT-6jt*+i)fs|z|<d`<!=5a(vNH9cTMhY!1ybNL~jKFsuG|d=)$6*@NO=~cWU{OFr-Pw|;&rYeW'
    '_^mr6SigNHkK>7JsIaibUVH4sEJs-m1kOXpbFm^~&$S93b31?z#h^9&v3N|Ij5`As<u%_nhM3}Bts~(0)v-Mao$#ScSe6eJ2?GixiB$N&jw}n>'
    'h6jZ?!$3M&0X5Tw#h8*Cb63*DeIN2f<sDmY^#KJ|KUk&?1_}nH>q8U5D$VF1NlMpXVO?_<<}S2z*k!QIC@uwmhmfk!>uoFNJCEOcp3()%cj@fo'
    'R=Ydwbzs;zWIF!9I8iiL;6pE?YgW=UhQSjFu5;+}I!LFC#AM)L^=C0G9+qKkJ%Yk%J`;D>Zr9W8p!XCJ%+$h%(F(YufV?QmQkGNXD)^D45Hw1V'
    '?uR*rbJ<zo#<Aze1@jmOiWYjR>5iBGE3PZ)IsaQ7*gR4PHjh+_u-|$f_@48>wgdGCnri$052ynB^S}R$AG)hZ9g?jQyqWjq{URCU)3G>EL~%QO'
    'MXBq><xj_3Pa;+;^XS`WuYWv$`Q5AJ&5y5Nzq+_Q`!;#~{ORR)uP$Dk+Ge@4^Y71ICNEw;cSK6E?yDc)oF$h(T%0}q_Ko@S)ytQEOI|#0Rwf?4'
    'eEQ<-&FiPn&Q9rmzRqi^?>VJGdwuch@>T2mi%XS!EO~Q@CSSB)zI>HjTwb1=k9D__*RS53|IK_uE&c7~OOWzk&1RBB68b9n_SG|L>HOvQM(6(H'
    '*$WK%cce5N8&?ccdcSqL5h*ez`E+%x6YwR^{`R-Ys$3dPY`KGK+bvZ0+(RW_sq73#rOL%%zNA2|-WT=#RXsOYb?>mECw~QT>S;--=#Oqzi}wvO'
    'U%BQ;zWny=n;*Y78-0ELtku~S)N8W|W?u1l^j8s+)*@ju-;@ACx|RKoqKjLvbG}@0k^deew*1(7yQb_&v&9YFMt7vrSRAPgYptT}E}h<N7Mt>D'
    'MTz>U&Bc;3W-ab%lFcy@lv+#v2Xm&*@ofKOhtiw5%%+$hy<@8*9r3-rN;*3Mv!&c#f_xk+3_)Ly%3I25Ha`lyqxIs*bs7`0S~UtAOtjm=sKCo8'
    'b4+Q1kBx%n?}=0{joVkL-K_rox7Vod_tf|JPU+_L#fo&BC%=f}C!h2UZlrvR$G^QTeMR18z1)b)?fwblrLV>NpDs<F|2=t@Jb(4<>GQKw>z^(Y'
    'wn09rV2k-4?e}QENBi!Y*bZE>%yG8dOmi9B`&+=hua-X&(0l9O6~uqXFzhjVkJ)?7-edL`j@gOKFa5_rcU{?MMR22;&kE#?vDxGK9?$o9zQ^+~'
    '7|%C!JJ#<<a$U`5!|<ws&xYQ{NbGTYkK23P-sAQciQ74s`MiG*Xd88W2E;aN{|s>59DqGm@3DH1)qAY|BC-0G&p^)aLF%f8&w$f)-Jb!a&93h;'
    'dXLe2jNW7P7m3l7gmOB+InLOUl1u#_u<lm%8Bn{Yk<S3%y`!?n@jZ_3aeR;CUo?)ZfBy&^udDiuIBqoZ8F9QZDtjE?<M<xO_c;DV<9IopzeOby'
    'XA=7BkAU>vT0bMY_qX>MfqvVt?J<9k`FqUYWBwP3`PCWs-+}N(9iIWu&DuW$dN&7PkJWpu-edJ1tG{Th*5CXAkX={x8Ijv)<TC<zV^sDyzQ^%B'
    'j_+~&i^TDg-@W^N8h2I0XF%<`?#}?$X4m%^y~pT1M(;8D%f#p<B>-fY-rt4OW-Xrss~h!y4!mv+!ydEun7zmBJ!XHIn7wA}P5Ha<TG#M7u)3=I'
    'bKtbm^*u)KF?x^DdyM`fF<SE0a<TYbrzjh>d<Lv;*8dssx;+eg%-&=69<%qD{bgY`b*EUY?vArtp1n<IR(>07Z>i{yMDA@({E^_jeLVK~y~po8'
    'e(&-73&ro8znf+}$){zuz)*brJrN{3RQg$P{hsDN3&!6&W_!Hf<NY4*_jvz51@F;swwbN}V8E|y{pTRQ(cV7?@*Bgp$NW9!?=gRm`ClmJ^ZC2q'
    '2k)|i&w}Kt>dyk&RloQ6yvOG~KJW4Q%f;t}9{+BT*0p>tq^|1!TySj+!ydEun7zmBJ!XHQn5|QL5bz}WsrCvac>CM4qddNi&w}^6xBgkA!D9w^'
    'FDv%4VlON9vf>Ms6-%&vGMlEqPZ}7td=|Vn>;Ei>-W-NKX74e3kJ)?7{!%e}QBFVne$TmC&F4XFvw_b8+s%>K<Mtl6_qe^s?Jpg-{{WAAr>@V7'
    ';GfsK-ZwCNEZ<}K9?SPw{zYSXxn|j0;`KiS%^S6SMm%q}^BEDnJv@6%-(&h7)AyMEg<|@e&p0M^_3`(CxmnF;LGk~?-rMy!j$G-w|7Bn8jX_P0'
    '+a7QG!WZp!*}E{^U5)Ox$LC-J0j3nG<P=4+NUEx`fb-u^P@nt=O3LI~K`Q3~X4+XP<yxVaT<`OWqR3VQ75nZe@k)GqN^@Q(QHeEU%~*4HB8Kg#'
    'e#TuA-yePHUl)1)d}H>Oku+w1gKLfX<vL)?*fO?^El*|3yM1eWm&j4R+^VBu%<T>e&fJv(YsQ+fW~_NCYc5xIJvJ^MYp7WAsk(w4H(x$hj1^<W'
    'Sn*U={1_z<0&d(aqhiRd1`3|s(E(e=ma%1Qc`94}DB#H-RSo&0rW=36im_s>7%QHG6)!*65{xzDQxzRMZq(N><dz6nGM0=bW64ula{c<V<dYrg'
    '$;~n<rrc_v;L05xuw`r+TgH~BwB_<?E#S)gDr%;DsITP7ClRn@EE!A2k|#SiIm?pqr@%F;=$x6{sIPHia!Uj(8B4~JvE<3lOD>|c%(HUr(Ti3c'
    'o%51g9W>5M?nr?(W6fAI);yIp7d9)Fwis#5jVda(+^nx)%54#_WGop=#*!!dsz|zc{Ja?ZU6ErEoiB@=%4>XEq^SUQj2&ag*zr_$>~`+>lUv?M'
    'LvEH)vE)_*1yk<mfGuOo*fO>}l`YrXrL5wyIk{Oz#g<zQ6l}Sp1GbDUW6Rj`WFPfK>EqZ>`SvArKHxhP*Z6Gj^xCmuY#1BHhNrS&n^d3U(dSz<'
    's;HQ8v%Z2Kw?)8`v1BY6OP<V<i-)mxysMyN#rxVCHhg^HSTGih1!KX}eOqtsTy6Y!sE<{YzO8qvukvlZrU+OvmW(B1$x~VK(tg}T?t!mkUAbLI'
    '#h5!S6uh~o2KJ0SW6#+0WLJWdF=usa);4Br8C%AdvE?~kE7^Q-hGG-rUU9TZDv3)cdrInEN!iy->%z)@t*~)y92>{RC$sS+S}w=>_MwK3Q6H;o'
    'ICJCWW5rl8R*V%-XT|GHGTMswHI%IQP+i4}PcI)U#)`3Gtavgj&a<-0i)>-bG8=nkqJA+QbMCj%aA}`1*fn;IU1QhNCFfQ2X;oivcyxSj7E;R4'
    'trjXlx}yg6j6Gw|*z?5q^4os^RtlWU;Vr0nhrezsy_@~@Rl>fpZ|ocUp3J_}`?!+0Gj_9%j#;-lXn1r-3alAx#+tF_sjT_I{qPu7RXWm|TXj^d'
    'x!pm*n!8e9%~&(mj5Sa8MQ^v~kNuYSzJ$(Kybr}SzTbU%?bt9jj16PMlige@tD@q`*!8_#F2~=K)mKjEc2mAa8uy*@>w|4$+t@a?J=qPYqH5z-'
    '<6gg6Jle&FvA3e~l+(E>m9LS;?Wz3wVB6R>wvBC1_EF*Q%<|*dj|mSobUq?HR@eA=u<`P-VyqY|#)_x2Vu@2Wey4lB#_v>5*7zM~YGB9MF?Kuy'
    'JFe%#)O+IHFSRTE$Mz)O|8&3n#r<xiOJA(5V&0d#DtP)eGGg^uJyws^&%^3JDO&xfNv!_U6juKUtH<iGdaQoF7eTFuq#SSYgEjRpgWBq*eI3+}'
    'T(88+-IL9C=KJ^Gef#~}KmFURT;6>A-SQ(=KWz1HvHJgpR(~9eYd_#KizJr#jPp=O?{m&$hv#_t7(HIUiU0WHKg|!{xBF~=`Qx`gy#2-5@!LPu'
    'a`hgrTYdlT*SF@UpWpxbzVr7X$nfSb#_SgQ-u&hLcXg+CKmP5P%jj4YQT_-vc!%&>u~F{=PO7wew52^$e`yy<v^(jp9EY9bNDW);_9=JU`H1gL'
    'mX~++cN<MoR%=r(9_>1+ZzNFu>tgYI3|(Ja5>3zOdM&P`bp&7F3w(hu@FkD0ds|eJ$ZFQp#Z;@4Dw;YPfg^APj=&K(lE={^OOkr0|70bh)UT?G'
    'CQm<AKzUUIQ(y{AfhjPhfvMfkqKs;l)CN?$nKGTaVu2>m1e!n-Xp%?MGInE83n>YwUQt~<_1mce$|o6!0#P6eM1d%IL~XaLGfI;_eqNaR5B8t='
    'OX2El16_2TZ>@^4OR|DAkOtB~8c35zTDqt`SN;q@UtL{*c{{3dYd|}21+KsqxB^!SxKg53a99Or6{rGLpbAuhsz9jP9>+Q@#~fa!6tm9N*M-;F'
    '&Z-o<peZ;5XW$H+fioqX9R|Z}syLD4ShsKhfc0AHlglF?$O2g)3uJ*Td1QUuMDFpgyW3g`9xe3M)diNfqbjckv;$Y*3S5CJa3zncB1%_Q;lnLo'
    'U0qyxJF4PpKs#^+uD}(z0$1|5Dj%cVnmB#1ZmkP@Jh&x=t#kEt@pZPdD#k8o3eLb8I0I+kOa^B*H&Gs&>M6HlsMRSW52tPeaR&8;05zZn)PNdL'
    'BZHd59E`wBql7$Sn#INO(th!v1+;(`&;nXy(Xu_OtP}vuv5+2SPA$}6)06|`fE<tmazKs@a&}LAIcW}mFAtbwWpS=FUNblWC*TB}fD=KSm<OlK'
    '{pR7p={WkW%y2~|<oJ8(KxjxQumqOC5?BIDl32=%tjbJji#ST+ueKOXB~L*uDEV3`G086zNCHV92_%6eNhHa<JJNnfr2Q_4cRpYS%zzm%qlB4H'
    'vHdLYe&<+53pA$&N~CED0ct=Er~x&gMg}$OY-z__O78IY@^Cp;7N<(%HG>my0#3jQIFZ3g>a5j7RaJ~WNY<|;j~Gu6aoBhj0Xtv^?0_AxBZHkR'
    'A3>V!AIrmKS5}-ShnEUMKnMr{As|EwAtqkBmBkOS9slmdUrhrt-aZP{8BhldfgvyihQN>vh7Ny_xv5$oZUErclNC!-&rak79CJ><we{sub#-TP'
    'cs)Z?a0br688`!HGC14c5}>fF*xBOK2ypc)$s@|sLmWn4MZnJM!%mVF5!8V?ua!E_r^aC-ycStMco!`r=Ujz($O^aHzw9X7xk5t7INRb{VvJD('
    'Uc5eDJk%!kIV(N@4WI!uykjvOu8&W<bGtyc*d60>FEf4V;j#+_CEt8pYIQA9#H0c*;03&Z7w|IZnVAq(GIf|_K^T&O4$uKQKnLi!Y?UKUFFRLF'
    '86(d>#3iE;NC7Dz1*CwKzG2=!EKL2;e>4&%%{n?5X?0KoNJkIQ0Xjej=l~skt8`l?xsMhfMv|jZMh6_t25Q`Bivc#k2G{@_V8i2-S^5yKw%?_2'
    '`|MbFbc-n=q}N6dBp!Ld2{-{K-~^ob{4#$U4Uzmw5gqxHGB9$!1Og_&1egF5!!Efgt86{$YlLTtDWRj+Mh_w$dB6!c0Vm)Dob>&&-ery3>}mNh'
    '`s*RxS~@W4bx~u9M;lNAN<axH0VNmg(yXI2TFD}%Q8NHqn%%-6rmY#M0X3io)PR~H+kDDxDZ-b1Ax)?pS}2p`BnkKcAK(LgfDeyX+WXDQZ0|=E'
    'r_P5<v-9Ip<m-r+t1E%#a$i0AT%#yR0!bhVB!MKKfqse?*6i%fEV3lo?Y^T~G+0&(Lc<Le>2ppzumg6$4%h)Zedlzqt{Qt1*`bCGP>$8r=+V#s'
    'RDcRl0V+U+$0pNd9Hk;h$U3Ezz|m=<$B&*cU<8bS5ikNqJZ_n9?=<K(z0f!3y#<w!;%%h|mI0~23wQx9;03&#^-Pg>D*bZmXbJF+#VZn60V`ky'
    'tbi4dW7aL4X)f(ppfn39p`_J94<a2&zz6sMAK(Lgc)aq9N#c8xWYKc;S0cN$lpxaWqQ{ZGHlPHQfD%vwN_=){7n|*kYPY`|4U=v)Eu8fFC<4W!'
    '5J&+jAO)m=lyioe7uly+;w_4&pf*^Vtpc#6EfshHFW?2dfR}TgSynbL#n^JFrVW;3p8!;8C<IbK3P=GdAjM~xJEJq(t6m9xtl}-H1s8uaMZydT'
    '24=tvm;p0j<~r9Dy0Gzu(&4ZZ<TA&Xg#<%j2n>NCFw}R?hy5+t%_85JMU+SPv0Lrf%eD+x)q&5Mernt~s~pGyIUon*fE<r;TDvUGgRK^iZhU0k'
    '@(k5f!p%@OJ=hFu25LYJr~x&g#$%gqe>NYS*V$w&U|N-wu+r|K2a~QUAOwVf5D)@Fd}dkN?afDA{C2x`IhrHAa#}FyH&P^uPb9DcR=^5a0V{pS'
    'ToqCNXxs*=;(D(geE`f?PX{dCPHJQs&<V7F7SIA(Kug~;cSm-Nd&Oq!@}t?}EvN$*e=9Y*3`qrEzzcW*FW_a+GYh+loy9%Lu%%H=6)ep@+Ei&P'
    '1X4f>NC7Dz<)UE<of9@(H3(vYoELUhIgkT#Kn}<OIfKSoJ>}bbyE_Lhv*eQ$b9`l0q2q0+O`QSpzz)~}J75RwT<@GBHx#>AJS>6&+-&R`Il&P)'
    '0!QEo9QFM(iSLW3cruSsx^%@x@~2--2RNQSYJ~AB1X4f>NC7DzrEi#S&syYT&k8se(E-S*yc#!}GJp$k0WQD=xbXU<^K%oY57~HTG%BfqquE1~'
    '9&J@X2nYcoAOwVX%re<t`FHyvfW*frzIsYH@paOJieD$t0$M-|XaOxA%gmNGF>cdF^;ZPrrCHf@x5%-KIagQ-HfLMvQRjk$;0OGGAMgWyJl?q}'
    '-2XjB<>PP1@}^Zu2{7#*dPM1}0zyCt2mv7=q;Hn>MHx-(Uhx-+&sh4^bWq~yqlOc&LLddCfE17dQU(ljlg5<<O!jpYF>>gj3y_l@paXP(4$uKQ'
    '`c~Qe<8_qpK8qH3UvjRl4qDE3R71@L{Xh@s0X?7x^qjZOY&#)QqQW0<aeWB++Xf-gkeuKM9DyTn1dh)8XK6ofY`U;2IPw<P$C1Bn5F8E3368)K'
    'I08rD=)8Z{wkZsuMoE42G@AthPFpZA17^Ssm;o~$*UVe@oqsu78g-O_((Ir|k+vS719X56&;dHmSf$7TvTiYDhy*wQ=8*@SfD>>6PQZ!RFSqCI'
    '=0#Rz=Fukk=zG5mlvG2Dx0xnj1_T2$U<S;988G8<&7v}&qGaQIGpcIOGw#)<>3G(hsjCE>a~<`lb6!8t1A0IY=m9<FtkW%`%nDN`@j?(h-KyHy'
    '>GcZ$9glJ#2jqYpkOOl1#_2Xi6-S9#6;b|Z>=(P()O)WfE4T8I;JH*=2R|3PszK-qEkO||0!5$*6nX43*{tH!?5=oUL}~dj`mN7ENhR<MG}GhF'
    'pkQDI%zzm%17-$Y)8=O9a59K#mQ%$`tC2QZIwFA;umV=V3Rv+tW?}Pa@xd<5XB)2`tFgB<^VL%Vi?5R&S^PSI7SIA(KnrNO!ZJ<0=ST7QbJEUN'
    '4uqR_&v4-BstAHW5C{T6AgFJiMU*bHwb;EhJ>_&@<7=cw7{5qh1+0J-umV;F9kaA{DMOTfB~_RldT4XxqzVWDAs_^VfDn&a+TD{(v{+>BDN#E1'
    'CTISNN=Whd(nHITS|A3*fEW-1Vmzj~N%uEQ8T+AryOI)8+CB78(p3e7fDjM@LO@90EO$3J*?v24_r1W;AEEdw>L6vHml|3I)dDdf2E>3E5aTn='
    'vX(&Tol3u&7Fv3J6tUt_2&8}%kOERb%CKQdTnwgLOBW~l7klw&14=*%C;=s)<bqwAO%*4lBv6hO17PLUD-2wkYJnIK17bi7i1C<a>|WmS0>EyE'
    '$QA-$RT-|Tgqq=gdbl~K9LNDVAP3}t9FK9jZBZVKb1Y~&wUqGE?V<;jzBZr)lz<XY0!jw$Qsl^3yOb(O0vrqL2?IvJ2p9n)V8r8=TVHG3e5Bs-'
    '-HtF8DZ^!z@N%Z19%{~t2X?>?*a16WXV5txqjc$N$-w4ROcgPWHrilm$pcQn2{-{K;H2-DA|I$If07d5BNRS60T<u`T!4#fd{XR`uVCkT1wF}&'
    'v%A0wSOF_w<(ZCo@VniOcH{IRli`cEt}1B!9kq!wq#x)3J)j5lfSy6?EFYuX%FyOeN)<52CfZDC2m?mI2p9n)U}VrOtEb$WC|yeOq*qfFEd6fU'
    'gz;$xYCsLB0X3jz$TssLt1^l2uz1R80;b(anJrzBzzSFaD_{kzcpS5c(lXCVYaVTqkG|o(r<@X2JdO0Q;uQ(3fEBO;R=`T%F(uxj=)Nz}e>1{6'
    'kXMb0dh<dFDDhe;;i_yw@h7KQZC2w5a+sE3<E)=?R}X_(<eL|vqgz1|9KG7tV&g?G9A3Z+cmXfq#Rc2k?Q<ip>gB5<&W&z;VRrOIfFG}qA9;~|'
    'ikHp=fCvzI#SpoVtIu(1-?;jf+ZEnq4{!E@jw4EIeuf?O__VvzWvULpGY`?01TP}Zm4zkYb8X9Oc_c0&oPtwu3QobP5hlB`mFXL8ITBLCRdon;'
    'rk@&~&MF6+U=wVDO|WT%!@6x&#r2LIr>hZ1=`Ix1A(Fq98jOadf<tf!4#6QfG{RgZ(4$KmeZasJm;zH^3QUpl(?!0ybMp{?it4?)@#9#~oGq+F'
    'pYtu%kaS5xI0dKR6r6%nBTaTY6j}0+WS{Ti2LV3y3hHsG-%6E7KB?dk9D+k|2o4FFYm#NTFp2gh!?5VkEEtVWg25wr1dremJQB3mauKEG55hz`'
    '77fFsQ@dbPYRU$e;1XPdOK?frVE?GWr9Xz{(jUWf>5p&;F2N<Z1ebISR^r6EvxNilN&ghQOA^8<I0dKR6r2(?Sq(&mrBiq?!6cXjlVB1|67<)6'
    '<GI>^1ek^jE7EDOr7oh*NC>Ck6r6%na7w^rg|B#brmQZbf?n<Jym;^lKEWsW1fL`=wtRG3aVd?bW7R-(I`s<&r>1hS2{yqd*aVwKI_yLKo8NE$'
    'V`}jPu+%H4N2Go$RT%lCf<tf!4#6QfBxbI8mL;Mb+7}GLp+l=c96Ct_hu{z#f<tg<q`9tax_bfj*=1!FrIpDZ>O)Vt6@b$D>UxB_&{q{#7ZrtF'
    'unTs<uKzylI=^Yu$yP+^GFyw{>{v1c!%octfvYJPw1QU93R>|_OVVCfRvJeu8;(XR8x}?@-Z{Y{SOkk;k&L@0$@(s;+-mC`T!2K~dU_n{byB5}'
    'M<*BrgJ2L0f<cnrY94Kpi+tC1pll!_H5!HkQ%gMf1fSp&e1cB`7F%RhR6H5GzPHO|oUVlU<SDDmCtpK#KKaFiPw)vo!6*1M(qij5xmzT$#4Vbd'
    'we(2T>Y|FDjy5m{=D-}719L=tb+axvYcc8^s%dlQ*e3va8VbQ4*aLfD5A2a})}^haz_Tl*Or3obedHX4fj965-oP7pBVnu7Ek*9V&as*@bxwWs'
    '!P8U-_P`$41AAbPoU`ukK6m&>wD}Szas9D+mY1?vI^QS=u`Vnj6wEG44(H$;oP%?4PRn%bZMv!Is_sQ+3tN_1p^3YJeiKk{XeHCYZ+JVvKo|%E'
    'VIU0D^4{txx66hD2f*NP%Lzz$W--&i@azVGhwu;{!b5l{V!w&4-P6>|5<S$}ud7WqPsafG^6CepU=)mkQ7}rvV^?-9c4o-FmNJVDUG({L(gx<h'
    '9GC-hV2*^ZK1RiI9et6)&Z(R-c^ZxM;nNZc{=gsj1ApL;gtfZ=tT%}ievZ|Y>2vC%51*z&um|?Q9@qnWB%HO#oIq0OITTYS&#{d@cpCD+9k>H`'
    ';11l8FxE0&rBc}0*HWg=p^H9rPTIg6m;-ZQ4$Kkp)$E}X{G8`hPMbWZMgic{6bb&oANT`*;E$lSn)1<Y#icNYPF2IusL?MNlv>KcCfEd<U=wVT'
    'aM&u#%?BwY9f~QF=-5UdKn;1|4%~q|a0l*;HP+-VDvIdIJlLq(6xM9_n3@n096DEAkxOUW>T>FWoNx<n!7aE2x5gXoeVj%`{M8DPs##8zO|3@S'
    'H0p>1f8Y=Nfj{tPytOXeyqI;A%iyO`O_e^)KHBhUD+GIB5A1<Gut&mKE4y+`sNFS}dI!`ms!XC@JAEK|WP?j^2`<4UxHR5im+_+dw1tG+fw^8y'
    'RVwwnX+z1U8BBsnFbO8Xr1AcG_$XeQOWwMwZ1Q*1=F^aVFbYP&C>RB!#(QkMz4ML`m`=r1Y1C+=ji8o1a0l+d9k>H`#v5xA-P^?2bdfEk=+iB!'
    '%Aj5|Z4!9|gGcZP9>F7cBw(*jn5bAJQCY?hu^5v2RduQ4>8B1RuX3;nHo+#?1e>HBwy>*0>~7`0q5;U%Y8M7f9ogU#T!Kq*2`-H{*fcBFQ6hI<'
    'L#v)DjoO{GLDba=2Eia01cPAEcyG;8`<PXtpCffks*<SJOdCcX!Qc@*f=BQO9*wux(sh;s(Xo~)flght(bLoh=D-}719M=GfUka9OP{#VEvZYO'
    'ZZmZd^#y}R@CY8kBX}fYufiuQbSi4oDClVleYIc_EP_R_2o{ZZS7(FP*tE3k`^3t?s9RE%M!ja*IPwSvkKhqJf=BR3*j|nFVY@Tc!%@nFJ(zc1'
    'QP>5$U>EFyUD8hbXp>xrTgS=)nRV(JmRC&`VHK=`Rj>+H$+)cWhfv*;>U0YFF_cFzcm$8&5j=uN<L&ipoQvJD?^sKfK&LL+=xJ&Lb6^h4fjKZ|'
    'q_3vw-QUwZ%Mt<RG;8TGr`1K3IUQ|a4$Of$FbC$0_tpETT0ENiiZ?RcY1LC@PrH*gf4VxsAQ%LLU=R!%@2$0JGW6M3QsvH}hc<Ces=yjp18ZOn'
    'tdVh5<93s@%(K$UQKwZ?oj>hvAxPBK3?{)Om;{qx(s+Me7Fq7h+hn;6k6QIqS=8>N&7rPNFbD?0AQ%LL#(V37)50xR20*7`s`P2J(MC^89=HQ{'
    ';11k@JL8SDUPqKJtl%jK-IA&d>NV3Qkw-9i1dremJc38#?KO#>Y#|7vRy|b`wL58JsH+nUf<Z6{2EianZ#A(KMjAwiqJe01Y!?nm4cXulT!Kq*'
    '2`&j3Y%cl6C=c_lC=d6RDChkU+<`lA2kyukYaw)A!mf}mZ;Bx2BOE1xGjImZz!^9*-c;qTz7yiwJE5+;!*zFH4XlARutw5Vi*+e@Fs)Hdl{w8m'
    '+T3X?1bbi)?14S7N5EOL*vP%tX;jl?Pos}Ids+&?9@qnWU=QpW@2urx5vLzy$aAcv%AHdeZR#|&fjKY-=D-}7Gu~ILhj{t$Y0HH8tH>=-y+u_i'
    '<Y}jkBd=_52`<4UxCEC*8f<p|UvB3%OSdCz_whTYzgb6LOu5PP`mUbtcLJokvW*_Qu3dmC&8`<4zQH&62H)VDwB;6A93}Be1!|4j0ZG>E8Wvw|'
    'EnybSf>|&NW{vgP?FcznJp5p?yp-V8KwU*v4RzGz)UbXq3P!;w7zLw7dhEwd<R1GPZQloM@61zg(v`@^TIXu(vFdDBRbE}t5@x|Hm<6+7mZZ-v'
    'pVDX@FZRE)iBl{Rhu2V(KolEn9}Z_{WQJ>S4X(j8xF%`1ZfMq<MC;vHuX-So_4|fHm`_pI1-oDu?1EjAPWy59_Mnzg8eHwtfvDAK8V*@KA>kFg'
    'f>-bgUWwZ5{-~DGIeOSHABtQ3#sS&o6BT~JFZcz&;FqG+n%$8uWsIG=OhdCq|H+`%QX00wHrNK+VB1K?E$ni$|6<bNC!sUvhb!wb>`YHpZk<&T'
    'R>3M*1*>4ySeH%HyQQ_casM!@Vj}^men~|-d79~B$txH<f=BQO9>F6?drit}Ee)i7%|JvtbPI>1lV&gpCcz|_1d{~(wYF7rAC*>!N6mV=ENXR9'
    '=TJu{7zBf05DbDr0^aKWUx+@tO1j+H_fRL!Q59GNYhVqmfi*I&Y94Kp*MBO;oJK`;_B4BiU{G5vSOkk;5iEj50`9s=W3wZ~?kcK+9}5kX)}_->'
    'Q*}%Y3kk2_6}*C1@M@&ZmiFVuri=Qq)V(tEP-JU!_@w}}F4fl~*Tv4N__{(@7zV>&7z~49QeJ!bLFn5{Lv;f%tJ^UQtNQxEC>RB!U=)lR>#^~w'
    '_Qd5wB>|>pF-01++UO#vBM;nxJ8%c?z#SQ5{T!#u?6WzHLMaC!Pi1u?`Fe)HlV3$x1*>2ctb$b|UDmC$%!<3{p_a$2ig+o2DQ{6dLiyXN!f8l0'
    'xCEEr5?q2yG6uVFGh?z`j!CD2>O5++3c;e5RB#9m!67&VhlI>EOTLNMf*3l~)2Gm}QxFI>bb>)J2nN9*7$oJbYxmO1Qj$W)iUBxu>J^4WO|@VV'
    'EP_R_2o_1XYyAxyNfPbr1>n%3Qy2=Jbb>)J2nN9*7$oDZ_Dde6f~PAS>!~y7)F}jmnmWNC7zBf05DXIX)~EV5sUNH$g-#XqIn?MCghVa1U=b{W'
    'MX(4K$++vvjf*YhNOY{H&Y@GM5EN?a1cP7@41z&0NYY!4P3+c2rD4=89EeJ-mf`T!kq}P7DL4hE;FPGzN`8FQEE$ST;XXv_2nLVf5j=uN@JPsB'
    'ZL$$W(XN_4h4y`d0O+U??14S72ll`oA!jY(r70t)i{NPt?V|c5YPSmlq^@jm2`<4UxCEDE4A%X^J*9dQz5Y+Ts5+NA?Lu&=CmURXOK=G;!6hMs'
    'E#ve-kVyM-`Ybv$3PPciNbm>#z#sSnf26E6jZ#Ve?8*h;&%RL@{v1VuKkx_sz#sS{W36i&rN(I!NA+z>)2vuWN&Gdc_j)-hoo}MfstfHy5bUDN'
    'a1E}(HMj=XWDM7-{?!(l=*La1n(EYQcMAcju4XU^Ccz|_^xua`=QoWmv9i=YW`b8?J=Rkv)u~emN;P$YP!I}2K`7oe$#`oX34VumS4^Eg`!*ra'
    'bCd_}z#X^)ci@hUu@<&`jB>$mES(Cf^QX}&1cO>q!67&Vhu{z#5;NEMt1O0g^+Hf+-zgA;jyl007zBf05Db#?*6kKDrTw_E=|Yr7y~+W&)bAOF'
    'P(Br56|90)unJbmxNPZORV8_wic>*#GBsL-08&dTI0T2_5FCO-qUI`k3Gi0UP&5j75%8{NFbO8XB$xz~g#5Msm|pbzM~#a5JZkg`LZg;ium~2x'
    'B3J~Agxpp1tcFnMG=w~(f%6$)5A1<Gut(fklUVX3hC?}h`WzYs!Ouw~_yd375Bz~YLe`o`3v1l#s~%033*N=NUs#_>{gy%K<dYCi!6`Ter{I*7'
    '$+{OLNvB=808H993PYl!Nbm>#z#sSne}t^{<HlOikBN>2^%-<(6@)@fso)SCf<tf!4#}8n6-(Z_d|yqSK!-jd@N-fK_P`$41AAbPkh2z1TIN|P'
    '37|$veF`<31wm0;Fn9!y;1N86M^g40CmTr??aBq<(7sU^1|3C$Kkx_sz#sS{W38Js{<sl*Gjyz{PM}k#5D04O1cP7@41z&0NXlEq&rE2R3_zm5'
    'XD4(7gGcZP9>F7cBxJ97897lTadfJvPoqY!ATVmF1&d%2EP_ScQ9J~xK0n$*_6l^3it40l_6k9&wpx%1Qb8(6#k;1_?)s&=E9}aCF%O$0F>Y=a'
    'k0M0!*VW|GKu2Xd4eAG@U=)mkQ7}rvW1SPm>B>0y+>4U}RJW)yoqFx`(d3a0F2N<Z1ef5FpuwhDv5u1XtI*x{1`3DaQ@dp_Ms+2GQ*a7S!6`T;'
    'XtHvbvFq0iL#I#|wDW2PlVB1|f=MvxdH(7qAil3JPQ5i(Hw^KoXuE4aA4TU%i4o{*6G`w~5C-1B8+Ze6;LRvot*SIBt9AW@b5Ez6`CLSK{*r@r'
    '&#0_Lqq98~;dDVoSOu$K6|90)A})I$Juag(+L)u=iowcTTANn>rU7s@BqY3oSMUm6!7CA)U0RzP_nX>gQ&ay+j91>$+Pv~N4Zy1*A>kFgf>-bg'
    'UWwT3CXJ2zBL!UfDr?iq+cN;H22_MqunJbeDp)nfW$$fK-H9Fk>s3=?QooNLhkOdb9@qnWU=Qq(b5?U7z2vS-*OUxGp<}a9C~61>kKhqJf=BR3'
    '&|WuHoRq>qI@An9q+_>WNNQ*XlVB1|f=MuGw7=fRQE688B{u@RP)(CVtv<>G>L>(zU=QqpJ+Md4S<RzO1isERR5S>Q8tp;>sU;g+f=h4-F2N-M'
    'gMDz%++N7QEZzQ4(?W<%-om<k^0!oH)R2U53QoZ(I0dIho9xmqGWS<z9hD#K(yTY`l|Q<58QGKWu4<#nu<I6}OtfpohHvl<zQH&6HrjIS`rbKh'
    '5<NNFtuL=FLNiZMO?LU(DU-@C8(e}*a0xEKC2@n5{P=3Pc1TKv`!MUQmM{xu!7P{svqt-DJuY(R`8BI)a;eovnM56hU=QqpJ+KG%jCNMH6K-<z'
    'abt@o;g6<<Dr+)nu%|Mg&Zr2hU=^%_Rj_Kb%O+X6ddP~kEsVnDBhFRVWY*cf%G|o3DC~k=unTs<uF+1bbpl}cGybMI#gB9PU=)mkQ7}r6QR&L<'
    '2d1#OjVg1WWQz|Xi1L=zB$U6QGN6XUgHP}YKEWsWG}>Z|tcr>!vpp?s8K)}|I(dp}vdPy@nM{7!;1XPdOK=G;jWO88GA^sJ96A(H;?A+W9%mXd'
    'z!kUxSKta<8D*$WFXMKQw9K>8n$L0dXqHcDw2l`hcRT55<Xo<=MWZV^D}w4OU11mugJCcXhK=&td}9v3`{2ycj=!yjzoZta{LK{MG$a^2f=BQO'
    '9>Jqg_F825XaMbMXc1>$T@f)y4PXjPfhjNrri}Gd6QxU2*fRcVCGgTNr%0MkBVFk9M1nu?2mZhx_%p^@_n%#qNxT>hpH3|$@^riCVW+PR%z-&D'
    '2j;+>F}`XOTf3&Ims9_8EO$Khl-T3zq{kn>PA~`t!5|m}gGPDlm%Kipw2ar|@zW}$MV@vOMd);efj965-oP7pGsadQ>K(zjk%&J<64&nW)Keml'
    'uah2r{5ruP7zBf05DXgct+i@0^x0QZ<<6mpHgQg>z#3QsYhVqm8SknqyI#w%W?xB_HHRMBtU0LyYhVqmfi<vZysNrVS#J^<+8k@C^5)b<n>bBv'
    'U=GZIIWPz2jPcb~5id;{IW<O~Hr+0zM4V0&J>c|&fj965-oP7pGsafqb+odkbe(N67C4<+O2p}Q(L+vO8<+!gU=GZIIiq}aE7$1vH>WaMq&YQE'
    'giKQm_yS+x3w(huV=Og^o@`;#tQdWcK&z4xW!gRTK+{zP*1#HA18ZQ-7+0-_<gRp^uC!)pQ}<t)^99b-RpQXOj(Su&uOEzpQ7{Td!KjfQ>m1cQ'
    '<R5=0fTcz$JrXsWsKTc$47`Ci@CM$%n{l@KCC{>CcN4R9RDKvwoqj<z_IO%p638nR9D+k|2oAxaQRZr+)Hp@rC|%g?Cm&G~f8AdG#n>;SZ@F{1'
    'ycVM_H&!IpHKM{V_yxb<7yKG!wVAE|-?*Pgv#M1}i%ab$iVW%s18?9Byn#3HM#5G<ZX)-DeWn+LkEgCOiF_UPvE<heM!_f;1*2e;gvV~GI4PyT'
    'bSS1wqhlL=1U2M=J8%c?z#X_V%2+pr`#<v-m5;w2jh%KaE$Vc-C_<;F4a|W#FbC$qoH4$dE*?KGByMEdETlx6Rtr7UbR>Z@a0br688|b>RGXhy'
    '+(ngJ^Y)XA?XSn8=R$2I8eQtD2dK+h!Yr5tvtSm?67$*RQyQ(~#o>c@Ym%~Bi;`=oLkNBi_6|g`GwQ-JSO&{r87$LtTbb(#UD;z2o`ty1(DfR_'
    'G?)g{U>Zyl^V^l17+aY8BwKtqn(k5*X@f075N)`5AfTNS7@om1cm~hlnV8+~uN!xt-v3S%V(q#i$kpi>2v<G*U=)mkQ7{TdNqcO)MOU`?&F?ip'
    ')hr&6R;{*Sk=2nCZow_M1-Iaqw9%$+d8G!k4uu2q>ew<Ys~Qr*DL4hE;1rxvF<BEkh16hms2-YF$G#I_tDz|Df?co+cEK(&r%mfG(XOK}roQ)t'
    'D7U(mLoln?GZ3#lD#9vQ1*>2ctP*qCk9K`;mqzZM?iZ_vV3xOUAZ`sP3cFwz?1EjeOU`K%Cs0%vHzDz-?XP4%((>02!Y_a4Py`#&6^6ku7zV>&'
    'n7G&8$7xhNx#y@5tXVxIzgm3*)2pK>?1Eje3wFUSai?`paxYN@T*uNOsdZ`^7+Fmr;T61sSMUm6iQDWd$?l`XL{7<QW#!M(INv8E$p%&s7;A%i'
    '!#EfR<6s<&6ZhOC%RX#!Q$_cQRe@WlK}f1~TL;EjUs^Z@$KV(ogJa@mtM=w>f_GyRz5U|-7My}pa0*T-nrxXpRO{%A3aU=EL(;0zH88MRTEZ-t'
    '1+!ol%o6w6&yTjyIXHNz9FkYZo`GrAP!U$aDp&=pV3nN9*8eaoyB7Xpa<gm@O0^n>LR3dQ_ynKe6MTYCV=T6eoDiemHyx`e(dSfO4>nCDU<+)4'
    'EwBZ)jB?ZuaV~f<rhYjs+VmSKV#g;E{DD942mZjHQP%3VT50$HW1`9}y57%6b7-)v7LSG-Dst(Zc<>26!6*0xpT=0MTV$CPcgv?VTE`2st@1f4'
    '@;F_MWz_l7N}RgTRF72`g@jk|3SPl0cqL%7<)eGT2b-Fe8yZ^(p{ie1mr|a7>VWbp2b*9MY=TX&Nx)$@=|h$*g^+YCrc0tz8+8CR<$*hJ2kyWf'
    'xHHOFGneP%uL5s_8illY(`=#0nzkfx2F}14I0I)!nQ9TG<wI7i-5=fEJ2Nz;jf%zNXzUEv)uPauj*3t^s~?PlQ7{Td!6*%nHOGCU)WTJ}MPPDu'
    'numv2PhfZk&)^w6gJ&vsTQ0If;E3PF@}W6)YCHkMnxeul_yxb<7yJ^l+LcY~m*T`TOZzEaSo3I;T$o~KnuK84x%PpGc3x(<2G`&kT!U+JhFe&>'
    'eA4(dtXnw<!MZ&|VXLnqtb$dr3Rb}?9hcSmRP0QX-~^lIld<zM!!@`D*Weml6Eoa;4OLlJS=h47WREF&iihBsuWcZH`Q?OLa0_n1Ex09SwA)L5'
    'OI&SfplAqAHQNQ^Q(HE;1ef3vT!KsD2CH?5&|g0!t)@9JIH)TOgJCcXhQTm7ueBdHHeFa@ay6<3;Z?I=C`z@JgH5mrHo+#?B<HZ3`&AL;kLErq'
    'tt_89m4h&<+cOlW`YOUISOu$K6|9nX*;QWYplVk*D5>@>LxbulA)JC!a0*VrDS4Bv{@pC?B3lZ7wb)-jD7D(1L&K}9D-45SFbsylFcq)0sV>7#'
    '^+PkP(Rl)fwRDAHFbsylFc>E9wVxm3(!DC(H)H)KL22e`9~x<1nc*5-gKKaNuIU-B>Kn9vlS#NX;afGY%y12^!8N!B*Txv`CXGLCMxS?htfEAw'
    'Q++-3G?joYum!fj7T7Y%QL8v9@0Oc&Zg%&4AB`DLK`r9=S}DTEFBKeuLvRQV!J#qc`Z+6>rWVcE^}Ss#1+KE>FR8?$fo6I%8Wap3&6r2-m+m$F'
    '&3ym%A8)^JzpSySGGEd(j}{+57RY+V$lAi_Pv@RA0$0r$8gSvl-W{KQw}1NG(pFKN+@;ylnsSrpZgFPuXcr$|@_Ew>I*38-CB2``I5m|)ALs*p'
    'pbzu~N8k3~m@MlKC4<22*eo>78p44u@CClW7x)T?uX<kYR(8D>Vpp@CF1T8qf>EoZ7svuxAPZ!HtiZ@}|5<MmNoXA_2Ef*-S6Fa0RRdk13v_`l'
    '&=nY6W%f|5qc4)|I+Y9nuST=5^lAwQzQ7mw0$<=O9KO=@E^^vMD_1Maq~3k?tLj3`(=Qmuyb6LcPzK6C87K>ivRXYkgzc-TqwCNoB(hFQfhtf1'
    'sz4Q}3XZB5-hAttl0l&L^4o362?xHw7x)5S;43`7wwKMzGjWDB>xKbYt7CAQb#w%2APuB}G>{e)Y3t~V@MmC+is~3^_6iBGwrZdYbb&6=1-ina'
    'Yj@?oyTX?7SE+Ym-I}`i>U9eSFpqX141|F&5C+1+A#DG@#(uGjO}(=pOA7JqYy(}moo^kCbeALsdteXjfjzJn9(!>bSHhs%mkfipL$ly8I|&EA'
    'z!&%eU*Ib|z7CI=hiFR|VU&5whryb!ad5=>#RYe-?+z`QBhxzm>fVsUGFbLXS@wKPwZ>xDL61+niz?TAdxYy@Q)}@e9GxvEiJ$Y0uEmXzNMHr5'
    'fEBO;R{D;4A4jEG7Fq7hScWYAiaKx^=%vP&LA5{(hygJm2E+`Q=E^1;LB6!hDZ-`GNS7=<k-!RA0V`kytPD8j{z|KYWa(8@giF7dE?<0Vffx`2'
    'Vn7Ut>6_-l?f+)+7{#fviThm|RpTi$SX2iz!|l{qb51sJ18%?#xB)i<z9|fvV2B9{7q9|WzzSFaE7v$?wq9qcDL41_i(7A5>miAm%jK1@b46o4'
    '3|%EESOQC62`qsnuY)>IZv5e0`RKOJ(rl_YDaS+Ud~r1-U1+Nbq>FNbBX9(cz!5mQ#y<~UXI5@1UU<!(=hY7dB~RyoT=MD)nm`k10!^Ul1r}<y'
    'U!pIy33X~S5vUpqn2uL1vB4JD0$X4UY(2|K3tK)$xzz*KsX=g5HCj&rsg|_h2|R%(@C2S-;-Ttqt9pH=g4M)ttbBTdF3<(KKo{tGmX&UAy84h6'
    'Yg@ehKI|{;6dYf^HB138zv>_ign=*+2Eu%1nz}WbGKm*f^knqGvRZH&Zm3A2bK-#=umg6$4%qQJXIVvQWgfC*8K<l9xapQt!%VM{CSE)uffcX<'
    'R=^5aIpdi1cWwl+aw?~cl}00dth7V|D_{kzfEBRfb<9uohvM<4Nbb%*gGJR4Gtf>GG=s8%8*l?|zzw(=^3CP4_D)IGbSr9trq@fEG9I-+42S_S'
    'AO^&oF-=*#ghES5tbi1d0#ZN<NV&o=?<Eh8-G@0Sb|3Cw*gXfqzzH}3C*b5Nzg%w;L7ud#DFUV4N0%sFg+K~O0VyB_r1TAQ84D7nS4;;e{WfYm'
    '@yP>DzzH}3C*Z{6m)Yh%vF79QqsgPVC{1D4*{9JLs6JCx2{C6H>cQr$cwh(YfE};{c0A5mWV!QW!7uCkD=NXp-%F1(Lu!E-5CdXB42bcV=2IH2'
    '<AwRSiQMB~t#Pv;ci`+&aV79vY^%qhE93-6;0PRnBXHFBPrJUiOOr%TwlHZ{tYtVfTvZ1^XZop;=d5xd2jqYpkOOi&#`z_W(xu=rHg7>C(0E(v'
    'F=jw2@B&`I3wQx9eb21-f3vd5M&_-Ir<@L2e2vt=;ui_5fEBO;R=|qKF$-(WGG3edOsXPY+U5;W#xmwYZ6)Ab>Z(Vd%UXgWPy~uV5h(K5r`x<H'
    'PU9+$lKK~=B#JK7RzlIGu6iiCtR*M{MW6^2fug>BuH3F~3z;Kr-C8=R>2*;<i$@z!0!ly$C;=rNyR1Kri1NJ1zQk*Z(*nE&m7wBnrAL+lslW?('
    '0WaVMybO4z%=?r8Cj*2!4}enuKnMr{At2-`vs|ymZ&fx}R0}La?Gza^EE~81H{b@`fSUo|EH`UG&a|p20;b(Zmn>a{Knh3!DIf);3>fD21gAVJ'
    'tsq!DH5KvV>!u4Czh<BY)PNdL18O|BSw#8c>Ca4ISF!WDr?IpdE~^BbGY$2ab5=aC19rd;*a163&MEhO7+*b2(1iNl%YaUx1+;(`&;nYnuuQXF'
    'iQ{EoGZ119-NIq!q#39IHJ}F6fSPM;b5q4hDGr-M)lj%O_6rD{hH@YW<bWKI19E)EnQe0Oabt@o6IE^@f~Pp2FRcZi^Gy{AbV*1s1cty67y?6m'
    '_l%ddVY=;O7LU&Q3!54zOqnI|QsSBbSCrSm(KU_LkaV4>U<oXNC9njRh8(nd61;28KuJyf^qVOo$0rz=0W)9*%zzn>Yo0DdJeDr4N=i^^_t0ZW'
    'R}~NfLO=)z0U<uKENxMlMSX)WxznJBt7@TTu%9Ak&L{_RKn}<OIUvVloMoIon8GeM3oCZLn!%z<z!_|($D1><fg5lGZomz=>HB69-P^?2bdfEk'
    'Zap(lQU^6d&D20MEEt#pGhhbHfEk}_J~_8cvtlj9n0`Giz<4?-(#5M2XaOyt1+;(`uVp$PK7GirJv++(+wstOMkh5KJ!cJ?pn9I_APj_oFc1d9'
    '`ey2O{q-ghzZ=e(vN~`&*HDc_=fwj%U<d4g9k4U(oXe#wZ<_UVLDTA_PMD5Pparym7SIA(1}*bm_MOaqsCO{;A>X;&^Nt0qfEBO;R<3l+def|I'
    'VXDkLBw19+(8W_#6*9hl+N|*_2Xa6T$N@PZXV5s)`?xAa-^_F>s-mXbOB*wNwLlDr0Wly3#0;8d>9&v+MNFrnDq_05v=P%+3&emJ5CdXB%%Ev*'
    'Pt47;QWi6ws;a2*_0z_UUpbHiazGBq0Xco+Oc#%z7lKz!^VQSAjJJ~-U<PyoEuaOofELi=u}opI1OrP@u7DM=0#?8ZSh?0Qvn&_9Cyl3^5>`Bo'
    '^swR;39Nt>umV=ViqA1C^ZC&hLLa(#%4uPx-$)TFK9Rr*SOF_w1+4TP^Z1joU+iMDyYflBtw-|ZTwNW|ob9MaoD2Ga9?%1NKo98YTW1~#T`{Ir'
    'NCz(M7HTx<N&-H>2lxOV;KSpUg*D}4lv|T#ODp(^%h|$8csbir4>uPi1V7*h{D2?u<MYlek5+D#ySo!-c{v(8o`PDq@wHOKj9)770$#uicmXec'
    '&-}Rj*gWm_F8cK48}r%5tH;Wef2gm#wfVzH;9S{42SL|1SA){^0)r_q1*X6hn7ZtuizLp?$4%s({M9B-g{Tmwl)pn5NDcH3#;8GcK@^ArQ6LIL'
    'U16eb&#jz=nujb|#_7tq$Lo(jq&YR*CJ?O7EFc`Q&Wa7Tz!um7TVU(5liIZ06xM9g_~XXbHtK%A``F(7suG6R#b#j;c6k}WKz5D%;0&CBGjImZ'
    'F8gWyZ};M(#4HjUrJG!sV7~fcpyllxj8g-;f+o-enm`k1y3Rrmr>u=XoBVS1a9HZ}4GN{6q96$*fh3RwlCCpQrL+DzrNhB!lC%8!LV_VM1cty6'
    '7`o0qH&vV{AZTAU9DWWBgQDjo9@qgpU<d4goy*Qy*kq%7Qn|lF7#I!o4#uTHbwLz}0#P6eL|ryfrGs<956n$+P>us~U<d4g9kBCk=d{Td1Eq;}'
    'yYcoJ`=6y*bfH%uKwVlzIBH$i9+ZJHPzK6C*-P#8@Z0?_1#j0X(SJ(hU0B(~7`(XMKqF`bji3=Uj<wh_C<x1$V6U7J<aoCS(m)zW18E}+wS072'
    'Yk60nA*WBYPP3_i)?LPgJnPF3&cGQs183muvY+m+8-5>0<>6F1jqeVhX%hxv=N1qQUgyOITVM-pfi1A*anen^_+VD`3Ev_&-u2Z}0+Fwi9&!9S'
    'ffmpLT0jeE>09RZTSVLYQ;3koQ%(mgzD8<X@rwjjzzSFaD`3Uvn7jLv3qhq*ObaR9Hi|&$%L7ip2{-{K;KbvX*<x+(lc+RNQkmGj*0D(O6;#5C'
    'x0N1R2BZQn;03&Z7x3co%uQm=edP2ut0KxD1;4Izp|lcmE;QAH&qX1@5EueOU<eHL-E((#Z~e8)C|w@@e0z$Nsdx2}B)VK(2S!))RpZfBih?AN'
    '1d>1!NE$NG<`+(6E>d)^t|o>CIw}KbP(RQEdO#280X^4P=c7&BGUd^8sv8PDjgA4))6x(0fF95TdO*)L)~R<ryXW=_g{Y~nQ}+VxK^Z6mWuOd{'
    'dF=E<oSP(ivV}>VWy+k8Jy=u;PJ`|AD0D_Pa070@4Y&a}9^Xvjg*7&D|4b#Wq~oin1Q}l^J-+yL0xh5gw15`S;<3!?p)yNbZ<MTzCD`ICs00^p'
    'D?PdlNCjTN3wQx9;HB@GjZYPf+shXD(@4B{3+mv--%1THLsEek@B&`I3wRmuOqa%O>su(>zcBvCB=GaxhKhK4_EL25^@2?S-oP7p18?B%dQUB?'
    'Q!NwavBb4|p4%%d)CN}(oM&gW2W6lPlz}o(_AEOM`bO<szu-ul(c82O+X1wJHqZvzK-(3TI{Mp7{)&O{r2I{!LA5{(hygJm2E_DDbCZ@IH`acY'
    'JPCZLrVeHXyQzWZjAoz))PNdL18O|BS;fiNivb;KD52y~T@M{64M4@~Lq%!J?e~rW1)u;F{h=7P$o-S<-o5{GoG#;Z^&(LCsz{>3TmM>EjL`vD'
    '01IFNEPzGRB%}M-*xXc4xh+TWqEko%8Qm5t)aVNWH~<IW033j$>y!JRx5sc^dV_@X^|UbJ>7+&zuS!4#r~nn90#sUdnIzdF+Ftm0897HQMl!`$'
    'P6a96Mmj_pkO-gv6o3Ly07}y>>n-6b)ZYahMUr+M4TN+$sIa4_2G9T+Km%w1jh0Pr$|_r%EFbsoqg6%)8Lb97#OO!?FaQR?02lzH>5}ecMw_HE'
    'i+p2F$7dF!-*ud?tAUgY9aR`}Q9Ymobbt=fxpC-h_X($6T0L5~`|IJzrk~;>OKXoDg`ajE4cv4(sNkii1~>sH-~^oT9;RuNar$5#n--@(8-osC'
    'H4T7x`>0@KKp$WNOn?b60VZe7(!Bhe>z8W=0gHL|H{0bD12RAc$N(82)3nSaD<YZmgu9hAfYR%sf)S4<zyo*y58wejnnt<KmNqfF6Joa)9~Ai#'
    '<GHFDP&wOA1uz$M12(`0*Z><~(>G1KjAc#>^On=YOSh3CSNakG6o3Ly017~9x@GeZ$MeKjO#>(1J}NXB&<B_R6JP>NfJw_Ni^|ya(>R12Yp8(Z'
    'R9y!bO$C4ikN^@u0!TD1GEX+EI5jrEv-#*Z9G+qt*zmPc;fG%wKmter2_OL^gI@U%ZC$?p>bD$F+C|mD(rKp;n4V~W2G9T+Km%wldggMS$b)8A'
    'G#F_1?ZScPC>o#vG=K)s0Gf-Qx!p+4_vC5Qs2mJA&7R@7)7B9903YB3e1Ol@#%W^ry7dTjC?6a{$Hw7V)Q}V)0VIF~kO0y}_somzQ@pf=xi8}7'
    '%E}|D-yj&2JgviF$tx^i0!)AjFaf5E{`uM7uQbj*IMxk@p;O0j^l7RGbbt=f0Xjfu&^D9!zKB#l8}d|E2am6(K6?Bb0w3T5e1H$|xoDibU&^EB'
    'ST`7WP94Kxr>P#$0Xjej=m4F|wyE{`&v1iq7@FeqpR>XOCcp%k025#u^w0f2j`q3TSP#*{mU3)5+d~~s=ez3@>XO1h3P=GdAO)ma7TV79#w}zV'
    'ki7L&;N$P41DYX~fC^9nDnJFOc<nOJl6diC>V4kr12SOg)l`K`znd;#e2M`XAOmE843P0!W?@&cv$yTd!DJBAsi+E=ZZBQD^tA#mzy-Jf7vRz~'
    '%(9BoWt3#8HCb-r^i%f17Ng)YR8a#igS}MXaz-oQ0$hL#Z~-n&!`xJHGVWvlLj?_Z9BZq<qVd|pz~PMPd#$u-d(nNF7&k}mCAwWUuAVTx0n;n1'
    '<)2pR=3oBhH~(AK3K#$KFMs^Q&bz8N+eh7NUbjjtYE?gOqE;cl%QU<pFQV1D)#@S1T#wBCnfvMUfKTn7^wk#G&=a!aO_F7~Gs!4<D&w;ApxgZQ'
    'R@>COQ}ZFGp0nVkT_nz=cYfJ?I8L24Iy-aEaev!w>OxnP#5D`jva(<DmeU<v(7)bw>;^Mkz1ek+x_i@qrkf;*?qgTvX}cK`N2AmYbMra1W{FK#'
    ')uY=H3RkW5=m9{Hd&!S23%1F_o5;0arEO=9mQMf9o^j5v>k0!eZ?~R-2V0!nzKAIIakBNiO<K2en`pli^q;+*qc>&rVB4E`3%K?#>TMdkP50F<'
    '+Yh)Bi^u3bv980?wjbLjgI(jeY`>!YgGpAew0g?j4)suao>16wlT^(IJ|(ST_*~RGn`dj*HnyFCL5|lKEQ=R$b@lVy?s4h4oAg7PeNNwej*7J2'
    'p4rx4TbA_xhj%~z@a;b@V%u-(tzVo$vvUyZHe<SoZTbJzwQQW2MV`MYi^aC|-F;NrZn<-u9;~<p_^D=(<S$%G59?R2;O+$~-U#H2=yUyQR&L)q'
    'ebX1%1AAZ(?1BAl!~Tr9?@7~mh5-Br%1#*qhL$84240}@jf8_Tk5e~fAI#?{u1E!>0#X5~fK+hfsleyzd$zP65)1_0!n243y+sLzg6D~RBYB{H'
    'rNjVY05O0VKn%F?7|<~DJxLZ%i^Pq9Lz5)>TUBSl{q~9kp?_IYKo95vJ)j5lfPOmAAKrm#yu+W{n|pP{x5WhT03N^tcmNOJCj`8k)$}0~<@>SZ'
    'Jn(*6jxODsN&<J_4%~q|a0l+w!F{*YOzZ$x$(*!*rt~ae?^%{A^t}oLf8Y=Nfj{sE{?ozVZC0n5UFv*R@2x(I2;g6tY6=*V`bL7l+Bs9U;7fgE'
    '0x|)afK2dOGJ)UM_k3~FZxZ{-CT9CA$!yRsJ&R!AS(acbc)r*-k`COYyVWmkVponSgag6>;ec>JIJog};I;MBQSIhqvL73?E6?RqgU+f1gTZr!'
    'zLjtMyB75|APEEn0s;YnfIx8TU-oYs`r)j$!*3-|5IohK#~Ff+^+<lP{|Z5G<rsiNG1vVA`amD(1O3-@6u@unhqtG@M@0_+981pQo&RY$l5hF1'
    'SM^rj?JqJnL#DKqvFRu95B|YF_y_;D{+{1w>xVb0?Z+hF|930T<0wFHRg%X7o+<Q=!~*AOCHFZa4iE>31H^%wj{_|`-_vCOJ3;&(E6xJ@r_~7J'
    '{*sV4g7?R$e5~qm`)bOlHaR}+2lb#H)Pwq4NBy9+?<wPXd{hLe9X^Uvd#T0POmOV;f?#m<8ef(aPUWRG1~GydL5v_q5F>~Yw-O_ao85&qpKbKP'
    'I8nD7ZtU@etH3?jc=nNFc;(Xvk#n2#hHh+Jzp(lf7=j7GgkVB2A(*^^8ya^^y>W}(<0I0sqf>ll(W1LL>G<#*(QhOiEQ*DD{Tr8UD!1*32gC#7'
    '0r7x%aO3g7YwL$Q@tuA@E*`ia{6$B{f@1NKKrp<X7X|@WaE!1w6ao0+BLRQ_KmZ^ByrKZ$|9YN#M3Vc(VkW>Z7V@OOpdf$;@Bkjb19$*GE5MsC'
    'fB3zLinVbX-^XrK%M#yLejX9PyRtx;U_kWS2?@XbW<fk49uN<R2gHNhj|T@k-~Tu|0UYa0hx*eJM49eW3T%UIuno4sHrSp7+pV{j&ifYmlX-AE'
    '-+eUpPKUiW&Hd^#c{lG_m<sqWYfT&=4iE>31H=L1z-;1x@=^ZbF7t{9>c{+_zYT~B#0BC4ae=r%T$nyCq*-bzyUsH=FnewZM+JX}nZ$&F#py?c'
    'LEUfZ%Yj+3G-buDk%<Y!1Y!a);gx(juxslLY&9N}O$n{SGYJRnH3@%F@EIcCNF-R=`^{>_y%LE9!~$XgvEUWOf<bHFQzlw2okXRHOH*&mdP9uM'
    '2gin&dW?+<$3`y*4A(F6W!d3WUu=62C5RG438Dm1f+#W1DB<?Z`gpjB*U?HoM0BgqEI#y>7dkk2)F(0!8Hfx-1|kEIVV;rU)3VG=xyf_4(}^dB'
    'PVt!qhVJS@=Y_uXHxd`hDq4J~InKEY!~|jjF@czH>oK8Y>5YF1Wr9}4c?5y>lBDT>LD3sQ{ib^OLw&C%Q%{}QIv31``7j^m!~7d({;;?2IpceR'
    '^dSO&kC`1Q7+9oqWO(@oxtY%ywhJ#dWo7bB`c(gmI6<5sP7o)E6F2`!!=SnEN#lKnbf6e$GP6W6v`YEUAYQ&vZYET$qjZ(*UIJ-?G(nmmO^_yT'
    'K27vp{Yb3lL-P5dU43@Jp|d>s#PA&9Z!IHmej_=7oIp+>C%m?t;6KOi@UU7;m~TQ*4F}h#eJf`(KCSPwdT_}EWCAh)nSe}i>nAg|9R2h!sZ$vn'
    '1!r<5V{=8~Co-N>^F|nNzeMXewY!rt;XIs&^Kc%{-#F)eu6|%k>ml(gA#|$G?MevU<w-|^=Lvr!86mOJ2eWvz1Ox&C0fB%(K)CsU(6RK!KZO!O'
    'tKvLTKzm8j<iDWkjiCM^ijz&@LW&#lC^3&wx=d^V_Q5{b2m4_E#<73K-Ul8MiUWhy=MfEtmnR()UbqErB|aq4sx*0#t%_)E(rC@814IX+1JQx#'
    'aP!e&$lCXmX+1J3HEcT#79JZIw$)z{7A~ynWjSG2biC9z6%X7l`w*`P1_T3w0l|P^@G62q!_+rrw6cd^2qb|{y=enLcQNwZKda<Tfqoqo&VZ7{'
    '?Pbs&+CzJ25A9zC?Yqw2{I>w{+vTPOerGN6yuYaBjlH>Fuj>yk;?&l^;IaY)00IC3fB^8?-tM2Z`8|C)&l3GupkI3?pA2{wrg4zq#rofJoFGMz'
    'B1jRW2vWprOc7gmSI7)=iwaXDgkf!O?9{^elf%dU<VX`O7S0MYzMw!ZAQzAe$OW(EEW>jye>@A$|0LOS4~Kfp<}}2?1!|m;ICit#RMse5NZMhX'
    'ctN}%UJx&c7q22-bd0_6Pl4>vsy1y(XfH)R4_wgkreM!`3ZNd;gL+W^s!;F$TwMHQgn6BTp!cD|B{^>j`MYmd0Diy^_yIrQe^uc3n){wK+s8!t'
    'f2cT<0B~H6d;+*q*V_vM@E`uefA|moUl0FJU!oVg4#7MwK%mI>u4cD&dSFzPkQ|aja!3xzU&+aUM^E4VBtY^*p=n8eT!q4!dOpo=gye2#djjDg'
    '9E5{#5PstbZ<zVvJL&arMlt*`Dj$3G#-h9~`T~;s*YYx;Z)-hAz+1U}VD{N0_LHq|X;40bcF+#mK|5%_^;-z~Y<*9c&SQeO?^T`!`1`98i~!FS'
    '`bJ^_VSq3|7$6J~23|oJ=#xnr{By#+0_4vLcr)+wPuC$dt0cRRk`llJcmNOJ0sPIs+aEOdJ!uZV9L0Rs##eDH^<CK)K!0d4FT;LaZKSHV7zB0>'
    'P7WXkkORm8<bc<d16tmGJR-e1{9Y~zw2ICk47Asyln1U+_ZAaDUSw59ARrJB2nYlOg4YxXn$~{0S-ty}TogFen?VXVE=4H-oLBQE4+f;pv=@Br'
    '5Bz~Y@CW{{>u5mB+z;1=+y7E70Guk$AObWNqjePEqN+C%1U}U-%k7yDF?R(f2oMAa0tA6q69mqf`<^t7X9$LYcGVdKg3f~ELct3ZzM&%ooGwTn'
    'AP<lS$OEtEz(CW}#ZC^K!#RO!rveTtc0(k0X7>S%gK;np#=-b2!1&S7kN+4A@kXWTDBfIzEXD^EyTuRe53j(KmUH-_I#h@1Q2kYXYVUFQ#<}Ll'
    'M`X|CZ<L+M+5F8FDSeXv9HnnDE7WGW+C;^Ypg>R{C=e6~3a=?BbnSglo71o5B0{s~4Dvy1IZEN+I(2U`7Px+!MYVBu%{Tu@2c!ei0qNlNrGo*N'
    '-?OLps9aX?l%7Fk@U2QIIlM&qTg(w_XPDflm-s+@AU+TuURiu-n|tH1_~8NB&mT@zXY%nwV?jzW;W~wHF&Hc!?c#&Ua*_ebfMh^2AQ`--WYBT<'
    '!%grGzm>}cjgm761<lncC4y^|y~#X~rOSA0l_UZZ0f~S_Kq7cmiJ<H62XdW$FPI9NMdy$UTI<nD23IP4Bk5onTN6KQPq82okO)WwBmxq_jVFSx'
    'r#JsSn*Qx#(^0>(4msLiRPTn~_dg`jig)^O9j?Q5xc(a6>7N{Z_tR*eAIeNe^5Y6*-?4j@x~XIKhbaT#033h=Z~*=)4%K&T{rFFT1M`hqQy!0R'
    'E<^e-{8{yG1n+Lwb8H0bU>&T3b+CTpSZ~?+_Qz2ucf)FncyX#V7UlbbFTi<!DKEqMq0W`c-3Z2=H)g3@mzz0rA4hH!KsrbV=^!1X-#F3-t^Gim'
    '{^O&VpBI_KvRnCu7GtBpvCj*_z}0JfSw=XOm)aON5+iaKjk0CDFlAKRXF-S{L=Ykf5rl{v4-o^#ez;wI_fhEp;b}0l_~2Wiba;5lM!1pSP(<l6'
    'TbpHEPtg5_uk{fg2oHn@!ozC_4}<3JezPdtF5Y@*TzJ^^8mv4vHSEg2ATnH9*UPfPzN&=a1O<WuL4lw^P#`GGE-0*`joTrkR6ZySRh~L146Z9o'
    'P&gy_Z3TtW+ND|9)Ex0oS|BZu7Dx-Eh1Zc51{~h=Yl*zzEjx8s@GnXuDLhl`+sX>3do9#2$C<n>++33v$P45J@&b9`b>xM!E`PY4PU|6w+%Q;r'
    '>g+JQFpd21Lh)}jN90LV)%H(>AVLr!h!8}GR~8{U2EQjx{VexhxX9d=Gau|zDVrdC<!6&3yo*y!63^NOHyR}NucmZ>Axn@Y$P#1;vcxOP5`B+v'
    'qip+tMv&MSo?C=CEK4;!Trc`<Cx^$+kxL8p?oNInKad~D59Ei}nIC#i-v-+DVU-whs64kEaa>o3AaT9=uOLqlC5RG438Dm1;uS{;FUOW{w_j8$'
    'G2&QyZaLz#uwX&rqV_i(B8U&f2jT<qf%xz$<AV=g8i`?kQ6W%97#8?OV}V;@k>yV&&oVxyARG`52nU3N*A@;uCVwDL^AXXY(5XC|xX@jfYG`<l'
    '@HcpkhthuBIGy-Zg5*GQAUTj6Ue)y-+V0-@mDqJ1nk8p(O^4Qk)UM}nS=<{vpwPT{m8oo58TTWH76=Fg1Ofs9;k6xFIBWNN8Z91UQ#zk1tn49%'
    'R^8bgU}&#O_3*+=#3xn|D~J`u3StGZ;<jVOz;i?+#$b=x#Esz<P98nZZJ-;8A!QX+8$RA3W)L%o8N`fRj~OjHKV157`@19A#^r}yv2(@828Uh6'
    '7X*hZmiDs5urK^#t%(9e0ipm=fG9u|m?R49f0O|KvDDO<KP^NH>P^)^I0y&fARL5)@Cgxa^61GeVQKPWySEA=-BWrVi1#f_7x{jbZz=+01tEYC'
    'KnNfN5CUFB2-q9?@gH*F&*p>rObhN_^=<~}^^R>m#g^MNz;u`n(_#AUGrjHU&3}(1`&O||!SQr&ZinaL-MgNbB7Rrw*=pVt=^JMcfO~Kc?!i5{'
    'e+{_r8T+0v+5X`zU}W$=SbO>;FuXL4Q1A@R2?>M*LINRykU&V7Ur5+g4}TEO2K$mTiUfxhg-ik`VF>^P00IC3fB--Mm|p;}<s!=6<H8}JQFcZ_'
    'pt-7$VW2JYZ3O~y06BmhKn@@Wyp9~O_4BA)#IX$W52dEZ{Bb2JXz%I<#i2M9hvHBiicdswr*ly^mH~OA+Vm`ME=CXMZ2=)VM2F}Q9il_@iHI(H'
    'G=5eG;nk15djy2&5FMgJbchboCn9>|elolFn&v)UxnBx@-rlc1qmSJ^i_=R0=LIJbkO)WwBmxoviC{K~zzJJgtw3-nKF2(8TwBOE&`_H+KpG$o'
    'kOoKtq=Bi^z%p99LD`tZezIx#SlKw;-YhOk7t-0_LaP}ig-a_9kQ**<7g7W%f)qiDAVrWOrcM#g_{QJNw};_V)fr`j#-akmfR?ZjAL2uNh!620'
    '{?x?Z{W5-dGHxP12!p>@d`A55uPs0f@TpB2APtZPNCTt+(!fM%z)j@hQHK48Qqz<ExDq|CH*|yIP#lUwaVQSOC!)BUw0qZ7hU2GV)04ch7Cn}?'
    ')Pv^G9GXLOXb#OMqPhFoI8P9}@!r1JEN;4YSc@LbTk1h`Xb#PxIW&jn6Vkk}c@jyTjo&Uf52`oTqf7RdlF%KxLwD#7-J$!0bT8df1mCS6%FTo9'
    '$JOX^y`dpghw4xrszY_CJ{8qB_hl7Vn@Z{}d$ZmQxZYZhBG@}J-VWK#&X&2SEtH4yP#(&^BFYbV`{{dH`zPEvKFNP8u3day--_$3O*|MpUwIM('
    'iGjpGVjwY)7;Ydj+{fusFEZ?_&oeU|7B^jJIEhbcAT^L0NDZV0Qo{|ThMGNTy(Ycr&}=c!{LotAbRnX%El3h136ca!f+RtbxPc_GU79&9O)pS*'
    'I?Xdz_!c@{yzp-zQU)o5ltIcMWsov%AZ6^hsh2H|_2-!=PRpAvM>ID9>4Ee>dLTWJ9!L*2kRG;o(Jl1S!?FH6)5B?b)1`;zCb*IGu*ixfxq;k3'
    'ZXh?18(vFp@VWY)E$xS7(?YlQjFLicX|gHdd4iJ-$OdEsvH{tEY%sfQP}tnuJGfYlaL_D1w{*~2TiAHe(VTohJ|G{E56B1PgZbtIlSZH7Rpgkh'
    'niN`XW}6w>tDGo3bhijOf*e7PAV-iR$PqKn5%spT$|2&|W4;OEw8W|6Lvurr9mo!32eJd%f$T8L?64>nT9IK_dX7n9zq0TF;ixvbfLuT>AQzAe'
    '$OZGv1#S`?lGL)nvHUFa!D)39WQ3;jH<S~;ngg-9>l8_Wq(D+2DZGZH(D(HQv6>IcM}>CvnI(nJ;^c$E^Mt>tr0^J}%fu2BhzZ05VgfPYRm6mr'
    'skczIe?%i7981qG9h_FC5e=>r`=&yHGv$m(KqMd%5DAC`uObpOEWO33?e8_>z`pS8!oXo&8d2b~$Tt)N2mk~C0ssMk0PqR|z}C#SKZvk?Uuk-{'
    '9~L4{^NwPG8*l?|zzw(o_axvphf$Ryc(dd@P~KXRBGWr!-qgYQGOGQ}RM`icf_hL7>OnoIe-#JmJ=Wejj@)`&<Dh+~!R!v+ch{$J5dZlb;YQ-a'
    's)+K(#nZz5f^QNMAP5iy2m-`w2@q$@eNURkGe%~Ir*!dHWNG|$J6uB@Mu&%Xo0o)$D;N606w#?JutjbpNPKV|ZDOi;ZJlOh39<xPf-FIncr97t'
    'yua^x(|(dtqUbf5aj598RVP=xc<bEC{h!;fpHxArAXShmNENq!?`My%-$z9ekG@oL2bia#^GgejwdtgTiz<@>NCBh(QUED{6fl<*;8gqIikn5Y'
    'sdrf6IMA;?!(8B5WWa#n-2fy7k^)JAq(D+2Da<4(oKD4a;#bz}wxfw16Dzm(D}4$v*lfn1MhvexT&%ciYmqWY8KewS1}TG-F}IZQuyHeFK1Z?m'
    'L5ZOjGfouUl}?Z&`rCu_KzblOkRC`6q=&hshujv2x4h14l=4Hb$&3?3f2|W_2;WvAMUWy$5u^xG1Sw*6DPj*R)uRx-CUZ^^y|qq}B0O7#6hVq0'
    'MUWy$5u}K@r3hy*x(TAyV8-d8z0L`eLw8G%8b}SK22um5fz&X&)KD!Q`jmr1r^lSLLwBiD#E1SSAwiHJNDw3l5(EiiZV6(?DRUo{Rw+NUJIpve'
    'bQU^6cIa&kk^{+s<Un#DIglJ?mmK2sx8LX<dg!#6b9U&gbc*oM+a9C`(gW#%^gwzbJ<KFMq*)a|xJ|tMg=lE#RGwc}=q@l^MCgl7G9Vd{3`hnf'
    '1Cqf^lEJ#ZbbxFmIFy`U7C5dfTnK3Bdn<R-jz7h_J7fTUzz_HVKj6RhyJ&YU{c!!kMfLTrrse4gW@)P^PR1pIy3mzf#)gEt+6$7wb5{7Wu(0hQ'
    'wIOaKI&9bdHA_hjBnOfM$${i>>&c<(=v`hNA2$#27rRu64gDVT3Jsq12}Xt&Z2|HEd4aq@ULY@!7v`E5wj-+@7!FNln;4D@oF+0fHUgP}%s^%!'
    'Gmsg`3^UCP@q=<+*fyAJR@g0WlALf5o{T_7AR~|w$OvSFxn=~nO6`=eYcboLuwULZN#Q6zS%IuTRv;^o703#6%?jy5rkxdbEoPe)_RE_lD;(t~'
    'E07h)3S<Sc0$E|ESs{t<7s^>--(s#=;jp|(vcgGzvI1FwtUy*EE07iDniaC_!zR~G3x_7N%?rl`PLmiK8-dI~W*{?=8ORJ|hM8uDJS*cb%8}vF'
    'W3GwexV}jOLt_h&7sw0b1@Z!UfxIxQym0*7-0bM15*R#9=9L(H3k(<;{2SqhG6QjexIkPWE)W-9L0s6HdE>%@;}b!v*p%dNFGQH|UfqB=5C`Hw'
    '9Ebz)@DO)WeURYyzRslRKCD57+9#m^Hoykh02^Qf?BKv&ZVG{0DsEZVC^aRvo6FE3cUv*w4ZMLj@CM$%J2<?>E<iksiwvt>NVumJ&<5H-8)yS<'
    'pdB3ALZ5KY;InPbPqlC9^X$TY1Ez&B?VqHB((Zrn?&E45<=_1K@2?*w_h;K(xcleV4^drVyANNOnZZt%X64x6?z)%FbTAzQyGdEO*!kk&IQ+Hx'
    'Y#bX&j7H-%GZ{>VZ!f81to!B52IKU{x^vO!GkPpqFPN!dDttT1@*#TJMC|@hcN+FRMo+@=MKT3Ufo}(K`=Z;*c4_KgzGxge6?EsK+idiJ^j|-d'
    '!ld~2ly$t07p2J;#S4>EyMXSTbb5^*lirJGN|+Mgj*`AKRiyz^RVtLKQc@LDaBd2+)aFrHzHAasf2=zVjXt9%qV<BA3Z}xhljKoRzA!r-{#17&'
    'j$KC2L*u0~4NQY~7qLZR+;ZI3ec?>B3MfxTyVv;n=)QQSgemdvD23e}qIuzrH2z$9LYke%&qw=ZGaXFF+1<prjcZ4a$e~7{IXSkI8<vI&Obip_'
    '-&eBLOXp<!m)g^@Ya%cchu6s@FbTdr<i#KOh4`FL>f=2=)nf`SOu@>2$zM7M`@hwmg+m*GaX7tNCW49Z?IWeFUN#Q9Kh&LteUH(DaD0(W0aM`J'
    'LAKwVdU$%_D4hOUc^(>##t%j7H8UAZhHo#avfVcpUp5(yzt)|MW~0%Q(SFTL29x32OEzg-f05WMqS8KO$?|0r;;EuLDZYlIC&sS@lf&ek+grwb'
    '9Nw!DW@-dK6!)pX#4s^kr8iZa+!dSj1t0h||5R^2T3tqsMdzh54NQY~7jYZ4{@)i4#Obe<XQI(){8+SJ^W>B9xA*V=_WfJ))6egJegEgb|J8i='
    '!?*wO*8KVJ@4o-bxu@-i=}*6T)|4gjeO_c$cE5ReQ(t<CkvlVT3pa4TdDg&vzBqi|4N&jqt1Me3_U`cDU6ekV?Gvt^%wyfU+=Y_YkL$7OH8cCH'
    'C0rWhcIUhCA~NcX`YcDiuxTVY=lgn-4f&yehzUPw$9OZ|vmNi}9{Rs-$X@V~fWoL>|9OGbz|ZPK1;(DSAKFpBnlh>{OIt`!ezWkT!{1s+sQK^6'
    'iUnW+bF_eZCrj%K2AgM{8+LK<x^<^AfZjR+%)dud#-H(@<M^*^YK!`M{-(ZQzIonp_KU;kuRfI(46G)AAq=XFEno}twuQAV%V=dywW#X5DM%mh'
    'zu0Jcvlv=fP~RBd3|I&javK(+7A`ss=3^O?M2x=NSOgX^M~gVzz{q^I(TBr+E9ekI^`|n3!Sw|2hcjAZ57@&j?IBI#)B$bxNt$_x?Q_0%ad{g3'
    '<tH<Tf#rnohC!jR2CQM0){x!R-=^5^vTo;+bcJ5!$qb>tk`P|tQx+S*24-plab`Zp>2JT0wSY#|3CF*=iZG+!R+F)3>}NXmZnvtkrR>ljYfd=w'
    'r!|Bb_@<7GJL5jnaj$O`Zx&hpq!Rc&)hDz7-)h2`fnQ~80b95cTQK!eZe4FUw3v}W9G5hWOEg5sDzJ)MvI^<5nJ)F2n`wkRm+6XafPG*ebF`0j'
    'lna`~uG&;qu<ssV;E$>?){ONm$9jLFoNx%}l$~tQJIe?$@jXEqeMWzdqrbg{r>TW>yPM$bck50y{JnJqn0}9@j6dT)$MG-RzP~+JUizHEUg4<>'
    'pudm+4&aj&3%~+qZ2@^z=-EKC_~cg5T1-eg=tztuU<q@ygtFS)mv{G1l@&k9z*BlEJMb+ffEoD3#R{;3`C7puTd%WJ+y;(ir?r67GJ=hNQ&2{q'
    '(Vy?=$7$tWs`%(1%T7D`r)2~i{idLdKBGV1(J!m0+LYp>e=Ixg=%1DmZ1kIgGWv}E+($nvl!iYmW-|U+F@XVOTnQNqxB&~OuYr-rc3~A<pjmrr'
    'BWSH9pci!X#SXB8+1r8pPxYYX2Z!RbFofe`LOVi3Vk`kmn6)Kr7S*PRlH|#JijsJ#VGR8iliP!5Ng++bD>_zzRm|HeWG{N$tvkIbguBGCM^nb1'
    '@t^PbSMgf@Ai}=rw1a<GM6jtpNy*4F@^c*d&u#?GLsmF-rN2?=DLvKr`<4>G1pMOux3vOXU^*8VzUycDBt7_zl9Sy)wXp<|LBCQzW}Dfb(QMau'
    '^p(Cp^3=CQ4lvRu*_dbMd1mvx``}4>s*hDCo$b>q0!(;QNamb5pV^#$HD$EU6I)1+db9AP)81N0fPwEQ%FHwK^O^a2*GfyLf+?-!wSBsEr<(ZQ'
    'I&=r#BPa9Dyw7LePj|kx>1M5Wm*Hmh>2ERIT8*v)3<``7-~)5?fx~qut3?&vC*l|Qyx3?yHz4mWER<0UZvi|5&zQ4ksKtj)gZX$w=n&CY8c)Cz'
    '=Hm(VO)26<T$#@{`f%7=C2tZkP=B(UgAA=l*AULgj33|!GxCEpiPPN;vEy_leyz}f@{_s2z;cw$U{GVc056!47i4#fY`NP}#cxB}sXXD$!n!L_'
    '9)6#w%sq2ItGSOe^Epm``;F|(H>yrJ@6A=H&w5)(=A1d7)ttMXsmhkJQ+}*D;f$Zwpg!SE37K!^dsg%9zsbGU?SJQW)88P|?-v*!zz1gS1C86*'
    'tKJKIhPMHq#1<NQ;|+Mj{JlZ?G@?s==H?QCP9nNOe*6Nzn2%o^?{hD6_wa4C$!;3H>#jTDN5Pn5=6FVP+`D_d%s{uxPBz({Whjq$PfKQ<S)b3W'
    'd+t>)GVI;DQ%!qs9lGQ0k&}66-sdy#zB|*)-syWbcli!<#-2}6W}ca!;mmh#KQBA?&Ek{Kerqv-=D(vZ9)JhT#{>L#ofo_scdPWY*W_+5MR)rB'
    'x-$FB{@iB2dCPghxj&YjcIHpZ2r=(XEtz#@eQvYfyvMxYtRKrxJL{)qgqZcFmdrY{KDSwK-c(-vM%^>HO?RkSZ)(Y`Gwbu5^{h~u_N<u6yk|w&'
    'iO;xxFEc;eneW^uUjDw@``WX(?e<|UVTQjWE%VR(&vgD9cZHWXfaAUH=imUx#RRl~hPrqF9xy`>@ZR)Y_)gmW+uBclQ|<ne0=a@$Z@d9-n4>qy'
    'UaGfuOZ&-PAjpMzJ#sSd%=_Ht{d5C(!C^n##(vUCKP)1|n4eT+#+mW?%((yl?jn=!yJP)C^X^-U?$rBrW%il<8Qr5ZeUiK}=eF8pcjMf3SH1=3'
    'K``c+IiA-X=l8;+ye&B8H18ImKhOu|m}zEuMl+qq!ZUp+IN3}e7oa@T4ds|=W_ms|y~x(Ndva0Vfhj90D|uR-ul7`v?p=%S&<{w<{4@VEod4}r'
    'TU4h1Sb6f<KdmIt<TpiS?wR}f%)Q$b+g(WXJo;AAsiwWX2;D*Ns>qBp<FlM`(YNj0x-*#c5O3l=ax(AC`yA)p)SBE!rB#{wX7%Z3zqOiB4$u)8'
    'AHWA@<O7=`sznf{eyi$abKhQt^1OG2WX_rM8O`}q8m;4n*<W8>a@Kp5C!6>FN|a~bCn|H#+|Ot3?K-c7&#q~doNCgWOVAzhwtmbuvpu8PcDq(u'
    'Iu%T5EAwchPm<@u`m0Yi@&45)5C4$B_y9gIA0OCWMBS{T@`EhsyLG3U{oXosr`{tc^Ul1_XWmyfwZ--p3WvL;)kl{lFKId4VJZ(evm#wLI4d^3'
    'fG^D17Yh53?1WJ@hgO^UI7E9{p>3k8JzjxV%-JidMHSsA);w&IMAa=ijppMQ-GzmAjQ$qDGw_W0ct)Jvt-i*&5x*l=ukci!&|iqI2ly0a=9&3f'
    '&HVOaucBY~X%(Gt)*FjZpYxWA%s4YXs~NAd4>pazN>6#S?1XdPT88?hceG^Inf3Y1`Z~(RZ)x4EIn|W6)}TA$9SNCl=6go-Eq;FQ^yl>kJD->H'
    'c$w>&&h_cjB$MTmANBVNPc`NJh3F2uPf=!`nV-@1>uvEVmc5JJvEpP`u0O3n`Lgv*@tAAodPZ}--O)@fqT9=4&)N0Wo@~~AYf+whzqHIh^FOEi'
    '($sfL6Q27<&FSt+(^!M{O=(&ZGT+Si?Cvw?{!_)(rc$^Kb+h!ucbjW1CCCluI=V9Z%>I04zmA>z`z8sFeY@&Z6W>{d?y&cSWX_rM8O?dTwq+Hq'
    'bJ-Z*DLdJucb1_%>OC!)b!L4=v;LG@;W0l}oNU5RD^MQprg+RXb3LEA-lQMW>~p$1nXA6Ui{OO&YEL!m-nHmX{eZO0Kl4AM`LE*SZfTQv9XG#*'
    'S^rwn3C?zz%oEP9O4%JQ36D?U6EpLP9m(puHAO`vdn=rw`jdKtZ#@BA!7np@fFI1#59+JLsRV|8`5CxDYdOKpprbKffEUcn3%1|vuJ^B6mOQqi'
    '=j;x5nA8#cD+*u=Lt^6#_`+;`!9GNrq%xnPWMfTs|6fj_&qXYIRgGt~oQZRsU1Crxxv+ik7QAJy-eQX)D@+oXReeD@H9zUKnTLn;mle`CeA?p`'
    'c*R`3qVtA2uAbEV!rN*dp5b3wNZ%OR4tNLNF<0;SuQE%GEf?-Rce+xujc%8DI7V+(A<e=gJU)R>%-Sb{9OCeGnvq{jc|c=eBj6kO#;ko~w{w|1'
    'DoSnR=(d`XbM%%L+&Vnl0q?*&=IR|g(i!Ev(d{x1$LOspq*-``$0zWKx%$NR*V;DGX)+I&=q@UxN%U36Bk+h>d&EBf?(nwT5q15--*86WF|fek'
    'J~Fsv@Dx1dc046S2JttXtEWtuO$N6No`R>$)l=#VE0{Qa$ka@v*<l{8(OOYRo9Kv*FW?Jv^#$z{yXJN_*F>jqao!ewFlRs5evGH_&D3V$NgbfI'
    'kN~6KQIwfy=4UqZhYP7J<A(=ZxLsfV2!p@Hq&_gPqyUaEs5jn#H_Xf%>Wi$Z<bh`4NqwNTkO0%)QIwfy=I1u^^--(BmRa)27COW4>oBbgcvlp{'
    '4hF==7x0Do_`+v5jq>-WXjh$T{yVGCo%x=S%sF#Dqd704Jhw$Dd)@kW)yd|(y$a<y?+U3k=Zi&iXZr4^zrXwOukX$G@Be1LfBTQO-~Z{Gp@)Bc'
    'Z+`jpFYn&BC-{s}TpaF2zS(}(*&b}hx-r&&{NwOY`=_=kYQ3XHdHk<e59X5_ti_|**Emkbw#GBYuv6x$X*}Ed;_Uf4-d&*zQ^u4@OxYq@JlfH7'
    'wy!eHq#c@z&)P{Frj2Qnn6@~LtI@-@D>BWj?R$%l+EExLjY*T3v?N=N9<*(ZY36L#T71k7sxW0tnZ%SeHogejdiAE6zWy@AC()<eR8#qH!Kcz3'
    'P7^JePlfsH$ji}`>-Av<nL(`?<XFq|RALsHMXg!nNX+#TF^9~d)*N!2<~n_tL1s{E204l|eojLk&$;@Hx4(Y>?XQ3S`(OY1_UB*R%zpRn_ixQR'
    'w>$mx*8S(lU)!^NaXcTAsJh?OU*st{qwT^OZU6gOXSDrilqAh{*=3YganqRp)9n{U)LH2!-IR9uroIvI+PdAQ`EZsj-C~{+`)1+9ZniJuRqFm~'
    '@fa2Ddg+@_aZzoe<jvAeLF+O5If+yI#%2$_C45M;&*_`bQIW>!>P@!Y-pwbi<Fc$DS=vQX-?6x}mHzXqEwVRJ`6k^Y$(uae4pzM*l$Fz<`NVjg'
    'C-EY#+}@KUZX0Er)niW~w<E0Mi%!_xleb}anH;a?o99;=V586}m$wRC?|jW#*SOvly)W5aujSoyMZ)25I2`VF9ByS(Q@J_<9@i`t-sM`|ReY`^'
    '6;6lK;dHm*bcKCLc018XpKBKi>vEmm;vUx%35UbsaJbuWxZSs>M|s<!L|9ilb`|%th9r|a+4~=V{HOW$r+0rcCaS6;zTZ^VnE&}djrkOp@%{F~'
    'c<$f-_kZl~tNwrg$N&0Y|EpKvhwnSzIjw*B<F`M&{pF``zkB<qEKR@k9{&3;Z_W4be%*h4^_kyUtt*=C7yJJETIM-xxx!9Y>E?o=p7wx0J;qD7'
    'zxyA26Q{L{m2dh_>RYnkI69lN{^#~TZ`R4PzV*8Oo81idYg}(1r!p6O<9xqM#?koeXKI0MU>n%R@Tg?Db&)7@K$b9&ezj0m;^{1GDqf|q7wiRl'
    '86KKi(ebu&suIdh8Xbjgq@~C_>?6qZzxj(X+rRwFyI;Qj^Y?GfFTZ~K>$~sF&u{<s?w4PGc0a%W(erFq=P&QSb5H*H-H(6!W!Oxxk(;uS&he&a'
    'A6z;;{3$Bp=-y5CH6C@E#l@pte0a{4g15t4ACu_(xt1gOTD{zg23|P+ga6<^llV`zUT5iDRF<~bZl@PJk*AQpC;6I+xRYNNd<vh!rzZ5N-MP_r'
    'Y1|aW>1wQ7dCG<IEMIFu=kg1Mf8k&F*OdNMnEc+jt^CR5iV<)7hN=Z~uYPAq^YW>Lci~-l*M#1+&XzVYk2cv}k$IeRb!&z4tzKV2*YfCtXW?0R'
    ')`XswyG=gQs`e#9In<$@pfR09z>n}F{AfZyDyyj4j15o6BB4C#)KJisno{6P_!7P}g)c4bLwQ%9v9~)(=$ZE^Y`eukEp?X~>MLPZ!}{P^cov>D'
    'nP+XcZk9YG+2`#qMwynUSQyvxH5RiizeM;JzJ+hi!nd+yDP~){TH#%5-&e)7j{4wPcov>D3(u<G0Lg`|L$&a}b?mI-Tn&}*F1!oxn#{ZE4P~mQ'
    '+?w&u)vp!Cw>*8tT+6Euo`q-OS(ACzVJnU@t45J9F4b%(W>0M?@Fjc+Uz*I9RyN&Tdu03(_g<+m9@TFv=2Sj$@GJZZznXzxjXl!dtrXg=q>r+D'
    'l)<a;D!gh2UN!zWRlinfzmh*r<<$qz!n5$K$vi7wM=Lx2y<n?M7^iBt6f>!=Aovshgg;H@Pf50l7jBeBJ5;Ao7?0{U6?3V+Ecg^Yg-=cHQ)7=('
    'wd;g+Dd{6rJx%Z^JPMDR%%kcJK0Zh_tAz2VR!1>+>ZpM?;Z1ncwB9r}JT;00bSCN8)RqEY!k6%+X?<z@$9b(X0li87L%gma_!ItwKTYOOX*&KL'
    '-Kj<xXKM5lv!#{}coLq3Cr#-|W8cXgO9XQy>34BW5%44Y2tS(4kFxv!a$CtPUG?q88lRYkDu!{Z!R}(_bw)9~3@^jW=Hq2!lh#1NiJVM2g$>Jv'
    'kKtqZ*mOR&-4`TI?Ce$w<XqjZa+cLs2Cu@a@T%#(YIN3W7YgiD;@PSv3qFNU;Zu|Olq+4n{D!3M5@EdQ)K1Kmnj+vw_z`|IogWq3TbPeCq+^Xh'
    'j&$lLXGu*R@FYA5Pnyh=K5nA4ioZrOp9A`;h4CqGXEDbbPzmqCyYQ}gc-PotQJ!KGIG6M>DZfPc7QTgVP3BvLU2cR<yy}$-<5~T-Vvgk#2fxCv'
    '@T(d4)!2~LtrXg=q~n!G8N3Rw!mDQBRpY;;+^-edujIe3?9~U)!n5$K={!r~OUp&*Z!H%ZUt2Et-ZH*~FU`l7M!(|={LSS;;j7C9-(AL&@T6II'
    'Qn`4v>u8iE9ZQ69q*FUFLu!hEAK^#%(R6-fKW^Oo3fwZLTPct`b-T)$R9_jq3a`Sert_-DD7Pj~A8b(%<~X<N)e7WS{l0RJ<<kex!n5$K$vn#q'
    'ODS=4_I8~xUe)O-=2JaQ@F+YAkDA(}#vUqfl?m!l(g)1Dg5Xd16aF-{KaD>js#7SaN6DWG)t3dI!l&@5iG9lCMOI~|9!mkUYF7*CSe?$ow$)P!'
    '@4~zAuBp9C%C)BOD=nwOqwpv^Y8D>#rII+3eyS46pBfzn-KnJp-h?;dP1AW(5iR50^>fz(7eV)y3*=Vb)^fHrAQb+Ef8k#<@vqSz1$wGY;a=h&'
    '4Ej~VyYMc&YfA5m(>Ps;#I9DQV4l_PD(P2UW$-Gz3a^^Nt9FYm4*&a7qw1GYcPURx35)Uyf<NI;_|p{r)cN|zUA)c{ThHG1>bozstBY0D-RtrS'
    'B+Tp@_3$>l4R4#m+dkQ%beqSx4eWk&)$jH{z0}su6;yY$v&|*U?1EhQ7(RxN&BMnYZIZwI&E%PS6S&x^w~mQ5wbJsi#o|g2`|Cgd^6np7Lwfh)'
    '_wRmuYrg&Em$yIs`TPIpPycp>EB*BA|M~gtw}1KR{rm5?HU9eTFTeiu?XUl*ZE5DG_rJXRr*TH~{_j6KPyFWJ{_PL{_Q&u2ezc$0Jj;@fE&aDK'
    'r7MhQ@tfZan~r~kyTaBJkl0nUczP4%@xNX>xs5X0A?lX7eqfuGu9@45oiB}Rui@SE<Y8DD)(M9-&x)#s_2_YJ7YaMDo!;Uj+Y^bQWoRcHTG!qU'
    ')2MMhln6Vh$FAZd+K`0dWH_fJ>V6&LXnHIXn53t^@)T_f!;mth6Ao!DQtmVwHKeB^VTZKQSA0lY!rb_f{`1El&D$Thhqk}{`3Ljozy9X;?Z4|h'
    ';V<uh{psiTe>wY{zy9=_-<w~)`}X^9f4=;@f4u$f*Z1e2_wElr{BZUOzx?p_n@dj^e*7TUZ-$=mkAHsi@4x@$KmK{)8UOh4$9Gpe<!^sI|FnPq'
    '%{Slv@Xz7pZlCej3l(Z<FFfVK3v$o+$De<=w9-qjdH<A4ulZ7$`tf%^{`!Z(=Y03m-{1ZC*Y|^u|K&fwd;jA<%nyJ6{?~Uuxh*~@@8913@PlzX'
    '%ujE>d-v`4@BZKICp?2?|Ia^$HT;k7-@g0HyKmnOw))}i5AT2ab@1<he)r3F|2T}t(35`o>D!-wc{}*<KYnSguZhxiyeP5{&WCJLzR8Ph2~dZD'
    '`o}*G1NHqk=hyTSdEB4W&wgWzBF(OOz)yAmHr^uaH@|=L@Y`?Rd|G^}?p6*s&0oAJ7f})wZ#KWV8vUy*TP5~On&#2s!wJIIcDX(qv>RQM4#qC+'
    'Qhe~N=uUhakTOUaqzqC9DT9>p+ET{vf~1V!%{OKIZu*q*J5mNIgOowaAZ3s;ZZKthEdS6Z+~F4o!o}-o6)$AM#&eoYlR2KXLe=Q;%nd^VA%T!U'
    'NFXE-63C4ukj?+k-ktZiZRGjBKY#mCu$_BHQd^Ww3X&<iH8bhN-JLVpUUs@?=Gq<&1cDMe+yasko#naTDl8<x6(CWl0`tk7(-tWT^<x!??;C}M'
    'i$OP0D3BbDX%a}D3e^fE-@qUc1cE>i2m(PM)e?x`^ZGq|I?Fj<JeW=JH|w_JGz*|drdITrBU@=ov5|EQPzK6C87Ko~pp2?3BY0c}G*&lciZ#|$'
    'S28!&4hG^t9EbyPAP&S)A#t3G2=nhmrpYTX7arEFS<*RsU#gO$Ar0clQ=K|-<QojcfjAHc;y@gT<50wLHEfk-j>V=-Q^!($n%uEsNRS8eKpw~g'
    'c_5F2kw<CnFk2mFm;-Sj4#a^t5C`I@xHul}<_Q=69PU!5PbDY@9ogH`<c=H#s-;J+Q9&Q*1AU+m^npGOKp*AV!*F>R;tte-I#37dKpm*#Ak-nh'
    '61IN-=ro=C$E7Fg=hO31bJ%dsr&gSJjcE}C*{xYU9@(*0r8wDn*pLcRK`KZEsUVeVNu~WLGcLTobM~;_>@<7LuH`gF4S!+Zyoif$cW+mZRdy{_'
    'DOPqLIkbXS&<a{XD`@2qv~n(X%F9&B+V)J+$@(HSsASU!p%FBKM$iZvK_dsFk<u7qwm-~(2jV~+hy!sT4#aVY;;>4d|FEhs|6!Rl|6wuzfkw~>'
    '8bKpy<oBSF(imd4Kg@s!;y@gT192b@#Bqq?xGH)6<EjetA6F*Le_S#Dfkw~>8bKpy<oBSF(imd4Kg@s!;y@gT192b@#Bq>U>t2*R0kXb9qpr`r'
    'Flh>8;~*gtM1n{V2_iuxha-~mC}Oxl48aHLKpm(9b)XK^aR};Yw(Igno$0I#pOiQ295xnqx0};Ou^8FC0G&qJqv{gqve%#@8bpI=5DlV1G`|PY'
    'ScTj+evh`R<QPl+4dnM5Mf8GR&<lD&FX-g}^&-DKWg+<*CC|ugEWu#QY_3~BX|@e5vOzY;2H7ARWb^xwO;f*YniZ8z({R~v#0l9T8)SoQkj+8M'
    '=3;s=Y_8s|zbM5un+q^iHCw9IY?`fuif9lGqCqr>2GRT;L{sWa&E^8CBpP#OX|@h3qCqr>2GJlIM00?m88x-f(rhfjVAX7{Tfb|z4K1=kHpm9q'
    'ARA=!`;bl9vo#w_sFrMunz7k7w8#e8ARA<ZY>-X0WYcL})*qaQ@htk{C*qvD^~R1Yd}0-Vm|U&t@k+iDmC_{d*q{&;f<jOT3PB+UrjU+(JL=RY'
    '%`yx!Z_7}DBns5)kJPB35A=aP&<FZJAJx*wo!jfpGEP3IH>Os4y1xK-WNJl^II@+d)EZgG0A-*Ilz}o(2Ff@DW%zD)qS_d#HcS#mx-bpCSnPdt'
    'fiBPmx<D7`;t+J97#1d)LMJDX1d>1!NCHV9i35^Evy`JDO>>WiH1!`1X>v3KWuOd{fih6W!74-b(U7L8M?;#rkA^fk8iFp+1-d{N=;A<ip%@k>'
    'nnEWhkOY!I5=a6`Ac+H#gf(rJa5Q8^E2g+&RcR&jV)YoH43vQ~PzK6C8Hb<@#k?@l6*@tIB#;D>KoUp-NgR|UtWxGHEOX~8EdBEpoV|%MPzK6C'
    '87SjWm7)6VP0Q4?H!a;~Z(5wai7wCux<D7`;y`tw7#1d)LMJDX1d>1!NCHV9iONdCyY;$W-?jbJw|l0ZjlaucSj6|6<!n!nHS!jz6d(CV2#uf-'
    'G=fIZ2pXxVMpooplyQZ8ReZ*FH0xtaapkCE>qwvtw1GCz2HHRy71Ks)Uf)ypy=EEP(WH%x#i`Xs=8-@fXajAa4YYwa4nrHoo)em>6|<yK;(4K2'
    '#{gxZ43vQ~PzK643}q+>hMBg|%L+t+C=dmrKop4LphVFu=_pCl<f9}_6ONMbt!HQhZJ-Ubfi@0V8|sgeG|fFq($s&Hgl|7X87Ko~pbV68xXMrt'
    '3^Q$^mlcQtQ6LILfhZ8gL5V`e_n>8L#~fdj{#|I<M*?l24YYwa&<5H#3~eZ9hMB(53kyVnC=dmrKop4Lz(iq{G=E{4Jbz)CFn_^Wo@fJYpbfNv'
    'HV#)C>d*4D%stD~(tnmG-;#zhPzK6C87Sj$m7yFMX4*n8D-Z>uKop1qQ6P%SilS#vhl4>k`E&hh*qWv%=j%?R09@o~OOG@16sVLO`9=kOpbzwc'
    'KF|mHsFps4_QTyQ=;8;;{E@3QJp##BqEdt89UByaLQn__K_MvQAQV!jL5vrOG5$aw=mUMA5A=aP4niM4Io4x6b&A34871G{nzKDU3dvifQi<dr'
    'AvA(U&<GkqBWUDsG-8#k5vvLsu}snkSJk2sG=fIZ2pTyIjkK1suBCKtBv*U7G?K4K#Tv;wLTChypb<2JM$pLNXv8X6BUTkOVwt27=0?y68bKpy'
    '1dSYoMn<jrgYz(+M_u$pd}gHAv+q2oJ}Kg~)jW-9a!8&E)gmO{z#tF=f<O=m0zn`LDiEtofmp^1#4<)876L&a2n2y35Cn3F0_pl*f7~faAc@8d'
    '6-cr|y#h%O3<5zQ2n2y35Cl>&flL;IZldrgOpeAh2_#R2Y6X&SU=RobK_CbOfgq3r6^K=)KrG`0Vi_Y4=0Fe#0zn`M1c4l+KngtylcO<11ybHo'
    'n0y0+KoAH5K_CbOfmBN%zTIw*M6|o^blCIylTpu}&O&e1FXI68{bqf5Ap)>&J5I9zo)lJz9=jA*zEao}Io_xT^`IWqgL+U;#njUtP3CvH2p-j6'
    '4qr|SzY$5+hBTQaYjtW#l6^1`2jV~+hy!sTj*5vR46}L6C3daDed;0(@nmX6lQlAxrWP6*#{gxZ43vQ~PzK7Vx-wGV_^J{bs~a-K8f&U6nHy^d'
    '192b@#DO>v2jZxhI8t->a_YW#Yiw;olQOo~rPdc&x*k~|3uJ*TkOi`+mMliY#FvrC2ldvpQTPcOnOf1~i)^JSB}UdUKp7|lWuOd{fikMEjAkjy'
    'Xqu~xrha8KQ3lFD87Ko~pp1i5hE<9(EOV7%=~o8dYl|{a2FgGgD5Ltyc)0cYw*P4sau0U9Jv%3$RRA!yHK4~9TdPvaiVU5OC=dmrKop1qQB+?P'
    'j&f2sW}3p$iwXx-pbAuhDo_QgI4o7Py@y%2HC3Qup#eiou~=2Pq*(5BM1d#}1)@L{h@$$UaFmn6G1C-|UQ{@!0#%?2RDmi`#bK!;dG{tI2R{}X'
    'FvJv#Rh3JM<xWQwhyqa{3Pgb@sxJyhIVl`7P2uQ81?N+s3RHnAPz9<uG*x(g#i&R(V2UXgsw$ZjOP!7=5Cx(@6o>**R9_U1a#A>En!?eG3g#(L'
    '1*$+5r~*|Snkpt;#i&R(V2UXgsw$ZjOP!7=5Cx(@6o>**R9_U1a#A>En!?eG3g#(L1*$+5r~*|Sm@0<8+nuOo#ZnW7xMI1ka$&Km>yZVrKo-aX'
    'Ss;r;kcCqMEgVyA;poN%^A_j=U7!nefi4bA7ry%2VEKmL2g}!dBP^dgVId1-fh>>(vN%9llreGP8#{C1YoEICnY%z32m@gt41{rz!Wd0%Rr_MG'
    '2~%{jR9DHiSkd*!0$CsnWPvP@Ma5+?`pIv5osL>ttZ2j#U976CTwkmn0EB@s5C+0P7zpDqgyEDz498q!IQo&nJO;`@87Ko~po~LP#^hEpFwzZ}'
    'VvB{UN@m4Ury~kPfhZ6KqCgbY7loso6popuaP*>rc?wj4Do_QgKou2Nh10cr!+E#`4`=CS6`_i4Eof53*0R*9B16BU3RHnAPz9<$6;)G3Z_-VK'
    'Tk=~hiZR8;<?9l~=AsmPVq1@+2{eHw&;*)56IIj1VEAd|-Srds7CjSXTp^^`-iR(&WT;FrE;0=O!ax`Z17RQxgmEy!a7rPDW3DkA{m5`o2FgGg'
    'C<A4njDu2!Uk)>TgPq|U01Y2$APuB}G>`_;I0R|9-N`)N65q+``^|cHGHzVfhvx--$4KU;G>Id7eQMQ_XGo9-@<1NQ19>2igOrC=o;)nW<zX2j'
    '56%)r9>@cEAP?kGO?gZc*HelIam<My3(>}=#p@Er#+nqXVoP@;2_%6ekOY!I5*3rgU22c{F1gEW%f7d}^+vPacu{YA4@J--UwfLYk+Vp(_{cp%'
    'XatR*5j28E&`8BJazA?XM)&iy3m+GIKaK5eXi`Up!qiG5Q}3e-bb&6=1-d{N71Kp`a5oP(@nC5``ds{9LGv8h+tQ?s90jUXN3KypALs*ppbzwc'
    'KB}e<KQ)mbJS=u*V_OTlq_M3m#lG0y@2CP*pbAuhDo{nmRMDGs9lPCj<^dN!Q+FEmp4ac$(>U##(;}3Sw?$3r$X%vdh3qg+s05Xu5>$dpP)Ws9'
    'lHTP`?Qs|K?X8=auSpzRic;%~tv!w=&;*)56KDcWR8158b|TvP?RFe(UGKJ6pEL>)#nv`-sbYI!ie-_d_t6EqKo{r&U7(A@(uGxmE-X`ZVd>Td'
    '=Vzb`bb&6=1-dv0UG&`&CNEYrVu&wRRaP!ARu2HeKo|%EVIT~IabUu*$`FQStS~I?!e9;qVIT~IfiMupK?q|~!W72D)ESJ4?g@+u^B3p>U7!ne'
    'fi4bT7gh<nuuRp3rCS%wU7!nefiBPmx;O}3{9MA^#m}bBUHq(j?&4?WF3<(KKo{r&T^znHtP*r#nW_s*w=S5wKo{r&U7!neQB7Tpe)7kOgp&_v'
    '-;NPHC}{p7Q#-npk*zqz(#SdzXajAa4YYwa(8l3u!zxJ|mdV<%Owa~%8)yS<pbfNvHY%x&k$2Y{c!~M@=!sd*`OSKFGQPCx!>0W9&-$cU1U`0Z'
    'R+B|`t5q#cb{#gPf>e+SQb8(6rD{@1?Tjb)#S1(Hlc5n^3dvBJVsm5~0EB@s5C+0P7zm?c!Wi5A`+300N3-2&TlT%(tzXq!a(B82YUF86lR0vg'
    's1_eN#|DL<5EOz!PzVaCm_jCf+y69=I{S7MC0!Bd*syR-#@JMmT2gH8Xas>E5Cnoi5D22$g6O+<>I9KoxEX>-RaB}V7CIV1AP5A3AP@wCsJ0+R'
    'Kl$nekzBYLf=E?Vsvs6R8bKfk1c4wB1cIoJAbx7M>N3rKy0sqbcW$p2<lEA-^~Thy_q=}3o)&|PJ=!hb9s3lkj3oPw99lsuXa%jH6|{1ATCp@~'
    '#j284EEBb2p%t`(R?rGsK`V!%6=!AUIm+i&^0%u|D?1dcUMo9|99lsuXa%jH6|{1ATCp@~#j284EEBcD+zMJjD`*9+pq0bXN^522wPt(hO+{MC'
    '-L6ip<S(`at?V*#Xa%jH6|{m@(8?id#nPk|t4dn2Ow<Z<D`*9+pcS-&R(>B^X=<8VX;x`&rD@{aN|U)2w1QU93R*!czZ0!kn&wulD$T7}CeE#J'
    '-U?blD`*9+pp}EtinluSJoRU;<ZW5ASaO$Jj#+jXKh%O+Pz!27EvV%%)naK=i&ZVPSZ1n)xfaxdT2KpWK`p-*wKTQOwKS_W*U~g|u7&eiPz!27'
    'EvN;x{C?D8X`5@Ysx{YQnK{?Oc`c{~wV)Q%f?6uCmQkzTeiY3=glRW>Sic%JoH*t>Ly>@C*3~;b`_6OrCCkpuYVgYLwW`C)KEsAokP1>kDo6#X'
    '9EMbyD<jWSEtSp9nkAJjwQ7;d)?q^`NCl}N6{Lbx4p}N)-|LS%rAsAlwhB_2)oPGb62pd6kP1>kDo6#X9D-Du7kUo=Y;D&ht86b;16Hz(99lsu'
    'Xa%jH6|{2DTJbb##jBE5JQKCTQ6RK}R?rGsK`V!+6)lH>wzg}SR?IsPlx5`53R*!cXa%jHm1=24UgrAXJd9_l7e5iF+@w)}O)|BjM<UruQ|gYa'
    'V}LSH2FgGgC<A32nlh|Xlwp~x3`@T<ER=yVPzK6C87QNI%8;q}IE%O7QGMFDbXwt8)7=fcg6_SRuPr^|$Wx$FcjOxt^npIm2l_xC=%ZTt2yPa-'
    'a0)@<x%#>4i);2RK#x=Qu39Nx3K&#GgJ=*9qCqr>=Fmi=_#SNg7GSDqbl#V(fI&qxhz8Lh8bpI=ejlPK>>{0g3#gW8%DHT(fI&qxhz8Lh8bpI='
    '4ofswx{o?tRr09QmC;9?_`V-RgJ=*9qCqsjL(ynH>U34fqfS>wA9dn;fe;O%K{SX4(IA?#k2+md@~G35(MO&5{vbqyXb=seK{UTR(Ww6>p}i|G'
    '(=?jDU8sn0MK!1f)u0+wgKB;!s`0d|#;c}kJhN59(I-@cYETWTK{bc08qJ5F4E>Ix{ma&^8je7r8dQU7Pz|a%MAgU}$fmO|xTS1k+EhPFvs>F5'
    'C9`9}>J78=D54kif?m)IdO<ITq!&y5Y|L(Lo2VB{^9;<+qljM63wl8>=motTj$WD?=U$psn|o=RI``6K?ghP|7xaQ&(97>gFXtNPUe2pF_i}FP'
    '+{-z0FX#ompcnLlUJg+&-g5AH<(>JmYwH>XvvbMn4YS8sq8Jo|Vo(f<K{1D=m=b1Tc5U5E#ps@i*<&nG42nT9C<eu#m_t%bQ|nwzvwCweO>^gB'
    'I3EVZpcoW`Vo=QQOEKqK=VH#QHy3km?pzG##h@4zgJMt&ia9vNOj>oBn2+<|i=U}a8<$QiJT>%g;6<@lgkAEtsYxt56sp!KJB=1PK_}=0ouCtR'
    'QZ1eM?a^euOCCI0wI@Dl)NRLU6hM=_4eD`8?kbg{WQRdQB!~o&AQD7^NGc|hRMyF;i{s5If*+ffuE`o3OHvDqEq#q5Py~uV5hwyhR7(*LcDp@G'
    'Hu)glVV><P7l4T@jp#8&rplDkBI5uc41|F&5C+0P7zZW{s|;aS#tOsIE)3={5C+0P7zhJlR8<&GD&ZVevl!c((Wi_IwJCQ-reQ!DNCRmg4WxlI'
    '4o(_YInuBUmWE}3G?>#s8b||aAPuBZNohC_<9Wh~2gOchY+AY|S8OavEho10HHttHC;~;G2o!NBim;R_!ZJ@0mOe!=7l9&B1d2crD58>z==yGV'
    'GS9Z)VVrJuzdaok1&WLfX|hGu>eMnL`(Pjr#DO>v2jV~+2PBS>+Z6*)f;bWl87Piqb^64S9t^~RI1mToKpcppV&dpcy7Pz=4_4kdvJi4)ZAp_k'
    'GMA?o9XZAWb)XK^fjUqJ>ZqPNoK(y?O5KsMEq(IHTA*@!WFHmufj-a&`amD(qeA+awCeW5-Ec7ICV#G94O`RoUzA?o|Eo=QZ&!m#_9#}JQ1%)*'
    'w1QU93R*!cXypL3qQ@!~l8S*&K`AH&rJxj)f>I7nDeXtm{6l!~VD?b#n>SV-Lg)mkR{&FXYgUg_cC1w?RCXRVq=HnC3Q|ETNaZl3(sb7EpDUEg'
    '=4Q>3%9dKSNM-A=Ar+*8RFDc%K`MtVm9Fpg$DPup5;t1~smy9MNGge8Ln=rGsUQ`kf>aJcD$NT$hkv%VYm!yA7pnm)Sw;@6pcS-&R?rGsIcTkT'
    'nzZ6oNh_X-THz=VT0tvl1+Ac!!_$hE!$4cxHB2k!9SF)Ya%csupcS-&R?tedw9*lIHvdMr#1qARk8jpi^hhLIX-eIZbqr7j%0L+?17)C$LsN!T'
    'iZU#7m0{^u2KR|Y87Ko~pbV5zMP=N%z1}R{<b!%++EjXmNbVN(=p%obN+q((IH3|$f=W;cDnTXHSBa(cOpx3ynxhg+-`O9#j1wwBC8z|Ipb}J4'
    'b(J(rpDSrrW3HrW)?7)Gxe`=@N>B+ZK_v&TlJnB%O3te>S8{IFT**0eC8z|Ipb}JqN)AjVUMlXqQqC;dp-nw9*`-jWHrZ{o&<Q$0C+Gy7pp!$;'
    'iP~9{9ojTWCmLr?b{j2pf=<v0IzcDsr20B(YM47|R%Py_Y1-Tg=bNAtbb?OM2|D>b=;U0(+{t-W=1$H{n>*pW6Lf-3&<Q$0Cx@mJzdf4F^DcN4'
    '<y!#^*|cyy-q=`?Qdn&1Xas>E5Cnoi5D21Tf=K1sJl2Z2-|EJtYcj;flGGw%OJAc16oDd81d2cr)l$TR-EPm4O+JWc+>88*m@JLxF-4}zl+q&O'
    '03Zy6fiMsT!ax`YCJd_#VOYir!_qDc<}eTj!ax`Z17TEE7)~nT996Rz+ndp+j109YcSfdRKpIE`X&?=xfiw<I8df>dund-lWq>r8(?A+X18E=)'
    'q)|y}B)`o)^-b=DOl53Zx+Yg_EJ-aVw)8cMKoKYcMW6^2aVUzglq$k9PZ5?rMKBkEB2Wa1KoKaSl8Wg1Zg(=zw%}o$Zg#&t9TWwMj16hBMb_%n'
    'G9&w7AP&TVI1mToKpY1oj*;6H15koE5)BzBj%0QE#E~8h#DO>v2jV~+h@)cS=uNuwh!YQ1-Fdnga%62ulQ}Y%rxqPK#shVr4%C4<PzUO$o;sXV'
    '%sEQkk+Cg(^2l1Ca(iSS74(5V&<FZJALyex`WUt9-mTa5`mXH<QI~uoPB>@ZYfUn@QocB{m!`}bdBy-`pbV6OGEfG}sFX6qUh{A;=q7%wUkzK+'
    'h5I!WA&u-UY0*ZG@>Gf=*La`~)PXus2kJl_l~Tup)3tlU`A>x%N!hY&Ewb2HkV;Ez>1FhQ9?%1NKo97lI(nEa&dN_z92v>nO8KJ5UYasn<QW5$'
    'fih4A%0L+?qf*M~PR8fW{=o009z^N3KQFc{UW+TX)ua*>+q)Y{APFRaB#;D>sFWn)WBL=d=UC)wNsBLXl&6v!xyA!^pbpf5I#37dsFXT<k!A6}'
    'vm^h_{f)71xmrZAwiuOu*wC|R0WF{fw15`SLZ!42ohvvg{EW;k%hsZXjRmQc!<Jq~59k3spa=AT9;%~<X>hzgI1e!Vu`oMqTyptx*j$b>H*D))'
    ')PNdL18P7GsG(A7kU{v<?1wnjI_J%whOPa-NIgpfS`4whDwUMT(&>l-Q6LILfhZ6~<wWrq?<ofl>Qk$J=k`P&4ZOn6g2>g9He+NgPc=3&j|b{N'
    '9jF6!pbpegd3Cf(Q%B2eb+pV-M+<eJ4%C4<PzUNbSamcmOJm2Hri^jNy86n7$NC{b9>@cEAP?k$JSs1b^JZ!CSksg-@>o}2+45LFB*+7KAP?k$'
    'Jdj7F<RKdFkA?%$&vDkxpQ$&eR-AKQf4sjvay6z!ANeX&>5sevgFp}n0zn`M1c6jYAdSo9o_74GKD}H%sZk_d0T;mT(4Yp1>{6vVjqEl^hy;-!'
    '5=4SX5J|N}vbgV^daB@Ak=)Jc(MZl3m0BeC@E{Q+f<%xA5<wyrl1S59in`QO^~SVW%(tiIYf6Ja^3|tKA9;rac_0tufjp20@~DtJ&YQ^z|M*dT'
    'x_tg)r#rBVI4rYEiy9oVQ<>^Gvg0_R5>$dpPzfqQB?q7qt8|rE)li9LmP#yCf=W;cDnTWv<d9Udct{}iRKe$q<Ze!nM{?Gv6eGEZ2Z<mNB!Wbc'
    '2okB5L?->=V9-tcSkU<)8yBrd7#r$Qs)>zVj2w^yazGBq0XbAi4wuWPHKv}bTeH&|i<ra6-JAwt<gZbkI(8WzB!Wbc2ogaeNTfm%nTocDpW=Vx'
    'P!k>G3_tD5j?K%~V2mvVsRPB<UPce-0X?7x^ne~Jr3Vqf-T2?xJ-Qp0`_p0Vg0;9|eKji4u&Hwq17bi7hygJmhDwQHXt$$(>O-qgGOS;+7Bg%p'
    'M<p6I_AhEc4X6P%pa#@XDK*RvzCWZN)f>}B(MK)vw53HBxe8Qijhv%`KF|mHKp*G>eJIyQUSxF6n|1r)F1Xkr@pJuZ*qYXRUcYBg>%qV}jXjBx'
    'tEKXRk*_>uyvREq-~wEL3vdB0z(sX%Q5G$XhJ_KV02QDDRDcRl0V=A4iuR*u<srC6asIG=K5RItII|t6SpY3^wWbFc`ASsEjJ#t5Hoykh02^Qf'
    'Y*YvvR;k#q%*Tdh4mK>X0XDz}*Z><~;}EcM(YfedbQ?}%)bPtg$Cg%2!pF8!H6UdBn1K;60!F|H7y%;{!pK#rGapyx&wN~&GxKr9%m>&28(;%$'
    'fQ>`IM)RUnZLDj~ByOy&QG+)&3=iM{9DoCG01m)mBsk>PgwOIU_|EXg)JkSq0m{hGh8|jEDNLy+vh_ZQ01+SpM1TkoQ6WV5Zg--V5Q!E{0z|Sb'
    '4SY!VJ9q#O-~l{<2k=l4Jjk!uR6PT+)P^a5ST3w&Lage25CI}U1c(3;Afhseuu4FLWhx>p-H6~Y28aL=AOb{yh)N?u?O_bd%)=O#-oqFchcUnd'
    'cmNOJ0X$R#4_+$8JVmEcW@<$bBC?gH6cSm-08D@hFaajO1emB4CW@Sk$k2vakSJ|FB3ti+2oM1xKm>>Y5tTti@uya1YQ-E#lze(+)-eDRU;<2l'
    '2`~XBDufBE6iis=V#3mo2@Y?72`~XBzyz46I3`pd-mpwPykY4+yuk^VAOb{y2oM1xDuM{H%R3wly2+pGSHspcbt7h{Q2-?}x1<LY*~?SPiag^1'
    'F2Du202kl_To{Rqq5W_-OSAZaB3|TZOAj#e6sVLK`9=j~fDDiUGC&5%s1h>DBZlF?FoYN20$hL#Z~-pBMRjoTlVd%m@2oU?M!|h4a<!%h8Tm?7'
    '3XQyD12(`0*Z><~18h_d8&;{<u*}DXWezsDdIW5M4X^<=z($3z(OL?$mcp5h9IffXMxGKCV<X?#fDNz#Hoykh02`IVhE*yyEc3BpnS%{xHoykh'
    '02^QfY*YsuqgMUFc^J?0EP5h5^{~^k?>whIv5JsJ=B6|;BYS;njge<a00zJS7ytuc01PvMF<DHri85ehZb}m{ve%~;7<q;SU;qq&0Wbgtz%UXR'
    'emLho3o}1?>Ey(!+m2HNF0wVF2NoG?Q;Ler!vH7%1)u;FfC5k$35thXuW$RGW=Zy7x7$&SwF-d5=Eds)#Fm<r!eMK70|vkV7ytuc01TA@gQHjs'
    'j)`P&beh2d4WI!ufCkV28Y+l}w)ddY45`H%LWXor<%(gky8#1W01SWuFaU<ifWc8L2FFA)I6BS1ECXl&4WI!ufQBlfVSY-U5;LS0ZwMLEHI*xd'
    '#qI_SfB`T72EYIqDgy>bu^1c^$>8WT1G5aE0W^RH&;S}Li3YE))C|eRn?i<EO(lzAp}PSCU;qq&0WbiD%7DR9EC$C!GB`TTz$^o301co4G=PRm'
    'qG8fiYKG+EO(8?7rjo_5(A|ImFaQR?02ly6Wx(Jl7K39V862HvV3q+ifCkV28bCu8(J=Jg?nI>;78)>w4U1Kk>xSh{2M)jiH~<IW030d=2S>Ra'
    '95d<Q=(Pj04&VVifCumZ9;%23U-jLcd?Rn~<ZHdZlg|yFfCF#<4!{98R22^DryYDl=N){_6AwN!4*&u{00;m9AgT+9(d1SwAJPq&f`^5wN~Xh7'
    'rvnGz033h=Z~zX~fP;uIm25~Y-VijTYbuuvi`@+v00UqE41fVJR0a%=Vlg--lEKkw24)#R184vZpaC>g0S!*q?hWS&7Cek1%qjv6o0qQ%4O@y*'
    'i-xT|4jMoMXaEhM0W_G22Ddwz2iWXR0>0m@4=?MJ%Oc#cwFymh*j|@fJ!I*600AHX1b_e#0K!Z_+>ai;(fvHg!pB9<J8Ww~6CbvgrB)9a`W-xg'
    '2k-zMzyo+N6A$vH$9B8zq;G>PpQ$^Ide7_k>}eQj&1n&o$k(1GSmZ2HEi-bD5a<9MpaXP(4$xsHI{ehcd(ro--n4X0px9WFT0U&)Yp?(ozyeqR'
    '3t+)GEd11N)n#n`<XDgOJGa*hf^6y8dShxOcZByu#LlgjkBi+)Rfdjz#te*r5ikNqzz7(rEJiFX7_q8@5z9P`SYQN<fDtePM!-n5Fw$BXY%Rqz'
    'BRN~uiIKdemVlA`V+KaR2p9n)U<8a*79*Axj9Asdh-Dr|m>B^hU<8bS5in9cjCiY)%~L%$lCNFOAjw&5IV8zFa-am1fD%vwN<c}4QDSLAiB%<('
    'SSF%`nG#R}N<axH0VUNy$*5ItKZ;f!!bqDvtX~bAPMl?(p@_L)d^<h+&U5w^$&QU`;K;6(s<X)M0|rEZ2oM1xKm>?X2qMi39TM5ns!0^tR;mVz'
    'Y#%c)0!F|H7y%<-#7K+;v-%6^mUymyu2v=cHLr&!d)BPfDSHnmU;!+E1+V}Xz*0%DT<HbNRRzIvWiVJc+yhtu3t#~(faNg3qWZwkp6#1S7Oe+^'
    '_8(Eu0$M-|XaOyt#ay(^F9AtDle{EV6<2m?StC?-Dpx&Gb{s!&0#3jQH~}Z%q;fd1wBf|67EUZPabkfJZ~{)i2{-{KmBop-oNiuOb0@pCtq~|Y'
    '7Ob8qJC7nr0VyB_q<|EVQbD9x8j)gE4JnqXNMR-gq<|EV0#ZOq^^h`2-*aP9$_?9gY*YhFcCA#MM|K}DAOb{y2oM1xK%_ztQF`6ZuC1Cx5q;O_'
    '>@j9w1dM<YFak!vh>;kXPxnU;RGw3kzdbzw$y=mSdgLD=&;dF?2j~DDpraz_uu4XURRwfdCZU7#KR^fQ03DzMbW{=@UMkbP66QK`x2Ojm`O8#F'
    'k6p$Ie1H$|0Y1P7_^1p%tkUseRRbTES@>Y)1AKrF@Bu!+N9FL*X<XJjBD5ZyhjEZ=$tQv+ll9iU{#ZOeX<Y6pkNnM*&yF2xRA!Eyh6iu}4!{98'
    '00-bO4;&)AL?Vg3<>6q^U3e(=rRSmMSo>llS5q3Gk*_{=$jCb+00UqE41fVJ07k`taW!m}M~vmhOhd+s3N<lf<-h<9paC?12G9T+Rf9%}%rG4p'
    'rVs;Q01SWuFaQR?s0uJ1?&eVz{v7TqM`s+)0?qb&1vw*ci<-ERyG*s}*kPQ&2lxOV-~)Vsk80y%+PW+mA90I@<6~B)8Td$y6Zilh-~)Vs5AabP'
    'e3U)!QHfa(qvkxA@c<p519X56&~dQnP&?mIiP;XL<~o?^03DzMbbt=fQ9X17-xYTM=ro=C$E7Fg=hO31bJ%dsr`9~)n$u<h`q;H$Jq+2oTBRP@'
    'WAK0pFaajO1egF5BVm&GR@3>z`g!NPciy!eYh?MWCi@G3$*v9SfyvI*Duu}&g9l822`~XBzyz376((`0m7|e)YsL~JQKD{lq{aqpfDNz#Hoykh'
    's2nz0t9PWQtxLu3*wnCj1ld@vHjQi<JYWJ$fC(@GCcvbsFezykq-FjrNXwjAkQTEbU;}J`4X^<=Dvphz<&=94$l3;tBFFkFb*N+0AOR2H0X%>Q'
    '@BkiFgNORrjdUx<5kvEA#?lyo2`~XBzyz286P3b5%PIF9j<pS%MU1soYSG4qK>{AY19$)r-~l|U29FYEF<R!%Vzl(nVzijW025#WOn?b6QC&<}'
    'WuJ>-RpML>%cyfPEY8IM9>4>501x1CNbpcTi(#32E{3K5TnvkIF~9_v025#WOjH{aC7+97RpML>%cyfPEY8IM9>4>501x1CNbo3O7NceEEJjQJ'
    'EC%OdfC(@GCcp%ks4yljIv2eQ9p90%y<wBck)c`*3duBhzyz286JP>NfJs$hqI|YvO>4#yMDOg!`mq5UU;}J`4X^<=Duj)zvad+Fs>Bs3S4Lft'
    'a>W%Xzyo*y58wej4hbI0XE(0Qy&~mG{}m}$T#*7Mzyz286JVm+m?-&*l&eZyk#c3!6)9I-kpet`2k-zMz~hkMQNk=n%iLLvmi}1`u1EnBU;<2l'
    '2{2J%Of)abJ{e;}izXpsQ<)mTv3Z=p2lxOV-~)VskILag`BcVYGv+Zv?_|b`VE`0>0#E=7KmjNW21T=7ckUk-@+^F^em*@PHdgI1*SiDJZNsiT'
    't~aMvVd^NP7M(OH<_(nKm!d`;<bWKI19CtP$T=wFOk0=6AZJA_)k4myH((@kR*yQ!0XZNC<bWKIQ(feYnrrsz!?0^it-9?vr8y^aIR;ZF`+Lx@'
    'ojk)2cmNOJ0X%>Q@EjO;<Q{%1@tRt}v#Ok`foJ7=Fcds%h9B?%9>4>501x1)D0nWW7sKX;9sKi>4q;`g!&KsAD_*m0vW_&U0X3io)PNdLb5N+6'
    '@5Jjx%|ad3Ld|0FMxthUq(Kd+0X3io)PS1mqGr_GxCdYAp{YFO7)+e(??JzG@(e%V0X%>Q@Bkjbb70`nbS_Swa;gR%6VJuTIsAYJ@Bkjb19$+B'
    'k>KexF6$4@!+4f<@e}iCn>33!E|s+*JxIx1ol<_}7z|(mEPw^D02aW)NLbvtz1}Rx<O8u|+**H}pa5ayZcz_4@|UU99J`DY_y8Z^1AKrF@KIrW'
    'q;8N_8bB6WH5nm`r5XT{<uL;zU<8bS5ikNqDvpuzAyN^97zPmV0Y1P7_y8Z^qmuaW-R@)_Y3k=d*0yI7Jk}SffgPJh2y}oB&;dF?2k59QI?4ox'
    '@z^j18XyB?fDDiUGC)Qpkx}BLhUxPerc7lpivchI2EYIq0ONpwQ4%gphJ^{J02F`%Pyh-*0Vpa53TxVQtW_6VDsjbz)=c5WrV>k5#^$jB8(;%$'
    'fDNz#HY$ycY3s5?Yy_>Djg7EGBd`&V4cGu1U;}J`4X{xuY{W^XP#E(j%wk2NE-jWwbv=Lp5C8%|00;n489?Z`VV6~%8+KXd-LT8zhFxF;jDQg^'
    '0!Drh7%3kj6+wt$00AH11AKrF@Buz5iH|aG*kx7WhFz9PH|(;wVHfBC9iRhrfQ~~&N15O-9vj9$17v^<kO4A42FR!+GD@7(FnvD5l&K76F#ra@'
    '02lxRU>p!IO2UQ7urL7?fC5ke3P1rU07d0Mak1hAL)-5uw8fSdO<~5iGD{Z6_HhCq-~)Vs5AXp#Duj=+&dIni{hW*oQ_jh_;G7JQ0Wv@a$N(9K'
    'fsB%TVKOaDPz9g>6o3Ly017}+6;OD$Uf1ipwqJMdA7_!4dUl>`!&U*d$lanIX5=qZsW^5SC-4D2zz6sMAK=4me5?qxS(rJ6qGMZ2nz6C9ymF9{'
    'VLZSExBwU60$hL#GjWlcr1xguM6U95&>h;;1dbgFRZEYZMhk?15D)@FKnMt_G(w90rj#AqH5((Pep|}!BL_-A2`B+2pahgu9wj<JQW=aG2N4hg'
    'LO=)z0U;oyq6ks<%_+HCGz%b_zCC4^aRMLU1AKrF@Buz5jE{2RQ2}%q1P-tPHoykh02^SVqSz>PlEeJ@4RfY8nAHFpKm%w14WMyQ&?pTVW&^_v'
    'TmTDT0W5$8umBbngoQP2I@YSoGZpz_Lwn{hV^fi(Yh&{Wfez3CIzR{L038)a$Fy}>DmsGpOh-ppq#@{tM+kI)4$uKQKnLik7&_uCQ!0&lBc|~p'
    'QJE%Fqy_*;00|%gB!C2vs1PLd-1y6?(v80?6L0)wapNyg0!ly$C;=tE50vNtNo6o%97I3}2mv7=1cZQ)iXx=k8-H2Vxbc@|){Va`Zu|v4zz6sM'
    'AK>F~@lh^3Du51yzyUVE2G{@_U;}Ja6dR>Za+p8AVb0VBvl>7HXaEhM0W=N@8l@q_Y+#sy3t#~(fCaDs7Qmu{u(()p3Zm`zlp148o91w1TcM@P'
    'WBX`<5D)@FKnMr{A(cW%dFO0gn19a3g*oSJTyV|?*Z><~18jheL%~LA&M=!8X6OP~01IFNEPw^Ds0u84_H;NHbdx{VuZFGZ%Gr6xDnJ;yThv31'
    '{ADUN$1dXpKEMa~03YB3d{i4B@?KKq;v;U+aD2?lGy@-raRMLU1AKrF@Bu!I#K+KnxSOR~{6O*C#|~}k0mv?eDkaEnqXj}h2nYcoAOwU|9U+Cy'
    'f$Y$x@dznr9%Q%C0wEv-gn$qb0z#^Ukn-n2Dl!XV*c=EmAm9UhfDiBiJ`NZks^>o{GW%iJ+y^rs-~)Vs5AXp#s)vuCoYrGKbv5VgnR-L<4aN3o'
    'TMtO~DOf2@_8Ucz0#ZN<NC7Dz<sguf{^Ip=k&^rt`br=rS+G$^Nsl5(0VyB_q<|EVQc0w!xhv{Ut(plF4R=Z1b<DsB7y%<-1dM=@N@ApRhg5?g'
    'X6XZX01w~+Jb(xAI0SfD8V}W2RXbE;nR=+k;!q7p0VyB_q=1yeL`wOGYOLxUs<F&FRAX_d28@6aFak!v$f04Rbca-fAZF<UcmNOJ0X%>Q@Hhl`'
    'Txh)X_o8Z-{$7}R>F))X{(=;c0#ZN<NI6WTlz*t^qB@6aF3dYrbHSk+Fak!v2p9n)hlY{T9a0T~n57Tk0X%>Q@BkjbqcZRqwdxPf!+0KP(G&AD'
    '>)CgnQ=gnSi(p5t<}{%rUyW+Xk#~3i2jBo4fCF#<4l}_qSxmHvQgGyIP7^rt)u<L6d4~sZ01m(bH~<IWFcKWT-ENOWio5P~*z@|6QO};v(rwi*'
    '=Sk=L&HC`7K51OmZO18sBSjXY2P=iX1ErKHc;JB!&;dF?2j~DDGojNSP3EU21dr-3hcB1EMb>Ft7D1U@&1u3)z8ckXB=7J54!{9800-az9A<(e'
    'Os;vRrDALt<yy28Z52U|oDFJ1N8T#cvLpW>0T18-Jb(xA03H>A$HFdj8T8oJq$&8=T4%}p$S_m@1b_e#00KY&h?xLMP36m({8^qAX^t$dXktdD'
    '($vZ#;~0PmFaajO1egF5Mq*+#Ox#gIKB%8Jr;Sy&*DSykc^cD0i(C~d6-Lg10UAI9XaEhM0W_)%jcMz$JZJ=s84ZoFLKC174-C)%8bAYR01cpF'
    'Bs3mwy}s>#nnl`!-EOC5=L_&fhBoxDB1>UPHIc3NK?H~Z5g-CYfQZT<!YM-$j<Jkzv^&B95<mh-00|%gB&rIDw)ddi5zB2D!iW`xm8*!Ay$>Ql'
    '1c(3;AOb{G1`$pfig1i&grnUN%tinSAOR$R1dymKBo?nFP-ThL?HIy|HN}-HinSvFDnJFO02QDDR8$ZZ)7E7vsF<~5A}Z#^X+%YGBtQkI02QDD'
    'RDg=gpu#C@s=_gPuEH^5vch4u0$2bGU;!+E#o@q0?R14>^n8V5#Ds;zj0La&7Qg~n0E<e)!t0kaVX?9uQ&_ROxRO<|ZX`ejr~nn90#tyC3Zg>g'
    'WW~yMOhkpYsfu+Y0V+TRr~nn90#sB66;4@G6^_w!6^;>;6`WfEEPw^D02aXFfMB6^y23GfzQQqL!h&-vfCaDs7Qg~nR2vqPuJZd|Ew^C`CzcB<'
    'SrV&yA4Gr%5CI}U1c;~%BAhZ5;TX#ZN4q1qizbi&5<mh-0Ex;%V(7cwiE>G-YQ+#rtSqfuORO0KFaajO1egF5V4^~paLUnyV=yNi13ba(1fT#E'
    'fC5keipqk*FXh|9d~?4p%-8>YVLo3N1}4A+m;e)CqSBZsX?nspd49q-VS>VEh5}T83Qz$mKt;t-F`C?#K#CQum;#DbrIpNy)nfoAzyz286JP>N'
    'R09*EpZvDh=_u#K>UInP#hT*ECB@p202QDDRDcRl0V*nm3a2bpI7YL=F~SwhRsaiN0W5$8u&6F9Cb#NMvDAhsoLDZbWJ#>*eGma6Km>>Y5g?*6'
    'h;YhKgkvlt9PN%^HUdZh2_OL^fJ9{=;dJfZa2{j9!-emjY87FLZ0%^`M8@LOiX!t!fC^9nDnJFO02Nh4Mew~-C7>d1$4FGniqnaT#7KY&Pys4H'
    '1*iZOhN7Z3=_Zma{S8#baAHdfx*)NwEX9P_-tXW6Jb(xA03N`Dp?DY!KaIS*ej>=CXJ%nmh#;~wql*!lYEx{9jKcsZ00p1`6o3LyR1FkPNuqE}'
    'CWT{yDI8D%DnJFO02QF3nyB#0QiX3cD|{nd;R6d`0W5$8umBd7f`!|i%wsI|E!pzA0^e`ehnGcNHJZIGO}xlapju(%8WoTMGC&5%02v^oYRHh+'
    '8I*;L#kPz?#!`X0kg;M^KnBPF86X2>fDAK{ahJ-oyHue0w(MlPTW>V$jTiN{_fP~i^0uf69J$L>%Z?q!34DML@Bu!+2ly}(ANQk2Z*)J8weWG_'
    '_ub6Yj3#Det4*yfvJL~F02F`%Pyh-*VJ0ZLgS&Z*i3cn8k<Z2d6*QBPr!`Hy$W@|RW#k+iumLu}2G{@_V8c*s_^CPh;Nd*fyq?$Z+0%k<rIo!c'
    'UC_v0pkij^85NKLGC&5%02v^oipc2NeRt$`MQ@kljD_Zmq{d>6I<>JpJb(jm01m(bH~@#4;OI@dj@@oM^H7VQsXJCZ+I>!4SlOHwA&=eK)kKgT'
    'i&blpoktFofD%vwN<axHIRuo*3o&(|ByP77D47*&5=s&y2TDK*C;=s)1eBPGlJwMndgi~ND_6EQp@|yX>r$(VEL{&E00e*l5C8%|7z&7fI}u|2'
    'cDv{=VBOMyE<9|hN--U_b~<nX4!{9800-bu4LI~&)pA&D!4Ntul~t}CR`fe~01w~+Jb(xAPz^jxRNG-<BpoJN<uCybzyUY_2jBo4Dhh|6Rm0(D'
    'BjNC~Ryh0&9DoCG01m(bI8+1<Bk!&^@De!|J(0Krr2N{f3wiIyqRyPm-=-#B>`<s$aqKi&AOwVf5D)@FK!~9T`N<zAGEF?12U_r`m^qA0&FG>='
    'rrH$iBI7Us3P1rU00p1`6lQ{AZ1)r2nI#{s_(qGXdaI~!8_Cz6CSv3)Qmr*|j}Yhp9iRhrfDX`MCORg4+y6ArH2Zcmi?kw)v2pR5NU^ylwSw5z'
    '-GBiw00zJS7yv^hz|eQ^RDvPBcr(DTP*bU3Sn6)T02lxRU;qq&p%P#i{p70zLwfOMfMKDgQo*p)-GBiw00zJS7yyHDVEC!ss>|5=$*~^mcW$p2'
    '1liKF^~Th4T1#K{&>oB%m(ymw+rG5+q(za1l#h@?t5?RAf(IMe02^QfY=8~0sVr<PQ($9N8a7rXU}FIrU;}J`4X^<=m4{7hW!AM4JMqntwIS`q'
    'dn(K}h18;xH^sbx5`<IKsDm7k19CtP$N@Q(MUG_<a;&N&$Epf)ERX|oKn}<OIUwgikmIe+J5SGCP7&2;hE5@Gp&aTIH1=Qz?0_Ax19repg|TB9'
    'g&nK<*s-dE9cFgG4%h)ZU<d3}89Sp^z5OU!c?g4U_OO06Y+O2Vymf{m{bm{0n{*qOb-Uf(`|ES|Z(aju_ODr;Uy2w`zyeqR3t#~(fTcoUX<q0R'
    'OXl`XB1`t7HJByOh=LZ-0$M-|XaOxoq9wTHn)5K8g<Sl^JkuuU%>s0iwIMwW$y}XMZ{!#ZU;!+E1+V}Xz`{sa1Scgd>_8`;tDl!OEmBk=dSFsm'
    '^-AGV;9vtAU;}J`4X^<=m4wZeDX_V!G;FRa0UOR=0XDz}*Z><~bLg-s?@X4$DlwBbhMe6}<amP{a070@4Y&a}=Hh04<!thq#XE$gldjOKrVA`Y'
    'BW;SPUcGFJ8Ejw!Y=8~00XD#<a<H*XfsIvZ*jSZ-jRkCg4X^<=zy{bH3T(XPu=6y{*%VTWM%)xrzk1#jHRJ#X-~b$e18@LN1%YFk101X3z_BU<'
    '9A<C;4!{9800-b40&x8HXfhAF;8FeM@MYq*^iCnSmdV(T9-3q=PN_Sxj|8Xy6`%rCfC^AyCMr@%CW9<_Z?wAYI93s)$kK`?P-H4iEhjRL0hj<2'
    'U;<2l2{2(KCLZi|dlqE!!O|D!6?603jP2+FMb_e!k|O&^fC^9nDnJFO02Rif!bxSBqmUJuTG9uLY~?AZMb_~E7vKV1fD3Q|F3iM5^6Oz1z8a>G'
    'd#`P4KocdlRi#!C+dCaN00-az9DoCGFcS`4-|bH3DHc4O=a}vHikg(j*pMbjWUWpuD6$U*umBdo0$2bGU{OU_Ok0;l%}iu$$VgbstJ4XK<X`{`'
    'U;!+E1+V}XX2PO3>CTf(JXrfxB<J$B2$xoUQpCM5c57A>I(Dm7EkSl2Hb4SM00|%gB!I+NkT|JmbIK#hE)DBL$xhWOXUUF(2TXtoFaajO1elly'
    'lSwP}C9boF^{ZiP)3>+o>yq8u)xeTHidCnQy+#g{fD%vwN<axHsSrw<7n)SEy<wABlA&4+F3B`_zyz286JP>NfQgYX>4@B#U(uO(qR`uO<Y-6_'
    'JTh0O6dE}O16TkHU;!+E1+Xv@7W41*iXN2sEwy_$uLmJ})~u8!dk-gI0W5$8umBdoQW3CNdck5<5G<C#VBs5O0SjOOEPw^D90FLpRK$7Z&#~-V'
    'fF88$UA0oS6fmg31-Jkg-~wELOJ(3<>4u9{Nw`=>!-W|a-~wEL3vdB0hX5D9J^QZr_)-1k@MYqw)0{%imCe|W9)@HsPN_Dsj|8Xy6`%rCfC^Ay'
    'CMr@%CW9<_u}9r@9IFUYWNAecC^D6%mJ=Dr08D@hFaajO1eh=q6Y>k)XF(<(to?HL{Wy`a9X+7PTAWf+WFHAo0V+TRr~nn9!dO%|sVs98vLaJU'
    '`e2c*Jms{=Iv(HxT!0I30WQFWnYc*a?`q**SB2aGbz1|PD6y?7wSw5*>A(Rv00-az9DswFa7cW+`(*js-EF^D)TBhlhBQGUYjtWtk$o_L1+V}X'
    'zyeqRiz>om+PW-iW+G!lM#5rVolaOJ2Lo6D3t#~(fCaEH6BfNmcb;V8LDuhF-;)_THLD37yVa_eAiEA5AOR$R1dsp{Kw>ONoK&<q<&k8UhV`Lj'
    'r)rh6WXHh+Ccp%k025#WOpJrcs8#oFy{^}HZ9j;&<P*!ck979+$PVq5&yZb;RHl#JMhJ9(4$uKQKnLhB5glU3c{mt!6F=6khOOz+m6guk=YHgE'
    'Oba*iRjAS$c?Sk)01co4G=K)sFcBILPS@@Y=RYN`y}Y#cr^S}VYr({}npE;(dv^l{zyKHk17H9Q#(`n7IN3f?dpKl=_R1&4E=4Lc#%?17IzR{L'
    '03DzMbeM>ac-DQQ{BTI##<XxFUxg~Ik#}H#2G9T+Km%w14HKae&CgF%91hvOcrBRNR+CCTZ0~Ns02lxRU;qq&!8kBXTlMZFymxi@W07A$vTfnz'
    'V_|DW%BYZ`qk#fY017|>C;$Z$p&*vu9}NehJL4FeKT~g9P8*kX-<^!ydT;PBOSk<Av17AZaItHxD%r97umKW40!RP}AOR%ifkfoiVzi~6s<%Yc'
    '6%Ue~o7Di4-D_0`l6{5^kN^@u0!RP}ATbXl=gqoI!^bex;z#vq)3NGzZclW_z)Q^f?|U-F4((|m$PPuSv&T*&1Uf(m=l~s{19Vge9j#Ky(J~(#'
    'Epxch0vlihY=8~00X8azjm5e9)Khh9YAHD6vU9U~__1THN*%KEumKW40!RP}AOR#sf@IPUZr?Efu}F4oTDTruY^+ErA+~fhPyh-*0Vn_kpkN*p'
    'rgf3D-T2=k?}@Q_(HfAjxgK>y*w)1W0U!VbfB+Bxf{8#F+U@9{`mk}C$gjQc$hmFdT9B~0B9%<o*3m!#C;$bZ02F|NaZniBxAFz<bl9x_SRXvN'
    'etqvLgf+`7p9Sj*QKo_Qy$J$900;m9AOHkZ0RhbyR;zdhgiAF70#PYtQef7nU;qq&0Wbgtz<|jZ=z0CgC{C{eX9X6Qs}%%G#gsvU6+H_IKmjNK'
    '1)u;FR0ahtMJQ;Qgo2h16fjc&3P1rU00p3+Vkp@A1j9`WHirdjPb}QhxxfGz00UqE41hspV6gW$aBf<#IWSQB&6`^~7Z?BoU;qq&0Whcx3@%lj'
    'TySaB*#(zcPA}jJ1YiIRfB`T7233PWv6msNShP7fDDi@X)m;n_00KY&2mk>fm<oi4?%=K%4}?q77XMN6s?&bfU)sZ+)F!n_?caB5hrj#!yT9Ii'
    '{pKHUzD`z_40N&0y7T&X#CQQQzB*@<JuK70?N556=XSR{aO|KL#-E1no;kZ_ndK+$x<dBM-t2uy_L9A1FWF1>lD+G4L%vNJ7@`SZ{B~4G_%1J0'
    'SNg7~L`nRv>_+mJ{3U<MU-FmyT^AgQnbhKTewP=jD}Ps1q9lJ;b|d*q{*u4sFZoOUt_zO-pzRhHyosghO59`#N}@L1V`MBDOU9D1WGorGE-?l}'
    '*SAIZ6_>LMOVpLMi)ARu+vVOPbIDvXm&_$|$=v1C*#AsrO=l<<a%S?n-Y6$CqfN)nyT5<`y$H!SU*3HG_2)nQ<-51vq(3@jE7?l6lCAq?>%Hx_'
    'A8p^2LD=!gSalh@s;6bTgb$a0zhC~YU26H6yuOq@b9$E^<w9X1oCqhvt9GHV*AF+<A`QuCWf{HL%Q8)p&+V7VsU?-4!|CSt%-$?L#T+vsN{AAo'
    'X5DRLX!~|=RQLMcm?Tw}q$|5xhQ!h@_RG}e#g!kdD{3g>-WZvCkDMiE$ysvNn4Iko{GQ$Qezw_N-6Ld2`4V@z(`8_!pWQEO7nW6i&@L9RXUb;n'
    'F;)5c>>2q=zLKxxYjMYO<(6*wT&e&1FJd<lyU(6Ato6YwJHQ{0y?pVT?06X?zT0tq5k&6nH_m?PyJ~^uNASuj_RQgoUB_-NF-!~-!&Qjk=vI0I'
    'pS@pYQ_Sf;mccQ9X1}OSET{a0O*Xz~ux{%i5|u<HQAyM)B&uUO_bzE#&Rp`mvxD<#gGYOwj~p&T!`a7C!+S>Qx*j1yiBKYx2sP%|YkU`3qEgw8'
    'XR3>39L%2BFC*uRDL*C?ZSR?sn|p`6BrnNJ^0ErMoFZSuMwcL68>Dyr$-buv&$~D{y)}He=PAL_LNuHR9Jjph>A!1xh4>^siBICQ67iY1pZa9%'
    'bw?zr+RJU0`g(T7Na{BQTyT?Ko~A2q7K$kD>u59eAW2LTlf)!(C6ahDcBiCoImb@togIWn@MzD-4VR%IW}}Apjn*|iLWB~bL?{tjg$SM9L7hEW'
    'wR^HFI$Ea2!qfW&>f*x6Pu8Uh_Keu<y~f3?gf5{==o)jv((G!#`XroW8*%Y!*%z;_8H4o2n3KNkf2zA(SLVf|H*$-+qIK1xbza-LvYtI7dY?Wf'
    'v<YoOo6sh-3GIA=a_gx}?Ul>c8QZH1DkHbobu&}k1UJD=a93%Ho7wG>jufXmJT+h27D+O8i<;(+m!@%^dsae06WyEpjMyc1iCtos*d=yz#O~<1'
    'NQ$04tq%u-uGhb-_wOH_f<k!Bvh_vsx`LDi^!nZ<u8C{nnz$yeiR*lE{UCn2a~0+F%7tqU?9~;iOzd?XOk@+;L^hF4WE0uBBKy`JJNL35eQ#Rt'
    'O}b-m*!@)R7Z=;>8_*fu8>&(m-y1ud2q(gca3Y)tC&KeZ_|nNv^?tj!{9e6ut>L|<B$dg%wvUNzVw>0|wux<GJ6CLr?i!8l{<uE0+eHTU(sFf1'
    '^>Q%^LwZ$@63@gl@k~4u&%|@yczzgkCqd8jo%_ck^Lb&xnge>V8np?%+^Iw~5luuB(L^*6y?aC#d8*m+f{lr2<)@gf>{KF}h$f<mXd;@3&K1#Z'
    'S6N7}TC~oXURjUApkC9pL^V-OR1?)iHBp@}sz3GZp6Ap%lWzBu?7Y6{1-D(~x!{|c&>G#F>QWiuo4cGSC(4O(qMRrv%5z1z!U#7ly33AiqME2C'
    's)=f%ny4-!s+HZ|%&dExm3w0|?rKIv6VXI85luwzAJIYnbngpG=GpRe2J*ZJg*lw;IpUZ2C4Pxt;+Oc%6~DgQo;Zcx(j>7&oe`WYLtzM~`;QnV'
    'hKXTfm>4F8bH?z<``NAEdR?#Y+J3RuM6X`B-f&)7k<y%A)4@bGkxgV1*+e#xy<cP(d3<^G!i|b-<;R)Vb}*4mWE0s$Hjzza=ZkFdW4%AVFEFR)'
    'OVk?9i853sa;pD`VPcpVCWeV&VmMa}j~u(}w(IS|xIXj;?TIt4+heiqk8ZUwE-s!kw4^h(vy`VW&a;gJF;C1B^Ta$cPt50w`L0*&nPu|@Y7Omq'
    '6)Lki*?B}T5ljRV!9*|-oG*e0ow2-Jx4>JJBp0bQhLd%u4B~Vb62(L@QA`vQ#YAzgC>DLy9}Ne^etmR$p*rJup%R6Gyws6IGLcLq6Ujs}k(?`%'
    'Cw<%hRCl|sjEhH4CQ5Pp$*UKxGosg2q%f-2b}*4mWE0s$Hjzza=ZtJ6xAR<FtlpR|?^d2Gx|3)onu%tjxk}Mo_a}YQdB=3l9>}w9PKHSEWHC7$'
    'E=phaM(r1rx0`#5m?dV3Sz@*dF*};v-ud?Mo`@}H2RPN=GCf39A_42euHEmu{+?;OxUll`cBz6r6E{nLk+<Y6c}w1sx8&{CycIiXW3T7#8ML#d'
    'l%KHkri+f&WY>_XWGb0Trjn^->efu1J+WV?&KFaDswUblI#yG?L#~pm<SMyJu9B;nbM?81iR3r)HroXy?WzUq%ifjMs7m8CeM&x)&*U@tOg@v('
    'Ir90z_3b-XML@4uvd)BFRgSudUfr!^G#O1slhI@}8J#_&Z|$*jFMHAVruE*WJNAa%PesM_nuTjj>U9+<3+wg$OJ0-L<TZIsUX#~3^4gPkm?$iu'
    'qowIg<hTTN5j^iQGMCIHbIDvXm(0zcxuUZiTWsjX|1T<dQ;XD?zUexYMet$|lEdUMIZO_d!{l(T9DW#dCqcgycPx2vse02mU5d&;UhGOTnM@{='
    '$z(E_OwN<Z!%xK}@$i%8{2hK$o4P~hp~+gZmaHXf$=bYG+jiCL0WU9CV+OA%Mp+)O>`=0qY$lt@X0n-V&XdhU5t8=UEijemOVpaji82%>ajN^s'
    'U^18tCWFafGB{TTkMHZ_L473R$1QXRd1=9V6M3l`mBGBCPswNUnS3Un$!GF8XFdmCimPfKd)@MNr}gTh)Q0xDjwZXwZnB%~CcDY*?Ah%;xS~Ub'
    'U2j}eS|^vNF`rXqC`;sp?jwWAU^18tCWFb~>>1pdbi1Eqm-WSaciVDG)*lsiN%Fd-Ys}`gB`M444P8uTli6f8nN4Ps**j;pqOdk8rAv%wa+zEv'
    'm&s*vnOx49%WA$j%AjwJD(@?!_`WDInM@{=$z(EluS^btr(4wbCC?Y9F@@(fD2v}@uaUdtF1btYlDp(?_T2T|(Y-x%>-TPV==!6g@^@*m8gqEL'
    '7G-(7syE4Ha+zEvm&s*vIeRXT9J}kb>+Qj~KJ*9ei8HR-W3l0v{ZLd$Z*D_lVs9x-S$uEpbh4Z*C(FrlvYaf>k>!KVxMxobi|u%EIum+UgSs$I'
    '^cuNK?vlIYF1btY=E&XPwpHUnJ-m@sai@B%Sg_74UQvy@R9@Mq<TLq9K9kSnGx?k&pU3vtb7Y^1pZeqa&@L{aS1np+Mz5?#T}rR%S#p}3Ca1}1'
    'a+;jZozuSCo;Ys72^}v~e>Tr5QJu<(ek6~{WAd0hCXdPE9rAeO{p{9ny{^}HZNJF*?ez_qkkc!wQlHr?`<nbFzsYa%oBSrfbLaP{>lMB`9W7IT'
    'O2>t$4(53$lEq{(Sxgp_#bj~rEEXNs9}NeiqVqVhT>Y7xEJk%Mr#qBvCY#A-vYBiqo3m&0q+k49I@8P4n8^!;D9hueP9%%TVzQVlCX319JXvh}'
    'pNb3NU}0L*H>^Nm?8f~?-jcWEEqP1clDFCO_QCFY;=LDmx3A<9H70MW3}x}V(0ybu8B7L~!DKKQyfuTxrhPQ<_l(_mA>}9StmUG^HPJ8S<Y}~7'
    'xq0{Z@4puT`sT}<@4x>1hrfLH_M7y_YD4b+<-flC>K`|MfA{9*t8c&l>f1!#yda*5XX2T7&Kb|*Mc;q^#ea~_Yt#9zKgpcWE2`S1gT$Z6SJz$N'
    '?+^0De{wu!%84dA-reZA$h4k4mB&u&;Zu8pV8e3DH^Qc3_GCl8{sa<00!RP}AOR!}6%r433KX%hP>qCGtVER`mb(!=fCumZ9>4>5I8Z#?dh$H7'
    'y4!VSvORhuw^&DPSiU||Y${5TE;jcw&;S}h184vZpmCtkaC(!1rzlcO)Q5?58H)I@*n5BhFaQR?02lznp@L!1f2iL-I!)*Pv0yl?TCzq!tSm<r'
    'B-ZpQpa2wr0#E=7Kyjd;Xg>zwS12VGmZ=XBi-joS!*U-22jBo4fCF#<4hIc~X1iYaOvK71>qf+4IZ7$9+^c{BPyh-*0Vn{)fr7%F4qe|1Qmt4|'
    'tXi->POPj(ktWu3DVP8gU;<2l2{3V>n7DI$y<!!STB1Haq{~p`hQ;0k41fVJ00zJS7!DK+-t0@}3qPK*deQm_v8EnHmRQ@dpaN8Y3Qz$mK*iys'
    'LJ=rT!bEwH01+SpM1Tko0U{0_5#7o7yxAZ41s~5?UaUSutf)m1B35=LhyW2F0z`la5OJW0Q1Yc3=6s_@Szo8YcWHnJ@Bkjb19<rT;9;ob%QS}O'
    'e4ECwtgq7;@_iZr0zd!=00AI=Pk<01*}X5?59x*K<ikQG%Ji_*jo<-1fCumZ9>Bwa;=#Au?U6`w*PRY~UVk#`+0$Yrv9Sextk_(ZB4cdpZNLFI'
    '00-az9Dw6c!Qr+iPQl;!xv)%)s8}dO6(g4V5I6t_-~b$e18_K0I21oKVaQBGDKihuGyn#`02lxRVEBE2VKglIE|sYz>H|Z%3`JmA>^;B$7ytuc'
    '01SZPK*1o|>5qnkQNc$imKUp!4l8O=<cF1=2_irQhyW2F0z@1rA|`#?|5SIot_-$EuV_rHU%Wn4Y^X^QE;e>D$N(8217v^<ka4icDE^KS3(M5W'
    'iBj$svD}Bi0XP5$-~b$e!-2x#p*QI|cDwBqYlr2<>f^(TS`-OlWoLp25CI}U1c(3;2a1U4v@Vjl;OU3Oh3W&uQYDJ`u%a8m19$)r-~l{<hr`9g'
    '&~6uQhvYJ~(jirdIyo%#A#eZ=zyUY_2jFn1aA?)LlX1atNH0?(92N>u1&5_R1P;IfH~<IW02~es4qo3I@9Bha5#_TWYPVno%sPb7C-ez@LZ8ql'
    '^baibhqiC`MtjD7ypZyPf7Wur(VyrR!k_Rb{0V=;pYT7R@DC#J#uEWK_Bx*H??`|!NtYK^9tGA`P#_6bc9w*}laEjS5C84*Unr*f&>svs&mY9F'
    '4E*Q8gPVJ|J9Pcg+3?f8ZM+cwU0PP~ulR4_-)6(IF5>6H=NlJ|hV_4HFUshE$!Ki*qWRzH_<vS==XS+L!EM*%Cb&N6cR$sF6`&Ta0JV-k=!suG'
    'c<!TkmFTWJuYXqyKZ|1P_#oDhvnQX`z8%bdH?+sjefVjuiO;GpmN@YG<C-_BMfue}d*bzm1Akn*w@3F~@AmYcqe1_4Fgo?!Q}Kj575_UKd)?E~'
    'r_qz30<lo}!dmoq-;0M&-hKD?w_m)uJ{t7<Q-3113O)C^Gw@xx3O|VLgg<&7ZCIZ7`|@z&!_BQda?c#+=!uB?Kfif<{nBbadH2KHufF)<<}Y7='
    '{?~WcN9j1IEv3NGlP7JrBYNbgiP)YV4}LtY_1w|OzH?7r1=~8WQ|k}LwZV_CqHTAr=G)%Lt^LF9PTV)XKk$#^zX+E#y0>p`KaJhdv5eMeF}^#V'
    '`2Dzp&cthkSi={b-MiDacju1A$MYo|%N~mU_P%z#_=hLKFCIPdmSo?BuRa@yP4V%Oe|vONv&B%)EA9;Zn&|9)%@aFO$6dR3+qPfTI%je%Ic~gY'
    'TD3pc<Uc2;wcDeklldP8&7Vz%qEPqv_9XRImg^RQH1ONW(xcb@aun8m^lJXzPHRU&FNx<8Z$YeeVsYZ1o00dk`&+o&sFerV<LL#@`&zDYXYEOE'
    'I0|2PS{sQq>*h!I)9CsKKX~lQJy4ODqwC|N(<9OTtD}>XGZ9^bwtIXMy@y9%^wEu6T2RqxE!nDkMhwQh-l&#v<&o=&eD=&6-Hbl<x?casWBE4U'
    'x}C{Lew3moYhir8svXsyiI15dF6v~~!dY0uSnTp<cw3%K-|=<xL@ey+L;omxALGG~ZeMiav0Th)O>6|lqv&18XT-aZEz2LnhUF^Kw|j2*Qn{|k'
    'hTQgX@Rw(zpx#Kn8`1K0xB}ItuWDVf!pY@K-#0#<h~J7Wi};+nL1~HAC~8suBi6ZL*LK|C&*ZXY5B(y0Lbfj7!*55a^-TUz_?B$nx%VF28@Uzo'
    '(jTwSYTx-{3n4aXfBp9F;)S9gMHzn0Zrek-ISe*B<9oMePuiY*V&vSr?MYX>Bn-`)U^5wP{lew_z2EhIbmgL^r{XWg)^6y#Vv!H-sMekgyPhLH'
    '5;eI2l68-2cE2qf@cf!P726iEksNsesn1gDh^+bjtJcTb^|kn@{ozmYJ^iZ~HPH#*if*^X3-2af(Q5QQqW2tiiCE}q&`+aq+^3>bTft}cwDuz0'
    'B-{&V$=B8X6bw$HkAly#_`r+zd|eCb5HEWrU;3<e`Qqbz^A?pV|2%yCiLCbWMerWOXWn15UPbTWf5Ym(vAbdw>WQw6N{(I`RUWJb{-^YoMSjFZ'
    'Eh0^#hTdDPR~N01^FIl9ffCzVxnXnDOIg|+AAQvqpA~OjiP#gl74KTSp}X<D*f<7jgS+&GgL3}$y7od2hI?m^<=-wWL%t^QmV~jy8;JW*ew@Yc'
    'mliBv^{f^y|6gj2liHuI=Yt~}6Q7p8T%8sxPBb*MKXnIoJ27%Wi?76@6C-CoCWm&h+2tXYTdYJ`qI<<6-q#!P*oH$JcW2ZE;^(`*oUTijC+i8{'
    'lvu+2`QS5s9j`R68kZl>+HSDDJ+eoR=N-vSz3&Z==YJBmaucuK-wyio4wYdXEb+Q_^!)kJ!g`Q+Q_*MS=$lD*><zo~VeAh2clBTyWBXR@k|f?k'
    'T<=JZXxQEJaqh-V_rs0j@zHbHHP7Gu_cxc;^To)FUM$nhACF{A%hxZwC{?p~^?T9nB05CadE+2A{@(wwKX~jXR*JDL2X}exi2oCy8M))=BNZ?6'
    'WNoX_%fl^2@YYX)&v^U`(akSvQ7(+$M^Dyk%~#=%(Ms}MhR*3yBk_N;)n}pWqhgQNmlrNt_I9#>S096}5s>M5{oq3*7d-1E`O+23j8??-g3d-_'
    'J3fxuITfFP_b+DaXL=ECcjRsuy=aX~jrV5_G2)`sKfn8OR-8zYNq;mM4ufdjT;`b{elp~WvvNJZ9Spj`sL58|^oHY4iyNyIeGz`F#7DJ}dV1kQ'
    '7yLVW!6(lZ_78Coev(h)56G$9V4SA@JQa6xyKud@v9rCdml&~7fhXyV@7aB?Bhj~P==Z-I1q}%}dSZFOn|xL4iyxE7j9554`7QNE1LmC8Zp1qX'
    '{@0VM*GgE(53%<#tSWqe!Shk@hA|u!))`EK9C$y>yL{O>VW-Q_U0*(w%l-IdF^GdEf*fv$H~OExT;*jN_HMnq$zU?7^(V5#brBMEU9c3fl85ig'
    'xwrdwZu=&D<>>g<71fI|wmag(HS6<v4dL%k;sFg`CbM#7QaYloaoomHTFUQ_{rAxzichonIT*x`Cqei5vdhA@f-TJU7YCJmOmBjYK41PUjGhmq'
    '5|@e!S1WtgO0Z-x(!nm%&*C$f?io39cI^*h+xIcv?+HkIlUhw?pAfN4xO2zhhvPcjQJp<0tGYf4cDx<uY!F98xPx#S^`Yp3Bo#l#)|-<({!|XS'
    '@h37|sn)G~ua**ZYoy7YX0JbSrHy|Okap~@)U)8K+jqrom55F0Yq5)SJLpEu2m7bp?nAFGBpX3EYBbu?T&H?xO`b=IpXjpLM<D8XsgVsEz2r(P'
    'cRS?2(eRv{1R86+2=>#K7B(IXYF*jx!V<#)lH1Yuv284Y`fztHwfM8{KPO@{)_5`B(-bdDD0*#j&Q_qnVY1Vb3zlLFVoB=%<HAtMwI;QVU(T=a'
    '9h})iIej}vTijLAnip<v-b{z#tWvOR)BRKvCX0J0F#_q&lPK%#p98Od9JUl~--G?Ko8+W%xDT4zGfnL$9)0)_GPJg^5jm1O&|bKMD7S@2uOcmk'
    'd%TG~l38<~YI6EJU1u;s6-gjyIQ;SGRVV_nUmI557t0ck`Psek{AA5fpFaII{#7lQar<T<_bk8q@-*7FtOtA4V)wD*iQJGIda?8A{3v%zMK!_d'
    'ZTr6cNqj<rC4M^)??S9dlkk|#Y4o8IKiFgOA-<iArENuqy3tl&JQDAJ#zWh8YvLn0k`r@sdGg~L?$nQhDVm2MCuY0EQGd3hcUb>IY>nc*;<&)G'
    '_^k&2_QV+ZLOZV9xE%XO|FiIF@$09@@BioNhyKTBCr|$$(UH+4gnRZ)dJKC(cl1Ori%&;;CS~yn31<UkynJ{2PqFsN;-#}>#bKef=x9PMQE)u8'
    '^4(qs%YFZ<E<ZNW;)Grs>2M8RDvJ>7*-@<f`IApm&js?F`7LTQn6rr*9t2zMAYSBd#Cx%=exGh{H0d3WQc!$7TVcds9EGtqir3?CZIoj@IzE}@'
    '|L3#VkN@;&K8q8q7%TpKVNOP5g=kOyD!E;jk2h7fdJ`=MwlnE=dvdDy*ngV-oA>`C)`LHaZP?Rrr44s&+<2R^@DAetf_EX-qx7<(Ra6FMc$h;j'
    'Hocd&^bVF+&c(M8wH5qjyxEC{Tb|hwt~<fzAl+0vQzri~y~LlwPe`Iuj*kER{r}WHK9f6_zV{#}k0yOtPy5Dhw<VXvN8T3O+>;OQk3Y1Zozz5p'
    'w?2HVIf;+b@oBjCT@x?(&_6kfI&|fj{1mLfKfOQynCy+9<YzUxQ!)Sj{G<2b!;2T|cIJ-W%~yB1qK~|`JKK`AJ^QXd7>zw=zEO+c(wa0*e&A#o'
    ';*)y*F(^vzz5evR`Ef?g*{|obwc<@Q*3_9;xmJ}GEJEynJdfvto~%e+F-$?%WLzB*Ytc_)qa%vz9KHV~k=HA#|1B6{Ilyvz7q|N?*ycPFi?Kc?'
    'KbZ34`iJWkt6H?RiIe=7qY$e%D|Uv&GUg?ScN1`S)eD19!t9m5&EMx>PKNoK7X@h|?@wkM!7w}H6A)5i@4KT(S8k?%`E9oC3O+2s)>eFYgb9P='
    '6(V57LS*lr{qf)a=eJax)P9x!yi0tbqtAFUpZ;BZf<BCn#V6>)vH0tt5wRB}!~MxhZN{7YphD46U(P0czj`}+Gl>-^eBt|G*G=Y5)IhR#!k0wH'
    'cSMRi;{`{|J{He>w`)(`c3qy^F#Ci=sCgUyuJ-P$Z@!dIfBp48zByVx43T{IA8wET?Nvu?WPa`RCf#2<qsi^BowoPzYp3tt{n~MQ!;=rAXTfVm'
    '<6nK(9*qXhukk^$U+qr+SFxcI|Ks&LzxGEzpM1Fe_1~rHKb*-w>e<0^#qv)+oJC`^umzE!X?NV(U%r3-mv25_Q}&S)thLd+{7veOhgtl^cW>X^'
    'y!+yBZ@&Ed*KckXd*<D2MnCbP7rzd4FbE8>*y6uW`~4e7yyLNZ^XPha_v6uxV~gb+FE0EG`D`tCsTle2P(Xh?TYU6(!oBLQYxl+O)`KVZP$Y|w'
    'N9`ZQ=Rqz(_KxW8_?X3i-i!6S>y5;CiEXOf-4Gv|nt-3ET~3gQl$Wi15gf}rt$p$R=Rf@IwD#`H{|LM%_D#Aqi8)_vh@-tL(X9MH{rIXj7@b8m'
    'y#F!Wq5U!3$@|-P-^h*E(bu1U``6&#FK#~n`s<@$uQqte{4c}LWnXLs<l&<BkG1DAf!pr$=8LmN>+HqNrFH9#pWk}@=kn>JxP<tCp{(~>EKVlI'
    '$Q_28hxc+9DO%WyvpD6PnS6y@p!h>ybijtm#njOR@mIl-xsi~xJ9V8&Fwb~0TZ%}%$;g-G2K%}4-;c}7e)Em!;OnSJ@kejIx%uL|Z~yYuU#}B|'
    'pQQ_yulZc8n88bekzTdjo4>sM?*Du9ZS>Nk#?fim*v+@!eHs3#eA6fKv)}*s4}bga+w1hZS+UqJzPJh3kn7~3@6%fN_g{YZ?GOL?_NyP>{Q19s'
    'c=Ls9C}{YHZ@&NXtGCzTF+j0Orncl_2;+&32v4KFJsjN+#>dgR8yt(2!u>@W`&E$Gy&$o}1Ia0f27_`W592$}#a^S>j0}!Wf@9C&G0u||au?p9'
    'SS!U|!mDLYeRzC!@=9*F0xbRb=NLoLgkOB*S$q`2Vi!IJ-?+WcA8fBHJ5{a@?;h>p4}-DYjUW8Qcg60+Zwno9C)ex8fiFJMVa{I50~ArO$X*Jf'
    'XQ|iHy8z<p_vr#Y%HvMWxbyz_$J)94WV)kKxJhW%{_6d?X2&mo74Lb3XJw4W0};uKb8O)$7iVp6^yAG$o}LhteR9?j?<-4d@(<@tMhh3C5x(ts'
    'cTB$fSIbAzf_FIY(vM=lYyMo&vmfQOOSr7XS0<kgU%R{k2!2@$-dF9;cOPe65}xkjyZ;OTxUj1)N;u?m%c4`h!k&08k4-=S(e?Xo_qiC%;5;6A'
    'nETc}b5x!G_2RsF-ds5U>+;3rCFg&Y@!#|3wXfy*CBf+m2?d2Gf7R?eTh1$m=Rr8*iQTP<{TZ<l>9@P{0|#eM#A3dc=W&P)Wk>w)tv`4i1*{6!'
    'p>XEtTN(JHntdBUXykS~XMOv@yOSr_oIUw0IyJ2JCwbb{+5NvJo*9kX-XQ%{>LQN#$J9%n#;1No_l>xJy5e_tZm+lcH(vZJk;4D?-LZ(P(^~LR'
    'xS16#Pk(bJzAvW<Piw~y1FtQj_?IWM^H4qwU9n}6?^NuFT?@SQ!@Uw=^ndx6g&wH=+hUj8+kX2|K;(;gE45d(dgJwz-%ehOvfh31_N(uI5IXpe'
    'H*dcG{Ohm&@kWMfRHSrj=e&6n4?$R+EH2o`u7#6@HTkl*otv<u{#d(zyq3?)3vU;Hc`RC9UV`kZ*<XkM&X*voNW36eg4a3z!~Ns0zy2Y7?j-6k'
    '`QC!}AopuU!rSBP7pL*RW4X1ze)0OrXVI?R4{zT6aQvg#<vSItrko!U&OVLiOnawt-8`wa2en^nAD#qNe)gGs@n@giOu|jvO{ZV$T!$6Z|8>5M'
    'Gx@Uj$H%>sdgJ8dS-kTm_Re0*Ma^n^c75ErzCK>F=#zK^X21LFv#_|&K8s)V*=N1aKAW~~Zsf`Pa^LN<&t%!+wX;S}f;N@6muM^K=V#C4&%cEy'
    '2OYmSnRWV~zx&~DVW;;_5+fiTW&5Y1-^D`aPYb#A>}mARjri@&v~`(!h1heQ1pgy{E21}8zFb{S8z-;l53J~cm->|#|L)0W!AC3j>-cBc#uGVn'
    ')*p9{pUNvOzWnN)*uVJVZ?#`OJoQGhln+l|{lD$g4^L+wy1y-d=Hl1%eEHKvzIyi4Y?!{6C%C%)D1PzEC!?9K#iyg4_jh7Ef~S6y^PcyQ^?wa('
    'Jl(L>IX*d!*WaE$`?2qz&I$`ZKH-PwRCdDWZ@>84&3}IW?whx7zW)5b-h3&i1>{FE`UuWHdDp?;h|N+%Y;_i%dLcJNO|c;o8_tgG?6_-hzJL4O'
    'pWobk^Z8fbK9L{fp2&}I)ALKzz?#j|$)8^QTjSM>*J6o+BwmLT`gQp)K9O2%JmhZh&BH)W>iIX(^Z73b*z7;+MYH)L?Z4&=>%4*gN;&`k5Ag)Y'
    'b2$+-{4~BF^v|C>efsqKfgesE#D^q@USG(iCa2VD(K=BV8<bkKUev<%B}~vKZzs_?7UHFz<30?gH;?YEGo0MWc`z9|Vm+$W>(NRUTz7CL4`cRR'
    'do=NbgTo^^Wi=TFTck5tQg|W2%}r+_wj}P&O?)kY-52|nLBpf?vI5(`3yvF~&Mq*BUM$au4=yaI1y>x@<ms{UJdN<8gIaXeL2Wd-m6H!*2Ui|T'
    '-EaxQlfQlb_S-k_-rc<W+vnFu?eh!k>gt7c)wZr~+pXJn$L(A;J6E=KYrkx@+V<t`x!k#N#kS9K+?UN}>-@ZJHJnE0_Cmb)=*fS+{rvmy-@FYM'
    '_wv$dHms{l*S)=MH=50k)o5GiR^z<&vUPjgId_}Q#<|_FZf{#|>(X{FUf35`)@|GATv%@VW%KAsaP&bg%6joq6n5*nFI-WAYqjn3ix)3$n@+pc'
    'e);meb$e-F+E-U?*SfSC)=Rs6*|FWm%ZsbatA=%P+dg^{ZrOu{In9?B=PyKyt@ed@nRu1;!ajG~t}7P&vU6oQmzR$9^45KM?ua*fadmOk?6fZ3'
    '+lKw(vUzoT^yG)PpMU#KURND1u+ciVE?dq;!){)^w1Riidf9NBoy+r=_HDD{Sk9HzXkJ<87p+_C;=Iv$*>YOqO^W5cytG~(J(1D&#dqI)^VJX6'
    'M;-U7<-WMRI={HQIBzvCt(N%H=1b9SqNA<$W%I>F^Wwri5C7_i&!gVHJiqGLR;O)Wv~N2vL<x;ctJ%J_>?`rs8tq&As$tt!`>NA)T&v}_FWPcX'
    'wQ0AV7nb!>UQ_b@4}W|6=JPMZ#a&pJ9qYwy=k~Jo^5U}TiXv|9X7lp4W7!?&wj~yEZZ~foN4&k3BUX>b3(;lgFJHE<Ixm93TYYKA_5K%jIQNtO'
    'kBb*~B(ClFV|ZmpaAC*nT8`k4HeJsVR4YHNH@)EaYjib4;;gsP&~>iUpTjfx+>QKr1&3P&e4XHv92`|SonJ8#E^_gY!)L=kmkT<U|8w$MPJo8L'
    '8ihw#uER&K<*(0ViLM{akj9mU|D@hr_|%j1=~_Zw@#PW0iBo;|aemQHaP&Ml3q~F`JNd(P<JIzs=7}SX(FRrQVfE!iTF>?c(_ht&o{7JH|Elrg'
    '#mA+q8fFI<W$U7GAz@MBpa1^V*I(Xz{_U4H|9JEEtH1oWJYIct6c6}k;U+uWXAe)g>W8P_$t!2XzP{*9-}XPf4v#~~!wA7SDe{Ga{K9*WzC3{E'
    'N9Us29d~|SR(R;5JGc{VM!c0!IDYSlE%4y9)_MBum$!d^_k*nL-B;gz``gpg+SA~V<PY_Fe9~rp7+iWA%*aR6a?fi|zmN+#8xMNjrzi5|L-|Lu'
    'K#@m(o-RLqD`0#tJ2PxSY{r8NSY^9)d+3GFhG&M!pXIL5d9ywV&z!E$Clj82JeAYh*Ws0J;-~pMv%Dl{#hZ2py`CMMLI3_~{o!H50**Uk07Z|A'
    '_ZW0kT`p0qn0HT4l9%$#d-vI_&BP%@F>rE1edLahql2Gzw|nf1w-?^}qL%8aYk5fX!{~qISplc>#hs+i<&bCN%f+6^Bgc(b(UlT!!-x-8;m*_f'
    'xg_D7kXTvd;$j@g1-z>L5-jJpr}O0pN9*LX@%2!B`5wdLwLzPSTI9jP8{3brISO7q>&Ev_gQFc!Kc2|`n;j66hKbG`Of)KYwZ!hAJUTzm^s`{p'
    'Zo<>dj^9U(iIrg(t%8wh<Vq)ZF_X6*T|cPttXQs=W}T9+t>_xV*}EI~^PlBureP^7$M5NEkb*Nb<pf{NmfHe(g1rFOd;=A2CgOcGH`%XY3*j|T'
    '@}SPs2iKPuWw)Qcs)bjXJ)ONZ@zl~=oi8*xn?Ss1A=u(Kl)Bte{An~e^S=v!2=XI2a$a~9fjr^=Mo#J3VhQqgG0R(!72A%FzpeVPhCNab-+VoM'
    '%VA!fNlvCey@?C)lD(C<{w_KLOKxPlpI(V=b`&@939>iA$cxWMlo6-VIhZj8!e_68k4N<2<aPLx@X7c@%4n65l!#yVhwFq~R|M7D$mz3uicW!k'
    '9bF3?9AFP=AQ&;49Zd1!vmE5k<aLq*@tZiB?e*UbeD6-a`<v)@^J7K}>q2nZZV)8lxis@rO_D3eAFdO8Uh$s3i<*n~UuMacKD}Fx-gQ(_@}T4V'
    'm&+bF`r++YpMU-CMmGNEufO}^Kg5UP;^_2fHZk($S8wC(-c3lI8+k-?H0VBvw-Ft2{?K2!n9VH*{0mmQ=<P%adh($!e<i2cYS;1@WoUwE(-F;c'
    '2d4!Dx8D#S_~;0o{QSOJzDVxrcOeU+7bo`tgY{Ib1Mzu|(S{<N>ybxq#oGuz4#Cdc@o}nG#OEq}p~%mn=$QDUl<4{7;=&U%!~z!=vT(#|@HDa7'
    '&$bWAWdtENukroU@acHN@=^9p@^7~^=saCoV0=_JTHxyK@%Qo%YSEjQfBxY1Z85yHo;M1PT}QpSa*UrOhWKd9iW1&AWBe)DUWcI)UhO*H1|KcF'
    'D%{}R#M`ey*K>lqhdi9iBlz+5H4Tkm%dosjeJxK>mdUb&g+#)<4nFwNAz8Vc;Ob`yqtnIy7G=!4TKsm+8`|6@VR^xH<VbwlgOjJD=fq~&8wG6z'
    'XMpE;?di?xNzM$P<M`CQp;$k>pTkp!uCHrP&!4^uKh3k<xUPLCFQ7b{|6Tl))aMsZrM+e62@c&||B_toe|!3IzN~zWWqSiFOuPu0aQU<3J6+s`'
    '+1`*Xb_+b`CiqYY{yMHedpM~RW$rIe<26~VDe;e|;q7cjkv81XZ;8wKqrQ}T$l(eoPy5)K`KQZ<Pjp|{%KKn>@<hMn_GIRY;I~nqsCZvnHVd8F'
    'O>m9O%}p@;H#c(D{^lm!8m#?gSZ=sMd-rKHc6)E8-uU?GUn9J<Ymr`}UFp}g$ta#BTs5owRCK@xu{T)zi!CobnV%jnFFFb1@vQe_+mkmxkPo7}'
    '8Msq#G`<=97~Vi2(od3M^BWc<4spaLi7uN;kR-NOJ7JQA3k{=--x8BDPoF-G?<NqRl^YI&Y*X}C;`;yO#bttmcYR@?;L^s~)fi{-MHAr(px5s|'
    'zLwXk2BR>#zCAk1H{m0CG@9CJ2_Pje3tJ(i^c@AF34-(;1?Kk*2ro%`bsa7K^2PEs&51h#{NegCxHLGrkg48$^--4dzpD#bx;em#%dW3iTqHib'
    'I{dY~Nd0>GMggy*0g9`ZFFRRPc6`~?AFky~R+O0G8vGa_Vgdisn9Z=Ath)01`Z~BiJu%Wz0k7lh=vMvJ>?*v(MR=QDzy131@Ji?F)aBr>g97KJ'
    't+^a}+ttv)Qnp;EEZ6w>eDQD&aA8j%9+ss0)8`__=S>G6+F*PA=_dZ{hR{Er?RP|<WqGn~za3a6>XPUe*NY$e`Rkq})|KeDVqKAs7MFgqV&(aK'
    '`=zhZqwDwa#iY@-q`|Fu63@h!oG!hoxcT`xcz?JKFMLhb9yKH9D{F(<u8<hX@c;ec`e<QV;%MPzjWsXZaQma=H0a_*z~NN@v#oA4fa2w1&qC};'
    'i+AXMns0XhGU#>(kF_`RlWRw{|NE}i6RVKitd0jl(fwdzq9zY|*P`Xjwz1*z!oB5a*{|hB_&R)WCO;`Rqe-XZO^=Vx<YZxdB5HWu!)!H(Z_^^T'
    'X&+vQ*L`Swcp?99)z&S(!GSC(8tq{6+`X08zb8JG!HKBXqkFG2zJ9T=`3-i{QXd~#(sg_@kB_g#|2(^HybhM}>-Dkx?Y}gdFHipTr(l6g+Xu1j'
    'k6zPy6@8Y%I|zvdrSHNb|NPnY%O|rNV91aXuexrfZz=Pq6*rWLZXB7YQ*ItlgALPl)C12NC$Gg8;5vBnS?Ufca&sCj;rivP<*QgvhrvREzdTvG'
    'kpCvQNF&$=1g~DP0qjRh7)G1Du+CS*XZ`uM?e*~4b^lr8^-cP<vrB4%V;;#{om`7&gUA0I#cNh#_x5$Px?U%)Yl*k+?~lS=40$e<*vy9yMxXj)'
    'p$mCunfNK+eoX#+GaUP=$6_RAkHyL?d=p*FlzuYU;S?Xg_*uCR6WyLO^<2CMl=$<7-K91AKp*El9<+6xyznHvaPPzWr#JGNmNR$c*u(Gwo0Dh$'
    '(bEqfujME3P52oWFO%1~$@%OLACFG|Ia-<?4X+J~FA|bhSw+`q1cS9o9o{Io?N)TIW4N)X9X(5rXEa*zp`~cqse?!1E22%9+n0y5Z$gKRg1M~O'
    ')g9rPS<z}3Z3?f$o7Bk{v~Svj$G!}_<Id6jcsv}vdj4FlgYv1fJF#(^+@6VjtmrnH@$E97i?#pk?&s&f%nytHc61sxc@*DkGaiz&@Wn@`@g_Oi'
    'dA?2@9gX%Nd^aj?5-cLT^rd|q)g3nQeAYnphNBIfc=lQ@;CbgLs<NIcFM01gd6L`nBY6w(#gjP2*4G<_yHBfP_51Lcnw*o0TCYv|i>IVV_nTea'
    '&9hz>ADXCx#eRz~Q6=|4WM6+HPjmP*`q}x}ejY7r`2Vx_?q6<PRig0k{Z|x~6(rdub*o>t)mGo@*iPd6#gDN~U~bb(t6C~`3#%oelKO$rTJCnp'
    'V@QTv9s!a-fMiG@B!Phih6Gsa-v4AeZ96~lFMRuTPMuQ^bvuNa^?mDx8MmtHJonjWpZz*}cf0NB`QY?S-JhLbm@WJMOml6{KQ+JDJQMh5>T`kb'
    '&&~vA{N~!)jEdrGR)(3W^@+WWuZm%0w%8tklqXdM??k1<+6X~A#y8K=v0zCu_o%i4jI!2}hN&w4;JLWAD`#qzMH}_Gs`e5{S)2>X9hh2B8snJm'
    'l?-!LJ-n2Dm*k#8z1!O*MBs)zuFB1Xjaz)CqrE6c_M#l{vGsZtL&4{=V!`K@x@%vV(0Mnv)ChXvgc1z~3u#QME4kv;Ss;Zf!ZH3-@rUtuJZLk$'
    'Gow$^i+KD=cy}r1Q4~OKR$o`rAJq2A#or0$(fiAnFJN1jALs8lF??O;M}a21=<*AIjKl-uePddC$-Gl9cqeGys~#$Qw7p6;V$}?rfSp<Qd1A2}'
    'a=C&LRY8I(hz_{htd4S)W;Itq9MmJW)VTZ&2k+(7hM-aAD)h(8MX?@ql~Ips^_bc28L<t~*eVk$6vn#Aehs4-;uX(Irtwb!DOW72!WvQ@DQ8H9'
    'VYF$^fNIk1!|vuLumTl>b`oUr22v*V>A5V^6x*Z>laxf=U_A|x_I1UN5_ziP3nrS8^<s%>1b-7>V;GH5K$)tjL}Xf#&qvvddg6<woyT&YjY%uA'
    'S!rFnwCsnl&|%fIS;*n0hEU`p3b+Ylg(lly9R?s^#H3i$Hi)}Pz%8XQ_KPQ60|mXSl1|Kv$0Ln}BemRd1U@{<l^nIJ5DJ78dw6Llsf<Wkpb#M{'
    'ng+3oPI(4{z;`hu0Jec|(JxOa?nD~pQx=^QYTZu2rhGLhQT~OwRp4^@&PYuTe7t9w1ArN!ebIvuYi_Q!cT2vF0;)H_0daYo?0ciiYwtx{%e09c'
    'R@E9Sy}~nue$;S1tJpI=>aj#0LDV*DYS2$IOR6vrR3mmU>fK!xL{#=SbpRE&=HPTvrH)pWs;jrWK!R;}Lm2vPbwat6s!FZo@M>;uwR+T@*^|-j'
    '-IGQ=#R4i8rJBYRM@)&Z#Y}gSY>aR*QF$bxM(c`Q^*bSI5j78kd116JXv^=nd;m_Vw`=h-x*8yp<)@e?$X}0oNoQ!JH;ZRb?{ZnkseOT$j$=J2'
    'JU<k+^1-ZdAHM*5lk-fb2VcSo#6?xdh1e0TmEPtk)wUBgK7wgUepfR+6m&~!?fM$vM~O9tG_&h(Lb2hrWmlY*cBV(v5Z&#U15k@F%tALQGC(oA'
    '++=aa9CU<Z(<&}ZF?SNtB$_EHpbLd1VVFh8vBsV<mYhW<Ygr0My}C<CAW_ePVb<-H;y2SkE~h7wOsRToYl>Dz$X4O#aBzaFtz)S8xWlR@tgxA+'
    'RJEE@Z*v@fOrhbll$f4O>2)W3xDnY^em<tiFRgvf=+9Q~M7DM7WY;z*g2Z$VWSFNfUO9K={Kd@E-RL!HbVc{8X-stB%CY3IaZ%ThF{f1E$)?bp'
    'r)4pe;xxr$Af{G7rV=~>)3~4|1w2M03-%-g^6Lphzabd{ghZy;c0?bmXbxM9q2G>nd63>^s}t`mE-8${qOD?9Qyx6wDh#yF9=EuCXcU_;AEm`t'
    '^Jxspn1_~NE7l8HaV?w^C7?n%JmcEIAee}7I(nmKV<_SbHNKJ~`)_Vl#a&OB;&4iKucXzASizL}o#&&sp`sU}OWt}PokLPEjV<JpW_g4uJ!{@d'
    'H%--G%ZR68jvRE<TiYve;s%oFqg0dfRgkeiWk#(3t8MzdYPuQ1piH6#2sgX8IKqQDe8NS$OpO%}l{76p701XPsTNwgNbN-Y0pSG<tlg@&1`ahI'
    '+y82Xt2uqWFJ}{I<2_O696#7vf0~*h-fGcbkC#}GPPd31_FH&~$8Q%&$$?@1@w6h9wXTy2l1MHrP)UKc#`@JrSOy}{qq{)j^vLg!m5S~3lr=K&'
    'lwOp=K?8$TiT${cwlH=RrS)Kt&-L)60&WkHG3`6+l5>+c+BsG@lS`DoHBXIX(ezkXa!CUhp%$S=HMTkoV5Lb!ijchw1_Y4vh@yq@gov%FQGbkb'
    '6}uHLcPrS<RRqN%{Z0`*sZjsa_SZn5s^p#)Urf~5=1xj^FQ<$S6^oAN7#VUE6ESjTVyFRp)HG4Q0ydZhII74pn`cXAl}L6Y;{_#<f$@p58ivgl'
    '5-8q-*%GS3HfeQgsFaguZi6*ZP*-F$sB7YXg{ApOb*K;nYWTxJ*W8<I;Me?&M+J&~jLM973TQ|BRfA2D!5BYLVPfdzEQh{ah5veLO(TMrt3<z1'
    'MaDQm3NARsF&LTi__ev&rAU&dsV`Y>p?HsE$yslA*En!Qc}ZDT5)mRWN{)^znls|Z<xK`$sY!|&QBLOM=*ZG<VMi;)@~Ri}x@LY5-5he5>XY;H'
    'ox6VN+?AU()UW?@&Qm}&am{Gi#`l8PY!AZ?gYJm?&Pa{g*5}!tz<`Vx$86Ilq+!9Ks6Z)e8eNvzDgz=Y9vsjerbyUHq|nmzRMPc7o>pTiFDG5W'
    'P@&1j!ht8Xt&#YHjnTT*DA;eU*_)fK_3E3oLATZqngQNa*BeF^HHnB){b(CutSo>pAdf7xV^ZsWr$IdssSj0opwQ4y?lH<CXYm@!pO5{Uvv3eu'
    '^PrDHHZjuyQULhD_fiqNr!f~ySMH>ia$^zjfj&K~o0?fE18+H!U<_*&#wz8UQeP?5$lhS+Oe<X6I>p`&fwgbzEmz5T#+Z~An6B@uXoKCZ+$zZS'
    'UzdbLm`JCpuHcr=Nv_^&^d61p^rPfA8a0*27tVzuG4wePqoz<lo*J9LETbBa_~Gj*_dcT{OxedRi&LG>O}M$7%WRc3O+{G$?1s5ab{pE>P7Pq>'
    '8C8EavyRpR5-}<=xi*H7S&_o<r3_VMzwM1yeIrjX{D8)0HKJ*e*Fh|*=1%5-4MX^$5iI-k@(jk)6P|Nb58=2qS9w2GWpErY9N!pfaD$0BOMXri'
    '*&X;ZOP^5?4=(#9g$j>U7~(@<D?+62Kv^0H1Ea%{40zqP=L|b7AjRsu4mr3tXMO^aM6JAxo9~%7>zzS6A~kG#IiBu@oJ8|cwWQV<IN&<Ss^UJn'
    '&EBK<&bTp*{g;~fGLQe1C@yX{4<{ad!D}kDi(mS>@pR=e^N=+xNmGVDF^C<?n;4icqBQ?E>c@Y5{ErD_?;jBVZQ<1H!u(<^{@bbY-2cu0`Ck$L'
    'qj<v^ZXr|Q6y*q+dc>KYo_6$uoULJzLxo_x_2ueZsXXh#M>9zS#<F*TJnsT~v%WE?R?DTuxzY??mq4P8TozEUuv(pm%B5MlMuFxTd@)~|!{<(`'
    '^A#T}mgl%YyR}wt`<-=oce(_xsj(2xscQ99Nm}Uu`?OoF&X(p%3-}DeGw^E|GoXqCohhBBYAy5|g@&-)jJwM7#3&=z17H>#!+Hx<D^Fj&bpGPg'
    'SHHxP`T1*Km{yFHzSrdByPcqjG!dCC&b9z@QlT`3QBW!hpt?K;K3UO+yfeQ#b=ACp6RA$ps}>FAO6WxG%uY?+B-3mkg9Ae?Wnl?bpn>av>(h~='
    'tj9bp#y=4=ES*K?H~QVp)^O9C8f*j|b+Zqr54<J$+J+eE56-kF9-|8CakJIIG!rG~oVqxCe){~$lgfP8pQ2n6k~kv&MPO_JkQL|hji=8$qLI7V'
    '?Y1#%BGdp*cGIt4y-{(#ga?*$UhdXEMUmgI+|m^M9`00}dYv<5oIF`5IrI(&8g`vd5a7sVKDiJBxeS6$iFW`fx<nRRcKP$B`o>}vi*#}((N`km'
    'IK@rlBjw%z*c8#L#i>h&upItC`Yg)*V_>?29!zXkLBjDH=${N|Loon*n4*jvD`6%+`gX%;5NyLRn9_ms^}^z`d4T}gt`Z*{;$3SWBLQH>l_zgr'
    'eu|j@KwXD~NWfi~`gDB*?*UBLyY05$3-LDIji)`B(-7UKuU;ulfy^UuwEgz(*I*>1-Xh0a5QHff1f~l?*(llyR`ee97zkS!=zD;9MUEN3Cmz8^'
    'J>%K5IIo__9d8FWwSE(B#c~Sk$H8V>{cd&DFBG-ZZ*;X%zq=EH{m5@cztnRd;N6X$^pKf_)=xM?46dnz6h*33{bpDH#2Cv+RP-|}p87rPx6zjb'
    'aR_8S^>*9e3V7`3vJ8Y0ME_8OIK;*CqEDU*YND$IBuPqn_3Tl{UqgL1r+?Wczj`TkuCwbo=K=LOQq-kEKzKYgWf(f~h&Z#hFn8wEV!1gNlow{#'
    '&MYnlGpFmbjbP@~X^iXB3{KaZi>Dhd1uUY=fETYD^~)HEXzujsg?eMwhstN_&GM<W`I&_?fD^OxjZ=ZYFnhXwhU-hL5vo|4#j|7T>8lvX`g-j$'
    '{QDH@Hf9$V@%!l+|8%+8fQC-5Ee59=^TBMn88lDL2QzccpizeB*=D(1t}m|5H0SH{^9%C}i{&-nhmkLwo}F>0(k^UI(Jy|9J;J-WDGWn@`J6Gh'
    'c@VPyyIzkxfcI-bN0@hdg?UWG!~a&WOF<Oqfi$PlqnrQ~Qd=AffKGH8Q3=wrnAbe%6VCr@d<$Z|@Ao!hC0dk6tgM}xls>=WU>ZUAL|ROVV4xMF'
    'DPNV2H02X(s-e4@Qn>1o0l`wY;I!s6*1}QXy9qK@ThgQTsci)zJozH<tdYE<KMtr`^cV+kz!b9vuRtjB`6X$F>{tCk(|kl;nk`iEx7&0UCg1y8'
    '>!ct{e1OHM6N6yC4t+Et8Y88$Okv`$TODO`WS~Yv>;p}xID;X4TshwxUcy+FTRw=crBZ42NSHqs$&NOVIqwt?0_elFFVt?FyL|1bi?vHvZeG0p'
    '^f}m-u&^_wGJU&t?!tvjSDv7E<<blWAA90DswJ(`<pubp{1v<F0I5)AR;P{&PI1|YH_pnlrn@pCb3YVl;*Wgp(I+d9UamZPBUc!&IwNTTLlq$b'
    'rf~Y<0Pv>|FAN>;QJ^q@Cl1wkC&gZ7*rv&KPsfssMSqy}?ZL?y=pE+MVeTGh9+#tYz4HGnWab%O<7T124eGV8ub15zZX%!4KPkK;vQcPmMdnJG'
    'T-&=CxmjX*aZBV{B)Qp;E5~XK7&!F%q{Rw#CN((1rvWZK;C?uE0^LlaM0sfPQd#JO_AV1!m4%J;J#-jjY1A^QYe69$+WnH|`Y<^d$G3*FT2VBP'
    ';>B^h{RXC(15UCZbZR)^X`XP!LDLUlsn3z68T!Q9@hBVOnAnyy=udTFHpuZArHM7fp-`^5Xt9=HB;7C|^CC<ZRX~&<<;K&%)e2d~#B@6}bSwWH'
    'av#9aW1~KbAL~KMxdyx}IhPPS>2%j9_qyZr#Dtdn1Kx(rJOc>f7upE>0A65Z2ipM%J%W373Ol4SH4bTA%VVf+&U;_$wHkzto^dXFye7Ew8LeTz'
    'rII`|uck1UB+tg*@{-||BEurBy2p$8-4LzK@DTU>NQQB_9yEucT#i&|fl$51qD{At7r;ay!F)Dk%*5fl1v?)n&k(aRaL#AGakWuug?Jfk5ad;n'
    'BZq|R$Uu*p-2`zJRzxdDjov9xIs*!mquCInkCM)hKb2&E=M}&T$-*@w?1_G97o+9%__rQq)sffgja>&5ZD4vzwS1+ETc|RR4S=VPu@7%qg`Yx|'
    'Ykb;B!;yUV`hlDBsYd7x?8!E6bv#j{d!vsiO6LIC@!K%lX$rTpKJ@!Qkgi?2z@nzZ_`%Ra!I}L`6o^VjgXf$)0Mv7^w_=@UAnB3FcZx|bZb>5q'
    'f)64%Y)lr_p;M!|ZPYn%-WQaYoHde3=)P%)fw<7oh4F;1K68(L12d+Q)LAISU=p1eC;+XH!~*}UMp?v+vcXoXJG6?@?oBJ)20~ZC*n2C|8X6N~'
    'YsKNR?KLnK9Cc)|4-s*+_+<T^><bm=qPjBVe4Y54c-DKtJw^l|mi3L)QDkEZeMgZS1G{GyPymy|I<uVG8vt3%U1grctCaI{Dx$F5ln_`XBdxf|'
    '5##EZW#5h6gyxhpRbw5}{CV_F$4iCiWe8}WPq`DE7Pjf8ri+}_5zi8d#5dr*wC#)Prs3MYJ35E}X-V}UC>yXIkvfhs2+Xh_q62l(r%7Vf(4Gcb'
    '9W|VsLjp#IS1jLFiuP^3KygD+;lykP+QFBcwN`(y;jo$@)Tw0=4Jm$eYU>7FRmzUkR9x0(Ex~3ID_}kfaU-7#i3a!vop~&ju5@n<>l;GuD%o9H'
    '8xG-{0B@py&fggh^06_C<7Eo^;AyGAfLF;h{YV{GYW$9gEBJ`!$6-}S@z>}E?CU_JUFUJp6h33EcvmQdn7k+Ky^#s~ylTxlcdcpBxT_H!s=4IT'
    '%Tr7OC{Lt0PbEvpvcm4+2WDLhlcs3%DbymFdAaSg&dgEswP}Hh=b$hIiGpzPZss^NN{B((K7%s*C)SI};>09(R&y(=Go-gP$%%C{QBcziVl~vs'
    'l5*Uf%w$Q5$-`&TT+^x3S#t!F;qZuvIGbG(20FnuaQcl_sIpoUodqT*@cZpn&@WQMN3_^(HCbDV3k|1RmlG&aRcK0<J}fzx;3z<8=!o{zaCX^p'
    '5k=P1!*b|5nh=0R>L1l!3pM1!Hf;Ph@{+sGhQGzxb%z*IY1k3rsDTqGO>B&4zB<{HY{SWn1uCW>#l(L6W|Ku|V6}RfAw@V{I^pnHZxPK+5PDEi'
    'QlmJ_#$d-s?~SM(IE0mdDz)duv<iIiM&0qq&<8{fnw&7{<FOJ?#t!iG>8<|5w)ZhkzK5p>iWfWvQAlGOKP$3e4dW=v@RYTWBbJ859kr`TAMBw@'
    '!tsxy3n<=1n^`mwgju*JQJO~eg%}Naol8fw`55AKkw!_1BhvVghKXX2;ikrRP(;S?tle(uvt0XZG&8w;%P~D@4}67&YFU#lWSxp!{j5_g2gSvQ'
    'JB6<$PhX<?4YG)Aql}wz8XMBuNleDPlZV-PJeI=LXR-Z09gAvm3Ds(fw&LLc6c<?lQ_-uRJV`}jilYAW=9~mDWMR=2P_yWTPs?X>Ur77;B3{sM'
    ')?@|qiHjJS+)t{|DorP&(Iu<*{7zIMj=3hWN@MV;HmbtXmn1b1J8Hco;j3seKzd^r<;E8JAg4fIZ7&05RsPed$;X_!!C^?>`OGPL3J$UhyoWs3'
    '@W1lRjB`Q;JWT-d#O>0Qn+jeWLp+0bjNQYTq;^PKX-nI3QkgT24H;=tKv-w(O0ofZ4?WP|l=cVRUWwH|ksd=htXdB?2ue__(2(Ozw8ANx401pc'
    '*ANG@7e@-wE}tGD1_onnLDhn@#;H0;)sN;5F+1RY=V&`MoNXTu9~KB(1IkCq4)O4RV$F2SJ;x$GriF)ta920y*ZW-{uUl{+q4w}=c=|Bbc2Rd9'
    '7D_q`C~7(B9wi)8bd|}}sGC}KhL_)<%+FN?AK(-$E8vll?^;b$KXx4bO_R1CuM&G+HGR73h#c7{QuvN5@<dIK-2$5-qF1p1mIw(vqN~xgipQa9'
    'QO$HB!cm@5Cr$W=Yn<1O6h0*k2-*6>-r&;!LL<Mp0SV5tow1_*L{?T-Ju%7AE>KF=RoV{84_?dyxmr2eI+CW}oU{n33R9}UoMnW7=u8z?+NY2V'
    'rN@-RDn)gdt+GG*+?jyxnafP=Ab$P5cuq;F)a1Lpd<qp~FO_hB;XV3;wSWv@M{NS$+gdFfVYPLPrC|0k)+BXpl80-jyM2s-YCY06Z0*=Im#fa)'
    'Os1ZdT@;$G4fn`wD|d2O>E`4|+(iPJE2Wv{I4orFW(mMUD-r&7G0T)?Wx7efBz#uYSXs{9Rduz0UlGLOA{Xep=t~xCb~`cD<_>tc2HX<5OJ0ez'
    'Q_^PPVmNZVr9NVrnIhH5%>c)#XcTqGuPhGBdk9w0Hqt2{i|kVI&7T;_)Y1I3Z7BMC&cn})Nbr$G&JAjY$JklIjCdGGLGlp@K=S`!JP0sHfeHJd'
    ')A)>Vpa){or^NsjHunFM>z}^GW79LjTSzvDi43-d1Wana(rtCf4A318stXCmN=miDnvZ7ZG*gDf<fq7JO!2gEhQo2=h-*O{neF6>_}QFQ|D|WY'
    'Yz6UUs>XH@oK=%at5qh2#F2v-(g~7lr==g`5J5#+m3c%<jz!yBN&M@ehZyDY9Qw{$;^fz3h_+Nea(eTk7vjEVzEP5`@y--(=_L!dQ`t&kES1y='
    '7p7vg%c28*XE&eHrPUt85Y;Gp7?jvUPAW5M4TY6WkmNW!p0{wq<2Cd|_~(a*)*YB)dxp*+F@KMzrmillYZz)dYOO|I?(S630fe~Lmhxu=ar6rz'
    'YrlHKwRWqnE>IWSAkns+^B1l;=dNACjGrjrUNi50u=8CMQeHx1!+J;?rB1M|FS*wnJrz7#WfU`6Bkc%SRqX)b>ooE%r&zhvX?D@kOSFdg=&E>='
    'N9r?DRf4GDR(E6~v$42i>uco7uzBf1NZ4c&6;AAepi>zTReOq%L^LEp<JBY^!NA8f@cODbS!K{)SGrztTMbP481r~g@LF+u21r&-69GnHaq)sP'
    '3<oh?=Hyyg5U5MedWm`qdR&=V)y{4DMV7B!s)CPfaHMr14QZ2AHPL+;4R|#p%&ywrb?tl>(zPJ=c7S*X?1Djk@?@h>?lt7SL|%yKn#5u?dm(<^'
    'IF$^#u>uewz~Wz`5iK#V;n?BjifdC8cXiba*R8#8<HSQdCCM`rg>bl;*Nrkkg%vaUSM6bfPphzUF3u<}5F&s6+8TllNJ6i+88lixoJp;`b}A+~'
    'v8sXWeWnJ&2A>5p*y2}U!6;TIL%wCWEH05yMbAK;co=xY8@Rf;6DQ`JM&I8KwO26Oi}7wP@J1oA1+7kTjn!%D8f8#&Zc>Z&_Ao?)GWo*}x?QIk'
    'Y{Or^yD{|pK8yg}V%sgUT%B2XRNIS}43Am%Rb<a&tyV-g<}P`A?%EEo>dF+9!?2*C+!W~!d&)zOy<QQN0>8*lj*^t%Y&<apwX|DpgeBaV>Fgs_'
    'R#pp2Tg|>1=2qoev{z&IY_jq1R<KK{ayf{zw7_%|WbRFI#Zobz^-Zl3qYe*(<^&DlRB$OJGNMe5+gyR~t69f7RiK)ZdYiyn;_Ie^JdTlWWr5UQ'
    'jm@kOB?<ZJ&?mH`REGXq3*9(PeKP&3n8_zvB1z>DdV0QSqc0a=I~wgo`FFdXv(a*@>?i|G0y$GH!Qo49>p`m>y-e^8s-Km17aiN8j#oBXtI)Sq'
    '0LP)*Laj>IB7q~f-Q9L8(cEWBGqVer<aGmxq5a{^Df-aieOjaQ8%n^31)C9ZVfV=qRvUI&1I%l<z0t*hY8W|&h#=!IUKr-x3}p-y#GHh<r{!$~'
    '+E_<wbU=v)8(mD1i>dhnpOg0Xd)+n*yFj7yonpCgf-+hg8&i_%j&)cTiqf?asO06Eq~VRRRTNbN<&=KHbJqMe8j~6+Zj@As?(D2;3dbZT+!!{S'
    'c$o8T1<wiKd{!BQIZ1dDP)HT&-j8O6l)n{@QH9+YQ3>2hxFPTXHv|<K)3&CRO88iqkVDADx$?$VuHqdS(`Ik5%eiSO(uO)pNh8k}HA~m&Pac7!'
    '35$kNJRLTVL-}asXFX@mbLKr~!E+Wp=M;I3#jpFp6y}a{d4Ehgbj<7A>Q?YzyNm7zc(qypLsR18g%`xk#mtHZ@=4_3Mo{almt-X=a&jSKdcf7h'
    'lIg%ni{V?4Y~*Kvo%bAT>VW(CIr=$IfS9EsGx&N@6@qs&RHq!X;`P;SU*tVja9k@G=V*sdb^1xXU3c3$=VeHd>i|jJ_AC~UKMNcykAKb@zd6sD'
    '|J1=R9CPqEN*?wMk6ea5&y#>J|9NAcKhBtOd}W0Vvk{R0bHN+mlV(rcQ3X#b`dMj7hf%_)xq{hx+=C$5T4mLfwkjBZPW{F$$+4002ooUph1J#6'
    '4Xc|>9Ii~V^NNmoa=VhHBmV6v$OQ0Ak=mm65b74~MQF0@Du)Vt<y(wgjoXdj0fUbdzLU_fL9&rh)tG!8O`eR>hFwm^Qwnx48GJrIZ*yevDaD!-'
    '(5V*XC~bGwTXhUX2hs)Ur1Q37;1TC~z*&n)H^5?A4#=^}DWqCJk(hn(`G^E=ecEr5gYhmL*x@ZPek*kZXZ))=AW5RvS-u;3j;lYa(YSc4Y;&#Q'
    'SBRG_L=mm5b`p%Q_0`C!9(6@<sA2CiNVkw^g_0Qpe;zNIpDEC}S2h9LWzTH|MEcAy7Uo{%5sN`sr_9<xj7nRN>1m)D(w3FW8@SL4MP{HDL1e2;'
    'MPb4Lwy~reMocWM$}z9p{+dfcuQ(%6#fr9E-xzjoDQ-QlR)R}PGS8XWdEi~~50JGr*Nv%{c`PbCE462#qkK0rcSyEbGY@byQ;y(Jp+tRQ{C+b|'
    'Dmw(BlM*0I1@`Rr+1Q85RS7%+O`yi92XaQxZ0%H=ZfQh4V7YP0r5sXKqfeD(7{x{m5hB^{qugnhqKlRRaHiRyWiSWgZf7hBU!o@9Gh8UOVA^N8'
    'ryVmFU}xcOr=C}3B#c>9svGJ!wR##JfwhpXz0##h!wv@h&Pz0z2*{$0j3qr7_SHR6RPLyir2zr7<kN|Cy=g2T7+>eMu7MF+V3V4qKZijX(GNif'
    'k0Ey;(q-}urIK?|TvlPEAo^h(6OOJ)*O>%OS$A=+<ER8$&W$I}6)~1ZD^zK}`3_$=?15y3?Op}K+23x7ickfr#OAyGMkv>E1GQ;Gynid6P8`c9'
    '9M+MPHHU5I$(uK?-H4`;kHV36J3J2t8F|n-^|s&IbSRCt(x?o0T)RceapL$xjBzxO803YR`hz7GSSmAVuB)<DxD#cQ3pz%RV(Dq@t|s5KHPnI^'
    'XiuTopi$r$ZR^4;|F9yC&K{V&j+!f7qO_GDh&Z>@>_ID)*?D#RQwr}YJB&_)z^i8F!V|P=lsYcaksHNTM<+m6Gnq2N<(uDKD?YwnA8sg02uMPb'
    'R5#KDPH;l-x5zGS!~$2<qe%4baL_ECb_?k-)zK}`-jU?@h7GT{{K8#L*k<`Nz~3c}GW=ixk0*o*D9U7A!xW$7`O6iE>O&QZyJeh&lKKow>LBA&'
    '6Z}ukt6g4kMhf=h6n7Fiq`3LG>tz1w4Z=vzz)sKke}Y~56*~tpO8Uc|IBrQHTTxCMMu>t6GfNb0@-ZW}KBpQBnpC>Ze{m0ZIz`<Gtw8<axphEb'
    ')Y9ls%#_1L>+Pq@?zj-kN|MEHXsfAp+1FSLwVhF?lDZh=&!Z|RCyLE3?DuY`Rge3o9dV>~hXXGT3V5Un^2{Nh(3oDHouNv~$da0@Kz&4;2((0a'
    'JDFTgGMQ-N)(@D3VV00>f0qqOCK1)(p)|Hgq0~TRHQDfUo)dQ#pQ2)J^7?E#kEa_u7MDlNg9_zra(TyM1+B}F>JkE{=1whRc&VkKLAr~YMxf<{'
    'o<UPXA~sGFDf<XbZwy+r!pxpHF*{Q!m78Pda_ccyv_}Ycy7}bN+atZ+G<E~oQ6~PV<q`Hj*2jC|)^yf>Fx}fC%@&SLT%L(FBJxUo!ZB-1?wHOo'
    'mm+Jr8sG%liMTM41*HNB{!?OiSJR||36ul2$Pu8$<yn$6K%9REIXg7an)<-_IffvUA$=}!${oY=MDd0ygGC0w46a*fy62d=TnRF!G$co~=NRGA'
    'M|7B093toH+^xv)QGHJ*XN6H<Y$Q#b%0BB7?7j@|ILv+x`K5e}6Sqz#jxuP3LRAL0A-M@qBPe>9*8NLV;-YQVe+16lpeH82BHqN8xSZ`~aeyb&'
    'FeF6<9S=sJ7mgFzZ%9B)<NiK!D7#@v4UVkl+pW&rtQ(86z=~%&e_<7a&Q_}rD?)VlsjKJDJ$3HdwF~ENo`cgH%ak^{bW0ETxpTeV1(4mQpX!3J'
    'Z?abGNEPoKh6(*=_inWO6i;TFSe($Eps|}Y`Y&C0qIUk=`6n;dp1gYbVsiW(PTNhQ4>dZS$vL>D>>pd`d}bxj@q0ZKR=R!81R6QHL|Fst93fQ<'
    'K_H&3lJTd$-C+6BmJyS(CCtBK*uJrW6tPk(SwKuW&&hXGS^<jAf~Xlz%LK_d8j550EkIIAj@5;K&?(Q^YK0hNw-hrt0}~?G!&2S*W8S3k6Hs|l'
    'RE_YOrEA3B2!seGRrr5OeNEYS9nSq@CO5iv>Dt939hW#PoLTM0%?nq*aMN?56h#h^8W??51PM=|RCR^y2`!Ykb76%cQ2HnQcO{bU(YkJY$www('
    'l2s(l@NuG};7ZZWl$O>MO7^4VX{bh3!p5=E5e}PHc|sdOdA~8y@7$O}if1`jx4g=^tby*4&YCy`xltszMX$sQLSHkAqG>8RASkjjv&u5L>31OC'
    '3eK`qt{B`oRtJX7h5wzjTPd5Z*u)=${%#N!+s?buB?wi-25)K%5Sgk5J(4iZ!<S}KSwmj`EH~BI7*nqZr{AKCmPUDS*hrHy43%;#`!cJ!pk|Gg'
    'V)^t;Wpyg$rY-N3i)ao`5rzF&7@A|S)FSR^sNflE@YjM{z+I=rs+=f{kCYFty7!^3eaKxwy6l0t1!b3<k%q;X*>DXS=EUQAiX`%z{4&MXjzX`{'
    'bsNi{@G&^gv`=AWDjqXvfB~yJD9=o|>S48>;;60eMn_ciCKtN6)1%xpsjb~=wcD{JN|}acPSZI$p(|M~<Ti0E)!W@r%@hf|&?}-c+=fb@LzFqX'
    '!w^TTXmu5Vsq0X`*^b;20$qH#^CPS?E&x}Zb8_6hB2nWncSgnlVVhuPh05wUQb6j_+45i-UMk6l@=cCZMG`DR+f~u%Q*BXURCr)DNqnEetSIin'
    '^0$kra;T3Ko~KX>d+pW$cR>h5VP<tIRZV5W({nDNDLJmHGX2J6<h>zT5!-p2|FMP^<5?{YNE^f4#T5qT2~0<Z%R%p;w-)*Inwc=I*;TTLd8ysq'
    '#tSu8=bT9#&mcF0yed;4QB~|G?^awi5X0%E*Km?xkjk5<tksGORqKN`x;7O%oSC7%(Fl5IHY}~7Z%z&S>#V!0g`*d+V2Wiz&6V*}ErV=d*ga&A'
    ')bFulm@mI|hn-tM5-AWaF1W}6=#W#xwaBM9g>v8cwGww1tvaLVl5-bDPS{BiVvrYizA$#8cm1Fp_()gAZp3i9l)!HQ5;t(z&`5IZMp*zL6frQ>'
    'Y)2WYYldl+Jl2wyR-@WR7mpRYM$*EoVQ19j3<>6t`HkND*2pn3IqqU|zcZr78)eMI(9#B!H3Ge7lvT;=bo0sB@?3K~b!^<mC11g1T_r^srKm8L'
    '5}t90M1#go{sL7`UFw26l`8H|CLM{RH8!58r)ED08bNq#(CyW_VNG|;IZ3Zwzj*WJKi96DyL>U{IXQ~NnFFNEb850lBQ`ds?8^*Azj(`fiuRNH'
    'T;3gS!PZ~lKW?$87IYPzg53Tv9MEU{QBRb}7RT~#pc9)=1HooJQNLgjY<cfgs#&K~AqCdx--|(BxTLs<P#9w6>oM)r$vqTE>Lx`O-T`2uUr4(<'
    'Y)Dc_cKC*al2Qy)uvOe}NT<b3<e}U(7z@-Fd)wk@C;4%(41_}^Q<4&dTnqp`-3+IBGI%fncJD;|!%;{}{BhK7iR^&kL~Gnvhuwkip$}=R+aOL~'
    '*S1bWK9uCNN-*iv3MwpJ9B;~TNx<T15%Hr61SeDr(-Z1==<W#(rEomU*Z&Oq-3u*LYIHjjO<;+&g>$jnX&z&vZ-Q9d->t2;)@Z!s<3uaVE#%!B'
    '=WExV{O23BYv-Q0c*8}n>iAdqP`h#Ie_nJ&tv`n@gvlA26O-9yO($Jt7uOf6G%a`$Cz8D+&jn5E#=sv8!#tNL-5B(Tboj-(1$+qFjj)E&J|G)g'
    'bv$XeRId$DzeL0YRpZGo=;60Q*24HF`mB?z6S;CpU0myJ?1o58=q%Mw@(m<z<C{G3H;I%IeNMb!w>C6&L_%yxaDe$)U@64Dm$cnWW`Wj|ZZpqc'
    'EtX9R;N_WiyTl}ZZnpFU<>fUyxwxaCzqZ>CbX$dEqoNEgf?5K{ghj(P)O{v@tEOa%eB8OoxXeK6Ys_)Oep{_>=`k#J=_-2x0Z5f67ZkE6Jv`&F'
    '*J<{BiYn@y<x5`jo?erKBz-x`ar+6C_}2O2xu;QsLN4dYi<607r&Bsq*(>UsHG~84n{gQMHKmBn1j2;gr1*XHVL=o3sKO4BWp(Qteh1J3**tNN'
    '!m+Ejd2B;2EGtV&R180Cw;DkFQ;uj=4d-jsGN1sBGnDM!7388=jsPdp-f90kDZ(fzZLI1uieFpfUd5AP7FT~0<Yg>SVl8#PP|%nC@Vtbs3T!Gg'
    'n+Ad^=ZAt9zM7rnu5$y;W++wrn>u@5boowSS-5uoa(R(Zic-nAiS6Ry>;YeHT)TMwCaRduU9Mfec=O4t7ia<bY8*N(@S)YJJ5Rz8DI;F9iR>?0'
    'ukprAw+9ci3yWFol$sm1MJ`w_@gII0*U!adcVP={j&yfsfY}2Yk3D|F&1{amg=>v9s7;Cp&zV`pY<~gXeF28#YjAF==m2ujC9^HvXRU*2n%k1C'
    '2nRMhjaLO34231<!g=CAuIvKD#XG9e9fBOxaMd-MFj^THULMItV%t#=c;kv-#WYM%szKaVot_p5JwZ}yOT!Od&#<-LAq#J(S3(2e+OXLKc>xN0'
    'PJ@#0x@TLnB>q!6rF^mMB`~hAQb8D&{19v8u_+we;ylurx!F^TrzJEaF1!bvC8i+Y)rVyliXj*HXG#lBk^Vh_W2$$<{4D?MwfL{k-rm?2woiX0'
    '5sA-%o1qZ7y#QYZ3xmnwZ^3cYZsCHeE1O%bU>ipY>zrpGz!8mYk$11>=%1+PEOH4t(hAO_ID<lg%fQ?#@W`iSgn={&e=Q_nGzX<V{F9dfp3qm*'
    'ec(Z><pNzcn=Q@g00Sk&9ASlZn+RmQ!cZ&&&v1eu#cI7Z=xug}fdi~|c?LTLUbD3ct?25WQx1v?1#254fZM>kHr7fvv0>T^1<$5aL?~CiIN_O2'
    'E2WMPfO7+{5FzUsW*i`d#4Q~@3%x??#Ow81J9sG+4aXJOC(b4a2$ZRX+)qL>J`$5cuE!n{NX~=a^LHr`y0YJqMf=*Nr_}biv<cjh=}U?AO<7^h'
    '{qoM~GuN&>@#X%PJ73-@`(N%%cB{OoobG%FYMC2Y=w-Lr3<HecOGAf+I_)W=5L;Hb0G#tk<LPv?SOxif(kah6%V}lO#|1R2QzxDPhGixyVUHS&'
    '8H~W;Gl-g8;hrkG!+E5MYpr#o_N^AhpfQSLxv0wNRdeW6TCS;$*4#@^Ubya}uhOLp=Wg<ki&rkVV_ZISmM3|T4>4KuK*Loa?^ES@O6~_OTzJ7N'
    'V?Mkn$}EKAHJm_XNY!XeiOGRjA+v}X@_`e$HD#}$+#;k2spK?*6w2p|uJeD6+C*3x?~7&i!R{>O4x-;iiH8>+TNpqYinJn|!!<4)(0)i~(TF)C'
    'uvzFxZ%WvMPg8Ocy(q;5eW1H8`1<-2kGX1=`7-h`T!6STe4L*-!~Q(R#|;W3puQft>|f|;msexo$y&=0DMzTHLn-@!`nA`VUa@CV-oB`V#gs}G'
    'O&v9(+Ypb`&{V!MKkSKXQ(P@e$s4wU4qmy8Qp2T1ONhK1x*ZtS0F$Ak-+aqo?{w)>I0t;EfwQ-bSk`bMm&B<av#e5xOeU3@L|WJ9snb~Z(G7YX'
    'X$occ7G7VCJE>g0_SE^SS02Cg#Dz=O-9oGqdH5#l#2i};?9^VkH`S$v3(ALFJLGBy70jqyWRZf0S25kWq{kI|gg7@YoLV+EvXs(ErHD@7DjA+_'
    '8w(ys(YKPM#SGPtn!m0SnLgfHANGUuerL-M&#kXdY^O<O?V8C7spp_+{vq8cU@_8bL}rygBwb@QnXOE^nI_$lQJ_inC^Um_b6p@Qyp2fRx#D7{'
    '%9Wj{4@!;kLO3cW1Mcdi7{~@qX}af#k<ejh$W2fJR^asz%%>AM7mc8!G%xjN;Bx%QE=zZEB_c8eej8aRE7Dw29!PgvpzUHHM895>e8l;Y+w9_D'
    '?{Bu+cx&Ii5PZdddg$EnJESwBuSnV8+v(E6c!JM^pjlNgEDz6Ev6^2fAtPU+_y>(%t6E0&O55K>+qH|~;PDLVWV+Ade=&5=b6?rz{T*WTxmXDe'
    '^)U6U{49io=)}`RMtNZoSXTx${tFM}OpSkLX0jvv)2ULWj!Ui0aHG4Ok5;bRZ>_gFRhKND=p9F&s;)mAbkV&Em1=`(zSzJUD<E9xswEPtR>~NE'
    'qO^c=01mVWBc*aOz*!g>@F4uQ@Nm(qwl-m4%-L^&Oi+ct^-aH5bzwrAzU$c$AF3`!fdI^PC&QkC-|dh+zUtnj;LKqG8rzjukCW?ur@rgtZ$0@n'
    '{Ib(-ZB|iyQTScJ((=xjXQ6Ne0)j}`?)KOGexB9@g{UeHNzxrwT^9bG0ZM%UbDDZ_fh3#kw)-eh^vSo#V1<qx3toePpLtc?6UL+Jf(VoWL%hLT'
    'WDbH^QcgpOCpSi0+3ap1yQYO6a(eF6TkSU9VHB2iDicSUr9aLzCa{2NI(S5rHVTAMR@Zfh{d$m1BSdtoO1q>{AQ6K&LQm=w!O;(O=4-e}p)}}n'
    'jR}jzy%6y8#-t<Q+d_|3mEvv~m5eE7K&gROA%!=s#zk9rOs^;5claD2F3LQ$Dsn*JMVRQAs>sPoUJ|H#Tv<KtW=DLhaY}lq$3IGIi}T`Ej>9Z9'
    '0(LR?xU?Qh{9MMe#<_g#ArPpbJpq}^FvAY$2hK}{n@Q>n0S$V)JT}c3qs?AFBopY^AYKuut!jBL*ge8Ely0rtOl_tEqQSTJy1f{$>p78Q>11!j'
    'iOgthFxg`zUi#QWP56#~n!%=U!}fTlEax3S90{hSwb}X}IR@6N(FMX92_t#7K_^8deP`6TRByzK<J-VhF=4Yf!Q5Gmzrj>TWA-o*<e(Q;q&Sw&'
    'CaKHmN+v6qw7Oh#gV)Yqru`!Uad;yuNpo$trkP(O3>o^?Q}{8C?xkm0T5osP^6rTekObE>av@V*<=gaRcNPMvzt@gt#`RFl;ATfK<RlG<D>&aQ'
    'K^vzElf<qRl##I|j#XM@AR`~XiLvU!4(q|_pncN*q;<H`B|sO6CD$v`#O$N!i;B!F`y5JJTGNThvErJtI$>{`437+bB^usJ${bdYocvhH{DvD)'
    'WTPYLy)D-VVY?7#?68MW!;tFH@)D|~ve+DgKONh2HbuJ69tA=bEG;=#Q7M4>AUU3~ZbVoqhF_x<a;B-EvE-b;#8<OgK`0rq>A-{o78j7?j<mLr'
    'XX7KB##(aK4p+J2Y}lr6^4Okcni1R6*3wZdK63}hWh)~ug+v#G&4;^x#f4RqK#e*mi(rQw9kNHspdUZ7?W0;B(w=qn+DEIW&cx_2j1EEjq#Fye'
    'QUt~hUnkL9joo*&9BIb=qc?i|dOsn1ZZyV0%orMCPE34-cU+=yT;rjPhSaUPIVYg74Fup|$HzqMz}o6NB`MRfy;cJ`I0_tnfL21)Sy8YfZD^8*'
    '_>>w$&1$0|0oa6H%zPj{8Lrnv{vyswxG5Q!fdi|a-T|>x1ng%siPyq;87-PRjugfdORE7eqCwyCEcmwiP}*~ra-%2~OyStS+W<u-{@HzT?W*2n'
    '_SqaUY^1M=GdpR=hXI_J0!T`mDj%ctWKGaykQj6_rWHb8I-#}=!gn{7#W@BMJc`rNF-ok!5E0{m_xn^C4oFeR3>P7LOcf^_8j&~OrmwwO(Y|gH'
    'T{6<BDl75Q1nTl6s*$zvq(wn3P5g~WUn)wt(YbYy$6~kARpt7Cf{vTazr-aGj9|7H-`e+$IeI_|vs}><=lkx8U;Nrk@l0{`<d;h*(nmpYj@(!('
    '2d_C}RoUn_g@9OeMkF|EwWByKCO;TSc=3R?A`vlRoy-<-4JtF~94{nY7Q))bV6#2xoY!|1KC@$9-KLv7)J$HW;_w4~JRWO+YM&U6W}at*)_@#X'
    'UAm!5%qb6py>>InsEN7MpIX3X?yP8G$xpFr!ptQ#QV}R)ktt)<9i>pqu`1VDPnI)|oUyx%ebs%sXtg}TwJil<9eqG(UWM`5X`{f|*5<n7w+GN#'
    'qNqncuyPuXEi1Vb_Ul!b90cg_nT9beG4gw(l&vv#r<bW;0K#%k->_9A+Jd#nSGt*q+geRqx3TRTkvqFFyL|ZEpAP@!wNKvo?ctlhKG=KzVE>N?'
    'cV0c%|KQ-x^9Og{JJ|pA!JS_m-1*7bX()TP(b`H`mm(ac4R<*QGD=QvMailb@YUIAsJ48zr)pwGBY~;A4E+AV-iwFtzj*kKcRs#*`@wU+JJ|c-'
    '!TvW7?)>cF&PNA#UO3qM;9&1B2Yc@v?C%}y{rtgC-?{(RpAK&CAAaLEpWJ>GMttvw`=7k>or60c9{#U)?)~;>_r3=s{_w#I|Bf@+{|z<r?%zIo'
    '{y(1oc6nyz;P&e?<U94Zk6t*qy?6MH=Rf($dk1&+4(_~paObxNcV2?R@E+Z`?*H;Ts5ZZk0QqpE+XI}z-?#VgfA_<~JKy^F&#&Km{$=R=VDDX='
    '#GP*)+_`hG|A&J+{|19)cpm=wXNMns>tO%ehwr~}@9wW2yz_%kUi^3XdjA`5AHMwjy}QpJ?EMuQJUiV3(4wH!4wuh{!_7^<zq_oL1Xt)AcX3j0'
    'zj^P+FW>voPe1<f_h+Y7xwAcjyor(#9>@apuoStw!C=T;>0s~eLxj=4z-(cr(9QineCzPrFT(qKFMe?N$8SKvd+)vZ;OE~ueDRfoz3(6Fy>b7A'
    '=MMLN_xR&C0h$-iPm7xutk2=?msFu&9_&3spxwX!F09)JJY?wL{&(L2KppJEYP=_oXHckou=f^p{qbLaboky6)P%ozu>S%;_h9c$?392FqdM5z'
    '=h2oI9{d8(;6;SYAAfuJqvuq;w+U6K6e7%P2m9ZFE<X9jONV#wFj75v^YT*w$NzET>J=Oe6wknR)$30V_Wl<x-^=gPO6^Hkcb-GE+W#xgoj(~J'
    '611n)a^@nJTTB?biC1HKe!m4Q3=r>T(8rkbwa5<+_2cSo-{016trGfu<~=D)t|0<9i6|YspHD2L$69gDnFI8wL#|UH<y!7^ImUjE0+0;4ZII|O'
    'Bz`PffZy-?yZR<PklqaUC&LM<R4T<BX#YMZS~4r5VF{z&W_Zu0d&_3I!=^aFChj?CQ<idQ<rj0SNhjv4U`yJaN_(NA;~lHn%8O!pjcNA9uO-zD'
    'SB;>M5|1Y@6^R4pf-~qltEX{7PvCfiH+-(nU)Crc8?Evs!InKWb~$zwX3NLug;ws_n?gJ|xiqU;jiS1MS{j^ZtP`ya@VB)Rz}jKjRSA;>n1vrT'
    '<1D8^<0_lUsher?B!#VrzNI0nzEtMeB0wz~mK(*85WTUnc{~T3p^M4dV+J<0m8%9=Hnh^>9xTznc~z$nD`ps{?5Le`c?3V9j<-(^P3_iTfd0m@'
    '#iWL)k!|0+m#Q+QPyuEX(Ib`#SdD+H)Z8IL6ixAN!P9TdMc{8HGwiq9c}jWE3hClhAXzEw3=rInUc%#5ipF<DE}6%#&CM!j2ywOxoh$wo@yC!m'
    'VbR=c9ApGpXL`>2c<Y>huJIszAc8oCueEylB)sHC3No`%Q+O$g8p0nb%?LcB-GQpOR6y%*(ocVrqVW*K!msuR47(<J=JN4yLXHU?W=6wY#)sa?'
    'xM8e!h1h0?ZZdjd(8wB{H~};@YoVkc<4r7Cg=J&3?^X!mOSw)VbkIxOn4k}{tJWn6p_JoTqKRg2ZdNkJsiYJ#Laa?#s66Z)5>x0rhF2yztJ<V6'
    ')`ewNjj7k7R$_(?s)b-!M-7c0U$nTN!<!e@%0_rsNEyMVzf<$q!dg4TYXulPdJ_l&yz$J6s%OM4UG$;w%V<ni-D*As`U;{*!7We<WuX<yfFF|j'
    'Y*R4TO@|^~cwEccj@<R72?P(mrB6oy9S5?#$JD$K&N%hWFi2~wAE4LER$!I`sA9v`1|`?|@+`4jMwJ?+p}~tR_*GrvVAoLzIsp?Kb};mMQst+6'
    'km$<o?)rpuS!ryn89&vu5V3hDDB^E}N=J60k?wo!<i+$<aO0GTt&TGO75#n{&67zxq_u$Q_22_Gs`OJSB~;?!uLXHjo?7viFbffxMUE5}yf~5{'
    'V-DWr0T*<JGVU;pEV@`o0k~>_wqlQh3nL*m3X{2!J5G!!vNLi4qWL^VZE3XcwP%(y4{}-?Ypu|m9fL6&?>Tgj3@o;6u{o;?=<v!2;-lfDK7ZOp'
    'OIs))N@?L?gySTq%nEIUQI3dTj^o+bV_M1S<0iMt^UYnj^!Vc!uiwaxJ>EBy>qpcz*??<mS;>+Z&5le326hltQ6E7vrU*uq?u$w>-me&^<En&}'
    'R$mM<pYRtln&t{dn}(y2F*_Z6Lggi_?Q}XlzkbU#Fv|=awnMv8m)^u1^6)N-&Y*S{y`Hqmqm5ou&8Fdvv5BNj&%#Lk&h9sR%S(ayb~p9pRD`^0'
    '``xwzCE>j>-bAgebzL>AV%cS)Rp#cia6o-E@gOl2S;X2_ShMEhuKt}+Hp?YYCRQi;y^w`%0OKvPcc5=_D&|2!isi9J9u(CizIu}zP2eXKIwp2P'
    '$zwnb+q35{Z_NI(v64Pd!pUe-u`O=kC6xKcVI?%4iIengB1WpELzL9`@OOaKETh_v71XZ_6G!v#8zf2nvM}E)@bDvkC~qdCdC#QqUZy{iR>&xS'
    'B(7d$OFnrdtsxwO$nednS(o^pT^PHGr~HljLFDR$>0B|_C|^=4$w>*^uQGCtT+A{@;qGM^wYXPbgq{y^kRE~9V>`z!Yi^jOr#aT@;nvY<ZMBJ0'
    '(vgrK_6>?6cIj%q6KrE#v#P7^Ibrx{3_=QRmo7kaU!)hJv-Q~jg@W)@O}{&Y;Y}P#sa8YX6`77y9Fm%1=umA%32iY8HKJRv13yE~Q(bji>m67~'
    'Yakpho}<<0lOGU+1p@ZPIISF3j?_`4I_mr%0Xn(hf*Nj@)O5-vOXtR<uqW6l{oqtjp91bDq$g-ZrKp7lPZ-JGQ&(a22*5V!ZYJi7(D{w+R8N#y'
    '&U1QQ=+#b}My1(i9<8tE<WcS|$J`QP>z5y$IKff}HIlGH%Us9iWGiYYCr;2{P=|q8+R-~3pLrzw%8T;0kB@~T+Hfy%sE+3**lOCI1GYa(JUQl#'
    '^G{y9d`<;tD@=A&!F)VqH&)C$H?EVKOU!x6NNsyC@Jq~rT~io%^ZKQ8PjNyz%1x*Fop?*=%2kd}3|;Cl?p`gd_k#du280ti5|%d-jZd1Iaz{~Z'
    'I^5<oBOxbui%^r$m_rVQgWYyeYmvtyrYLpo8|oDDL8vR$S}mwN2d&QF2q&4Q9@dEJj%_`6D@1(-TG><&F1h_;!fil!Qu@#hrh?XT+ZygfEh?9I'
    'OMzC72RtPqv-`2S%w#|=!+)d_cQi8QW_YA{Y}rs&m!vb&s(2*TimNAJMU1M)c1YTkJhis-6RjgRr--Gsl4?wJVLMh1Uo9>y%q<*u2(6BSBe!AU'
    'U(C=E2NWZj04j#7?8t*B0AHzf3l9esm7*qEQk5+-={9t<w@_NHVafifjcr!7$?1i~2OsaW4|{D)5*BdV(Wa3*5tT`oBJAxeS7zh-3GJ(|2ajHZ'
    's<0X^nNEX2A~$mS1Vlk2V{CK{UrNi5GE70t<S=9Gn?_YkDSg~E7xNX1ERu3uAZO+SA$&j%P~A9LF)kXL$7eoWPXPFsDSjK@5^|j9qzXx~*LHga'
    '#s2Ag39GRTl5%NbB?(d`KEhESTWNiZ2R_BXp$iKifHDq>B6<#Z1xHn1tz`K5iY;hWts)(Akz=oAM~ai{qu1H8Rbq=NX_pKt5{a~)WkgAjbsXVm'
    '{QOIz;;~UO@=HmeW5$5vb{H>dn2KAUybKeL7qhik<`gg0c#;m9*g0nc$)jwEVz|jVzHO*#bl?}x*)$|L9(2i^hQR@udg$v~#I_0Z$qqV3gu67!'
    '#fXnVQb^1-RCD->&@;CeV+WWQcQXjDe2os<&++*iu2}U}jUq>f2jbM534eCJcxZaGXJ8yGelJ9nA(mhGC=|d4%{VS3@qowu5O`o#C`2$(cyK&w'
    'ki~17CpGuVVf6*NI3pOPJVo=y2w3vc;7MUu)CS#lwHy@B9C;vKzxjB45%fVA<1AB_fMh?6D@}mMCkbt)pZ%@%8V0o1FUWtbGkE9~I-1cwVue17'
    '1rlIe6J6CI5{U{MMOr#P+Nb0VFotq>EkqfZ!f|k))AcThnHW@c;<^}=l;F;B--9p(^2WfE;!_GF%l|l3w{a#ydIQr8thX1!Mt#r0Ge@4vvq$a('
    '1UB~S;00S4IbI}73Br<GkK<2F;XaJCJcH5lvL^la&O<s%9e*dFR9cQ5EYg~TR`JF!*(_I`xp?j!pM2iUWnyJt+V<wsB8Jcd@$W|Eh`}lz3(aV{'
    '+-%BZJHySj0Ass3VF!rjMt2a#T{qPQ@_^#XQIZQg&X5jE8c$0^)lubS9JuCw)M_&tUP`)~4NkT;NrRQf(QrMbUvgmvMh)bzy5S%*v&FG#4G$%I'
    '0CI(w#sW*o?=VVSddEk=F?OX7H9mfSz{d!NrP5@~K5a4%lk7u<U`;a9u@3#H_=KQD6G$HdRVgtuLp6mxzoXD#MP`fWFE11%R@0V(>Z<ccDp;iQ'
    'Y~qt9LZ60twI-zagK5&71pN$bF?U!z`)Gn%#wYDr^(|U>=GZuHj#@4YuLY!N21XN_b_-dOL)zF<DR~;~r)haCYJS?9!Rb#mgDmQf%(J8>P2t?)'
    '$Gtfj%^GBD+tQ2U7xR3JzWiN?rScWVHF6W?kjZPYFR&&)3h6Sceya6Q{FqbsJFGILHxfC)cK~??YEEghF-c>_xKeVZS?88bJkpOEeT9#n>~Zkv'
    'QB$Y+3ZGrcQ;atBg!78h9LPf|2VixCo|(ooo*v1m#~6gktCd`-bJT&u+?}jb%Em#%7$u)d)pcYHK0zVS?KF)NSb#KJDWo#E6BxiVg@>GUME6A7'
    '31f5y-DtwZK(#i5^eJaeFZ-A}?qrX}xBvaq<t4Uen_%0I!S>EqCU8Qig64~!{K7*%raB_ltm4c>`=jEN6@ylBX2-aR3QDofRpl~#I#V}(81lqe'
    'Y6l|jbAUN$eVUt|?h$RGu2V=8EhVvdCaXD#!3=@+bsC&%_Mwr*CNV2QURIH)q|huYWC!<p7T33S?c9Y6m##cfyMZ!9P+WWn){UhnzWx}I2?amj'
    'K~tEp$rwn0x^=A(N2I<pBOK)|6`9@wY@NjvPb;ehMVaZFq4g(qp7n7>JPjhRHNiYC%T^r>eZ>*#0o^Fkk$gPqlnWCLJgk-DJ4cF78g^P=9rDSE'
    'ku-z)5OuC8=0?&8+8zWnOHw|n;gFS>4aS$RUbsj@ii?zD*VfR)vxW>j9yeHsuI8Iaz<}s?w?)S(=mjq#2Zs~@#m(R`AWdZR^~o(-TQbp&f%6;D'
    'i#WTFd985U(f%-oD{GoE+(ZC2-GV(2{hlt;m`^aD*bI*~J4OXg%0;NpOKC9AF-(vvjcU=feNn89C*muA%wQe7Pa+>pO4v1Qi$f-RYD^hy%#cz^'
    'tc+ac<BafCd(UNsfO;)^aBRX|ElW&U&Lq9<Z>}|bEGjLi%}B?GiFiP=%2p_P8QSNn)-gjfIZ{`CU4?q%HBc4p+l<nI3^+xf6FI72<ysU2%C+-j'
    'qJsq<5`sg;HNTCrb0aNZXU5h~!}k?(;c*@{k-~5?(v%`{GU6E#Z0V~uPgL3ZkmPeoU<jFkm7YtSno&I$<iz2ph%!ZkDx?b9cp-;d$D4kelvkFY'
    'mecf989{x5!2k}Hkn)5nAteRQ;+WJQc4}(5cyQvnb{Qw{Ik-?2O2m@vKk^~2IOqK~WxHHa71^vL8g=<|zBjxa#cf+ni~5;)J;-xTzyiwfD=c=2'
    'mM4lSPxK_Z{Z0YlPz`{-pXe%q(**$*xNKram(cOT;ash#jA?0ICFPge0X7fQp{SehdQad*##=!QZVanvj8*kR=nA4BIdBvGE|GUy!oPxA8c$hA'
    'lwoJn2&}q}uws)lN=r8@e5lzNY~~oIF{nht+$<j_B_?0v=~0Z9j$@_(G<*+Y7+y|#f~6e3VqA)1Bzx#XiN{ssl)h<QWj2I`*xg=fi_;#;18kQW'
    'dbW$|h|KgY%ERo;6pnTC-G-WY{82)30Dw5xMA#iWm3cK4n?-n<X9YEL3@4FmSBN2$N|wSiKz($OT53Zqq+S}R6_BQlcPCExAqA9zbtuF_9%zXc'
    'gU;kx{!uJRfl{Gkj!Ii>Ryon9GE%QI3pg?W0QffPAcr?`#2O1HB<ta&VzGxe)fDick3=I&8tFo3B%rC3=9*&w9?o^7CMOCp%D(;RNCCy%6zRU*'
    'G@P1F<*nU!E75AFuBk8%l^80x(f!lJT<nzK1XmTlYHR&Co~PP1S$TuS{I<CL?zZ_!F)veTQKV0w2Ei1MwONdne|iDXd~p%h(l`m(Ek3}6S$z(l'
    'Jm~gdF}s677oDSLXQ3t~GMx9E`2`6SM@gTE4(~8`YZ|N#P74Q%=u|G1XZbQ|yE~|HdX_2=7mix&9{y1O;&t7Yi}&s$v-1nhHCnevW=@?xed-MT'
    'JX2q&`#$}wpE`A_%y0dL`T2!8`nj;Uc={CooSVV_35ay+`juwxEUb%z8M+?p?o{0wpjsmu(%2a>2*)nWpdD1**5<nF_yFpnM1Mbg`57ZR()$=m'
    'hyoE|6r|hx7^7(KgTrsUBT;g0?;XDN%lm(L6<&UP_r1fn-aowkmb;uY95`C02xTUv;fwfH4f7LRcG_8pZYWWz+a@M9;?mPb=?LJF$jOQ7qhMd8'
    'HQ6aw-O)~^G~0w(@8Sa>0{q;WHQ(a{!=}h60xtn=ySD&Hj|8*9Vo-OdRm7~+&d8m9$80lda&`uuu|N?{?u~B0>Tb3g4Pc*_M>I34adsL@s6Gt#'
    'QXS$EcNupW+(j#9w+Dcln}dItTRj1LBFfmq+Z*u%Q8<RmS_H6}RPonbHEyN?{!$_r#AAyq=2r#-nhhXa(_(o+K(u>uAr2h$JhelnS#jnHF;FRR'
    'DO$#JwIL?+kDbT{hH-(ohss2Jqfjdd`g~rDwU0Ur$YReTX;`qz(a<r8i!rHY?lC@ERNqRb6Gcn(K_oLgno`DDDJ?d~4je4#5Tgr_;|+bv*#~c8'
    'Vd|}Z9pF_rk+;rE)7V8)QFZ4^3oeFD1PU>tl^*kBvD@?OtpOCCDJ?Fz%V#NI-SUXfPFN;-JUh)@m<zTUG+Jak)NW*9iQi2mylNfOh=eqG#Hwl2'
    'N0pDj8nFBW%(2p|aRaq_E5DLAg>27>52=tcrd3OBRo&PK*T%Gk8$c5fYh>7}B)h|=kKQ&gLn{;$8QO6RM=UNyOPAnyz;l8KSFE>Or%`n;kDwh|'
    'Zf~?ZesYA((WCXDcxL=GAi^1Vs|%Q~BQmKGgI^SB;8nL=I;~I#!%}G+(FX<|GvC^OaQn`gg+~ux{1cosfaV$~G+D;u|G51f<2}uuKiq%&+kgA$'
    '`MHBTuV9=tjFk6*Uev#R^g@J&2#kqDJb7Z+#{_=aEtXHIBV+d@{XPN&b9&O33B!=X<R83qqXA<7YJigo=*b8<re|ZPP1a#!p5_2B02>DAd*i)>'
    'y=M;gK0Mg_#V5Dl;23*CV9!niC!xlpLxyq`Mjl?l-~>ZXS`!?L6%=;w)2XbEWF|GEW#2pC^GgZFo;*u%6_K_-Q`XGpNykZM))O9eKuKMRuPx(0'
    'iwp2$A;wyI{9CiZF+Ov4L9@t3YOv_c&tx&SbUb9Rw4WtIdGGG84&Q(Elb<|$u=fs}PZPMq`Qvbhb16KJ36x?MtH|Gvv-1&}das*QfTmoWUC^6f'
    'J|_9ZIIlCKd7XKwlfv&z;slK~s-zu#wkp2plnWAwM~o=Exb!~tP|O0RfhrFT(QLLt9+0oFxr%Vb;6f>eE)&K_M&|sG4Kn#CqfP<s7sLidBPSx8'
    '#fnTOvNl~UpUxtzWkoM_Z4+0H)51uv(m1`XV-tD3;gG`L{rQ6r@BZzhR{<oShN}DP>-}II@0W^;3`Wk*4#U3I>eR%XSz+BQWu%^6I66D$Iko^$'
    '-m`E}RJSAI=H4YXuWq9fjhM5dKIonEm!epr%@l_?V{~M=HlB~Xz(}T5o<7!#w0z2@#F}tzP*R|Z)U0TjB{l6tOTA=^=?Jwh+9qMNp>Z>$2b+<!'
    ';K#Z)8L55a@?Y%eJ+3TBR_&U$T8!wqB4OW>hWEIS=hz+@ti(F1H)H;&Ft{FC>(Esw_G#h`a5m?swU}@XFz1n$t5f-8WO){)UM62*v)WPaM)Yzu'
    '^XiZ8n=)(cX8DZSBhs}pyd21NurmzO9jycqD?BpF-<<8oYdB10n32roQ$$t|kbS1a!J?36fE(1>AhHZN<+fCsSv7qyPCCggqQ^#<#(RZHL8qDS'
    'CX#?%hJy%p>p7O&n&It<mNnaJ#4{#Q$S}0UUFQrJ%%go!GQCQKPDFmrN3NBgpm2#mT|9n~-bv85VcicVG|V7F08z7nnJPRsaxA*c9Zwjj>}`cp'
    '`Dkf49cCfLOj1kKvi(^+-K<gDW}Wi^=Z(UgVVb@uG7!-)DM6#Dk%~Yy>^b6FQw2a6snl`TDZqP-t@WVQ&gWJCMRhD7XJ;N|W+NR=LWQ<tv3Alw'
    'MyzbpaLU*3R#upR)FMutMpIZ#dc#bbbS!yIZ|FtNM$ijP7o{O;+Or=Mv^l<*CIpi>VeLwaYRQ07*vlJJ;Fy7XtjV<DPvI#AvyKCP9W!L3McPMf'
    '@vN*YSq<e_kMy$^2Z94jM|k4#tCQ=eRzzb`+wYkh8bLvD6~Vz`ii%WD;E4q?l8D0MvhJ4-2wI^Ns<ZTg2qGA@CKc!TCC(m0C9a@m>3CHy>6d9$'
    'mF29P1J?GDk!ngI=pW5okHLTuQRfskMFt&U`$25bR<G$73#DFV>R3m)Ve>i)X<|DgDw~%TV0qERk>n<iye5qZyfETj3<6KVckzKIP4g&tiITG!'
    'C2f8#GGVCY;QI?o;@9hM5-j>?kfNyd7*9BCw`+<F<H&Rk;LH0W4vV4%oqH9Hucfvfb1^uM%2{c)an;SiR2|cvPs~i>rWYe#WF=PoK}E1ZIEace'
    'g<AJODgs9Im6%q`Ing1&l~G46B$fEQC;}uMbz4nxa`W|8t=-)=nvZFNP5pWVkt5qgjd5zDtV;e=tws!6g|~yNuz_Cbjpj-aG`bbSk-YVGnAiiM'
    'f?YYn4hm(V=m)0xLF`hI!r5cN+zT~h3C1eIlqs%87-aYo=+HW1e~V>(;&2)yiinRUMz%6#34N(z!c1LNV^T{9#_qsxM_-Mtfl+HBLWK?v2BDkK'
    'u_>dd?HHz_v?a|H#5vIrPmhB$vF=*j7n0$BK<oZaO-1_tOj>6$qXL(OwquCGAtQR7ygGhjvq={j&YIVE?et}awq>Hms_?Lym>pP{W2<3y0M1HG'
    'hzV|nx<!xG5<E%aBH&%MgoN1z9`$b2I*@BqL@+yyQ@K^+MncLr<3=M*MM}oV`|2?=n1A{iB0~)`BGbdnOyM8xCzG0jP6Z1T4WGrJsp%NJnKs#Y'
    '3D2a3S@tTLBLj|@UsO((9X=IBH%h2azba`tZj@%9&(0#@BEl+9c&46DIJP1myV-r1ZdmJ|u$~aQop540{)h4Z=%H7(VLmbvv<XNhl^&iZRd*yW'
    'mELje+6Jfj`59-nypU;U8pU#yaa;O~vNGtwcE0z+{rkWD@bLNXQ=+zabk;A!D*aB(Djj(@OzNtD)(QR6I<8j7hp+wkZy$ZjGE^(WL@tH!rkf+m'
    'i|$cFPzEH+i=Sy$C6@iEKQPlHVvfwX^@H7|#FRY(IkHg1uVTbu4AFLpu`=j95;U4K%>~<ODrxkai-|nS@F(~uR=?SagU&GBN$wZZf=7mPwp=P3'
    'o?YrlQ{E|M$&-4=FzC%R$B(8vEZLw~$N<HT1&aBJpopB%?0F&DoS8Yo2d&&pajZHf;-ukeqC)};=1NTOMxk*!M`j&Y_FynYiWhkl<#+U!pzsH_'
    '^Gpf@=A|-DUKVdyC>*_qgxaYFitw#S%j^=Ae8Wf3-e0V#S@711e&MJs28ZFCUru^r0fn%(UFN1q8zoQE<qf6WRH7|py)oP=X*a>olX{SPFD=fW'
    'nmIkIonjh(xIwwas_vW<xyBs+_4WJjefR!rzdQU365@OR`qRU2y>j@oJNLi$V#U#9d2|$(kdw7|wtVo+;cNe%RihOZN5`U!QiKV*(R4>0^G>^$'
    '7L~=bC3cH1()}vlD7nvCq&X1YEFljfXPj}~*q&jGAHaD5{3-79Olf`rusoN#FtcusO;|Dj;t%s~nJ-->wja`|L;wt6^LApaaLT1X#hjWV|Aftc'
    ')Rv<Ru&v}a+s)*W)jm^O5Vl@?=b4qQaTbb4w|j)&WchU3ZY4+ZX6~}g!Hb3v=gzsCr$%eXE0~+g2y&7LaAF4#u|k<7jbBGxd{3W{w5eur%&B5V'
    'T5NA3eWMN=$XEJw+8Z8-M|&(Tx{h9;k^CCFBitb4#9A5JJ?MCpfhcsm5~0b!7QO+JJLaB{j5(AP_xpeQ=y{WeWp)*F9{>3}2m9ZC@bll^yZb9j'
    'Vf)g--m8E6=!Mt>RG#f<@OoVd-o~h97t?E?hZbQ@UoxJS=fs$4?Z`QXPaiz<@!fyzR8<oeb;IxGeD>bw`9#H&6fuV5nN05FQ@DJbGk5@pdI%lh'
    'TvO!(o$Onk9N2qeyJwvq89?>2f~NR8t*|Efukm6Pdwf3l`QE+X?0@pgHxJ+bCZ|Bgl+15```-6{ji~d&OL*k&?;pMl*z*H&7<$2tYdMZ?%AGnE'
    'b9mBnXPPaSv$#@oF5@)UCBj|5MTcYj6lLE6`<yg+OjS-)I9VCvRl;~H|2Rz#uhwxsd6mK)5Ok8@2RreT*Peaw&hyUy{)2Px^>+_{{TJM;g!>L)'
    'GtMNI6F#1(U8m~(O4#j`8pF+AsMQl_Ucx(qH9xGkT2;EhipCijtUmzByqb4CJi9BvzLxqyukF`^yn7a3efi77nVC5i#uei})qu><#XMeUz>qSs'
    'RJ7iQFFzAe8sgaw-+%GrKRpXL#D)HXqz3a^BQ_nr{|3ASoI2QhSEpCcLM|XCdVdq)O2M`C1{0a*(P8W3KmYaqci*}Hug~d3?)U!no%?Tp2sp3r'
    'I2}I!Z@enx9*1e@dxmHFoeyJV3#yhEQlxm)BE0+zDi!wsNG<iOeD=tb{2h7hu~M`wQ2PGepX0XM|0CbMn!q|+Dh*(X_kNAp>-Rpm_xf#g{J#A<'
    '{P^U1e*hTb9PfU>*Slzrn8kh4N%F@1_n*7>!fi=pu2*9ZRVoU<Kj7r_F~v#;o)7l^3c#E>d;`{XPaR{J+r2z9^XT6``q6`TU%CJCv*m;R-yZD$'
    '3|@0Oc34*EhEk(P2xBC==kW7Rt6?dcil}FDDehL)g`G{o-_fNI_Ys3v?JZggZbhe82jboc`ZdnDlj-~jN9K9}@2>&B5zDuck)@-c6Cl>ZEft57'
    'lD))z@+_x9)W4-G*VP8bEJ8k-F^zOa$#SXz?i}f~$}4|19Byv<{axBcT2ltgb^gZFgx%`<*|p(d04ELU@t_^S+i*)%d)PkafvLJ%-Bvx=>h2V='
    '09wq#0F7TID$5JQZMn<Pi37;?@n3&*_}&lBPV;Nn8vQ`8E7r1)Svy%y0)BV^Ljyok)f{tE>F!4p<A<vI#{y`ALd#Sp>k#)7@&_1x$2HDrgOv8{'
    'U}G7RE>2UlbvW)emR%zyfqC)yzsBFwFd5_317d?PaDMbUACLR;*<ZEd9#-Qwcxv^ajlCE*!!_t)en}L+zv{#S^sl5&S-gt|!;s|VkMG`o@Z9fQ'
    'BkaDJyiE?WKfHhV{Idsp`v?2~;v!{tVMSfk<EK<N^Sh<iPxA&jGL=KBhZN7jJ$&O&_x|+C!QP9X{OHH`e)Ffp_g}sL>N9T24KfAMCLm^GdBiOs'
    '7uHygit7!a*d-|3U&h%gcXFw2?o?QR(M=YlJv%XXekvYzz)<7I5~p~U0oeBE$c-NEW}&n~s-E0&+e+M`nb3;+AGg1&;u`3k1R`A|&NbapA|!yD'
    '7)s)5++5bxI2=RLtAWN5wOT+dH5VOL57q_NQKt@uV-Mt!tyaI=+2rj%I&nhL5#s|p1K<qseuy%~a_~6Uw>|vXtqys7jQh|YQq(eb@kgU)h>=2z'
    '?B6Y}Vns!B$m0wc-oZxD5v}hMYz^1}7=KgW4*-V7+1}{F@z0JB0#<V2S9Hj8W=}Z{ID$iLfu)J_mnd))fW#>;kh8^Z<l-Y#bCS=7D-dHYB1#U<'
    'KWca;=8;Lr*XVbxR+#7l;zp>4V1ywFX$NeZ!=(klGI<R-?u?odhK`<-FEk@Z*nV89C8aKhwP<akgs|kGPSfwVcNt%L-8N-7oGmR(pE*@pFyIFr'
    '1v{|DBw6fwoV;)jSl3|)6+@SO`vEa(a?8&o2mNI{>MW7~WS3~c#`4kBf>ATd+N|kD5>HkB=IFX$Vmzn^pLo-}qHYjsnlnyhU&^ENNlGIsi4qy)'
    'pr72NlbZ*L41SHXy*MgHn!!on=a8=#sOUdN{8jW?BdU~oK_KJ!*U`zM3ef>#3`dLeco*iuU;q5z4?l%t<m0<9f%FFg0&wu7sD-hT5I=yz%(=4w'
    '_sI|6I(+LD;MTPW2?g%|;ai_P_bn(KlP^BL`^`^&_%ewMZ=+11O}1Fz1C~t=_U@ukc=y+bzX5^e-Glvo7}EWp{R!oc+i&wZiUMLh#gmGq`w*vT'
    '3NZ9}`u_bt{u+9E@YnBx9CP^GpAP@!HG&SF_$U#feDe%U31ywzAntsJC0POiPUil<|K;#cKfnL`_wK#8ho7E*3yx-#xS+uPx1_qBhEQu^WwV%f'
    '9)=pj`}f{@{lPmQ8N7RMS&Qhr6gCcLWwt7e9hjO-{PsTZtSC-??|1jU_v`ZFT5ACFLk<>R!l%O*Upai?B^2KXOY~qsWK+N2G3CYwzkCZgAXEap'
    '^P9u>|45(%RCtDv;DZP6yoX@X@D!mB8GCE!hI&LLSxQrmp!-5+P@O%lbYLNnB?-{JwCP-&u3!<N15CY0mWK^#pcOZ!@0L}Qnf6q&*Ba7l>ha4R'
    '>C>1r1qn)<GxO5)IidwjTA)#ru+UXYJd%m={o`+7bx|JxtN!}0_%iRoonIX6|NP+2Zx8OgfN%D{4GbmY_u<RWeEjE^?*ADV^6<4ELtR`{DDe92'
    '!?)fp!#+KH{`au85ic;_%&REkLs3#hQtKaleD|mKUil-bkziBbc0bEdfK$n<EUbfi#+(ywOr<5_`5o;4;o#1jsPMV-y@NaNN<)oqeJET}GndPq'
    '{T#eNftEsjEKi-qKMr)gUUk3XyUS-`z&;?6-w%U9)g2C+#nbAoh{~;C8;>%V(}@Cxx~!IN1Y00Pvz5Y=xN=3nu69r@yU-JBn4i9S>HNi~uYSon'
    'fA#uBXAO4l2JH7+N&!7}mJ(7fPgVNB1dT>b=%d*5H(Tx9O1KN>_hxa}@`|V>35xu}3;j-5M1j4zL>80EBeOHJXXX}{@Pu3McZZ!u<&kEyxkQao'
    ';J)%gZ)XXSw8#qcO1ZQ!9<Oy9yCYPt^CT)~7H6Os3~#;Fsmv|*b{u~==q^bR+sceHJKx(Gk2lIPPHGz8Ex{=RWV#rlZ3apfOEYu9rfLcOg%+ve'
    'tXZ*qD(Y<ZG;}uOlxMJ55648W7x^uGR5M+eUo4-R8IK1Tg>W=lqm5C7k0eD@)Q=^e5Gt=sDA4d>wFZsRNX_aD^iiIL9;7XfH&iLZ>#&Q_-o*K2'
    'iPhrm)_SKxe%shmrP=C-m{GdbZa}N%<7i}5AU6ysFY{;Cxw1GjW3Sm_^K{c6k69C`p-?8^&}-2_jz$(Z%qh;{6vM6c(Z~RSEIO_xeIymXwH62f'
    'G@(Qg7TOydg@V$c+cQDbINb<>Qwme~>nx3gk2e_buph#RNgfFLOEECOqT__OH-JYZED9>{qPPt*!4k?FO^~hVsp5Bbw>RJfT#5}o2}g~$xa>=a'
    '<pPHJSwwTytU!hJS`3p)s{>uM28j*DlQ$>spI)1<&nu|({YGmTR<J+S`JzIv7yuZm2Bb<#EA5pGMIpTzjT)^GWQE-dJ}u!tMIZz{ln4;_hnt<S'
    '!WjzlvmP)@d~d4U>=z14>i{%byI413TS@lj#Ly2G$1LNRXb62NsKSPi6>M#;M<XY}XDJIOONN3yftN{8fh-3beKgXjv7_2C<zn1!u{|gQn45nz'
    'UN*U3GZ1e)9=0|0VAs1@8r6R%5iTE*Q^4!qPGK~fxU%Vs2}>7|XQq%dNphS@&radH@puYYOFmi7Zj`f6A^0{0tfw8oLD-x+NK>d&#3Y))X8!L#'
    'IG?=n+ru}1?Ti#pL^=)U{@dTik7leoXTbqZ!}&ko2RYPsuk2BOcIcZ|KZS`$%sNx1U5C*E4ZnCgpkNE);Ifi&_25Lbg*W?uJh<~K;vk}z%pvJV'
    'Jv5Hc!(kg@pD@HlRG;K4bZU)WC8lJwG_rKhV-l$G0}mZ>V0olWR9eGF(l>pBzdTv8SWPJd=_nZs66zS%OpYBWmZkS2D`Hv<L6r91`6Y@0BoEws'
    '^V=x!B_@>_@d+493<6Sh%w>1}51<UmM2uC?JX*f?zL#!voysuA*>*paMZOsm@AMG$phbq=`yoj}FDN1A4Ji5GXYW4v`Lk^A0Qn5d?1QAdPeKxk'
    'h9bkFS@pGp{qKm-GnJJ0jBY`Edj9_V-v<u*-fzEWiF^qDPj0_?|M#zA2j)zG)qeijLo^P8==v&5V=nf2?(p_E5C8Iu!*ATV_b-12K=M#Qs+&Fa'
    '$?aEZjBiIP2+ckF5)T80e6aT;5^JA5yn6?J%$61&g`J0LrAOf<2n$diaNzLIKYQ@z>of%b&(D=)Nd3J0(*4)ohdD-SC^cIFETa~*^UYt~fA^dB'
    '-uWSjDk!Ibbo#<?Xni6O!e)W@3smC$d$0XF;vX>h#)J=F`T-6LWS(bVQuF=3Y38+Un(rR$y+wWeT)ZfF<}ATW-#=6lG`~y3dfym7t10$)B--8z'
    'O+jDKI7}OgA)f3#OGess#Q6Bbe?w^-g|62atWX&C36FDO=27;D#0^4lxsO8KFI6KyRPwZzg6+*63-47JM_WbtJ&dPDVts_;U}IKd(cJsdPe1<f'
    '_hen4Iy-DHqihUQ`}po{xr|oY+S&wkxPixqbLj%c))gZhzNxhu?&=u6-}}=mhx<RgclY^&y}vr=P$d4RVbI@o{>P20SIA;W_I_Sn=ia}*^WfcA'
    'ly!LT!C!uS@3p(o$iW@hZ0`{ve*VGRZ{GXy%WQlm$$ScjMr8Xr5bzG4|IxkI-#C2n6{>byNrvx9bX0u#9Vo`Xjq!Z^=huPg;CHC8{|hSq6BKcg'
    '?tdTNGyP!$C!!}oiS=Skuvl5<eDU1V&i(JcK`+Fsg3CsO9VtP5j61+O`VOD{1~kqGqip%x`*=DUGVbj?6q8Y*uR#va2yWQ3TKXlD!pNwUMK;J}'
    'qZ+3h=?gwki}0?Yxp{-=`~DY$HRp@n{;i<zux^1c8tG`%EUqKc&=3Cd;5R=d(80oeh~4bHbNJrNhwr_MTB{*QrhohBn=Gq35v@Pie^(~D|5um@'
    'dqoI&Bt@eRYn$$JuxEO@NZGx&KY8vYR9=1f19Ezx`Ox_rjd~50%rGsSkl|+HoZskoH(SF^NAGXtdGh?V-N8n;gS^P|zd!uTPXUXaE2t)hCRk&t'
    'T_ZL_N70XWl@|8<9L4YOfBo5p5gb0R(B_75<H2(HlivY)G1;Kuj<imEJR%hqZ0TV;?QG#?zyIxDAO7qPAwG2SYvW)cmOvesrbTFZq|5+g%{V0V'
    'by}g7RK!h<Y-Q&-WpJ=XXv2O43q8R}0i6dze=9Kj6Ub$u?{80bS<u|;r|?!#jmH>X_W4MguHlsda}`gNDWzm8eHAIpji3!gw%b3~Zs&8{<BFQk'
    'YOYXfcKa9o`bM5VRhRj13HbGksQP=V1v{k^^z*rTyH&rH^YVr2@@O<giBZUwv{vl|+s<R~Bfp}%UJnKr+hja?Z1++_8ZTkLVK6|@6iO)2oM)xg'
    'st587K&q0%m(%chNh!TZ=an0e3rniYVZUAd!u6+0EC5_x`wC1Cp7EOhTEX+HdJJ4ZhLO+txx$iP+UN((Dl`E<8{O>=`tej_qf@I2MQG0X!V<i`'
    '+1d;Mz4ADJ?7k1neaq+`+AMgm>1JT0FkJXo`v$>jhU&dsjy1*p0Je6$w%w;>&g2Q?Y0V(WeRbGqc~SNiFuK|!&drU$S;u7{E8F!U=h-7Ub{JCf'
    'CQ_O>e#dX`eho9h_Q~f6m{7oK2M{`F0n^?W$D`8J^)KAGS-Wxd3)jzIMAb144I0A0Q5)UuN@YX~gAIlpn+BD`<0UE+4jNFP)rEh;pxyMG$A-=8'
    'AP;!X7jZ^o52M-F;p^q$053{j1vcO*%n>B@)mEV_z)<X5y$&eKBHLw(v>6?bo<rySC20I<tcKs)_(TFb3Y3-6)lO%p&p8sHxX#|tH>*63{08(-'
    '=Fz%y<DakGe6pfPcGjqMHeXm?29T(4l?q@tV1TOnPhYrr{hy7h(O4(dg2TDu5IoL49n{YbPg5WCf{VgisS{PYe(sA8U+KoROIK=FAAj5!BBm*s'
    'G`Pnved*$b+EW*=JYfN4i13(H4|5_sRl9V-sADctYSktN=NSxpd0DZF^ZA@KXRRaULxGRvGe>kxJ&0|!;tb1+p3`jYpm)Xw5K7L+;yF8=PR%YP'
    'WReSzyPd-G>%_?5nmZ#&0x(eB8BAIg1`VuJ4b3K%3T7dyg+LjC{O4&iG{Z`TY`avm@ZSpmZHXGA$C7gB%d6Me2RpRZfY7YdT?dTg5lzJzIXAkS'
    'LEga2N(Dz*sp#{iw$(*K(U&&zpI4nbW42L@*jZ%EoyK-utrj(8!mt7~mKpWDHAD-nrB-Lq%@;Ia&52Pa3z(2K^tEBD4MSCnUMTs-o+ndbK28NH'
    'mGiw}fwEq7hMTJAJiy5q2qjq=Ipt`|>chBDqE0QKbCR8Ej4o6v-JXQqZL87>wK2aPe<`(~twL$j@702SKmYaG*EJIH-_PY;Y7%7<zm22VMN|h0'
    'E^QKIN_gPZXfbHs?fmDt;}VFfa1?Lq0Uvk4w$RkFTf=sPXu9T}rb$?<SsHdwYgp^{^D{?H>T&F>$Y`mMgH4X9vn0|=J<@!SX#AWsp%WjW$xSsy'
    'q?Vdu)c!k~x_T>*t{r2dKT(1o5*dL=%*`H66*OYg1ktVt*vY44-n~K*7>$k%toy2v5S~t=qCsb@YTN#n%0^Vu>0uZW`c!1HO0y^=F^Es?&*h<|'
    '61u(>C|1S1^ZCy^pNDrVrP4fkaIU%6FFpRatKXk4mCCd71rDx@XfIXii{aTVaPueNcL|SSm=EI<^iaidCt;2}EPVJ+f;jf1VHa&197F`kV=YHI'
    'q?7Os(0!;6E&0G@f1MQ-Usno?R#=m$vIPu0a|JgMK9ch;4HlP8gB6bbbg=49nr(kjgUyiWY0!lYUR&D*=?sY#+8cp8%@<+|^2kcPxh|Y%t&2j~'
    'CUdGm$1SYF=V2%Go9KoBy>!-L%Bxeo;hH2PbKLW;18Yivt>H}a_Hi5bTAf<A*;EJer#z!u0GtRJSml7u!7MS?<SfbSG*hZut94kBPNRm-fBCOV'
    'jj!7r2&~9-q*+2-pXPI${QWh*VNJv8B02^I&YX!?20Rk5JJn@Okb4oxSAb!H>BYzidVM%d+Z`ZaBYIzXWDJ-<N80%4rNU)MheyU*!~vhS3)46<'
    'wJ&)*O%SXA^{e9OhTD(5O-9;XVtU%Sdi`cDbb4F{D94S<4xP#5*4e_iD#)L#gV;u{nB8{CxgM;eyxDhJ7$_OGMPN2a<{_5qLP}<BC=Zs<!_$G;'
    'Vm7pHe*on$)F9fVHgPhrF?Y?j8m$l&4D31zA{09A(;z};8(l?xb71Is3$k~J@|h9X1;$7bO&#7Zu$-Si0bN(nH=<Ix+8;a-49@jhk3k{W!@`KE'
    's(!C!*34s5C9Y7&8?9WwSvz;_Qtj!B*Kb_9dc|r+5undOTS#R(L!drLVxW=(m7sNh%WqLmL`7cAdfW>j7Szw{kzixQ%|c&hV}9BJYtO022@(Ha'
    '_TIg_t?arJ%-{SJx2~rRKmtHK_yh}8LrGLgZAoNEN@a&(V_g6jBrLpw3y8(AxR$GG(s9zxbgxd@?mQ-$&N$tkJnWuMPfyyhlRx^KjH0srSAT`s'
    'kMq3u+zU`r*;zAFi7DXX-1FGyoPGA*XYb#pdX2ehG(I3&9@9!VfI|Bd#vN_Q<iWO|O&Ps%fVIq~!A&JUA)7+i-&eK3?vS-q<zw0ygoidB<lm9o'
    'M6&h-m59w1_*JwG<dwxo%Voa!ofnR1vV}Cs;~(V5!y#59h-%*OE+DSd>R7Onoy``Aj-c2w65F-0u}9;XYjca;jZ@r0C$9xOR~<J717Q-j#&{<H'
    'O3GV4h0<j@eTpz!j+ZQa!5sQV!+=A+HJQN?k3GL7%w1d135Tdv4kA(Z?e~GDMd$<B08-lLLT|Tm5YZM*pe;)NqnbYKnsfRbBHb4?eb|S@^tsyq'
    'BBoEWgS?(O{L3bjykcTf=$`aZV>Oh~9{HWV?Ow6qCd?!jT`Vm*wO5rdrbD!xQBXH~bR(_5kKb8ziQf8L8VvpQx@cGM_B&^c{55;`h0zZ6yXag-'
    '-R85@nf%8eXDY&`B(m(-xLw3}4M!;ZbxGtnkWW#X72>ZYWN1!#XYa|~2d#s{{re9e?X~Xgeem!J@=Ub(N3$;8f}j#QkIwU^z_g^E4m&Gh1iPgS'
    'Zb^lFD6DF}Hmqj#cJIl(CHg9KVUm9e`Co|QHVpd6fv;p)l)X~QTQ4j!n?Mzy+u@DD(F`y9-=n;VU6RN_JcM8K3DbaFVfd>!8JbOR6IRXMN9d!0'
    'adehxUKr&K^_Y(1BAKna7$-L3V7`Jj%ZM82TELJCX)&-|sIK1fU&fxCR8NyuEv!i@0Zkh0$=Ydog13cM8S0NLu^ulGGXZdOwNv@2erhWI905`h'
    'pJWxr0=V{z)(rvveJ6kAleFzOyxA0C#Z_2XL)LvQO$HgO6@xGce+^TPa4}p4@;s-ja9l=~B1SJX=^ta!g9^nzG(8_Q@R<+mxD6e7zT;gx3*m({'
    '-h%r^^%91_kim>5)LaJ_(7r?N*og&9i|Z4<45hD#!PICR&|fsV@r-Po3hfH{?FiX+-tX0GK5(`#--6cSomggeU=(C4L#*@Qg4ey85=D<}Mq)zi'
    '>!;TV-mPe=6%gM8-tTzMB!~6#bo6{Qz8J|c*toVW6R;4~crQH!kuyMr8{RW%ozQyTx2sZN&YWX)IxCAOET~3dCu;RJ*7C6)u#MWG9Jtq9e2B38'
    'VhcddHBKs24&5|Jxx|#}aP5X36HpSB8adU<9Bxf@#x!5s)>FJIU%7L*|KZWzVe7;F!>{Ze$}4oFiMl}RvbiH_RwXY9yG_AMh%v}77eO>UqNbZC'
    'r#maX`G_v=f!p*4coE0P&K1TxKL&{<I1BUQ$YBQ01<-s?A26_~^BmU}zj$5Pxn4mt1HIY|@OIwIU!k|dHTrU_0DHQ`{oU{7@gbQhf>F2GZ07Lf'
    '&J}3$C&xvF1t;54BQ&0dT_T{YS3bm^=HEfzfpTX$j-vf(|ExcH7>O8S=s5a<jC<bEc>vArBFf;Md9N2vu}kw&Hw5`A?B>Mz4b_e>JoG`~{A;<#'
    'a3=OuV`O(!7DsBZG^Q3o4#FnMFf!^UTg&nCI1{$3epG%u#xSzx)oA>XMViC;s6otKNn&W-I>O+9#|VhW&|nGD6^eM4AJO^#bUN6dqW26kI}bfc'
    'h*(2vsz_9V%|+cTkoQVa#Pz|mknS(@$T49i<@@-xxVSR2HriT1zDB-WE{piW=6lwP8;s8yBA}JLy>ng;mX`6l{jOfr<q2-4_^aa0@o2Y=1~#$Q'
    '<-<chm>+7vu|exL&kL3@OD^T11V?0^wQYjDft7QT(<c2~F{cgsxzmC=%J>xt;n@r>f<Dv`f()3)dT_BBwcx6i#0&?m`E=mDAAivBa;v%9bQiD#'
    'tNtBXcX|B0kXWV<j*kz#+-+5cVKZ(6aYPNWpS<eI_m9fNByrC3vK^jhp#Byu+&m9L)fwp349rVaDS0aNQB>YCZgUdLwfKZGmh)Ot)<^59p??&L'
    '^u$pAKK{636;}5p?=+j0Bw@(RuXKh$@j~44as-+tpiT5g&w(JuTora4zZ;97u)_=(x2~*_SA)2%h?<Ijyf522U}}^>XdD68Wq(wXmeGZy;Z=4t'
    'RZUixAm@8kd@NgfH}QL^s;uD&+MwOI+ZW;JbQ41N9zsH(v@#NJKsKO>X6<L905dOnA(Wi>h<IXa7!JqN%Z4DtgT38@r(5MVEh}gcMUIY>Ul(N`'
    'S|VDfAs-ag#nc^Om|-xAEktceDilroO~Wo`n|6wpVZ~Wg8C{CzO_*KMGF%i*LxH29wjmUm4+v;4afI0~NS7U}FUV))nV)+<Hvtm%(P1;!$cgf~'
    'qVvVqZexiT@`VNDlW$q%$?T9>a+&_v)XgOQwOr!Y9qgc#m}~)Rhq>N0U!e7e7&gsEaAcm6RMj<IDmerqEy4=1h}k@Br4&Af8<l<++mP7|$=FSy'
    '+T!jhlfYiYkvC&IL5uFpe@T8f>QZuUGoPrCk#h>G=$9&GF>J%lc%{!)B-kw+XScDm+1g|_jdf66S`>L7I({Z`TeklW6#n>QF8l7<TCy94uZcx8'
    'Hq11yv|z8q`UY2bZONOcV-!TJLK%uSZT`g`e9#%-Qq$X9K|ye}9g|gZ4aE~()2UcYA`eQuX!a5URWrVJQ0BF%h8H2mbi3#4DG5nk_9Ka6lb6e$'
    'HFa0vo;7ui;GQ-4>7QgwAN7hI^6hC1E?##guHfBkn26xwG@Hh3fR&JrT7ywr&k#?!Jvq)e*Ru_|?s>RR1oviRj}%95F6ISZd(lV8KGhFu#Zdn2'
    '(MflHG<`OV9x{D+D+>wwS4TO)^-^iTM;LN|+3PYZyLa-md=1e^baiDBgN6-ksn-X)A4vc77*<9KG)4^8LaAlm1@*}Qwh+M*WX7Q1n&>`J^bo5D'
    '#z5b8RxbLZ?)U<frn88YbMalsTTLRmP`wT?VkZjo$OOiN7h!Q4Vr&3wEI@ftaX~`Q^3Qp54#uO-e2VbUQlN`Gkf>0<Y4^L`5at}|n5n1Bu|Tan'
    '4IugQGZ1}wTE*A#h{)b#1Q_h5(aEb02xU_l^B|s<aaqGW$*+kKP)%iFX(*_IOiR%$116C_^bViJsz1_1VHqbhKZi*upc)#nMya0dFhQ9m6RQ$~'
    '5YP4Azp|&MNaML~it;<wnC-EO<zV!N*KC3W`nTJ-NpDksc>|Yf6?6Lvb&TCtWUXlQk{_BRNtfyFG^XaMvZQCyH?)Du4~yQ!vPd=}2CreF9!|%#'
    'jI$vEG-Hu3SB_b<NR(XFB5;pcA_C<FJ@oLZRxp#@91d?>)z`(1N`-@G)DCpdmRv_BZ{iWGT$@2R#778{7bNNlsWGGh$~zX3?axenWux>mn3wPS'
    '4`7p(g8A%x43!uxWpWN%Xo%o~2o8n4#+?iDuMWk5<9L<Sf~dSg)g2NJ22`{9d<?v@bKbuQRz1Qs#6T#F*FgxufV~aHp^m<I;<|<OzCnPrWaHNu'
    'vFzA2%|lQiSn<ICh+E?Ed2@umA%W0)90q8MCc#PrM?u^3NYsXR4Hyd#KT)vIc`)*VD4GxP*3i~7&G!kFv@FTBV&IR8tPsNe)uw#GV_++<>b=|a'
    '?BDRohLL)A8X!pHGqfS`s`5)EexoGc7-CRVJB0tAU4-EXYDJ^oMnxbmP0pw6tB2_-;iKR1PE_?*K28}TCcgaiuYFFG8NpOg22KCD^+af!@I$$R'
    '7i2HS7nJAHAN%r~QKVXfhm$^>dGKw$ymno5HTqeiQO*S8YE}HAntifFvl}VR{)@y|N2z1oFdOZ^L8G4}8if~@qVSuv`h!HP7t0Mba5lPTHCn$m'
    '(v|uc+(J#PW1#?cR|}UMJTN-+jZa>G%ArnYr`C;5RmzoX_}Y)y0dhJ<aMc%>rj@|mu8mDWz|VvThgY$Y2@b~v3Wa#m_buTONSO=Z^>PhS=jX%x'
    '@Wk%cDWVy}!^Eggarala;tAz85`7`qOL}l3cIqjD&Te_PaD*my|COM0OT0=6VCfSKJeq`I_XzRM@Vb?{xf)|Kl3KD9<vjP%f0Xa8<?|{%e}UjP'
    'q5TXP?*ShIkMu0yb2yuvKd?G<gqBKRsfTb9z%RA63MT24Kf-K<V?9qRD=3<`*va9+-lGG$Zi3UzaAuZ@V7DKfjmC&YNieGo4h}UOqmP5Z^VOpn'
    '2<ynw@tfBj<6Q(ggiKI0H+`h%6FgtKAqAusiozq?L*#KFpg=ocg~LaTr|hA(2fAA0IeDp7(tJU?O@9f4g!AVl&zcz^=WXRb<A~PyGKZQnwxNs%'
    'G~+}i`a?rTY|x2LPT<N<0(~NS_1b&y#jdl-YeLtIP6PTI235!qzHZ)i*B#9#802=3=rq00?z7&N*MmtFGJ<y<tCWd4Z3$=FysTsI{_fL9$F035'
    'yLTS#wH`h>-a9<p|6up{;j_Kg(}Safz1^?Guh8*Qry?rM_bPn1S(LpJ%kVCR)L|r@=n)qAve84l=Eb-Y4E82}AxY4phoK}4$vz$#j@qy^S;nIE'
    '$*y#F_n+K<_<_6*jZHO9F$hCmir<o$6cu7DqKLK2j)*1IoR$NrF@%^tJVhf}(c70G7hDiz+7-FLSg?Jze|P^LD3?bMb`fLw!QNLnw$y8C9==rx'
    'wyM2uWqo_Ay%BD9*TP!07xp&S!b-gtcB}Bc)~i;losD*-x7Jx(TVGq>sJ4S3tZc4t)hhmuY&1`6u;6SPWRUsRj#tNMEAOQ8(H6`{<)dH=|5khW'
    'w@XjfxAE^*8~<*gE6PWkUHrR7pKDe8+oR9DP5r!5*U!6EqkN5?(~~Ow?QG!RHnr7TqbKxtZC(G}ppxx?ddT%tTkF)@TIF=Xi0^#uc<-no{PddM'
    '`g&`#zSgR3Z;JVmLC<@?{B-Z=c+ZR~=4b&Ckn9XAHY#|wclhxBLs*MPdq)^C0c{$~170@1@%t6Kg0DV#(mH-{xVL-nXsKvu<5l5<woL4Gtic;~'
    'cEw<rDe=0{1(Rs;z#rZzH;9X-?IwPdE-iU89;4axDtN8+j=bDMo2DIf3bIHvX3m4oh-@0uM^+?n={@P(DwH5`A>a|0@MFFuz<()JdW1o8c2u>^'
    '<m0^`dF0o}@vW*2@v2lN7!dY_)v6f@(cf5WG-lJw7OUFk>7e~q)RMMiToO^au^5=l;Ep>=3w1|$IHO?kKVx-M53E?Z%qHQ@gWbDdQBCOELc0aA'
    '33z;BF=W@QJyRFeZMtn*+aT3j*&J`ShqqCi!+{p-#BBmq$^kBLW4EhJ_gFT!KS|(ud4e?>N8zvCjb=twlDA?^Eu8q%u6Yxdzxf`v8Yka}7A(np'
    'RUf0e6S&1;!;pK;WDt6CLC9f2k2reIb}M$ynKce!Q?Ok0E%s#$Ky9Wt`GxS3&uob(7WbWBf^4|o#{<N^N*pf>sJ4?zda6&56vb+?wqQhL&xp8J'
    'yiY_Z@&)W-`r>e1F#>wssrQp3axcQP7Bu$2Qe@O7q-Gjtq?xrJjY0UFw$N6J4)aU1!;65kV5YJ|a(<L9&P}F`2v<=Z)dyF`Y}(nl9h8TumJP*H'
    'y(ky!tLxa&=UxPqLgEZYg>H~mRqh5Hz0nC>(nl2ISWTNcHp7LZ6i;=@5oCv%A&9x4%!=NyAJH04UBSY^(oiat(mRz1N;lCXV=yUc8l-FSCYaC9'
    'PgzXB&VMn=Bl(I;3>v9BCX0xUwS-W*P_^92l)OZP@3FvW*WL6`+wh-`)TR`J6)+kJ8PL^$RB2TpgT%1$dzDp5<lZzGhGJ26`*_tH`BJtvY#{gN'
    'g8?UF$Hc#EH!<$oCqaZJ7EGl>b~Zv;o`sk|B`U`2(z%Ns?;d{e@Co+;-Uc2Nbk04>mOLAq1{<+@%ZVw$j1NMtrX&-HgbYVe&ATP1hucUdFj&M('
    'pV^|Y#F@7F=8{6N*xjR~)xg-_`v=Ex?8Nssh1bXI1ahg`S-&W1CofDrsX<XmF++s16k;13|H})VqUaH(&mHnHX<*n&p8QP;g_V?yAM~?cd5d~w'
    'X~$mky+pNF&W6ZHavszgjjMb8GZ>ZtYCEE|QP(@pmyn`eDs9;v_?<?Be-h&pVf5~b@?7*L<70bAp^Qize3*`sB)m>MX<@JxnZK={t}}GNlr>}1'
    'Q2<h5asHItB@}GZ?GHKFq=nmH+DBkMVZbgX*S%w}Nxg?a1nBBY0(s#}h4*jJE^>R!=d@LMjdph8*#Q;Nr1B>NNq~_z8bI=238!Pj?yzX0EqTDT'
    'rGWq4>r*yusV0!6I1=}Y!N9xAFcTwkoS6h?1oJ^x>Cuj>I)hEN`*dZDa3i1wpbwmP-p9+>A;N@U2tYYe1EW+-8Vw<}7+@48{=|4<0gx$OS&kzA'
    '<>7oLmr<P1*Kx8hBBM+5Owuy3e7VEw<-AYTm%@o(xZ%>c&f?UhDts-@sb6mk%0q0y=^@W_lth6kP3E&m1+N4DsoKJVZ7`zZe8k5^2E1j;W29kG'
    '3C%5ZE;7EdGdtcyog~i(ykF|TW;}-eji&-*^cW#b8~4$lpk6D2+}(NSWP}^`G?ujQ)G?<>kN&7PCa^(stPJ|(WW?njto!`ORP<vWZkfU-i8lBt'
    '<vm(1GRPK$0P-50A%tBY1bz@uT!g&B3a^uLfb2b8&od2}EQrd2S2GNmR4&sk2#&GbMOs5I+4;rU$9B*kNccHtN6_;Cb-TcuQER^Av~rw;azWji'
    'U79+XnEmv{L|vwJBk7P{w{t*WCOVvy%coYm^pt|^$Vn(Ay<?0HtQSgMihFh4(3<R3<Na%)Bf_VY43x3uFQ$q9Nt7>S@wL=p(?aHJO?%g(;BWtU'
    'QaLr2E(Yv{j&7$<4_9Por*&vDX=_w0&Sak!tW@VJ<q8(9&ucfM6YDaIu`Hqjuhn>aiv{Eq>sV5FxREfxP)!NSE!iE*yt(kKVt+5(6J}-kXgr^G'
    'LT9>le4#&z!s(3S=C}-K1_3&mH9YbL!`wBLP=>$G#?M3k9IH#l0&{{ltd4-RS`1|%0G-WalWOu9rU~C_#0f217Dg;pJFHvru}sHXo@y$1@4((l'
    'rgUcSDG^gA{HN{)Xh&p7HPhBa#FGlKhi5X#S+J7Xt1gxgj~y|rG}>0Ln2N4$xw>?~vK)1(I}Ri1K?eN^9DU@<jUExfDMXGbF8d-f*^1ui>GW=U'
    '@=>*bAlI;m(C^TiDTa{)&Ol2D^K;XaXw?Ga@CAOvsNW;SNW2E6?*`Mead|c#4!De7AF_1X8UbqhsN<E<;gPbA!Alsg<;PY-BYgt=6X}@P>4(wj'
    'ua1143|v6dupEWKv~!-H`u_|Y`qk*xNx69nQjMqt<CM#f3yWvY;72rjSevI>zC;_&Aeah3h!w543j$=!S#kit{^HM^RHC98^SbmrV4`4wr;r;{'
    '!2OjMZ>;DETO9dP`JhN)KSy1Pzr>HANnq|l{1{C7rRpYq8X$BG`DK+<{@<b})R2-<CEX}_*E!W8WQ(FxvVV$ZIcLSWqUZhrIs&!9V4nLqweqYs'
    'mR@eME3dQTk+fpO=6cb#xf`t;*^{C*lB@+W=(nc<*>lz2ji#MdcwC0oV{1kf&f2;Qyj{{av8+2eI?T2X%NqVkxZI?TDjYyxk`6j)ZQtaiuy0xx'
    'oEYMWwMeTY+onijSchMXOB-?mb15dS7K%Ju8P^6fy}d%Pu?B)Cz$yjau8I=T>R3bEsP!X=pn+$tivbXFh#S$_Mwmxe#KRJnQg|`TIBHYe-5BXn'
    'tqJV0VG9+qNXWkN23g@>TE`Xw7_VHs4WEGOnjw4{D`4RfyXsgn=B|t+;<NlYduyiq8pd~K)G#;?g)%hFmAYymMsq5rpZwu}{``Obvk!NlVd`Iz'
    '(&>*W)6q9EClKXx`uwl|^wp356cdn%lueY$?HixM1UT4h^v3d(zOfKgjtN+}p(tR_*l!cQr*nYFOdZTAO2}DBztv>~F3qv5S5xO?5zBIpY7x6i'
    'Qz4d>2{k4xy<qV@G|4^h_NRFaN`av?k4b<CKk7%b*7!MnEyP|kq{xCvdr*2#@Z=88!qXL0pK)!vdLf*3@#jhz{?A_tg%Au+YL(iWzj7kw4Xg<z'
    '1QKQJJ%~5`C4;s3rz<jpm7{Qf^gLErg$_dL(-m8eTj3k1gdn$~fqs&|SuG}ihQmMk5Qvtx`0N7;PZ4A@7RimwIj&l)kRyR^XL7)ziH_!EiX55l'
    '{d#|bu2b{~&2{oY`B(ZAf&ek<DRMM}D)I23b#x4i|6T#_g;2(a>C{_>eD0xP+)xp;6?XG^y_TuZCL}tWAaTVO3JDE$!Vx|#j*m_1p2DPQbp9bc'
    'Azi!71}MBMZ{hlA=BwN20b8a2ukimnZraF#$bb?pk;tv-)!oGyoiP}+f*CsadX;gtQrTEr^WJ^et8OITAwpB%C?|+NZTmz*J}(JyMUs<CU?W6*'
    'Dkjp8kyEA9<QARJXZ-;>EnQ-var^6evufjgTuGE7&3i(d5g6PkoNAjzi?CW+tvox%haZKVIi*aK%S<KM5e5xTsKZ}``vZSbUZH)|_yqbceZB}@'
    'h!v_wL+<8``vrgP$CK%}!%|E%Z;LPG1ELd#)#Y_3$-i9ZgW+#{8_S<L;|G71S1$aG1LFBJMnIn8N7#c+v(1MGd-NDFwGz*98%w4e+rLGxJ@4TO'
    '*Q^?b8s3o5rFj^C$bEwvY)Wv3x(2^f{$>1&6!1Q#eU7)T#xI6(A7_>X7YXZ^urI|S0Zqb}dBhEe9ai<J3=53F=n5-YC9u&#Y)r(uaTBRQmpSfR'
    '1oky`&v93W!|}*C%;NQwDW2UJ`wW((eX-Bcog|vd_hJ_zGR%-mlH-^FU8Lwj>$yZ<B=Jcu`lW1TyaVHqL<)un^`PmH5)z192T?eip|-C(8-COY'
    'Co^x4{z1;4q<&{Gj>6dZs5dtGRN!S31D5RH=<XhWSkZifsD{6$hPbA#@x+V>i-WmXkhC)n5N>W%Dv1&EI^P>kW|xjJ%Z2;AkC_Y7e@9d+7iruR'
    'H6>n|5pfJ_fCN8W<q4!hkOh-l&e2*g)*J|Ms2<IgrZl+p@>kX{CSx4tb-`E|B9_evzvIoRCeVa#Bvy~GChW2gQ*obNjbUbIb2zV0D44m(G0mq9'
    'CwndvyJV3jAID@w^8ye8Kk}7_QQ%ii<DH{Mftt9o<o*@SI97$R#1Y{|e$C6Xhjlxd<|g^R+y<fpCkFiE=}L@s*bQo)VZDSLL)Y9`b{%4zwb05t'
    '=jcM#Rnf__DR7b~xeiq88p#q5IJ1*_X=oT$xxHikOP_X$?E04H6a(?)Z#Yq$YGd95O)Bq~39r?fJllt0#DV-sZAAwS(t5fy-AUz>ksQk3G)`)4'
    'OVg6MuOg()0dT1%_pzH(B$#IQL?8~YH3>JEjC58hIdV1TD<qh5e*cIlRx|}Q0rRl;(WE~OyM+aVaB%EJT-!SR2?s-VvzECA;|HTd<WXg$(0qrr'
    '(gjkp&|MpN7#1<HthnK0Brtj1XUWG>gOv%Dobs~czWtUX50>QL@ia~PxfQk5C)0B!7PqAfD;!GdxR5pBXAU2?(CMfEnF`2?=f?&73GZ=1pYkZY'
    'XyK#8B%pAG353LzMN3R*9wdQQ#gqng@w4%4jLCA5Trd^5e+Nw;k3K(@&CrV>LWm;bh|gT+JsC>CTmxkHD_=n35Lf_AG}@GTF5!s)L3O}zt7l<X'
    'Jq)|(E^OHA8+HOPUrrhPptIok#)QESR+cbdOdE!&^QWRvwsFq5gZ%lru=2J9+(aP@(a|hQX|rNw<q)ZuTB8mz4PQx>Ut>&KY|VuHEb&E<`-ncR'
    'a&JrATl>brzDc+GZ7y~0yS4Zi_VDg_I3YwYG9dC>w#86hLB$VGvmMWF)-LD-T{J}&X8}Wev1&0xHC&aLzuO{^-Qgxd8Zi8%Po9|hjNXo?>AhIu'
    '()d8ORKklL??nJ25pXyuDY;@u;^Y=N2c)<W6O@X!G^i646dCFvraW0Y!=$3UWnxnhM>y3l@>-1l?xRgB*09)ER-1C`#hSA?ihAOjdy^A^2x&cc'
    'uZ4I6xN*#{a=xt=-$9Q)D~yzaf!>r!$Veg?z@<YA$@)QvL$GLUOrltvRD89V*JMTWkz@OrWi9fh?MI+r-UKtyo5#AHdl$o`B_ZwB@lvNiTpd$^'
    'cuQ3FPG)75!W++m%jr<gNHCcU`iy$$o((+<Z&#On3$lTcg8@%+WDIzW8!W}A1O*N1;PnEXRVY2I(wOR_e`=GM@hVy(*M(wQ)Wzks;FRK$%_%kN'
    '=c4J#!~Ba@bu-no<icKFMScsb3EFTkqwB<&V`~K8JT-bNQbrPFr|xmz^3dRlkmtHOoG+N&y%N1_*kGV&fI3kplhuP5;3@A?iA$;-ByHs))3TUN'
    ';I#h609-AT8NurryxN3KwK+0Ak)CkQoL0&iF1;k>lUnx1JqGwHt{}v%H-mW&g@-*f8VuVY&W`8i<{$`7+{cDRkUAFuyG<DeUZ<Z_Pu)eZ|5Ts?'
    '1z;~uOp_U2;F*(~&aTX2MbCf;;|>>v|8gscI{kiAXrz6DL<IG|OcyPo*|Y@51;th`9ndB5!ztd7Txs`S(CGVTlqfZ7P#HcT_!aSo-ifTpagr3U'
    'dw5a8{Y>0nGGr-<YMlVJ1r&mxKwnw@HGan1O5Eo@j;0%-X)EuyU|vTkLF4^)A^r+xvq5fgT@b>c7_;xNt|)q-LM<basIC*P=k!FsPmFSgz)TqQ'
    'DN0cXPm?wogL+gvt5&Nj^_5HV7%(16QKFFGa96%`AouxT&+CEF`(I^W!#%nS1Rj5&XyOQcP<@khrY(x9_0{w6vO$O42qr6HAUSx35Sd*}4?Qv{'
    '3JZ9MF!5OpXey!5Ra@VHM$Uui9324pfeDN!uFVb7;*}&``opf7M$NomL<%)vRC?`~&%=*Ilx50TX^Zhn^?7vsrJ&3Q7#bFJ#$=H2MT4P2oHqPi'
    'q+bRqSN^r|OOVOJ;iT@5LUR4en?|ryPA>5$Y@3j*uG5`pC;gR#6;wN)h(fT$M_na6;JSFmMSRD4f^m1}9YjbJ^_-|jutNQ>M=kN(Eb;Y7)_BTn'
    '1P&BLRyBnN?n*bD1)cM}^xT*N!BHs{w|p&4(N^cYKj@0=D8^YB6G+HDFc}1wz&Fp%fp1@qI+$QvUa`@4{kwEwgF0=dZ?xm^Wh-dEkXVIz@<h<z'
    '^@zuubQ_w%85Rwk_~Nn2Im9Em@yWNRRQh^U>J5Uks97o@IC)8vDlC>t=;0hDe(;4C8@0qCjZh!oELk;<#y6-`AM)D!$RK&=9HF@9!}1`Qk6><Z'
    '-4X*bRu|vdHSH_Fgza8<uVieSLZUo~bt=^S>jbJNXc(ia^`h{=M$b|r7V}E!mt7RPku8A8Ay~F<*{`CaIvN*Gi&nkDCJMV}RW#vnT`PY*qS=#O'
    '<4Ic`&Qi)2bd!eNf&46-zA*84An`7X(eNhjEwxBlo3d=^O>;!1rM4)$T+oJftFSVz7$ukKqG9A{#->S`S{y`35D~Fb1hcp5kp|)$c1zifo3S|-'
    'mgRzN{CAWRrqZHuAy7zooEY%0b%*@I%du>#_@H;i96*H7C5Gn^f^=q2QJ<v}Iefu-`ki;04%(Mo0Z|!--!`U}GtW_w0gO9+p`w*0XtEezKo6ty'
    '{=|1Xsh!9TEcGd316Q|hiLu3$Eo;F`VSqD=z}jDKEhu5aGGH}4)uL$viy5<GrNrZ#H9OIl9}|7^Vc&{~R_x0W%PeV?kx#GFl@p6{iei?Ug7e{0'
    'LlSa<SXEQw=FqXsT^2Mc8R@%76et8nR!pwQ>&r%QEHq+rSEgg$c=|l`rqQ^GhQkvCn5j!=FXD})+=odEoJ$7eh|EZ<W|L_MO9k%{J7VKF4h$N&'
    'ui)ib1}LJaj{GE_7pfv6M9-#U;#PTf&I5&P)Fmh--!x^y&NzcPj`BVKE+XHcGJVBexhAy$QM8cMNPtnqq3tC2Kzo6gkeS7_8GG0ey^1315Pd;Q'
    '98lg{^?JT+u|zGF1W&~U=ND5;i3~ziYQ?#dt)`9Td!@v*hFmZ}Aig)G%T(vBrZPzhqoxqR8gdqHvbA-TSVPXRzSER!ik5UfuT~yfIgl6yrUnr~'
    'YFyH|!{jL3urnwSaBC^VM%yYbCg19sF`I>J7e5F`FQkAxj^Otf{pomQ{e0rz+q?7h1LEcW<f0+hPr4K-qO#$55=c$JMDi!mCDgAxV%edU7+H%P'
    'fLkqemEjB>1sq4Iq)#QW-h6(`STyJk3w><D{}Qfv=2?e20_Tv8Gd|5Vl7<9xrRq7>Q_GpBN;CKeGqZ^9ai$apt&<*(>FqCSTeQcdcC^J-0Z?Sn'
    'a!Sca*<?T;NwNtEO#=>t{M5e}b`W#t{YJO&KHhL>oy5zZR7=~ZCl&Z}t6-{&Fp1oX>V^SJ$0<zcdXS7FX7DXEGan&yJsTlF;m31M`~0zb7{Nng'
    'j{#qpf|?HR2wUk%$vY*##lrifYP|n3W4x4aL7BMb^s!r^DyS|~$nQhbLdY7Bp_m$5C{-~v!3c4iyn*t_tm=(bMq?)E6gz=MBcw6m!>>*Y*q~?A'
    '@q8jpFl=Bdila<Tc8e#wL3s~EnU!>T3_9JCs{jQY@ezahNK0*5-VKIOmZej|dX{7sb`Eijf*K+ZPn4y_nAIVBS~Q-iQ<j&jf}m`Ac~SnvMVwQ{'
    'd`6R}sv@P}d&XV62JP!2Y##5$lWZcKQfa)z5~Xcwp2l}FjZEYs&O0JV=ss1A&9Ek^XNFbjlx)IzV~pkt`l<Db3N}orIziEEvzpH>B~%2f(!{ST'
    'aa2#JOMN7oo@fg!)2*F&f^@gJ>k0fi=+cz7<!2-+eI!9G%Tij5=^F{vqG)IaF?}E|`wP-Z^B*WnJe{bVYRj_x7=uy`v4^7$8AnHBmjKA@fjmyc'
    '6znLJZ&+cY5IxnsXIms?4Jk4;UJF^>V)Fcs7j$NG1TjOGAp{&_`CW^dLNP{2v!HXn<Ix@#YZW0I`8J)mf$iy3#Jc-0bh@PoB?izOIm-r(C{iX~'
    'p>K$VlQ#^3aWBWWuXw!nMWK@EwMZ{LK-lmv0-T5fPAOhA9T(?c$3B>}4O$Z2BD))l;|mOHMJ=VXvcn?!A{ki7x{@sz5+RAZwtY-gEG=OC)wE%$'
    'E=XgPZLC?!qO-KcCT6LTA4|*JtID+?1I(naRWLG5Gb2hdjtSV3Nw6Ik3u$H;a}gH|#?_)IlGa5q9nL4Ved7x>w!BT-Ny65VV&_P)aU|^<|D%AA'
    '!#gtZ5k%79ESwT6g2FHGMmP#*pskM)sL;4Fd5DSWfP>P7uNC@AnV~^Sj?9PWv)QCfHBMJj1Mt}S8x!^oU`raWl#lut&-&o__~4KwDN)EUEMS+z'
    'QMYwY9_mq=w~6-K(}jU=O~+AG+6M;HAC>keArP>1Fdp<f1pg8RLpXHkN562Bn)YBxce_?y!bcWV!0BXhWf%!xx+}y!#H1OQq8TC<#ok2B;5^-I'
    'tMS`miJ)J#gv*6+3%S+OcZNw4Au_VKI>ZS8{$NhWv79!Xkt2GR&->NdCI+XMtK=nF)C-{u+#X}^@?zGCaEeUn{Jm_Viph&hUPp^4y!1dt(FtCk'
    'nA7S7RT&Ri<kzi0Me8KEB;ly%<G>oLtE%^nE3<)V3m59OpLPw&CC}%xE+~DLhrL-xd^3u^Q5XemMT$y)ws&~MNXmY-yuE=@{(Fy)?tR7lVGTOC'
    'C7<8jfBg92F+HwsS8CN>P+6<2)wbH5jqR{kU0>gB*K6xRx7*$dtMGfZ=daw^J=$v>?mgOrfALKmd<3IE1F2|^Y+$&084O|B%}orHk^m}QorZ%D'
    'Q?#w(Xi=fGN>$6Xdb#GW>>nPtb`S49K;&|U4n|ciXBGvWcm7ccnDNG%c&$7;`}JJI%T;UDt*xzYXS=)A*{E%7_BQI9^)3vj5{BLFPPf(#*1J7S'
    '>9o1o-QI53YGGx&wqCDqZdZC6)c}!_k}Z9dXr~rd*EWO7MtyxPSZi;t*Eee2wV<{UZgkey+tu1ex3V7A*1MhRc4woq-KlQY>p|FUZ&ueg!mXed'
    'Z)Y62#}jN-)~jpXdf4ez)_Pl=&2TH+>eRZGUbWlZsMOnGSn1Ta>fzQ}r`icB)o`uS?rm;WHaqohr4zc^`DkmSm1wBa*;w1!4yzm05ZbJV>$PyJ'
    'x?XR0+re5GRM*$4we=bdqq@1d0R-z+dbN6`vRw_U!FsU0wjFPXlyFXR6enG`Q|qi(fFSkFb|u{03@cmp>RKn*-mY)-*4x`beSN*xgDI+ScB&h-'
    '?X~tcEXmDYt=ryM->xTG0#@l<3f1j)HLUf*UN?kK_3h49uvQ0BZo=~3s&2M7d%dmh`g*li+1{>hbk}>;-qw0;tJ_^$uWr@0UF{?n!gjY^4YoEr'
    'YwML-xK>}USGT*hwGH^v?$)*{o$ak|y}MECZFXwydbL;G2<vOzuvT4zby;h7!`$@>r>H@QA6WTM4<DuMtOtEK9M1dg;5GJ^IU7-b*e%Jp4>6h>'
    '6~>|aD?%>&g>+hqz&Gl={Spr%$!kvh*RG9>!kZhgb~e}Q?dp0^uZERweFNsY*4^s1YulT_=2lqm1{-TZ(5<g;Y}PBiaJ#+^TWY%=^wxt~@FsnV'
    '_odzKRBCImjoMpvOkLNmt#$BQXS1`_sciPTLAzV61+{j2v$D3mv9%V$lHFeGtZ!9<je5803$=32c8Crk>*OV*NG7Dof$}JnvRW`zkS1_?hy4i}'
    '#f_IywZSC=lG-dZu1O@`yvOzi1{)E5P?i=^qYI2A>DSS-Et>qMwq_Oz6!w9(4YG*{*!<*epa><^Ls&@-wba>*i{4klOZrquk(p*8QjFo~=uQA1'
    '&}GT+c^5zD`L3hsBf`lv=*&t!$wW<~D`f^4DmWw;GGU16U?3AoO}rR@A7U}B!Bn<`rX$rm)He}Put|>6hDd2!>(yYA(;K_wQl58AaOVvT%$+$%'
    '!cB_e&@%#PWjdTqLklX$aB|X!RmOmvMN_8PrBA7<mg<-)EpLnQ3ZezFw52W;394Bm&gzY&BUer)I+ZTJTb`Z`#%%&QFOc@FcgE?85pyl-i2FD@'
    '5f4<VBDCo+4)zW-Hy?GqE3>X^-?HL~$LNBruC&B}8EUE}Vf*t&{D<=Fm1Bh!N~R=PxGWwawDkBwp`?Q#KtOt2yibXOad~g75$Gn02|g>taFoYO'
    'vS367hh0@rgiz$MYM?T_S6D!)rz5oKk(VeV2F-wQ^C`^Ym1qwrhiN=OnrQcD9<+-!?;bwZKy`RSsPY*QDt3QVulb2!fFAx(y2{bea)cWUFVKxi'
    '4!WAVM#GQ~zcUr$C1}LC>(Zoy@(morQE3>x==iDK=mt|L=;{ZGmMQ%Ztt{6S3@!>wq8(R1SSb@u*d(BvKGmu}EV{ojWyFn~&;-s#bMDi^i3iS#'
    'CS1xGgg8|&`Q{+56<(Ndo=07`a~||Za3+n%Gvh2ZT;$b;XcaNG1~c(BliO&)8d4W3f*)O+_d7tz&H%5Z{p`i<f5@vuM(al2vwnoO@1q$aA$oUj'
    '-}dgHbmtwT?#?$3BiuBru)JZ*377`{Ra|4DZEpx?u=j8j;UTjsY~Gj`4>Vto%m8RL4e^2p<RFSRL%WJMe<3R=r|g|~lH<<D2Hp|g`?1HjZ(F`6'
    'E^6!w;(K>japWBi9-gA5UWPNPQF&j*)6Bao#{3A`4wHd$`*rq@5U|#}9M3U68pAc(Wc1EOqIQy+nHc!WBV3RsRTKqS;_3{7(OJ2JaTp)1EO2ii'
    '*9NpBJUwVV*nhl7@+0|7z*5AcdpeJRG5bd8ySs-U?7Ith5V6oK`{?1FyN`CCe9$`BJ$|5yv*(u4%d+-<VDSYjV**R$W~{l@?xR1s)!J%XG7D<Z'
    'YDc|<x#Y)aPnw-a;RL=IW!YRsXeB5*(^kmZLwqMKcmWXyFZNXTR=vEW8MMRo7R=*>e{>0~eO@kPHe->yCDo&RPw7~!0F<$s5|MB`JzM2US8?^M'
    'UNL{T77t5^iEzaoLC%NNE6RDLALAX(-b`^{Y0>9$=X^Ns<}2gP^>xFk%JO}IDN*IgxrI!dbf*vR_IsBloFRUu-D3SAbaEIB__@T%R$LPvn|3fN'
    'q+MH4XR6l}FAs-|_!*A;xX;X&<ZMnuLK~Jn?b7B3(FY^6aQ`C8V;|$XMx6a^I-elh7v)(#U2zVc8^Dp9z4yhNu7RXcVVMC^LDhF(%y|jHNaJIN'
    'x4O^CwMOl(o=*=;SC%nR7&L|6khx9`TY&CVi7-N`=2cP2>Xm7hye^~K0eY-ls44xB#kl`YJ+-KZWsDMW)up(ten&4k{9K+~Y9M7X`%O>$q!`nD'
    '!bt&*aru_Os9#>iiaQ;u*R|yr`i4>41#6L5gow}N@)gRXc2SLA@}A;J4Ck(Vqw7I&IGn<dCWC$l#8~31q+Vj&qlojQ%+7GKhHwBfnRr*A(@bI#'
    'Otz0<ct!qqNoGv17^e=6N1Xy@16Cn{i|#B)TB|X?@!DHY_V4NQ4?B=3aqzX{2m4RBj5QIC(@GysY}_9PlROE2CuXOLdRcPBn|OX&Sa`q*;TRsv'
    'pfHpvT72mj!>Wyeh-#sfRz@QxmXc4g4Ui=6|6@J{$!l)mZ4sz6D|i6o)<2sDlk?Ufe1YhtjDS?la$GiyPCctui6EV)YW+F`=or<j0#JWFA@gbL'
    'P2Ljm)vdnWm_RcTi^T<q&c8f|wU0v;5)N4qBpi=`7vq(k_r>*VX*jcs@xqtCDomRA@ZTYV>S0mKx6wCmTvt5HB!_Jd(8Z~YekI}Hq)SSNVRrH0'
    'naVw;*rO9)5$*tfIHIVoTyk6$Q<{Y(kLHIJl>)C7bc4x^q$KehMzt_~^X9U~O_3~NO^dro0tj3X7vq}yn5oIT@~y6<29H{$Qer~U3x0~uON4*U'
    '$DlZ@;bBZ9Fj!K<=GzW00tvz0uuP~Lf7cgONcP*nyIwky6-Qx4PBM~wqUr?7`^_3KW+knKd@}XL?PUH3`<X~bdpns20rPJ-bIt5vmJ#Xk<1Zia'
    'i(3UcjsL(DnA=sj`OB~HtFblVN8?-3<(cn@E{1J}UMqxJ=TAdkkC>@O#A(ZoM?Np&fQ8=@mW7k5a5cpcj%G%TwA>^`0vT^XamKn&q0dR9wstDw'
    'L_~H=soqgvBOg9OB}S2Df%IQfrvoL9P4J>asL(mU^<i1$MGL9y+ouT0LhR~8)$2PhJusOtbwEptLg`4o=Wsb|C$Bb4&5l6QDH2f57DFH?1}EJ`'
    '^Qy08P>%J<WGZ$+eS1RoX#x4p5BekF+>Gxbz<`L;kKc++(PzIWvpUdXKmY*PHDtd-R7^*lp@du^8X9MYVFEEE+g-{=8%{BW>@+OVA|jX$^9*I5'
    '%^|R3ww*z@M+khxkT;Y^LR5!NX;Vz%JL6PaXz2~)g2H~xKvO|71p-(OOG3k)K1Iq;$lIr^qNXUd1CVRb#|+JK7zOL*fh_%&U6jo;5(tC^9$C{B'
    'nR@O&+Wp`N#UU;Vtau+Up{<|x9^bv&x@&xWy!(~CC&m+g_s-LYkM6aO;q%_Hf9=S&>!tKh7~bet16V`%q2Q{_3ZVFBAGZp5?$F0~zxq}2;oyKj'
    'Ji15!7V@o+{|Ah8pmiBrV~VEb$#|02nN!vClNdGcwssF5;#tvpwD)Z9kqYpZYsLLGlc#m>;i21LCbfCwbg;Y)gO{r`8N7%q(bpw;o3PBuG-zyz'
    'Y<eg39JTD*SEi(aTZ|bnn-L`Rp)}ACCcjD^VJ0fU;u8)kPVmgQNuyH=CVgMsOU0R}nO~7Re48-|WX=*&6Iq7VF7h&+f;>*h*qAvd1)?qWMI@t%'
    'mTOyj|0*2ObP;`dvyC2!kqXYgjTipa+E(#4-?B!<+wJ+_xd6#5-X5}QS;E^t1m1G=L{_1rVwkhhxbD)O2=`Bpv3VtXK+A#3_>|uTiZx3^S)b##'
    '9{DPS68JuHmKlvpy#UtKP#XQ>1$vVbM?iIEO^)18l!<m9J=*`U^?2{`{^8eJ9~|y~c>JJQb>+TIzWVUV!{dj$kHpK(YGt#&xmMk(tvMSyI!33L'
    'qldq;*W9XaZEaMxmTP72s|Wjs$HZTs96x-#2hANn*uQtwynj@Du(x~gbc<vOzj(mzims`121;f$b^qy;yC}~dHCvd@rpSAcV36f!?cgn+ARycX'
    'w0h$!oOJT*G`KK6ng#KzQBdLHitm;~wDnB7KZ}Q^d$aLKdNb>HO~-Bb96X}c%&eh#B{Q<I#CUC^g;_^EvdwzT!p)oHw%R2I!T0Mi<S}Ulu`2kI'
    'cYGe|iyfF$+#HA!Mlb`Tv2dJ-%HClpu6p1Fq7Dip=+zmkkXbL=sL|<l-1CP0kFYSsNoe6>grN@<^}_d4Wji^r#oB&F@%$3>tqK1tPJr!qT0t8m'
    '`51JG!BFwsxZAq3zkhsme7JkS9!OPxSt1ILdbQ_z=88UG7rEBK{{AE0dsnE-T&MSj1%ejy*OuU~Et9`eXrgG<(_AdILLpkbNtKD?Cn<AL9l!cf'
    'wdb=1kSJD2uR$DO7<2@kdivxmPxe22(%O4`bocP#!7+$xj~+hx${tG`Y=dqGvn2!tEb71MVVZints2Ly5z<c=#}v{}@_j=j2c67Hr0h1Y%w2k2'
    '^nlB;GVDFV->(eC_!<#erxD(>;pOV3Hw&JJUWCrx!9b=0k-&odo)Bb~%0;+r)<<^x5778!Hj<X05X34>Sw+e!rB5&i%md4Rfc5Mf&uQ$92K`el'
    '_&SR#H!YA2Mb%4q`dGOGnY<QvbSDwy=6#31S<JKuORa8mMvF2=wly^Z>8d5hP<&nL)C+!U>cRM}iC~I+3!=5IRNt>R8)90o&M=llKue$4yKq%n'
    '2vA{h3ePw>wg9cGyWM1c(yFsePGb&{y^K5Ar|X3qy){PYOT+qM+>QmKOHKqQ?K8CIu^B5xo$B!tpyKRzBBA9uP&cG_OhHXFSA_<KiL)UiJVPgJ'
    'tK}J$L(yL4u+szM+E3Ikr!k+^w0A9kNnx{zxkf7cOvBTLHDNhvh=vpXlC}-R3J{g6q$T)O%;WIJ=rCx>ktiuKFGlDmTL&IKxxX*qK33fJDpgA='
    'w15FCaOFp^U59}I09Yk~f)XfW46vDfk^_iEIdrg2x0drJZQ=g!qvhQXw(7mkT6cY`(_Y&O+Z$V3+m&v2V{5y&z1FL3)wZ@ey;^P4r3V;xdZVAV'
    'z1|b*7B-b}i|^<th!^mZz{@efm&6kc)TVf8iL#^gFc9VXvN>PD$FxwS()g0BLTVq##9fb^jPiVf@#?TlwJBP3VHyqsveR={Wk*omYmvHVJe#zn'
    'l1Gz=dR}@&Y*v?BXmFkk=22^cS2C<Ov^1QaBo)0Y6QMWEAoZ1n7+3VJ3QKN9!|`*xhlrw3PRX^pi9=4OOs&tuOFDuXut4FKD*OUP)TBAo#qv<4'
    'P%K}%V>r_Ls@xjH=K3An1FJ_5AKzP*;woEtEEgqhALjARFzJPfXv^chNf*U_%tgYPjVkuAV$HXqRzD7*pj`LR9L;uPqfe)rw1T1vwSfG|6m|d_'
    'isnNk04$SfC<2A`?~BMSW0AS@Ad@_gGysFdNMHvEX+Kx(iLsK55hZepC%GnYGJ}J)F2XjFd)OXamZi6SG#}z=W-w0ewZM@hM^7Ig$+TNZaTAKt'
    '6~_pFq1<x<Mj8R5?M#!<68|QXQ;Ljv@&y{JRYR$IS{^Xyw0}!rE5n#pv&U8RboNOCfWaQV({$ckE?Rf?n)AfbAZB1bjHd@v=<2<(eOiir1zfBf'
    'c7mxL07GXY3{ybmauJ}Z%oIz4DjxRoH9Gn6Hc{M~8FSHRZ+?re)+})Cj_FL98;1No)!_navN5JMlCw--i2I06i0A-%PBT}d`6acF-?S&Uz=LY)'
    'VUu_hn@r7;@IrtRdz5?g!GNCS{ga^7+b!KkaHXq_HMBL-4|H?kiv6M7K3GBq04%v4B%55Y4y>xhf<O#})-VjvVVi)TTR;|IB<P_au#u988Z0b3'
    '@q&HdKR~en*x$H~!M*}zq#a^lGM)+u1tU^6Ofg;d6xHKSFcKI-$lQlzU;3e<MUJH~frVw~;4`EzTj&(;SyCqt=VsXMhAw{?TzDp>h<12&CmYkz'
    'AlELfQH))p%gO$jaXCm1oA?TRYsHm!)q#8F^x{9HDy2q1cYlO!wu1z=2L=BIXlt7}#|5LM_O4~lQTisqrspHAD&-LtxB*M^47OJkmY=}(At!^k'
    '05C0nnnbA0#1K~wC|5bIDecQdNvR`&?L@b(<Z^Df*UyLKnZ`-*sOx2Y=NVy^Y<L1}Jwz_lLcAUIjHIoWQMv$H<$Fnsgz0j1_k4^|0EF9vm+Ir5'
    'S1MTzmP)d<61N811)ouCW>M)V8ig&5x}4TEI<nB&QV!gFjqb+rMFMYWf$#$Cs9vdM4Md6$W*Z{?mQK0uu~#Y1$q`l!b#ky!sYFecpecB!rz1{('
    '-8KCX#2uZXX)rz`0jM*XGp2stjj;J)FdacR8lG8?bmnVbr8aR*J1>N`f)}uH2*ORY>D@he3a>hPaBa(;70i}o(-$7pZr!TaY}lA2l?^)4ZGAgr'
    'tQgjZJkYN&<_dkb&lFS;r5v}lQEEKEpq3W>>QYJ-`HgaHK%LOW0@a6}ab-N!ppk&0(~WFVJ0h1(5Joxsd67<$f;=eWKZ|S(#Osc5<-jx@5>_hm'
    'W`2(fp12B~QYNY_67J$~ewju?Bt*@O_?(8z67=(2ozNt0$;?E?!6F9eI#;rjC|I&9U51M#LTm~hxQz%yS!P)`A!QPoPY1GI^$ayid=6xd2kWmG'
    '2=3;)@{#dsEMh*5tKfv26)M#yUagcv7ph;9??_2l*ajug7$vTTvw^ah%X1ffjVL%DORIgngZEUjI^!gy%wWSn`Cm&TU*aH8qp|314bh_d(qmNn'
    'dkAtsUn!h;MPLjB8@UY4p_jcQ#9zDQ!7DI5AtNHN!JJa|lysOq^s5BD-tq7UU^GI57J>>T`k}#Kg53!EGM}-gk1j%l#x4W3+Tj^RGSQX6a11Yv'
    'NBvGdAxr5~`Ve=c?V>@%+f$YzFCM8;Fk-*lp@xN2p@6OcguNe|*?;ontCIGTM)FK0x6B(xgh;R$`+`M9gczQ%j(`v>TXL2W+A$mQ*o)Wcub6Z~'
    'wLL%I+-o$|-LblHs;P$I;Ltlee8r?^QHSI3sO)i<uHs|8wrC)=KGQ^C+P+mMgem{ls_DujmWQ}1Y_S(#D>d#^D)w@DXmdO48UCh@%`~Cw#g<Pw'
    'vapOeNtt`l*?Ogzfpq(6U?8svPIPTyf?Qdh2XYw};A?`*&?Z0Ssk5L?6NMwKzUypWsF@m4=0vvK8qVmt*5r_mvm)(nY3Rf%%wq)T=?}&rbt&i)'
    'F+;xPX-F7`xG1nzX@%5nQ4Pj6PvzJ&Y+)2IM<yZ~(hf7{YjQ~ZuC^&iAOo{cG302~QL9i)POxBAQ|&zly0PUMrt~tP7%~^q;H1_C&*-~d8huA;'
    'kZe+sh44dxqIcM!k}{MyYbDT0!4*Z#MP*19F$v8istgH{@&)69&WQQtn{B@!geZsD#CkTF?de)3B?K=~C3-GOLs*F-PaPH>Vv_|6l+t1?C-R!j'
    'i1m|w;kSg%uO+B$-U*evpxG^Sgvj?|hS;*b*}IqUv!8{O!1W}miN<zKeoPD%*JEiHb_r5g+~cHrTF8*W)Hbq&*Vk@QaU5t?-5l~7iOaWk%i;iW'
    'SizHxNe54A!<<%XL&s2!k_V98D6HV68kFY%x46I@PkoSYEYXSK%{n0`b*X+VHR*!%D$2nT2Uv{FkK{2RO6o|zrWN7WG>(IaJ7Z6=-3d85f_HNE'
    'Ku?ytb)h%P=Bw1EF~jcmhN<lh7qrLfW9n#!i&`dnvzh<Or@$^v0}_i2lCZ3!WC-2)MjVw@ReRzK*+Cc1lpN5p@<!{>-s;QTQMtQm3MtNsFnV^='
    'nTWBVG&5Ni7031Tp1uAODO!G1O$<ZL=z|Eg{6hL+I4d>l+zCnsX2$)qMY8mw8q={}yck+x?)0Jq@TwcKN{WzY3hGK8#0I2UWJ8*XW(A^|R!;?L'
    '7ek@yy3NeM4%jr^XdxXCPHGr55=CDFp34e9aUzG&s|ylKX>_pp2z^`gA{($RP|1~Qsy&GyuPa}!6O|1WLot|}8|hF5dx!HVzP@x?RFCt(i5xjR'
    '5Qn!3CyP@7uBNyq-jS{|q9zgPoF0#A_g$d)>b~b&(%q`yI#9Fny0YmpEg07@Oj=6$wHv`siLN2X9F-W;+5ut5xkMJ^Jr*{WXVTInLF&0yWj7{6'
    '8}KShJ%C{5*0qyiY122BY8%rf^664aHk~mikIA|!L?|?^2#}`#_MIz}Z{U@z%}0EResu7(h`lYzRI$P&HsOn-*c5SdMnsHEB>&&OgW%8T#`R&?'
    '-W4Z_hi)oGwC|lGVLT3X@imU!BITM$QpO19Q8#b3jbarJSGKP(>K6|@-a*h{G$uUzA(=$*3rn0s+)>bAu%jm{sU(AL8yPfu{;Ti*$<My`yWZnt'
    ')o<smj2wjPNAeESk7XV)k1l8E=50rgiiT6P?u3|q6|;K`XR5oi>egt#o}{zgtC&A{9jH#M*0g_?_Uk*I3gLWoypP)>pagYAwsgf$;nx_9IGh&@'
    '=mYjmUpOo@QfE6ZdwJ9q_nz$Dd9>GhwEMOFr^f{akYSCdo;{k=LZ(1?lO;&eJ46E5{>j19&z{W68XLcgYQa}UE*4__;%=eA*GDCEcz6NTh}5K<'
    'yM=33>1N_-FEZy&y8)`1lh+xGH@z%r^#=FD4u>M}2I;}kk31Kz$fiNFx=Qd+%_^tkzz86`NdXRz(KvWnK!B_?iV}^&B#LWsE|KX&yCzDnw5II6'
    '?_3ROC&f~1)XcSI5cb)Yktt`H(}WQHx|blXA+)g~1$uC!?*?o!m9~dSpP5<AO*x|gFP3HYgLBZ*Y?3l~xmIo?85RvaBlQat#atb-W+ux>oU%J!'
    '(Cxl1Su)yxqotNg@4@)2L7@aEwzm|e8^7`?@jdxolT_3~Dse6izB|W6CkQ;7kKdemCB9Tc6rbx;dGm!hhjdHXG{12w??SwgnQqOoAxI%hm8;Om'
    'j)|oFc&*w=8hsvy;cVLP-~iG8OoZFgEfB22>1_rKrL`{x45OM|gmAi{Zz;%EZ5%y{_R%PqMCW5@dR6`=AIW#|H0%uKC>MfsxjGp~=w#c)V3QrM'
    'TB-b#COXv^4jL4b4ZUPltVQhW1;`3Rq&xiHI@*1F0O!!dC&zn-&vqZRj=Wn~Q6(b<9@ZxsZRttG$_{q#-Fx`tgVxc4w>j@4=zrv3?u(RLa<C-<'
    'qCvtZDi?U`%fjhwEc0&(R8I6ZCMbd(@|QY?DZ<=~L>;Cf6Ddh#lFEr5D0DEw>v&q4oa6O!FjJ77{vt{Y#<Ny%2D-qRM0`OM5Ln1wrCeKA-7Kj='
    '=)R#+$38wP4;H8tlkq4DOX%&SU!gHTVc?c8!s#HGlrBQ_3d6g7jB7)Q-J`xs>V*g@i_u5~Dmo0i{a^&g7p$HLYy7o2W?~tHOov|&r(?O077bXL'
    'wWO=Q>ib=hkIR!DF4QaJyE|-)z>%W94`T~PI0XlnvvZiD1LfuBJwYrvvB=5En|<97@rFK>TQd5h)w(XEZ%u)<lmu*a1216l&XT|jRMy8_u1<`F'
    'Qc0x`)VO;YG|j+&@`UB4_2R`aQ(sT7-SB3*u@;?Cty}~$Nf_g((P}UqDNrdEnw)QlCJ9@%FBC#L2U#nG(^d#!D31~9n0DzvpRYb95|S`WIz+>|'
    'b!nIhf0K(ul@&=Wbfl0~%ZVTR{WLqlo32-T{iar(Eb=<FqX%lgn_0UkvUKgZ%muELPrfRgi^yZhsTqs9BF^2*t0?n=zia~&EAR*9na0whl;<Ti'
    '=$3%S60t)&EVP%#lctgabhbX?{P<f{t{r`AuCE|lnf5B@>N@H945)8jg|~DPiuBwUmf%9#tw0}SLFJ0vjmXGGS;Q5F7(GvD!wIM{DwfnTZ<eoH'
    'PhcdgbZf%$SM%A;n=&L;S)uW$sKUgg94iYc#EKiYm=uBaNaDX(sf1MrjYrqCsPHK+BT_)(F?i{0N+*qtb05PS#Lb?vs}s9rd@+J^>byUp3VTW;'
    'W>wc#axA4drVH<AZ|<W9yCsGIQEI*?&4?f$fsOzzN~On7E_zgO_#EODPm5iIM%&>J9K?($RB%p}kx`*qdJ|)LvULU9rSmd-{{4e`jjsfDJWMi*'
    '0AXFUa)Ykp-9MnfJI2BwK}w<D6g?3zDQuqi52_pN$_pD>o?6L1v{5Do&VlQW6exO7rX_@<qp!4>g6F_hkkWG@21sZ9C`2q-m?)IMy3JH){Zj#P'
    'CM~Aq=YfuuB-ml30DC#Ibb=fcg)+iY<IJrn`6F*E3!lUAMSnbxlzb?oR}?%wdpaLH+Wp#xhYvq^aNN3cxc}i1h&l`<&rDDkKS?hd$K29U5-Fes'
    '+OBMLE`D>A^qCYf6H=~?A(&+XGy5e9$2EFpAwK&ucdZl><52#ESdJfg$#yJnp6<!SiR!*hdR~@P{71>>JV5dtd&HE4Hh#fYh~d)glZ+-|p><@5'
    'xZ^;Z*Ax>WQ)B?SPGOAdU_73X-Ap7ubfwP0SbQzR7FAAM9X=7Gw{?{xOS2EXxWF+5*~PDNo`Un)5GO<|Fj!w7>^W|GQY+E82)d3B_Yc2ff}q+C'
    'W8y?;Ey<{o$*N@AvKHymmBiOG9YX~hWtY)(oQGS!qeYqb*T8rn=s_N~;KtXD)Wg<+z7QLO8-_tGe=7qmzDnzwoaJI+QhEBt$d<k0f~a<+%ciSX'
    'OqmKgG0HZIWl$Q`ZW0MmsWYi8CD!7JuNefDR(vc`PjV{lQ$iF~-NlMx9Zu8`J?~E@7`A7FeQTdy=isu;$f^C4l7@y3=JQ;e<AA`1k=D)$PmVy>'
    'i(3I{r;T?(Z@-x|6LS2&g!{qkP$f0dyGTJUR!`i*ZUt2Fe!8)=oB1C#AYxB-EP66yxrBc-LsFfzSRBIX`VWR1A&wj4`BSnz<+Xv2h#SDnzN?O~'
    '_k6nqIi6q%I?m(2(($L0)hKUbdV^&j*DK}o@lck+kMCuSeEIzmU!JH`;gHctPINdaRo5$(#;JSVX0FGWU0f<DY#eZ}$yllUFLXUFvpQdFnG2K5'
    '%u&k3yvB0;f5B3`$s%~;<>&~ATeuBQbfc$li}9u%6#vLqxqFGr!8jJW%UkmQ=ftlmL%jX9j{@3TTK__pb4LnEOU}zre(>u1fAZ6R_pO(oedpz8'
    'fA#XSfAjLwfA{j!@4o!(FJ6A~A71_7?|%A!z}LTc`Pq+Ne)c=~@Y8?&lRy38PyhUP;MbR*z~4W3`HfFL|D!+u{BQp9)%W4^r+#XGl&hLDR9HqY'
    '@~0NoOPQYK%HhZ1a!JX&q`y8*BYuL|S<LP=iEj3U2>)#B)jtPH{{G8PfAI3NZvshQee2se3xD#%&wmIXzW4Hz@4x!LzK?YN?hjvn^82s8`&*y?'
    '%Rj`a`Sd^joQVI95sGDX|Fsvfa|gZIDozqst?2JP+X5o~!;k;ZpZwnUkh1^kTQ5KPx1ay$_kQyI@4Wi0|Mcp|zY9gsYvT|85Q@QcFol2qv~`YT'
    'u}UK-(&_-IDRi4vJMnnM&6VZdMl<sU!TGZAMD0>i5A%*8&2a5lAyThgR_0I+8S`0dK9W5!48Cc2YU)*K8o|~Lj$#dFDg7~V#qxUXFW0tZJ~GdC'
    '+p?{<k?>0{*Z`8$MROeR;MB%SbDS&IRYigU7Cq-S#{1{wf<!;_n*a$pmJFqs#zj@|-t}r30=EMo;7JJN3a7@p7d8ryG3;bcJCth-TJD%%7{I7V'
    'e%bS>O7csWVrcp9BV<dYCN)ii_+kzBB*VG4t+rL)5v4`_)PBRmAWYx;zyJBa_5}2FdRHBj#K9>}16guq3x>C3Y4jgKA<!z`AI(yVr;7PH8ja1G'
    '=}SnH>Y?-)K+C>#cHckT%McgIvxqpW6V}1GWSMw6$Pr#<7yLtx`LX()jLVA|!!7gnLRCcP*ezcKFER$E=E3S`qVHv(9w-)3dD8Eu)-@QPNv}%7'
    '+fVstun>pC!pABwVHx*7L4I-obdN>Ji}IMZVi8uO*{Z|DO-Ac#QQ+X7x<d6%Qsp66=w4>8ByD1m3jXRay>s?N8@`rlX4-}3*<`nzj^T-`26vpo'
    ';?BDR6C5txnJrSBfxkMg--J>(s9(AM`>Xu%?X+{6Jy}cJ)ZfOWDK+Yb>up&o8p@kYocBdEV|zV+!GlS(_EraWT3Bwfk)6SJk{<A1<Bm}29*9S*'
    'MD`{|d|pT(P)m4Ekt39hJEUZuMeQD?vFBs0E^*TK?4!0HjRyomg`(@Vb`nKx^6j6$1`w7&%2`5l1Ipn&YWfiupzB9V*aT(PIq^09HE6;Br!p2*'
    'ww=vx?Zn5`#WJOz9S(51XkkGe2H>Q0K`3*B`CGT_mKUzmh;<JYqP~Q<AzqM8w}gNJ5=>rHl1WYrm%)q%NZ6JY<O8bZ;!9aL<}NUEy3!E{`iTl6'
    'k&j2Y^3#%9*6gmkmgw0+u~N`a^hb!4GHDs#7pCszG?B6A)6OK3@imRvW@L%6<Sk!46>DM?cx^vDjd>uW;$^(2z3Gxpd*sh8UC=~w;<vtqMQpd^'
    'oZ;vU{hcXDW#KZH@0+%guSbSYnwvjl*VoIc@|`7EOJm^-Inn&!V^?&T;V0)3{gt8YEt3TK(W#{Kbg99vbqg%|(sqH2W$CHUu2Hyl@x&!9Ye`!P'
    'uN2V_5hsvoaaI<U#ZxCx@;M6%`iG0jgeg>^1_Ys<xBG3q69371`Auf`*_LFBf-6Ss2ZkVwPFu`P?q{&IZc7oIJMSVk7mSl!ls~1!;=S=w{8_L='
    '>7E|7I`Gd-SSU?CT#lExQT1`p<wWiBmnC`Ag5|#ab*b#2!CaG_Gk$!nie(Bwcg^zG8&{1k_kyry*Q-m?&^WJdPA@YnVscpKudi9PNUoSxxA^(S'
    'l?z90GeqIpKIXDJY(0j*j|9Zr&Fd96KrI3xXQ~P_yB|$L44FJhB~j7as-)bk-qhwq3eMXxHyIKr;U98r2pLOnB>JIZp2=P;u(0L$j3dCwRa+*M'
    'h@IaS8V=6Fa)=ioSe*}4YQE>?uax^?FzsF-unmy5GoNAtmdnN{c+o$@P|C9W`L6!8*qM$&+UHbhEvXqc=;=N^d?=n3F?zNI?T=<gmH2QMpIsOH'
    '6q8s&(_2bo{V<pg=aYuG02>5tN;@dy5cxpEe!&=6aXoly!MGH%g#SS>iEv$Y=OPDzo@l9po|bj?);!)$EO6mKfldR7Cu%F&8p49}U16oT`XwX$'
    'X1D|jH{k-Z4&x{ddNY{S3nDfx_{lPdh*Z@V3G);{FUNuzMU3zy8)^RLQjUy{5eh5id7Y0`5;Jb4L}7|LuqJc<;?Ycv*P|^Hv&Bgir-IKKp@BqZ'
    'Tue7)rlMXm6jv}**|x=1)gl)Wr}#{+_Y`g_e`ZS;ExfYYB4qWVX9kxo(_L(oFQl61hrvgARWCC2VSi*kc+abX#+!ksf5Xd2y3SaY<`u3`z?n!R'
    '!49Z7Jq8{RFh9)z777KI5M)R{-Dy^2X5`#9+FgwwM{?5_y*PU}pb_H<%VDbBZQ+4DLv|QY8Y-*l@?<>8`(}xh4jcUR2+vYV|7zUz-tz7|+Q0ji'
    'qt=0tE#S%V;ltfW{DgkIMqmFvA}-<INJPIds)F2m1g0QnEu|}%l||jjE64!qNP}QxC>hIaJ{g23y}>v@yj}k1?qvR0GxjX+6^c`#6uR6mc=L##'
    '<+Z`fV9EMu$+$k`ghit6vKT@>>$oVSxB?UTh@)p+Hz$cit7Mq~vJ`g|M9z07bv(NJVDIs+5#282nO1-t2SpZ=%0}Z063>2pJPOP6S*K8jO+w&l'
    'GkT6Iv8h;3VUg{~XX6}5n?Xc&+aXPVLQX&sOs1MqmL70n`iP~u(CNZ<4VLX&E*nlJi5a|*qYCSsjD^<LIv*!YJ*LU?VW!{-&ex*OSUVctvA!>S'
    '+uHmMi(4#yS1dk6N;w~Q88IS6sfrVjj7KH8*7yVu`qL75C+O!eJ^iy0CT$vZ$Lyp*8;QX)NTO%ohh^kj(K`C8;Gu`+<LG?e>kT+-W6(M08vUh*'
    'W-J(Tt|+|qSj~l90Y!!vx&P`nfB5RR|M2tg{?Sjr_oJ7e{D7U@UVZo5FTe3AyUPiu&`-Yq^0RNh{Oo^!`RTuZ`RRXoe5||_5AHsG`PtvX-&f!I'
    '_N(9e9gKiQ54zv@53l~!H=&ZB{V#v~4}bHGpZ?~5#)tpu?_T}z-~Z&l{MF~*`h@%&fAH#?-$kFgPrmc=lRtm?$^V4Tb<Q&>qnBp4j+XA0pYDj8'
    'g(&FeUnhc|r&2KTUw-nt=)L(H-vrXBUb8m?4&3PStKa#zK%bYN{@0hE{pHKgK70AuZy~8a`Fk4KAETe`XP?-={^0XJ`g53+&%ga+9LHO{JYF`A'
    'DQn=$_44~a`^KNZWGk=BSAY0Jm@*PK;YT==U;W_kU^ajnpa0<Ze)^-&(#Nu>N$KAC{h$8*k6-=v|N82;|Lf;}{70%?(IFu7PyWSUBJIEN$*bS^'
    '-aq`9=0_D>+&Nr8uYUZeuYU9|p@dq(pZ}Zx`05+q#j3vlEv%MTI<D(Kd-;vuc=^d+2v5??!6lOl+;EwR9r}Z3Td%(H`#=5P{z$DL?b<DN40&<$'
    'zVQn;Ue#$1%S7m(ed7;a{mtJzJ|<Ka_Fh)R`|0<7`1!a03TG+lTmISKQeA(Ej^(h<p{u|D>Ce9XyTaR6aEU*Q@gMX^=TYBiAi^O+xG5_AAVtVs'
    'obs_~MqQ*yx&6^hr50>>SEes#A`zl}OXh$QHtiPa3QziKA^{n(^netE=`){9NC;Qq9SgL7p?NVHbGl7M>NwgV;OEjic|mG+FF{9)O?JW$^dlqT'
    'qSC80A^~YOsW1b@Sbmb^wq6>oz}dYOkv>u;VncYgJkgYJlu1Q_N8IFSK5Ub=p$b{)xvH25PLzR98|&-!^?0T&m6CFIdN#)maC|wDnygH(CvUW?'
    'Bw0~4b}m(JL-afl6}70|X`Z^AXy$-^<lb;W#@%z3Qh4A9fZA$x$DI~|N2BzQwxiA<hzJ6mUp@>j?&%jEgoDX_S+roZR}Q+MWr&6_t)TX0n*j})'
    '<_r{3c?^ITG{wL2Dj{DedgstWQ$i)*wiA?<^cE4>sO25}j;yjl28wDiC7YtRy6V-WDKVwzqy>m>TfMDSY8%x`Wm9$%_!`~*^|2MhJvh61QcOx@'
    'Ad#&cP{2M{%9qXv9W21DJtM#1KRw!iVhojFqDq)*5cd4|qdrVaIJoqxm6E^?;a0LoE7CtYM_jMevL~6%22xWLRR}#_R#L5$E19)0XSazVU#5^X'
    '2k-`?N)$5dr5LzUw?B1P&501vh>*{&htIDPny6hfHVsAc+t7P({P+=@vI9?|1a_^|e>j87tdB&mxh>-MR*xE_{CF_;XQ8+krtD1wL8q{SKHPnV'
    'pr=^1%<LBem+5wu?|s_x(2D3y`h=;Lyn`nnFzT;m`$DcaI}iI)==iAL!<6)yJ+PA1qAOsBp{S!7Ct{nU2O;-FqNqTVg68ZAA-D;)EIEjsjW7=~'
    'm>`QZ*DPQXCdg@KpAcwPIp{$%!j4T?;YG&i-me!qVIU$vp_gn);8^RJ5HG)7krt=fcoqx_i-?xY=8LZIAV@X+_X*p36wR7GW94+taerYNQGuQF'
    'V#`;o7#=mC<EtjBu0R$BC0knUvH#DucDxr`4W_xqA6CD$tx$?+Spo-x<qnfI*<+K$nue&Tbv?0I4Z0Zobj@GlNE10NWj;8^hT#3DuoQt3G94Ks'
    '`gOk^bdVPabV!-0{sxWDrvtUXR68oEr<KoBHm#6@dgzlTvV3MYDUm2{He9tL-h9kv2b@D@l(5qMgGAQWHyE~H5hVhU$vUA0*pz`^<!Stx>^2xd'
    'CYy6a=i>|N`WVBpE>OPgqb-eu8Og_3x5#cxND@mhP=dLWj!dczVp0;03ZZlZ!q`Q>qmV#_BIQzM>VE!$Lseu50ofA7;p~3N!o*q{C>jkgf((J3'
    's={oHK)A$Lie6nXm>oK8Jx+2y8sjk(cIOihB;5XR$=<0m%CQg-Y`k=iz`>fy$nqi)uE00bd4h?SMCJwbF(_F&!;3T5$s6=%A{#LQ$zfnWXLCRd'
    'Y*_JxcFdIHkh?{a@1R$j@n|z<gVu*9%&kyq(bJcqY%V5I0LL%ag}O{x*ari2f&GRY16Rt)(bI#2!@Z*;OMl{uh5m#?YRw327d0oXtLm_|BROhx'
    'vV|JpwB;Nguw;Od4h%qS(9+|634Xy&B@;^zBQ7xEQ_ac%gXe2nYL}F>E;5Tt0L?fJ-fKcw$E`ZWl*-NJ3gQJ~P{7g~G=0F+^3i;lHv%l1P0tEk'
    'R+e{uukHwlFk28hnIn@uD`Z$lLKjq+v3!tY;4RP)WF<K0*Nfgp(c3gk8-}bTXQ4+IQ&)WijYceset~C!Ryf}v^vM*2kdt|8#klvzpHUUmB|!I!'
    '*;1I#B<4f+rRce0POUoVCWJbj8qvzloRIregZ_eMO#MCOP!xWqI$9~xobqdhq8A@p+>1x|H)$%8S4c8irc9uIDki32M$hn2lCA^PK2@M<EA#wQ'
    'WnwDuWBPQcAaA{*xLWChAiupE_eR9Zd;MuNTQpRjaZ;QYloVzms#PKr@S6l5oN8|qoxjp_R8?T0wtlQDa}f9%x673X3daR?Er3S%B#NZT&5G=Q'
    'Iby)kz)1bpl>nEFI{`Y!6n5bNIMh_Qz<mjUYcwRK$@j{OQ&aHdAr}^1c_Z%t_Ev9tc_{HvWLe0Us^v-rI7_L*|H41wj|@LWh|p>1yu`LDQWaFI'
    '+f>DdcPj?Jybezj4^^1!f(BH@)oreS?XB036<1aL4mymH@5M8rVV9Dv(!2It<Xr_T<r@B3kN@y`;U-QBteb<NRUjc~GaNA1YDK*PlvB>@`%d^r'
    'dynpyj`xm^z55Tpdi?ZoPx@<p5snXW*M=)Tv!EOAzV~JS+H!B;sWROf>Xn*yW<Ww{F9zlmQXFkDJtA>6lMo7UW9K{3i}F3_@FA)!aEOk}M>E9k'
    'eYnp<XhDIzyfqS|JT2gVBpFn0TB(t~B&*_TjcZ;A-b-+$oEy<F*OP=Qv3X_Y#8s9J*tTet9d#){*1E;wuX;1l6%1v~;a|$+)QPt;#+8pCL+;%!'
    'W?36V@4X9h)(~;Le)4;N^76Caqzk>j|L;Hg=G~)b|NTe5_3o<tIY%cFH<4N!=2#i!Bv?yoX;~*qvY-F{r$71YzkT)1@7%Oaz9qnPC8e_|th&s?'
    'UmnbMzI+D%3x<=O%#!D`*(7SLuEs}!N72y?j;UaPFz90XFxIM%@;XTBckewZ8!1Jnk!yAtA*MY0Xy&`~SJI0huKD!AxM6gc;$zxROfaA7v;qh#'
    'IZsPgm0#fnGo8*S*h>PRL5pdWH*jdgo-CM!^*%VMoSyj88Ar8jZ*P|?Nm%a0HW#&sWC)B=_Y5z(_H1lA#Ccgs-pBDB*Dehac&515dN@K`A|lVj'
    'G#wt_2h9;H)`n%e%{8<vMv*ClE|wx%co!gmP}<Hi)WLe3LK0+kHUTLJ@4?Vx5Gm{t$O1<(4VIXKI_Wx)<24%|Ynps3A-@rOZFppG6w9tP9yPo+'
    '`XjUZFNrjE-3@W-0O3i*7YHoK^TQFtm{X2l)NS&4am3}uy<QHa_B^Ky%Z*350wE#kcG<-z{zk!6he&sp5b+`oRY$Vl_Hv~h{H50Mi&nb{3ngbt'
    '3AlKcuZbhV;<a#pkOVm~54nbhfXu-Zz;}wS&546Q7tel?qdCYmcrM^OlQ7qiv~<WEIgZtZGPzakrP*ya*g&pGFNK2^_sQ{|#!t^V+caFxbFQ2k'
    'IT?7Cizm~NP4m4PjSQAG!TQ_3KuHl*lykag=8W*M7$Fxp`{#ruH76o>owW-<;+#Tm%n4Ji@#G?Sk>hN|&NX8!I4oIk4}%w<sK;1|Y59kZWp9Cc'
    '@pBh&76@7jZa^mo*um!~xqEweo_>%+7Mq(~G=!xlm&{O{drDSk3e^PJWfbvsI@-g?SO&`|wun5rXN)ZpOlAUBAv5v80wm9z!-kgQw+!kOjMQ1S'
    'ilx#@#BfnFw)~4NIKIwuDW|2bwW!AEm<SQkZ5)Qie4Emgeq|($ZrWpmc&h;@2RjdihCzOsyBBs~k>}rUbPMmJDy(9!fz_T=OWSa!!k=3OGiVxb'
    '_NkSe?z>@>n1)~H=#(ZM1kmF$pHI*nG&CO}>p!D}c^}V5&qw2n(Z}i`9D*Oy6<L6*Z;ZLf;T_B{19Hwu$veecPp|Mk1!%wjF`v=pTTmt*JpM6}'
    'I1y7`FglH=R{5d1?qy(26IYCiOvic6=aTX1Ib<8mXPGfTnb9~|tbhxmSSq=DlrtztXW|L2A&OYMb&E5C=hXgz^?x=U&nL*hP;sDnzy)s4PmAXV'
    'Sj2p&%iw5Qa(Z$NHB%XiX-<Uw%Wzc=a27$Ixdw-X=aTXL$RA)0N`hv266bg?@EusrPBf>tNuekU3MQ+6qVx?a!x4%GQcxBI)e-=lC!~xpok>JE'
    'O^_cSt2k0(bnxKBH3u}vib5!*Vm6Ua=|}n@laj7pjO<R(8Imk!l}Q;w+`1u(tq777J&q2v$Wk<WBC7bsQz(@>X<ehrM=5=K&+OVLrDvl>9iw7-'
    '8i7DQK<d$V4R??`C3Po{)tRM6u|g~9lbc_H$cu)mPa|OeA1yU9B+f@k+TfW&rs=#`K_HKt4ZJOaSl3u`;j;dNHZH0B5VmPU`}aKwFk}bqV)hl6'
    '@B4Q})@*q#S?Y$MxucLFGcVzNq=aZy*L9SIi{j}<m!8Vn#ZV#T6{>s>qc})B6}QqbcM-Zh#cw?6vQxk=dMfXqlIm5eH>#E0OD5w%zjMh>F~LYD'
    'N3wEcvv?JSGrVXlla4otR@ucVngy5a2E{gk-N8UyfGTASsX}7bWQ=Lny7IzQUu71>lJG!24JlD+U*MWD{~ht=JVzudTYA|$#;e#q(!#nxS4u{a'
    'mKpkHd>FR9e|l7wlO;`*tl_ez-<n{?BDx4_dQ}yqpi1%WIUmXKNI%v!(E-B1q@rv-!KCl?hh#&EW%uEs@lTu&majMEwYDXo*<F!k3|TEVpY=*x'
    'xq?i;IK#Y4bX~?pVb_bLCwQKKt`#L?;I(Xj&<2Zq?oi%tkHQe=)HAN+4d)|um6LN_*3u$A$$?~>BcYhSQSg)c&Kj81r))Snr{ZF~Ww_g-+NONL'
    'DDR!Ro)>g*Yjai(a=s@69%pB^H(E6H&y3pxjTdFSt5nmh7adv5jVk0b9PQnMr|uz{sm_geO<7U)z_=bPN{)lrWb+|8U8==mt)Q!+2HDmK+l1kx'
    '<7Km!*1b709g#4a=LlaCD~KFx9h<>O2JG4fYX2JLc<)MYUw+1wv92#OfEx|7QBgL}8ZP}%lxeQV)2Nxx6>$n0XiG}MNo!sBwGzLp6u-H|{zrR*'
    '_z2A>(<A5JLVDy}ER`f0YH&l5(}tM=lJi2^xgybx!|<5aDJ63m4LWkN=i)ipxl}R}eC~BK!Bc(15WU=RgrMPwNNw$LCPKDB;R?ycae6zt1odRd'
    'G2GnI)zoD7u8j6sRFJ_H*sq}1Lp+$voXQ+7!nE^+viUp1xWklV&i8!0s34lEtibyLrI?I<DDVE3;Zs72V$#JVi@YX4J-mH&O@=F(BbA;AYz3%T'
    '-)XC>$CFdzUNMrUlQiu4oM{zZGM#5GE$StgNavYe)JlWLe(xZ#XtR;G3Oh>4069mB&t-FD=9H_xa-kERf~DLWrpk0>hs<gC?%WNtQ|5Gb%G_&b'
    'r_8DBl(`#Zr=&xysP2e=y+4tMSka50Ov=@W-5mvqgp|S$L37Ykx#CK&F^IiK&$3^Gc@6vV>#4kz;}XX&qa4J_cH8MpbZ@SJOo!TXfh~dB1xpJx'
    'G4ywl&setrzCa*~a@eR+wDeDIhnwV?en;d2Wlw*U-j!YiV$UeWQ8VXG`^lNi`KWRYBR##SOzUA7f8ul{%P@|Z>tBSKVh-}Iq1drMFP=nauxc+6'
    '9EB9#oO5o>4$%rJ7&PbW2XTTgP4UO3)<?&FRv-ALR^Kv7i*riB*UieBb7f`CEuWP&N9kBmw!(dXv#hLQh^fg~W3QR%d)2Es;{qx>k7?|HJJs+e'
    'bQ7m`P{Xz1(27b+xsh1g*V(NaC#c<|Bh$DbW<1mQ7n`A0A^XTc+oDELrfb@w_+R!oUu?BB{-%LGN@gn8+qy`LlY!qXDaY%c9cnF$xkUq;p*k$L'
    'Vt~wueue3G*o(F3W(~#Ha0E-<k;d*tQ!Xrv%UzK2>*mc!ChVJ}k~p8@la?J$ts%^>ax!~xZi!^aPACd9@v%yBoQown&fOr%an@oykps6fH)i=;'
    'Fky{Uj<g#oWuqwrCt$I)BSUlfnOSz{SqvwAjOjkQ%Vd$tC9+86>?~5t_ICNKPDUtt`l#&70(I4I5Ln#a*;fZQ#%s?#+cKetLDL$r^}HSGNHB6-'
    'HjO9NEqGz&3*m8Jj|k(MZZ(~km?;EtUuUko*)~V}$CRuSC-F;G?q>!9>+;#pV|{un3_`m$Sg9t|blNldhFKAoPlsR$6HDhrFyDHu#0b`ImTn+7'
    '$u3}4VER!A{A{zXn_=0*CY%R@7=F65g_Q+R?6EqQW3hB$n$2c1T1Mn`9)NW(CLh@sXa@SaRa!l>ir<{0F$$?%u>zOg!dnNPr^%2j+<dZuq{?GJ'
    '?^favrwbBOEJ#Ul1MfE5sg8UMZ|bb?&T?Tn2(eR_eMOOw@^0F$y0C~zhh~<Fx9sY+P;4bX!R7)lLC=&;o6E?oi}T-^OpTx2d_lVKrE|K%7(8_@'
    '6L~sgL*SX-(xF}T1-mmH&iRHI&T;JB9AWRGB|V0{E1f=bCiZSloB36b#B~NfTY$ZrNq32%Y|?s380geGMdUlHp6ZZl#E+<=^iO3BT~zM5eG42u'
    'lcYHd4nJOlcwCJ8po&7q!v(FFc?#_J<x|_0RxT0kObc8FuhfR&a6G*<N|=uqbRy=DD{3&G>RsDPp^Nk91?|dr+(dBwif-VE<P7i%ISnL<&pglM'
    'me(n8vl42FLx2%Q)oGKv381p7^i2Shb7qO$5(v*AHi#r^7|as}vote}<XOZ9l8|wkhFukY(t;DPC_YYu3%$|dxfL2Bn<zsmd?{R*{Pc!wkOxZ='
    'o^)ktX>9DgtGC4)bT+-=Mbwwcwqyqv>AkWXRMi;*^A1n~#Onh4$KIq~lfP!4G)wCGy`Vn`yCt#q9NNB(^?M_gK201wv=nD?Wzp`>2nU46iV7Zk'
    'mZvIVf^e~kP}{H74QJtW*dO&tq`et7yY8a&wMp&e4v^$U2yGyeh4i#yhpgN`PU!kv61x7i$s=9F)R0)K$SuP6&e<`lxnzuLPRFR`mc;j7h~`a4'
    '9+@l_2DRq+9vsV_Qap|lO>#CI_o60a?rgb+)5}dYsGZ{IZX(5DZegoQS&kvXb5ga*B_P6c7*(6IAi{GP`mhv4c+T2}xj00)XCG)c0|C#)K)}ge'
    '<O>4<&n?}Z+*<<f=3MOyn<dm*Y_pd;Qm%mHi8Bb%b5pXaRpk;`^sdrfE))3_uBGb-^j*UYnGUM^_wTRnKPh_$21b!^(nM65axUA4bNOyp*O%=}'
    'Zh6HvcL<USc_!kFAKZN`coiNN>@zFz@z{$sh@v@~O*rNgS$)~tC&)eY&g*wPc3$EIbnxT@u46K95Bd>IdO4S30JRQg)4Rqxc4aJf(z|W>Gg!`s'
    '5rEplQ)-E%MmdaLXCK2~oA*Nt{mQ$54va5$Jj7^1%PE$k(+|&UWsHrtnjt7$oc9NzHZaHEEHmy?et~XH=R-6I!%4onII#!w{>GwY8aGeR@dkM}'
    'zT|y~tAf)AXpl`>|Er9oAOs9_UcWdW55f|(VmNk}-KxTreGe+>0L`z*wbIGQ&`KhSa?Dqi1QwY)Spx$VmRgoV@Zb(RLl1EUa;*$ET*$WG2;5^0'
    'g;dONRKM+27py|2_3f;@flfmZUDKl?YOT-g1y3siBedb)JT+W2j65a{ik|$}wvi^gaRWo4co_ssJ&0Sk#E}T2DJ;?bNLsKMGh`XudnuX`5RXJ0'
    '5ojX@(#Z1K(HF{u6KlAq7co!Z70ukW%pXUI&aVV*8|97Ob(#er<+M#-pPAq&*&EM1^VLn6INd351jA0{?lTEWQIq7mOwmag&Saw%rVK7S4Z@qF'
    '@&Cf`^ndY~`ELmqKaPc;?xX9(!e8FKHx2)uj`IK%-B@rK&O5s*+xYTj`hbv(5`s_eAk*uBV(3gbowP*EpxpEa-MqVkI24uDoUp{O5vDvCUl97e'
    '@X5LI8KkG^(FgxJBa}lSjB|c$;G?_%s0OqiQ4RK36>Oxg*FciY>Nan$7QtYzBy{>@@RTZTxX$_%FJeM<5&8$#LDxopjG6hdwVot_LmMG%RAI3R'
    'cx8i5QDSz>{o288=uP`)W6=E~$(w`?CI*m$nOj+*7S>))-b$Tb)VoLFS<tyuh~xsNTqaBa<~4qy1N0b<2}J50N_EcY!-#K=Q|W{pw|i;C-*o#^'
    'jxnSu*Kgnti~4-`d<=ZS3;PJp>lv`^*o`D^dPWx-F)#C3fD?<yMT^SVtXjR6tTA&0R=p;5?CN866c(UhIZuz9=WlWBBRxYHt&=%C*N)fIffJmV'
    '4^l&Ep_r)PCeVLj#VI{VhjF9zcw}zD1)T(i)IGc?wXy=Ww_4}}+-gyt)m95Rb*m+EiCcc4%)?FOrmuvT?Qt;e5(FUPI>jk+M1eqiAN6O5(j*EY'
    '^3txMHwh@Mi-0)+3VX9~RQ9c=@}_08Xpp+-?H|zxa=8s{7^y@rdOm|#faHe%ULy!AI#$rPm0$Wt``-&#`{U_qG@eg8;VST(0pjb!B8)n~nb5oZ'
    '@{QZ5z<(QSYxFPt7yGwbscov?_<410bFKbMUghUOfH?xpLi4}$|IPot^v>!$ny$9{qg8|pnq1D#$D{g+@B3&0;sZyn3Qw&HlIImb0kSdWhJvsx'
    'LAyWb&o0Xt_$>miWki2b21v4^RRk$;h2ywErh~^aKpR7PR_^TI`x<7DZ|3TiaJ|;s>~8m}L2oOp_tq*K+x4xmv$3_d*{OH7w<^`nW>{Gd)~eO+'
    'R&Bdi->Pp0Th;B__Es2f<W_bM?>>0=Y>%s}1f2>L*lu@&c6+<MRsDb1d-LWtjx=v@{??~JsCNfsiy*5i>!i$nTb60JwsnOh_qapiBJz|l0s%Ha'
    '>abeheSc3LRaroRl06nTV)ym**hE!Uo=-makuoDnCDM{tMWwaTscv#s)J0;_G;Q)cV|AG;lh&r@+>CEg49dy45d~|iGBr%6MJAfE)JZKfrIV`8'
    'Q_Mow(v-CtyM*0u_@3B5_L`-QKvB^P4|RzVoG<a-^6kO-`ZX~^!RGb$c#AMl3;Wg8o;bKTvFV)OovcriyEcRJ$;B<NW+TN7A+pf>#dkKL^1jP|'
    '{~aq1#*g^iC7u{vv2674^!xGt-47??<;B_Ei#sRj-FN>!+57OHX;$rj_Z_7l!L&Ynmu9uakD=dp-<>2MjQ{6gOg$0o7_htO&TqoI1I%l*zH?I9'
    'xr2=Cy`!0n6AC|c6MpxX6Y5(8O~KwfJM_QtaR2VRzx)N?25p_Q<cd2l-<`S5SliT_v?Ke+3v7$s92(uP{m$aM6Sw?>F*ar)tudMCRyRki`xp(+'
    '4wZI-{sDg3-ihyo6m5II<x)s&k6|C%3>(QodcZKy&kK^u0Oe$auw7s<vQ(a5owwiH!?xf4XL@|jKWw}V3E_PvcpC(}H#@lCABhETfy`~l%h}of'
    '9ok!3YG|^5$Nv>~vEEbslGAF!^y&A@<NEC6M2uf0e0}~ZeU;GvjSt4(b2a)eJQ`V-;1yPJaZ=v}Hx!+o1Fz8Q$OGfu*@^8K5Vbh?7gj~Tu&-&i'
    'Z~qEsW511$$IsOchgP?&HNNwNBIDNVV7%h*$H#UMmt6ef>|pFy?4SS3K3f{ICI=YX>R{}=gr>cxchdH`51#G%y^|jQ>x`~0*uMR4+ialz2Z}k0'
    'ldMnz61ptz`RDx;yTF$pcC|Rey1QuowapT`iZ{R?GrqE?7!b7E&XHIu8<~C*5UGD;7jeS!oQ`C@KItF+UGZ_IPCl*O6pqQe!Ec+-$WHTITP?0{'
    '^Z727F<9f&?>h87ZGRgaTK8)`UtX?HK7GC`L3Wqt7EGcpj~y=Yy`G#T$F5yCTYsD586711FO#!q<E_lM__+7s&>qj4_+N@?(BkU#c)#0d*E8)e'
    'y&1XlZm&V;j>P|&eos0oaYFm@G9`$$_i+E#ruUz#M;K;8_!4XQP$vB=Pg`OsC2b1<F`?AA9pa!Y2j}Y(n@rf@p0{VV)!Vq0*Rc&Yv-{nmOV{6l'
    '|6o7btBaEe`SBTf1K3G9v@v!#D6<+UES=WoHzA8RVR#otQ#6Rs@7>McZ)XeD(Ue<GBC!u~?w8#{V~A>%#g>JK+#kM3kK<i;*!Dw}9ANj&2F6|C'
    'o`ju0=`i@6bhFRwIDWtRmSMwBvW<uN@y6lP&}}-pOa7W<)zwT~QN9+tBUin#Ay;h&n*Qvv>jln;MzT87>rT<PZO_r48@nEOqh0UrZM^5#4cUTq'
    '3_Q&M@HAfmJYPjivn^_>=gvZjEnhj`!I9O@TW6fR)`$y+$ldi5QW$!$bFnKpahHd(BeUIGKWerg65sYOc@7eR4sGo*PBZW`@pNS04;r2L-56t0'
    'Ka=$AKhaULA5y8I{ddWS+KWZ5#+x555eMT33#-rH9xA#|-PGN7T??O~BL`t^H?E#ryeEU@MclraMXx2z!J}5MkBdsGzSSR3_S}xUHg{&-u8>8~'
    'YeX&8I2b<xVVeUOZSpQ@tIdQNuk~wd1;DIrW9xl)VisSxCE@eUYmz2sccWEzld=z8N8JTMk3V|YKOzP()C38xP<KIdXO{lm+P!n0z2L+fMT}}-'
    'mxyei1V;33%w*6T@DYocL852w0BHx@@Ev~yMuAFZOl^_~%PRT5sJj^3?%%;X+t)Gu`(SKmv&kuue^&eFCZ8l;wy)f01FX`}$=H2tkHz~HpTvxk'
    'TUE%31b)d*^bA|ku{chofz-A28V~#1=!d|2T;$XCMv$i^Bw$J-<I^a#!m!)qTRv;eiSGc+h$X*ncTYMfdFk4{+}QrJZW_K>`!Vk4-b+w1bM|L;'
    '@RC@;`(f;R-#_Rj8#pKZ@@oBNc5(9Z)A-Wmj>cioWx#oTw%fLMc>q{7i}p$dZ(oZj7AGfRhFdeU8`KU#dt`UEdqgSQ{jd(Jv^z*UXI~`U&e<T5'
    'aVNyA*9Y!254_uXuj7L)usm?Q+pYij-~ar<`oG!YVov#3=bFEz#1a6nKVXdO^?ylLC0qJGR+j%;|92DrOeT{NKUyvpAAFF_5oJj@3bADCBYAOk'
    ')CEgMf36ST+`4n;&Pe?3umAej(Ih)4M|ZM=G#&l*ueYdT+QXUDi?yx!Nscs>$>!067vb^Yt<kN~pDtGX^*IM_k^0u1TX+5h{N=Ig-gIg+bgXCR'
    'D$VNYbe~=?Y$(*|JBwQ{L1Ql0e~F*RpB%oSSGW~3W5)`OZrhbn3TsQ|=|6{WZf`Zf{kbIF*^zUxxmYge)Qpx|*|k>HK~~$v5)<@p2?lO?p>1Bf'
    'y?scy>iGELaz(tu)EC>J1n0e{bH2XZcV%w9ar2X7sG=sCBFm`ZXySUg`^A0o?3(SD80e9k&1_9+-}Lm|cQ1S;tH11Hz&l->D-~#*`~|q<>XK|d'
    '?x(j)|L1?&x%}m1lt*8UTt|N%U5qx`-)UzXl~=$j-q9?<=z)l68|;C}8<u^@r)%2sJwCs9!}oW6_N!hkhrUV<6SMtMw~fy9KP9`mO|FQ|q<{aP'
    'qdfYkYd#|%yXcLjx_9yTyQnAneP?q!Hs<>wOxFG;O~>8^wq_R3b>kBsHr!w3`PF>SZrQ=;!!_S7aD(-0SHQ9NJy_Ha2X^=RUw3n}H8kDi4x+~b'
    'R}9>>yrl2_)DGft_Tvse4{jND`tIlsWP+iMvhZW~>Eh+=tT#D&faf0w=;(k){|kQ~k1T|Y{yh3HJh0<M+IRlV=MERpKQI0q9NTf|J3-eKFr#Un'
    '&R*-qKkU)pTdnxVNBrWu7k?RjIPcw=JK>&V#ryvA9bbMt-|Hq2KKSG4zfaXU?ePIq_wx5!0<We2X`1F`u|fZ<B+LFc{r`X0{`b_uf~CM-_HlqP'
    'q10=aPBxl&NEtmONO`8`S0Gkb>Z2`zG+TK0`omkdo`rf=)ScwO1U4t<8@^iT^?DT7agOUThupCv>sUlMJCwmbyk(P7Q_)<i?YMluuub|?UHb}='
    'uWjkuC(m9SSoZ+R^yOS6VunP@gIk#0!q+e!UA|wsRC9!J$mf53IC^l2p|p<$vl-}UJ=PODE=hji?V<XX4X34PP}*_bB^<ZGh9Plx$VqiTmQ)`>'
    'aCCJ+3nE=MaK+;FA)Sdcl}@MTiYi}Br$Nuh7nqP+#-_nF{O)`LT_<ump9f{PZ*rfs5qi~qaq%(y?fygNN}D&^KP3xp_<PxoylTf>znSX~-H-Uw'
    '^_B2hO4=tMudxK%7w$|c&%x4!w|joar)?IU@Ssc7>jFacw2LtDg~$A37l3kLYfO`7z*fMs_^$(xHnY<KDHL1>J_I-n=szNj(!Ss0Eq97A?eG8>'
    '<H?g3A%*2Y)f=DP@7B`?kDqTOth8TJ$~?a<v&DKaRppNl@BQ@LW(_hn36Kt+pzG1ZES8ftM=*Yxi;1h-A}>!U^!(P}Pyb2Z?v0nL*=yo8$F@p|'
    'MFI3@oPYe*^B4DiI-NfK+r8(f^bz*pS7O>P_Z)vZ_8VrYo~=gJUQYS}f@iZ{Pn|W<HXvUoi2dNAae-zFAMP{umpB|<*U~?jDox5R;GUVSZxcoi'
    'uV*35Ys@b$=7GXZt-$N%<@;|}*NqS!ZH&=dVb^H3avSF1$$^TU(tNgfJM}v`CD|eLVPAATxL2cb0GVlkD%*?oVDIu%AKd!9?~md1m;0wrUp#p7'
    'czXZgz2`elq_Z<nXFp=ikC&G}QZ!uq@F_*nT3vek$U8=b3CA8(HZwXVsFjbLX`fOO8t^WsA1-i<RQPtw(~FvRepPlfazhLQehyXh^f?X#v>Plc'
    '_`0<%9>p)OD5fF6_taH#w7$oEt-bw%^k6-Rwc0}4iOYp4^y`^TY)@tV=)V*!J*GU$LG|v)lP}jma~Zib9Q&ab>6MP6T`6$U)60n`rPRU1emQxU'
    '+M#G>ly=&!Y3$}>tAx=XZGD`LMGk1!qviTVQC=9k1iR9$9>Q*H0nYUc4&97wH<35LT|Q;(>WyPPx;LIh#T3WSpZxsnK91zs>C>s5&&Z_|YlAXu'
    'w*wg)&HQTp#-|Iob?dK@54`^qmjn}DdB2bra6D*i!;?S%fsD}~XL_z;C&KUXLF-^Y;4Wx$0-sp3<DPxBe;B+YJE!>V3E0C5breloPjT1%c2Snm'
    'b=mLxD-Jks4h(wXPSM>mhhAv+eUevKip+K3vwB<7)umjs*<XE5T9?Jv{cs0G*K@YMoG#x+nH{>i-6!tzI7dO8qL#8RU7lZzH$FRb)3gL{8&cXx'
    '9s2y$V(Vj%oc$8qx{1V2U4ccvaGTH8t(zQfG1ERzWPNz8x4R?a<h*U2a$GXyZhu^_+uytAt_SLCkN)L8p)z^ybyro){ch)DnF77C>7c^I-G6$O'
    'cl=jwkv2Gc>Rl<E*M6|#4J3hnxi=nA4+A`jHF-IXNoboe^}2}sy--+@@E_Ili|Oe`=PQjN*IT`xZKJxt!v*fhWt%<;*`|rO*olt(vPoWOO)1A4'
    'r3<$@b@>mS%wl_W)ALWFJh7mGTOv+0(D=Iky?`_Ivd=0SX!BJ(qNl<^WLj+w=+0cNK2ka*N-4aEPkaa#V!#=qVHONpN`?+S4fA85X+H%UWl8Y`'
    't(=GzmDKEBwNm5Y+g8wP-*(*xE@=9NskWKX-E_V5Wr+!8J0OP|VtXUt9s83~Q;WT*P=?J;YO`Oq)c~B_8Z!2WJVz5SZWeAQ?^sZ<s7Wi&UfVqI'
    'i$`tqBjAfj-#&e>1r2*H<{uCFILaarF62rZ%RY3!;yfAPFZ6{=;`tBDy2i4(6Ro`@y-OSq?)D*(_Hf<UAN;W$**xi9Mkj&!lCK2V#k(WRh=(`+'
    'zB_e5ix)nQJXRPQ;eHS$Y_~40HpS;(x|n||j&kI4lO6r?__6bhxH}3$hppO$Zy!JYh4ht|OYhU@&AaOzgVL>Wnvj0ZSSvoX5ok7Vj<pi3TLr|5'
    '{s8rLrMOa-H1R@doAj<heeXdc+fZWogMz3))!|00%AC@F=0rX}+1HS!)|YMi`la-_?sPgl=skNp_B{L<_4PmNfK_oociRmh!@=mq&(V}xaA^gc'
    '(Sv6eRax#^E-y40mM=NEB930K_{EO%e&(pQo02makBT%WN|olUp7=){p!%iWB_Os5*P}&`vb%t}78b&;tUsO$%Kc;Q;e)gI-s*t76xT1v<#b0D'
    '5XBPzB2ouFix};$6oU=Xn$KO-xb-}?O89_>+l0fr_8<s_{#bj?2?y3qfJU$e_{dyOn?$2Jn0F>5&tD7=bP7Q23Z*$@NQ$c~UmKXJuWNZpbS~sL'
    'ow)KLqkzub`8qW-mJc48Qah|e=vx%a+rwfDO6@Zb!I!Rj9wBy{iQ|!-^MfDkRib1(0nHIXwcN5R-Qw=pnq)J-QC+;|sBP<<6J4dixX9l}oTc+('
    'Ss#v0vEmiws&Lc7dw}mOs2#o96Fpr?r!{gQbkO<zk&3T{@80TM1pAxlVtaK(oaKblz)Uq+S1HHU(pc2JzLF$ro2xk_e7$}H3I_mQXkX;xpe=6H'
    'g=yG&ZGqCp=SMF*6xzU4a-HiaFy(%r#3qiJ=n<9U9=kxb=80u_>wJc7Zi&4>lN{!wZv6uw*}%%!ywXmgW@k?d58u4o1bb6wavB(~(9)H*bIKKs'
    '%SM0d^R;^0CYov=baR-`-<=PP0eO@ty$;zYTRmSl(H6N}(`JY-?lBevoGm`vU@1w5w1fBC|J>El-18-t4@Q3mp?R;Y?!#5Xc7M&6ulxECAH3VC'
    'V-H<n?#tEQhuB-%^?T-e#s9Y@wy#eZvA$Fk4iI(!^ug0pYte=r96x{l;)f?czc?6?5EqGt<lnW7!m+KeNr@F>na{@$yTSUbwSVQn(t4Fqk<|lB'
    'MQKGNmjKUk{#IXo_}|OfH9HWbNc^Wa?8`}!B+2pFMxoQFMUc91;EQRLLW~mX2PH&TvAwNc9%M0^gyl`U<{=H9fBR{C`sn`s>3ys4C6GCN^yuE-'
    'Paj85qQ8FmrThEo)AsL&Ke#_XyIMt2(eMBJ-_!j)*TKKT*Z&Tm{@Z`Izc>B&|FJ*r$;(lGSDPrH6R>P0`gqc|LNfi;z3x@{vd7=A8Xr{TbTCUR'
    'IKEkHGDt^X$7h1axBB?Y-Mx(y3ADto;ZGx5e5fskO1_8ZREd=kB``h^@`&3ab>sDj>Y%$ym_N-fooGc7DiDV>zx3TjW;I~Hz&~+Y>C?t}%&qGj'
    '5olYhRll>jAl?A)5u>rRvDvQ9SN|##>V4;s?g9R=^(5cLu70E^eA|nT`02rmDdCYg$qGHYfBN7dmH+$U!L!r*FP=PmaQb}XB-|ElnJf0UyL$5Y'
    '#|J-6A3u5i&*v{rAK7lk>3CPWpB_H>{@z2EOniN)^t;8?e7=3N!a<7JCEe}m%n=Jtu_EeiIKl&OwBK)Q2UBe0D2Q_5YhMTs=kolAzmM0>>(n}<'
    'LbQEtLmnxy`MD?8hh14)JENCwN7P$ea_;H^o22=<wXE6>=yFY@v?3>^A^;n_!ED*$LM)qHIfwrVPRy29)s}2cNLM!@Vr74QuoQeVU0qo4c(NRa'
    'WuFC&`G*T^j8?V?-P|Txvw5uglsmjU^hp)}{O3<}UytpfyM!*Cb|_S$vi$@YYgP1@%eQ(#Jd#(T#iMQM4mawx{b}^f{k8|<ZwJnW7%DQ5;E`ov'
    '>5;b=L1-eTeqjTph~W*TLk_p_C^!-I)~YHt>_jcY<VK7uXVL7#j9vaOtBM;cG#3hM&DK+k2#6!NT-vWT(^<GpAAbspC!#K&ZN_!m!`A1Ad_ASC'
    'hadcK1JzwSDH7*z5mQ?k+s^yA&C_Eyem`6yN_%_VkpbQx@r#j{$)g@Um#uBdYb#q>=Xz`JbTRm3karqf;-<TsV{-@Nf*X<%VkL4Q-)^`0drI>;'
    '<pLXhMJdD&TsFT*U=A5II3ye2%<kZL|BpQUZmN9<AkpDsZ+tZFr#0d4V>D~~p!E6UuLlBjK_2w(>?!-$@!5`J-`J1qj-K{2r~}(Crj*8ZVe4J`'
    'I^tWW{|sUakrQ{Y7;h=)u$}N?(`eed^48@7a+<}hojS=1eR>@qzD?vqDv{9W07%ybc$@@p1UhUgSmw)-+RRML_r5A2O*2l)U{g^q-&1@b)rRTP'
    'c0|~wMT0IEv|VDHuESnt^7k9Q3tiHj*=xv2q;NVJ?b$&)Wvu<_tHkYZ>8RT=e0)o{M_63@<7H@eMl5SP+BUy!RbOGhBM|T#P7*kOO?<@61)U$+'
    'iAA8$de#jM-HjaYvpcz$`0K2n>&~BF7j0rs;&@~?dmQ?mhNu4bHFa#;!pa+4JPu`S<Gude=f1ClFn<BO!9KbN9<25pa8qu)5FpjR5aFrIDc=6N'
    'TFn0C%v}CK2hoF<ZL{-RUFjRwv;RBv>wkWs*KYDruYVeydM#0ax3#`JYC&QI@Z^-Yf%F-c>gsc<HZ85D=5Rz9W__XO6rkh#bC=$yV2`i?#m`b!'
    'msZ`^R`1sHh$dK-;DTEZG1uW^%jUY2tHcRk2?`xsS~ZfS(B~MpI3?-GyV`8xnI0YKZ6e*Lt@8?MRTXs=T_$7Ineldo053PbXXU#UjW^6E(J|Sm'
    'FS0ZMv4?}&u$0Bk35?w=Ej0H&?*QeR4%!8UZjAGt+3Lo<{=!u3{s#D?e9=9C-1xTL<}Y-zd0Tc*Apm8h3)CA>9nR{&mwk1SlXPTvH^~+ODiJkO'
    '#jfMi+gb;Ph&Hk}q01=&g$p_BD$gAc6d<Cc1o5}$kYQ%jfxUX&W?D*i(nm4Zb`&iMkLQ<U{Q9&_DLCaIBb8x`mt8rS01NiNpSuDP_P31}vln(R'
    'g16D6H@*G9{SRfb9{W6J|C5p9!7z3N_`62_ys>57={e}Lhel~5>ENc8MA~$L8z&glYpu$6>TNmGVBXmae{`&pRZqM60|3bJ(cySRJkp4COMtb>'
    '1Iv<FnbOvswGK~5mQArV@v0|bx>Q|uh@RX&c`)7&S>D`Rz=MB>A!Gj4vzU_yN8`9V>l1{BhrUo$c>eNulJ&GE;YFWpZtld|14u{yHXN^BUbBm_'
    'w#{`zoKNozzxMMm`UXUFMnV+~az0<LFNW5d$(=cT8+$=gaIC-6um)R?OA5Qe7*IvN<;BNCrS%2<vFB{~J*nfY)pE9db%z%5x}nd$;6O4)$L-p)'
    '{N!X8Cq48$GklJgfl3cO&T15V4LT3RUI(qq!EiU85!+_AyLq-u_pZ9g?FQc2o(nLTHDvM)AI~l6V0*_dzG_li-1-HHp0-%EyK5I4)240jE!}FB'
    'gGa~3e)~S+T_YKN+y5k*RgZ@VEAk-fl9}3k-97Lmjd-iHJZ)7-*42x&i%*|CfAEXV^G*B>P4sZ|#6_^o);3+{2WNqIMj)%`@x85=gs{{K&nMvO'
    'fZuHDFK}iTPL25*%o;xzxMH4-bex=4hu-Jv14+^JpVsSY{OZ*!tKS-JIK>cUx9ObeBMdV>Cat@Ffc9OIstq=+&v`kW{`vxpt=$^`=lB?7ru#3`'
    'GwUtrT;L*qWS2gw{q66-p0GZ0*7qY+1MbxE&W6-ppuVxo0qE6=5_X&Mo6F0K_3@F-7eH2|!`DFCSLCXAesn&QtL1uWE_;EM*xkd|zaD*Z-F+VW'
    'Ot;QZeKMkn9ol(yJ~Df@exjeO8ArWMLN|x5+i@hXI50iwts%0{o$Yj8vQvqil?J{gZ{BAwf;x}%-UD7yR_E<kbY(rnoU@JEYojZCSw=rZa60wf'
    'p12;4$Z0)VGF>gV>K!ZapQBqzx8n$4wLZN3a5;|FM9-$3JH>SPPKN{NIbOI35as9Uy-Q9;?WlvuI_OHBeWGpt1(Supu1|g5WLIv(WymBep|f0A'
    'ZRtweoweeg_YAqPTyW&baM&qiDcH{`059M0%aO}V<CoIru(fJ5n@VWNmL2F_yY24tXpg@{czUTv_g;=)J2S3zAhN1)Tjm(ZaeQF4oFPtP$a`_$'
    'TQq~-oQ#_7-tK^ttqF%U?wD`qbLe-<`)WtlpEkH63>g`Pap++MUv`-6eE9~9x&u39oBDm~je70I5K_6Y5lGI$(6`sxJ~u6{UpTbiHvWz@v|oUQ'
    '-@ROqUDVr_fpBI^G81j-|E+N%y4D_<fb!@SPWZ@rAB<buHd+&ZL2lBHy&%X4Pra=pdPAAb+*@>KKZ=-q`(kx&A8!uOcD3y?>plyMu*ycVoduw^'
    '$kdiqTT_nzV0xs*hjZ{oMcdJKtrq*(k56_C+q+{Hrf%uhqWUp{qK%{7Qg+8vBMVT-OHZ?Knyb0vAR~)WxLd^Lxu6WE<v;F>W<8GbxTP{3e=%`h'
    'pRKK+Yh!;OJ##Q+Z3v1gc;VllTi$@%F!Gmu^R)xU4qPbKX?-MT&Zp(b{`>IaBUR-C0NbCmWgduo!}~UlX`wkp)0s@JE<zyFWJ0RA`P{k6WaFr3'
    'mlVLFm+5>a+$+7X_14>j0ozYQNlrHqE|ter?g1&F+on<Ihqtfx8?7!cXYR@R?d-y4rC&$Cfz!OB{QBcU`de+8VBD}3ceL4=bMBM#!}Ro*r(ijz'
    '_n$m^`rzRy{rAzm$3HwjdGfex^Zf4*o=$<vU)+26aQf`$ho}A5_ovn+!Pd67e|2wu`DNhUkEbv0|Ly*h$1hGFznFf1@A)Yi&`;Xp7}j#MDF#xB'
    ')5xv&^zps#G28z-{hmTQ9)17tpVMcj5AXeQ`ookwSx=t)_~Ur^llxD9p8ohWEvHYOJ`c@&G3e>{KR<Z*!;}{J=)wJGPyT-T?D6SC*UAg~u?x{A'
    'KD3CR9EbwYwcPO`)roW0%RAm$Zh9-AZ+{bA+mnmnFQ8_(Mefm=gX*r3G`gMA!l%|xxA!Du(pV3?BZs^0@#Y;k+p)&5hKXN8v{WB)x)GJxpS#<p'
    '5Ugl7b~s4W*-mT<DZ&v^qN^|rkgO#>A6|~FloL}PLfU)7oMdos^x-tU&(3-bPMijdI!v)K_pC5SJmiLxaq#f<i;A{{f_S=QSliZI+XnC-(Eip!'
    '`a9UY!*rr#b*&pUos_kFwH?Mb6e7iu1n373v)&M9+$V=t_qazc6zx6?4eZlCw7Rd}xG#NQ)<ehlb!U`Xx0g>#L3UCv!$s<%m#t>v!@=m&pa1MN'
    'O=H)d1mgD7&ylvvv(0@l8=svGtS#0mAN2HL1McgAW_jgU&(J|uvd+&n(AM?EX!HE&2HbyyYfizX>sX|13)WNll+{u2kSC;ih@QrAjP<snF+Nqa'
    '?%k$?{!Tj0&gOn?bxG-Vqga@0lz)9IqdO!ES&=GXGjcg76ddqOJ5$46+d<GHI8M`VBk>VuJA<-%N52XJNRT|X%Q<fgz3S~?lvJcRY5_j@_+Jw-'
    'Mea!>%RugMf(nOfb90_JGC&_$lpLINJ7m1!4PsXt`|H|NuU+}~hY$bpD5zmJv<iA?UY&;({=9R?U&!<6#su|mnO@)D*iNwPvA?rnm`w!;v4F&)'
    'I>)O|oeg~B*m@g04(IrJ!`r3xKBJABEY90M`6@X5ISy)L)iHJc($ae!xLZjs*nmCI0IQg`E!q`#+UeexE_Ciu+;U@YECH9@_2{!vv?s2`RM?6@'
    'll4kk=yOlUQ6HPjbx(J#_RzWb^jE#vQBVl<pW8(}rAOk*gn%QQ(2f;GI&9ZlU_n$+EJS9?NEtHh<MLG+ZttrWE4gjZ?QhX@ThG7&RrgO}_FnsO'
    '<Z>Q$Yxt7(TXr<6y%O!PTeQYuvi=3I0A<~Gtvc^u7fW{Vdnr}xI_quyF^2MS5Pht*6_yZMZ6J>jozOMw@Q6OYxQJf#a10gMwX|p2q7I8xtjp+>'
    'NTp3n;+H=SKA&Wt-Sm4OaILi<HvUAitra;x*&{s^@-uDWCtnR|KL2dhYrCk^@fV0!zv}jsqMTMUSH8RN=zi=3FUrf{(icU$Lb7(ZEqz>bJleoo'
    '?uH+XHt+B82FB=GbYlE;8%Mh~`%z|-p&NYsHMg8%a=>nRt%eoCcXnmVsC`Xy@~PX8ld!01n7dm^_afN9!@cR4#XU+E?YW<WzzYo-Mx65F%e*)k'
    'T*^V`#4oT>k=Q}#frhhb0E8Ux$DP0Q{~hAE38vBSwuHK!=OrYecQmVe>~bq?=AGFPXnUgbwwyC8Vz#rG^wwwP%<UOpn}p{2$zPKpCt8PX^;IWe'
    'TXyH*3M^l&qi@@$Drys^5kMlGn>CT|X!f%AYu6FmM!N>O1MzA+dGv$jXCD3NH4QZ3{j=U7_CEWqM;Y(x&hw0EFTm1?F0N9GC82I(<EK6I(!?E+'
    'OYQEhO0oFnrN#Yid-}=O_%(D+4SNHy6fGgRu94t+)^l12q<5Ez+FI6Pfo;<t+WQBdw5d-l$+5J(?+ETdn@(~-t>B!ky?oLep>+iaL-otBhLsI@'
    'u|_WP;p4zreW7;p02_a?A33saIu?BH-K*f~<+7ih{4U4UP9;xz5dyt9(F$O69;b}KT^Jz#u~>RQec%Sn_cz1>&t=<*e)8&z0SZYtnMy`)F0W%6'
    ')YO0MVmZip%T77^!rp`6ZMUVvkCYt)+|CZoiH(ij)D9f))*F0o7airx`3jui6oChBt2RqUhQ0N#`;J$^uXfLgIbGH}Y^%Rb?o9x6_bZ4mHdDjY'
    'OAPLnkK76hOX}RsFaD&j>x}!^)@DXn=hN6GF$07D)dkaR!0X!Tywby_JWPD#ZU~6N^ZYP5tfR<Sa-ZyW0ncq6i{m!<d2)U^iR=<_EEl!<)oP0='
    ';lOmR-)Zv3B+Xy=q6M(`E5ZsZ`lKplmgY^VwGb-JGLxq&XL-u%suluk%(9g6G#8@Qb;)&+@S@3ulBUSDsxl{vtaMyd*x7Mu)@36LZStzgWtwDJ'
    'krfrkCg&zebf#HT7c#4KYPc>{DJw3^JV`2if^U+7wVhjs9@>%%-Sw`MCaFzXG)!bkX3C~Xnj%k&!lXr0WX7<xRz{U|DzsopRb@@4b&>I+GMP5@'
    '_;V|)T7ONK>n!rS+O2!?U_(WoTzuB}%<`=0$C5vvwrOnhC%7b=q2-3GNMCG+_jq`AsxtqhkLSn`HQx6ve6;&STrCusFz5a0ayPYNp~PihPOKr*'
    '((&y@h0)u^S+`D$N*+=6yv6JG4mtT~Q*$<?-P`PJ_nt=d(d8QRq_ZcZZ(j6mt)Ja;uh%B0k=Lv3P4A}cc5K7`v4u^(w7mm{khVyT{l*D2tyk_o'
    '8G7k1I=AkqNs!h<V7%p}dKlrc^J()F58hKdvNFY8M>#vDA(4k0;N|$;awhe=<%c#?WBZ^ji)-QFd&&Z0cbF#b(MTJ(7L738h#|K?aNuWs5Gg+*'
    '{2LrsTbrAgrhR6Fy^uBFAmpPuXfty-c`d|c`m6^X7a#dehYj|fl3Ch+<T^iWM-RicgN1(gV@JU7Fou9zj^8mM2kYK)4&uGg?<1(kd|Yj8`ef@a'
    'ST8?k!@;+$3FoOdT=+Cueug7*%vd-*9JdKe`hHrw+!n*E$cfOOmiK4;;lcB#_g>up8yS7SoY<A%W#|i+U*KZ3XQdQ>Qu~&r-5&>Yp`9<-^ERcL'
    'S=u!njc$M8*4^IUku%SI5ZJIbb>9BCw@5eiK{xWw{JfrDanqT)-d`=kXYpu<&Ou>s=!-pnY7`d$inf~1#QwfZ!r-Q|aR#@0U;9vz+sC8Z@7Qs4'
    '(?-3kYp><@-MvfU{Y2BOBMTtNUSXO-dxP}q?#Ly!vP-9atQ!z$#cev^-P6fKPO?sZ>rHSIUEK8>>Vq&h(gwK<KL;+~P%II)iH@RcPYs9WyRnrm'
    '=>;3NV;wdUeRngDxr0O8ZLjAg;NHyEH;zHbdc9bPKN#JPk{th*e!85<p`#s3aI0>Wa1r_oNp5!FzV?&&LRlZ|7>UREB-<~rZEdFgKDs8Mplud('
    'Zy}kq-JF*3CmzUa+1kugC-ihfa1V%@g0TDAp*Z-!tBP8GM2C$jRZ(A#7-pzqm4&lp`V8X-qiJyYI<U2;4m^IKH#Xx*uWqRowESFgYHh~jX{g=h'
    '6~x!K>&4#&;^2p+%_*{uD%gAG-o+I<T@+HEB7~xG<jFN$8nsQU-j*_@;hJr43*S{sqIy-cy4rR<6ML{Rv}^V1XItsZ?(Iug_RFf?L!+>wQ1orI'
    'QR^#i8*lQlEuR(|`x<Inje`b*)^ttVu)p1GLiAS)3|(U`W>(Yhs+Za{S2LDjEB`!VE|Qq4nbPw!()|0~n)==9&)>hKLa_%f%SQjbEqnWUL&Cne'
    'np&?%LYo2XuaT@b+(3VijQv}rYm0BmbQz0LJ4CWik%T->R0$!DDJ7~VgP@pmAs{#^&Wj=~NGvyrl&O}yWU4S3Pn*<atSoX<2}q#hLBV?5laxc+'
    '&aV`)zo~NIj}+HG?-s!9Aq<PLPk$b@fb>O4wiP})A=XNQtqka-o-atX{ZF<gaqcD3|LghDf8Bfjs7>8@jU;-5GH7qAL1^BR(L5S-{f%9E8h;fJ'
    'y6ZI!w`g1W*pr^N(8A_~hqih@_yJGbD|qY<7%zfL?C~fIn8k}paoXM3w9~JS5N+_z+`K(Q!n)n{u}u&&?p(P0&RQeK3|`tud|#C%goTme_^!47'
    'w;}v1ZBtp>;*N2y2+D><^7ZSri#r;~gVW}IJn`+12GiV*JM}5?Tv6k}^u6!W{s&*1W!$1}%KLxPW5?p0&M`;U=Ku5W#<6PKr&u27X){e<+~wxG'
    'sLM7^rmszDchEYv4Cf9WP@wc<!{SbUYewPSo@3hqUtdgz=p13TQV55c4#a|mGn?B)jIAalJP9h1%_cNpXCrBq<zbUw2DEV&X;Z>_q8)T~i25he'
    '!scMw=N)I!I$_$TFUSA7g7h}+LT<wj_Mh8|92AV1CX+Q?VKuT122>Qb%uwJ+!YKCPcrZiN)@j<dPGc5-Jut`qbBgE-pz9{F;}riia><f=;Tb*#'
    'XXMj$9<%`*KEY?3Jl!VtJdCFAVs~Z>v+SRbjT$uIPp06Z_j=pmff6+AW#C-eI0efXI`{YokAHk}?O_fvSR1KIgRN*sCeyPnFX+Yx8!@9jw&p9G'
    'g{YLz^xWAh0~2O0rlF({F+o(-$MVl}{=xf(M2-8_jsbqBugLFCVUYEC9?qE(Zf~6RE(WMegSnk3(;lu~P@>?@vv%#S<bD+1bI>}Xe{BG7TM)`R'
    'x}JQ=Pxscat*`ZEnnamxybU3|+HaFzbqL!3wJkrlk%c~+HDu0Ex1i(FrUxNkwEf#kbT)@VzcSWdG^Wi1=^D28y3-f=(L#l47kn0Yi+&uy;c~n!'
    ')^5z&iMMSn?v1x3lnsPZMziUgj|t50xm7@H2d^0Exdu=V4^2CTPP~Hfn%r1=?Y%a~9mc?v-SH6Xx9Sd1?XstTl`TDVa^U;j-}1F}wW6$DIQ*Ec'
    'y1%{m&480uRIzK9r`9fZG%A@Dv!w2+A1$h)w&&&^W6pT8{pWP%-K}4oZ+N|LxsV43<}L@D&91!;F1!opr|oOp+Uf!0ZePy^oD|v$Nui@_dx}mV'
    '>dXe(xdiaOeLF7~IT5qXJ__3Fy7_!Q4fWvtH!c;B=VDvH>)nBqO`b*V_vSX#GI|}6vH=tphMf4RPMj{b#bWm1fM42n9kmEt+GzK1ye{<e?AoyR'
    '-xm75(ewTn1B2sO;I9c0-xy#pM)=zJ=09|hF9xN5DT@BU)pVG(wKCmahdja?Xte9iVxPFaUHt1B3>25r`^`8gE~giPl`R($<Dk%PZx_aid%6x6'
    '{WiSN+nqjUxZi5lzla;`Y651s4lDGA8@lYh2G?Krw&}zU-@onl(e|lqRWpp!kz20;pIhx@^y%|9+e{w5uAWp!m16XCFzQ1~e>EHVi@bidrR`x#'
    '_(3+1BPs>(HRA2>tX$UnG-esLD~QJYm)(R74^In#{a!7|eVHP1TVIi4o@p9GWG?o{+g-J^Kf_M%+)i=Baj$=6+A&(<))W3u`k^8%Sw3kK5r<+K'
    'j_;gy^;p!vhR1OB5+{9T-3iD~?iyhCLr$WP1E|OS1@9IsvmJy#f-3*Z9u~1iDGp!VaJcX?$4XDXkjAG=7Zha)`pBus$JJ5KgdGY-9euJLkyXn^'
    'ygzsmW%_9o0?`+f-7Png>I~%Li6?(Biu~DEZGp?Nt!^H4snJgEzdTOMv;BShMQ;)Io6T7T>F)iyb2s)Fg^&r=>+01+2nNvBlG#TaeG|^MUTEVU'
    'Zo1&~)l9!rLA80+gYVMl>-A}!#N11m5p@`rxOHDz<;2!vdo$@6NcXtk_D`cH3o5chD6UPrK6yCui43nO{qVD^_2tp}QhBW|r{q@a09K*$_2EXh'
    ')>kU_VX?WSSGHmh6(x^rD2b0&W%Mo&H?`lHC0ad$zI{My&!exW4eq1gNgZ72Ya8KUW9(XQ7T4?kpmwE;^>mFq4-(FX)GK&l_r=EmcrTD}uKjLf'
    '0&X+0c&&>woRbA^e>*c^driX5I&%l60UWp&l0&!&wur^D<IeoyQFr*cKFJP7Ud1%9O@pfsa7WAk@|JkbK)WUC%GRl}=KUxM%x}2dyBw~gad)pG'
    'b|Ku>uf%?eLxNh2#{#S595nhY5d}Vb*QCBaWIzpj-3-+g<N;~3EOv8^C+Tyb4K>?P(ro!=lBDasm4a)RuSYhGJUdBIl(X6v9BwPcZ8$l3=S@rO'
    '<8X!Ld+TYA4QrFjx^htDMbMD}%H?pmys}x(IunFPM*&U8Mb7OP=SyYh81L>n7}wgm0Ug(ROwJs=`gpb$;l$P=^dEMuKU<7U*2H0Jrc>m*r%q8H'
    '9u62R+XfrN;&|h#?4T)|&9)4j1Mb_`8eF3Xt+TbKi+CF@U2HEU&w<tDw0q+H@TdJY>x&uS&X~?MKk{_neCP_e;b~K5E`HMn^wL53>^S3gPkpMH'
    '<JMT|z~P-M{PSyEm^83-ZAcY;)q<7P0JucOcu#9O=NC4d)cebN<l<hg!-;>s)k~?m6LEC8TE9YVI>;0`Y~s$~x`E;etwU54{@#`i2ddQX4`#Hb'
    'o;Em^<@+EyZ1}plu=CbrXMfT?Kil6FKjISN|G4+;!M(>Xrq53A{qWCm{8=c_&=~=v+TmO2(ez^P)86XkpvM(;^>U}1m3zCxHR;R+S+Bxod~V&q'
    '?uROlA;~`_S#7<+UfZCfDDZEHpBfKToxm6`le3q@j$UUzc7Odc-Pt5kbO!xzTX3>VpxnxVF3VaAnjvQPID8x<%&Ap15Q?M!MnU7Yx%;!j$<u*k'
    '`mqxH<#x9Hv)*U}-}jYRJ9-GXGUBGm<s;&)sS!wBcjoT6qWCp;U>29@X{fr{Vymk_oNQ+Du&C}lY>xT1v+g_VvO%P-E7s)s-Jy&K*Vw80;c9*Y'
    '!!NrGq2cxQ%H!*mO}nM@{m-8_N}P><V}<99ocwLgXJ0>LeQ<rb<i20>+9d?QAC9`%M3_Cx{qZhy{szt7VNegA1=EcpkjJ~rjc*o(jQD{z_8<`R'
    'flLg;68b^Uk9wf>Zg1<2Et|Sp(qGX<>-Zh}f(D}U@tO{{8;5Q@0-U-U4(5Vo(+hb%y<B$wogH9=#xz?xn5ipo!o_Uv*q@Ddp1fO4NN_3$Ou=?*'
    '2t<~1R#R6UciM^gyFc<<L-@cIF0h4jx7&q6EVjUgT_$}(OSPts^%q#8-^X$-mX6zXo@PhAuoJIx^m#ekYO|2Ed^OrmA!ZkLFj`!lliLTMk9;Yb'
    'gV9DUTC$0aqGGh8Pz(2P)Mc9CtIL_8WX8Ti3}0zqEs9FCP|%gDk9&8cm{}Kwx$eq65XOA)%-a}dYnuk)^A_LZw2cApjQa&Tos-<fl}}2v=}qCP'
    'ANCxMEpIpIiM-L*pmN`~abbK1zW@Ef;)&#yNKStA#k;sLtc~1vmhIQ}@56gO-1(_}v!}FkO97HTnNHPG0>MOI9&)9+(hOv6^NCYEu(o=Ej~Dvn'
    'X<PPf$H#6dzxV6lzxO*o5BbAer@i0V56*1jInL;lgAt*|lkt-aSJePFfiB#~iQ}^XvQ7m<l6}_s#oqM=L0zjidKJ_?I|f&)kF=1ntsg~aHnzFE'
    'r<Yj3ILzT6A?nCoUv;z&X{!l&lXu11%Jo!9V8lm2TMJvkBq;Wz^`LAWa}L@(=w63TSGUCit$)UA;uHO}UBU=@W+8I_R(yf52GTqoe-Yw+!zoSB'
    'dCSJGo3V)S!?S-*pZ)x}6%nFtHpB7VR9sM-k&DZDJALo|`jt9Ci!_Km(jPA8GjVwOVRrfa5`>E~Z><TP$hXIl*W7br+U85J*m&F3I11`r2bGpt'
    'x;x_UmNPZFPr=F_6I=9TgO9-@MOxNAJGkkSdr0HC3gO$#m3z6NEVL6makD?vlx}|N*SNRW?NQ)H55fE`fHTJ%@|)e)LqnbI(Z3FD^3tU>J=s?e'
    'pWOfZ^z@g}zvJJJzu)rAjeMm02ma{Z;|D*UK7TQM;<s)A)}~XNE_G@p<>{2r<8<oH2NqZE-};aL@B9PqUoBVurXOuGa(~Pi3H~Yb-2NB;^!}SB'
    'MN(vGc;=p`tg5pA7$rBc0asK|3B&)#|3CksB4M|kG(`cagVGd%10<>22exhOp-P|dni-j^qLw1BwJ7VlNt7z<#x%Lf>a4D%$+D{MeclxXp)RVd'
    'N(>i?YD)YsZ;D2+qRi_g%`=wB1TQn*NYT{FBz2_=qY9acOlG;Ple*aI+KS2OIjdRPB&=q(!LT>3n*cA1yh>zKS4<}rQvgA!O_~Z(%7)dAE;Chf'
    'H;{KuZpE|EpH(+gcWGJ`_^2+VLdT_Uq)980Ck496vs_i07g;SE*PVBWr4L^WI7d70+0HlFhY%Uc3GGJ?)4$se97;G2`cVfrYpTx^T<=#)btOT8'
    '-IRKs#WXP+P8GlF_AbW`XQ|;yp5$3AWZ7Wf3Od5902?A|o#K6J{9?N_i1cyBG?PMPy3RzI6?K`FWtx_<=Ayy@l(o*&Od3D*aH_!Dq14^&U3pz0'
    '3DRImTA7L!vMCr#Yoj>lIX7i3Yn2O=vWAtssw>P{6^g4m0aUY0Bw5A)1({pH&S9kV!_>TxwX7PQn98s$uQOJeQk6NwZQylQuvD<7ZqgF}a3wXS'
    'g|}%^W6#R8x_+4OXGl^?DJ|_W=)BBLZZJ`vsJh9@T$Cm$SdE1Vz+l>>Tw`@vk!W5eb)DiyF`YAA0dyLwG-oU3-EIS4hAjkku%v35L};C8Oj+lO'
    'vm!}wAyuuaiq)dzC2tx<b4v4ytI}{yuvs=~UI^u3;S2U{duCkH{cVKeLNuaIQ)Wb|M2e#-Dh+-()lJTdx=3(a(<TSTsLNa#9B|FKhq}<OS8H2^'
    '9(uR7Ci{Na_Zdx!G~dyp3#zA{Y@;u#;qBhl72Ox38pcYa62KR4Dyam}rmkfo(h67z08`i8<V7KxQfJ!WO4M2=P3~6oWh0!$%{&)cR;)@=C3unR'
    'CTVn<8J;IPH-#|_cNMT8s<LV7RAx0#fUv4cHQ4M_yRrKEChAwQA_oBEjmmK{wNOTzGBXW^!wsqxF98+_FdxCc)KvyV%v7Gr)QAF&UB8>*P|*OT'
    '$at3Es(`d$V|c2HD$}%#9LPZm4qAhKR7RwkXevxy0|8Y@QppsfxjS?_)~r#lILxfD5YJV?Ib+;_F~LQtDpQxzQXVcEmI6yKT^lS?rg)iGDZne>'
    '>sYHty)wl_Qi%rGf~RTGG!5p4dyon2x4|K2ps2DaQj-Z&WL2R>m8#nCvQ9u$SdV&br0b{t<0@d*$*0Xmdjy&Cs5}3ozHff7U+4Y#`@4H{yAR<*'
    'PNnv~V%q@I0AVlzjpkWt=-%V5Vh;@NzX5sR2H2w3Dy#FVDU-Co9ViN!0)==)f35SXg+RlxpJ|nXMBwBS#t8BR17>J=rdU!N0A49Ha4|500%|H#'
    'mUCu+mYSx|GB^`n*LhmxWdjn_Bx%M|(1)}JSD+ePRHK2A6l)X*Z02>YREkxaBmuZ`#iRk*^60HsU?_${Zqic7M5-z;1^xmV5Lt#z$^h7!0XD%S'
    '<XmSNZV^i|TrZF(@JM-4<6d|KFj$F<`j<&j<t$Bf3FL`eja{x~o}dp5SAgEttSqXUHv;g^GVETFG?mbzu1aZ&uK!^Fh^m-T|9^T6_`Lh`YJT_m'
    '-|nTa7Jqs{oP@0%c>no7tY-GmKBWo*^Q-fPQ^17+(AHt+)$O~lZjXSQ_!U(h9nEJh&e&xMM_>O<X_2^Z*M0hWwY<6*EseFOFCe;MAes&Y=Qqo%'
    'xpJA5oVF0s@>(B`FprOry61CxceQqvey#qWoRt>#<1jLH_w%!dR?w#AdS+`KS@*k<eF3@Ir5>_HhFzd+_<FLw{5aQpZ3@aT@8!x@R$Q*$Qw{6&'
    'oN~oTpjF&3z1pWyX)A-e(huGxBf*A|%MHo#IOcSHb>61bT`tJ4<?6%X=qauF`5Qf-+Yb1Ay$mZp#sH(epY-K~Qd|FxMxWf9zs6<0d~-gsLw^?#'
    '&tLWrslnlk<wNRlZy(=2Fr$yltI<MJxw54<J&w3o>)Cv~ZWEbPbtrELBva*bp%3hLRBg^q=RL<`v~pJ7QK*Ua>XqwiA}=lu7yR<wY>h$JGg9vo'
    '6Kh;CiUk#FrhN?KIT$zwkjyXFQ;-YX`XqaG`*8F(O#K~M4BO9f0HpI!T3o%h3XzMe)y0zh)2s^Err03>!oar978W5dy?V`R?Wo2YUcOqmxlS?H'
    'y8~{S?(I>xpw=amY)MC#{Pp^X2wPI58F=bnXBT%FxFb_(AjmY&RRhF={|OLglL`YiGF2d};G$nG2wk7gZ6@!*u`e#>4yXG@17lZ*sT~x3L*Ozj'
    'Y0A=M8kkho8t5*mQ~}lrTgg-KZXAPt_^`aV931e!`D(R<XvdCxqjo?Brb9I;xS1r^nJAhZ>_Nh-JQc*_fCI`wS*6<mU?9msfg$h-prcwPAmt@^'
    '%M6q`1&siM40a`XwIHw=nh4Gt7wd1c#R3p^M6Cvv{*Gb8-Rd(1EDp4;BzPjBK#VbH#lTVmd4ihfq9`>;IYyk5v)a&@>U=Jb-0OBQNTq3l;{ume'
    'nN%04QYOy}B0M>(!0ibIrlnGL5*jR&tie5lMip5J{;q=XL&70zWL6Q!oHSr)=<-?=dpdjV%5jkXb*9>SLAH3KeHX+zp{-va5cU-nNVVCyq9s5I'
    'v`d)Jg#GTw{d>|K-(l{aP4E{@mgWGt05=HNm&T*#>FC*ghYUisM`26ME>!2A8Qu_Tqx&<OU3OR5AriWR=+>fj>NXj@e>0PByw%2O+G$wI^5fG%'
    'uGd%YI*xt@BQuX?6rxNoJx#K+q9hhet6(3!*S7Gb{q_NMW8DIeziM~fut!;y=k)m_TP70|b3idVpHXo-iv_J7)^!z6cE^ALq6Gg{&wbf9`g0;F'
    'P=?PZEBzYuarH6G0P;@pYGJ1}1exyS+;sfp!@+{@6cDwODo-F(fs-LP&GL+s1f>~k5(BY-G2h46Hn;Gc6yUwUQ|Qt_R6{dxUsZ}O5=it3DLhh!'
    'kFa+JGM~7z4kJVJf4yA31|o}E4c+F99TtaKX@>@W$uh}Ap$p(0uw)tdG_aQ}hq&8-^M>S6Rtc_>xk$^QF&`13{<!{C{>qQ2d|EqV!kY{nJ=pmM'
    'Si0mzZOVoiOst2e0>T3%TA(^R57E?Mu<$RQKo)@HYJg{f!%}Qi35m{tt4#BTw%~)Bz5C)senF#lCSV1NT4NHGPE*doHJgH^$`nemoHd1(EJ-U_'
    '+nMm9#7y&ql{thA0sJa8<aL&TBd!eO=p3A3N<_u&c9idK_ldM^hR!2BtZ^VTwp4kgn93>$ng)_onE=;HnHi-eXaQs%>`_`(VQi}Lcg?6>qv2uG'
    '$`evkGDuYk#7&S7P!GYA%9vUj!5Q)Yd9EQRW;L#&cS0V46a(Qjhe;kL<KJ8}AMY>NPR8g4q!VHzww%>jnS<qSvMObeO4D4G8DPHv;M+N-xdDzR'
    'eIVo^ObYiwKwgpb6ofbz23G}A<EsVf--l+gm@jb#i^UN&7$$RRdgw+gG^Av}e_2Rb7Z7Jytx}U?>?YT`C@WDH0#kgoXp2ryU$5sw!**4ve|r9K'
    'XjoDax^s|*m35lrQe_#48U|1RfF#T61_(lAic&4%9>k-9LBD>~_rz5V9e;8NW~Iy;Pug|P3qnvCBQ}GGZY48d1mK0lkZw;x7&N?~!O!^|%>4D^'
    'hE{_p2xGcYRiQOt9#{y&U|(yX2vLwG2Sgs26QsRSGRJj0zev-e5yf&zCfV0V)L>vR`+kB;7TCr{$OasnG6qNpqIObMOk^4?WM+U23!n(8lM;|b'
    '693SU^xXYx_+HiFUZq8;5{6CXrUA+;RROjsO;d=Yi~+S&LI_CXmL9Ji&rS)}*O!4=0cB5<!)Ef+JvW;v<y>8^R+raKYZ6+u^HYU_P+uv5Gu1%7'
    'Nu`sbD5}%|`z1JPlNGtrnWIOTVXl}cD+suCQDt?Cb59KC8EKVMu(&1Og2}t^#hHd?<EpqU=YbN1wuA71O><*c(qt9rW8MG*OYqb{ejM0Ya3#Ta'
    'H_T|9mf?PE3HV=>`EMHEb2{szU}g8?`!6Q;$HeyQXj5Kfsj0Z)1-R^l)ro0xnS&z*jjGBzmw?f-=}n1c-)2h8?Gn%gn1h6+H7K^sONmXvxkv?&'
    '77X|aa0djEw^OpB?)1Yq*(uxi<U?If{f04CLu!F6!m(qz0Tsy$!3qJ4DRj<2!$e;3CfS@_U4GNq`MoPsNE5(0CCDqTIpDtp&yz8b`b<h*Rt)zO'
    'gnnaooWS-Q?w{>H?4UA@%E>S#5&<#{M5utA5=>RpwQeLQ!(o|L?Htd!oL((vB;fP;H<;sc{bADf?>(rTK**{zkxZpB!-Z9~Dhpon4Cowk8^}eb'
    'Do&b9%#<?be2H<hjXD;xBFh%9Qc)BFa;_}uJT1T=WlT3kSyyRZ1CAWB1uDaqi6k|^XAN$E0KW&CMk)Ux9D>4-0U-rizi_^J7m)O?;Z~wn?Syh&'
    'Cjb*d2%INIK=92mnC6XS8t5_64F?bJCL~m@IT(l<Obu=;W{g!7Sp`PRTJDkrcek;J_z^p*hRK_IcNCiJpr-85@$;qvy8sf(8zq28CD`7iloGOr'
    '%0-#6GRxB(n@3}UXaeHo#TBI?8Xnp<Ir2@~p}~q*RmKxp$Q%r-0hdr~@Fq+H6(x+<5)x0&-HgB|nVOdn?sbxtqG<%?qpG^ZydVmx8rYodU#}J`'
    'on3Prcsr~S$|p%RN&MZ;OPzvVWo08t<dWKuxy|If*5Id-ysGOAtlg`{nj47vgJe|6xfXN2kVnta=)q!$dnS!YR{I+O+L<LeB)U?H#sIyfAez8<'
    '0RP&E0;jLQQoLF?sw|d#HFyu)YcpTIKZ;s*s)dSa5I~*FLS=$MEXY9(6HrR<M?#mP1e(>gW2=Zk1r<$!;|iV|X24C=P0j^yTUPTb$skaG&wjO#'
    'R}i+>gW}}le?0i%!M!8jVwhuY4&Bbv^~5|tbQ*9CX$DT2L@dpPVyP?&95%qd0^qfI<JZ^okCC}$(tXp;LXwV)RB<xRAkHaXfz^^M6$!&3B&OD~'
    'HcaIHj2bX>Tx*DsCC432b8y!c<GG|SYe5P&G=8;kI_M9f^7i#p{J}Q<W%4)Ic$gb3aXH<gK)5Pcku;nEX<}yt*h3tU$w}NR>ddedVoav#P}E{E'
    'H7oK2Tc~T#1`Q{n;SP2*FuoPI1SSo!(}}9Ud{$+eL8#0#23Tq!ZWVRIo08Op?-$qHMS9(i1p$Ex%^D46qy{6Dsv=<^tiZN8?hlX+1ZYT4?jU$h'
    'qBes>labvP7@jw2l4$`v&NZZ4!!_v0tHs;7o~aq14Nc?Tvquv;fcJc*k3z%Y#X~~%VflA?hW&4<#+11>BBj$8IC-$WAg>Y=kR>?cnwEd{_L5%='
    'Dooe!IF&!=N50J<{XB$V9U4sRFFS`1zuv7g66wK3gB9ljJf#Hqf|d)V4c!}Qa91kQbMzcHLgK62$D^0d$KjK+z<oZl29%><5je6=CP%ycIf948'
    '<cIrDe<q)p>GPkTK7I1+#pw^zrw{MF`0>fJM<;35>h!@+r;n$Po<7W`PaZ$~=jMx_pPx=&{Ea*UpLegQ;q?CW;gkFK9-f{g@r%d5Odmar`?rrC'
    '-+OfW{OP^>rzdpfH(t^Z4<EcZoj!f`<i(TTx6fWoPaog={^9BL`3ub9QUBHBC(~yyUOea?hV@Nt!q)CNjo>vCare`yo7|J>4^QsX2oE0r)UNWc'
    'zpLD7+qt1MxIk)BUpV7n`(m-2P%6R+r6TN}nu|%uM0m0^CVJt+<|ZL^diRP_a}PY8UoP7p=frHKS=~PHEKmFUzc3yDY&|*G=abi~<@;!2Pgu8K'
    'kAFD*{^y^%fu26N-&@w|@^aR7%;)p&2Y`3QdxKsr$oe+1Da<t>yZ2J*3!q)H?@cH%K=0ipzq(w`m#@jycXF|$h^W=XXF2LM<mY1c`f7Paky^yn'
    'Qcj`^S6_J7VB|tP(Tl5d_b;4C@blX7!57<JEkZt|cYHMo<<)j|XKiY;=rA|l&u3UHmc6}>^&3hbznGNy<Z?M-S+55%yL>-eQ}o#S5(hn5nu*`Q'
    '=!8fk-u`SM=#%rC`P{yd?VEs}ezy+>`Z=>GaI0)whXf}N+HriVc)WI2*S2!A0s`-y&Vx^Jz~1Mh4Q|<1$M&5?-&hmG=cB`ck%#BiAH?&d)cMXf'
    'uh;V(gZJBY#@1`Qb1{Wq(VtZ-UDuOsU^ig*CVH9E4tQN(kpbhYCP$jTuWAS(;@U-6;|;S-TKF5RrQEqrMg^70$72h(kNQY`w2jSM|H0Nrs12c_'
    'jF7Q=jUw}F%7rPCqDpGWI$R~BwB$w#nO5K|vRabSrb<L3MBNztlc#9{5g<4=bv{D7#sLQg9)z4qbJMW8D!`NqQjHdcA!Dy-SjlozRx%YrN$_-%'
    '(*2W&&B<co#%W`^BWIi7K**|W(_e`KTiyF;ccc1#HVY8>9FTjmKfsw~dolfo<aYf`&#!#2qKo%)X40rVhzTdcIDPQLbVZn1*x0zxZN+>$3Iu%c'
    'tP2;WE}Eh}s2FHkZ1l;;Qrsrr7TU%CISveUQEx{efU^gq3OQD)tWKJ=lqoNZI;oomA_Rm81t~NyRH>M1$X{BCq)f?>Qkz0lU7x|#8!r?4?DN5I'
    'UB@}(Lb`MIkHa@Z^GzU_l0arsRuyHEXIg6jjb<{@iONzi;2HjEWCI8)b51og$}+8TMKW|<ZqFC8WKw)#!s_kkK_1u!(;Tfot|>QMOAX|mI}jC)'
    '<oZ$pREJi9EmuvBSxS*)Rihb~d8TkV3M>tBerEDg<~oD?rXbX?LNds}S;Omu1RB1*3W%Hew^+n<>M}PE!cLw7Mhe5Q&E$*+$dE;yLg0Wjta#3p'
    'sSRYaENu+=h1N<Z0_UX2p(Yapt0?j``=-m7u4gSn)ISl0wNyYk!ZeA<fc7{kmJ*HSH8RCjD5&;Ht?Nc{rE;Axh}TV)CYqT{uz?dP%5S-ntChdv'
    'Vm5qJm^NB*;LoDT8db3-ml-QXA(E=b`lyC!Ue_tWEg>^uiBapk%u8MZ`Bp`~y&_=g<|dXDq6>!CB}m!AxinRZLvhv#SwRNZN)<RLmM9}lR*)0A'
    'sH(aWvQi)_1~PG_wBBA<S~lz^)<xVng-h>PmrQ`qK>qTK7kMouOX=zZ(5fQk08f#sM(0e{iIS`qSUqP=&1A~RwOI|UtE!6|0l{~CHur(~!)t5O'
    '0vAbFO$ziZb(I%7Z&)EjlQ7O}p@BsTyd|pNWMoKfm}ETFv=2o&aAq0HSb7tSn}g(E@z;8IVMUXavf#2w@*D^dH!RZyH+7;jL%4#Bqcs+6>N+cv'
    '((sa35*M(@OVI#)2M&$p-v}5f)OiXvadx>r(hHk9XJ&?%C3C45=u(qwnG3Qwq&Tmn1Oq6I%JZ73OtK2Cq~IK+3A>QXs*+YGUdVxEB~|h5m*q2-'
    '*c><OOTM@qUS4gmma0_Xb~7!Tf@it`odR5v$%)*r^TsH}2tvt+q)f?{SE$02OcZK>aFH+34J>aR65h;*7iNGt8kS;ZwZhDGRn{iSa>i04b1s1('
    'K@>#FHP|;Lsssq81j*w~&hior`S!wCR@C2qVJm$OQD`dP4KFLts}fj{LH4Lg?GJH<H$d{70X9{sIsPZWYcvJ1d73;ifHDQS+83rthrkh=baTrh'
    'VBEpZ7NEX{%xY;>Qm~~42cDEFqiBSjyw<>Z2%=<3lFACpF3Z|R%%vKDnGUQhtBV_3*}El38ig4Q9~)o|kS6Q8g7{DuA_J+aL}pk45rfwm11F_$'
    'tRTQetwHJ1T-H<#54SQ)hbT2j-M3g=UzxT+x>7KMMM{B#<Q-m`Ttkv5D!?Cv)y5=AWvto1BHuETC0GKhbI!@xN)HG~tSpk`Tdcy#8#_>5U4uhP'
    'l17lZSW_6KvH>`R6(ID3yGU?2QWUflWs;Qa3d$&)iBdHi!szL5;sz<rt0ZcAl%?&=ubVPhyE5ZRUMgj>8dC&^2Y@QIXmVg=pszB~c_o3wW!W@k'
    'E;67ntde6Xc{Kn&MN<5+;B)QtQw_c><r$NCE-F>&MihW15Rkeuy3R7(`HcK+fPi(Kmf&l_E#$z}rb<#VF#X>~LI*c|I(2gnfn4X51#QRmm5@Sp'
    '4JfaZ1`v_sib{ZdNz#HL3#Z~)#SP>-k!KW5ic4Z63)3=9l^haMt3Pf57ndsvahdMGtOA489M_ap*mH8!kYtsuc$IP5`C4X;;SgzZUEy#DIZ<?6'
    'o>X8^RXzal)gL+g_3T%@YwD^7$jc;Swc@y#tS%s!0rE|q<{%azetE9*M4GyQAPPQ>0^o|K$OKtr`G6!)CG{US_4gDowQKHa1EzwU1Gt7%+0-H_'
    'S<`3*kRkN|uzE&OZX&5mR^y|J<wi7+MQg%9$pAsCf7_kCe(sB_`Fz)PRaJ^<6(!?>*xM{4`NPx{Iiea4aWa)PvBVJ4u-iN@Bugq?Yfx(7pMi=0'
    'k=K<b4v62i?KO5f&mjmGiY1UlGC%^bBN+FZf@xBLrAe@T4Dwf<Kz8IQxt<qkou{VJ1IVT+{y>m9=O0Lg`g*Zz{*YpIu7J%=4q{4t5cqA%Ur~Zd'
    'Eizr_5ck2dQ<4&6u$~0yQ`We&;9Zl%41vubcw3joto7w`2d0N;n_%8xKuuv}$y14$6$~OR_yR)mHE^xUgaOZ%lRsIOLPBKV7=Smk%>0k<>u=FW'
    'Vxfa<j%j!SJ|KbQEeO7A@Co3jc|#6wnyE&cqA_JAC;+S}YcQ_h$jLQHn{tTFDa!H>7KzC0YBShVx~9RCD3gS<GEv|hN>HYRCs_r#JLN1hq}@xD'
    '0I!gnMgpR8++4Z_qyfh_QmTP#kT%UXV%2soB9#!yD47Q<ONv@iS{9Y4OjVa4Uqvo;QD!NHT32a;rT`-)R1G0PR|OwZMHXpU|IvKU&gs{tVw4bu'
    'IA#hpEP(hqc($4n1LTx1hHJ2v6}hs3T;)QPm2ALn3V>VL8k1wT0bB8hPTy!wxk7iD7{E7>cCk#eBoTI(!4Q*g9K!`r!f3F~xV<H*wIO*nNyhWE'
    'u2PVU8mBS<L}~WNE`Y-Oj`(~|BH4~rknC82J{d(>8k!<e)?Ct%Ai%1O1hYn!X<pi-KFWYJtD2W3G5&z9A(=T#*dHsm*mw@U1B}QMKFL)DE|deW'
    'ij)-s+_dBkL}f6WSOQB^F2S&Ah%~h*Yg14nl~N~$4XF&%=361v+2V4Lk;_wH(Y)XpWO#6ODWzfoZ$Y-!s$!b5(j>Bi%tGb`G{_SsvRXhqW0d(~'
    'Kz3}h>Koys*)DBaNpW<NGAd9qAVdCmpbV5Vfm9PEm^Vm~6&NA|i9#n;2?EDB=U5(uwv-JiZ!7jkh>km_Yyjdl%S%cbWvYr(Zjr14!l!IB<nk(W'
    '%u+Q;RV5(sI>Utq7iVz6!7Swi6gk85=8wjqJLfMzQL)nyqk!urIooE8fsv3L<Vq%uqzI~p0e%!GvR!~hN~;tsQCXHu52%&NeD%kppzGE^xu#fA'
    'aokMFbW$6x!OStCiz3N1ld7RqT#_=ckhV@11{`;mS4<>46GLdUPK$2@`_8>$Xce4Q;H%SI7P{b&wIE4W6@?^}8H6(iNv5QXHH<PLQIeG$d{N5s'
    '1{WBYWXMw6fam@;3cA)w<f#2?$Mr~RRX}bRVDF?Z0b*b%$Xo$l2)MH<c?QWjS7k0sTptiXFxpap2RG#Ct%lUZ%{MX)n)m9+<(Js8mIjQs0<$fR'
    '0G|W^tIL8ls;R;2<=~727>@=E%@nQ_P!c)Pr#MPVD<qmBI`K!d8@;uh>+|Jmhnb9l_lCs4GXpXvK=O)OfWR54(?Zt}p{pD`9xoGMUXVv{-i+y7'
    'gAtJ>2*<#=ly&v3molZoYBnF~u<5T#ZO%TX6$x;8EgHoogo%uk{=QO@JnSW9x&YssfouTk@}wb_qy)b;ARIN{NWuM1uf&ql-iqaF5E&#T%Zn=2'
    'GH+^=0*RGCWlaL^x=JLNY+Y!^@fasZF<+WQ43i|FFBzqt7(&Y|`6dU{B7i}*L^Wk*>WbHZ9g_5NiiB(gXo#x8FXobruu{pI6&!3XgjI0vAf-Z+'
    'a%YG-maMojoJN|z9Xz9wRSsdIsTv?NR!eRjQNYgYCQn&eDTq%7j7tiPp$fdk+8RaSdy+Dn3`kC_uD|udoPTYU+-uVqE2IKj1^P&$5=A)41dI%p'
    'sfq5GJd;9WMP(zgEJ+E{u;>DyMX_!5khKy_!nawKLkUM*DPV>5E`>=21kEVk4QMVgH6<(sZ&N{}Duv9nlBsF30ursF6&AA2xk~d&gI6n*D2G-A'
    '!Rwo@$lCB$SL6z~i{EKV1qOjbIwZ8lWmXU`pusl-NpM9D0g!}irHN4kE61CP8H(agn^YtNgeYhAH##ONt~ZS0%1RU}&vOp$Ex`@SOet_v$g~0?'
    'MCluVo^cqtU}?i>_M(wS^0bgCXG4m_s{B@-X>JvS5#DghlfD`}7L~IC#H8j7f;(6O;55j?3Ze-Rb3%qm2#u7a4g`o7TBl&!3fwD3ndT(BNn5{l'
    'xW8Pkj$8`&9qJ*#mu!fjG^sM+2C6z&xgggQj;qupjY<`TlqMSZ5R#<Ak)|a}lRN<_9wI_{lYZ07Ls@ucW|wb}7KzCV;I;-*4ag=ghORl-PtZ|S'
    'W?;F&%+*z9OhcA$Nc}aaFr)-#crJ%b;7O9)h%*LXt@w^rNtuAxq%6F7!W)VYm!v2%xL>Sn(u`!C2JC~tF;Tc&3AwteO$i1lHI*I$yQ0ay&8ny-'
    '=Irw05tZL~x68F82N+3Q_A-+u5#ZIBsne{GjmR}iz<N`1UHkxtz^W#zAu^?m*W?{Vs)Hd<r!1?#?Sg!sza7ki(6T5Stw47e;}B9MXlnuyFBf$U'
    '_NQ(nFku0Xfui~Z9Tk9)6^%&1c@GhxtSD~;?%L`TJ77;s%D4okOoQdtSzb4}O(|Ea46-YO>`M7haXcn95Q8cJW>Vt5F-1v<hD4(5CT^5>D*3|d'
    'iW*Q=3De*#Ql1n=SpXwaRx4aJ&{N8VOrBjf?<oZ;ls3(?0@fC(%=00ylIk1ZELzu&vjP)J@zf>Px~`g>)Pp6?30y&`@<cQRcsKx}&E2ar4&Fhe'
    'l#n2)R0*zi2v3yNw?8ZJLu%$KrR=y~B`;K(fma~)4<#bbGhG!Lu%;V|2O%$AlWH<nYOqIOz`4q6N+&IHo|HrGtQotJ>(%mlGqKCHsnObHCZ{}C'
    'rh&wPWj1LkN?9sF6k1kAW12E42qHm`OA@dEr<LYFJA;O%8}Qqrax{eFufBNAD6el>u{^ISzk{N*z#P~=Ws+4%Au8O$oV*CZ651r=DUhd0AO}-='
    'b*y>_Fw^{I(Iek^evA@&;`*pOGf65n<;W63F<xZ=SjH%OyQy$;M({MPL|TC;WROv^2Fyn~i2Up3Mpn1x=euwTE}SL{6xRSkZF0)sld>eQDoIAG'
    '0;iN`_-{pt*jSOLItBAcVKOC|Ne7Rtxf!5beHBoaGGRH@D#3}!riMgPkQl<~mugJS3@8;Od{V<^C6--~;8mIgw~q|KgUtNR9NS`f`DV3z|24=3'
    '9Vc~JYgU*BjA5B$Rb^2WDTHNk^eLtCE)@2nsUYtQg+na41nFaC%^?H~nZCZ2BxysAofp}9P+Cc3fjibvZV`#&(JC)>-qcd@jLJPEwU)A|YKf}`'
    'ai+;|!xAu$OsFgqwHb=n`X*fCLcbrjVl?2GDpn?xr&j^90pn>r6r3EzG&!H>qNx)wkabg)I!mP~8K?gMRSw;$Jge&)0l5$`xdX~DX^6hqZqC3|'
    ';Be}!)EcK^YAtfB-!uiK?JP<1D!_H+d6PkwNs7A2hAbQTH^J}Gss<SiSujl$h%y!PqLC@)O+Mlp#3-+cm!i@L8DvLR>k4$XF2Ec^$SjObhx`pP'
    'kiFvDsm;T&?Btx1EGrU;8Im%uE+KH>lHeAlC2$)R;7M`{RRGd9g!@ba5G8=41h3@~lB<dvVlA%mbXTC8x)Bhm3{y#hj`K=zI;R}>pIoYOqj<`|'
    'r-OaUuuzj{jTYbyA@C>sf9zlPKUmd{u!M$e7-fTlNjansDiNSTbGghwpA44(u$+=rB@iPiu9os2lDj78bj9@$Zm7%hW|rhrS?oBf98@#crKF-X'
    'iK1*|q9PeeN<;Ri1}6u0ClwHcsniHaC~wF`(t6JpLo!L3RX2k2NYk`qQ4sePCFZ4ccglc=tH97v;IN@gbL1ph0ULl@Hwo@kqsVEd#2r+&yown@'
    'ciGJx*7Z>qT5!<Uq|PXNB-REwgHg#QrmYe>1KN{yiepMRFpDUG1<65!)&<UD$iP-NHvxs@4yx6Za;@$RuY-_*D~3q})>1BInP~%TARAJh<tZhv'
    'H)SFcEEW7b%cV#tYcFLN$V+g8L#}W)Aw({A83`naEpAYyfE5IoGKjzsr|2Z8B#r?q0RdTntpm4F3d2}kHDzroPVq6#P*84N-T?n<|EAI)J61(r'
    '--$7}C!$FWC1-2Wyet?8g-vsnHF?1dOA6pYZR>4u2DDD$bVVkLAtoui5y8X5;MHO$my{(G@tB#CVwXfCbDW5xh^-`PRFRX@tTtdRj3$q9%IZyS'
    'f(4ZzVksZ;iv^mgZ)j;Q$B5bG)|CSp5))By!|EEiPXY(l8F}kBO=T*~v;rqvWRSRutR$hB>Xnh?F&N|7R5zk{IzL}7?4w;LR@Z4ldFVlWL59=1'
    'PJ{r8#2HpRsUSj99#N2J$+?YJ0q2y7MoUczqX%#X<zD#q3sYL|GA4+|h`h)PUQ*`6JduK>X+@zQ9OxvKC7`pca-hXRh&+|jnoIMPvs`n<hCJ|+'
    'w7M~cuFbZ%3w1QOCX_;tRi%Q=1lrY<6k3)yC8r>lnsWX^7HmMK>q^&Paw|qINkz?oD+lGP;szsDSG(fBfYgmC>bf>rDZ$EuNfKFAv8*l)d6a=h'
    'B^3^>kV4=bO>Pu7;DM`}H4Uo=brm;(If#YY6{eA4vRvt!G2l8`ijpusxcVGe9?X*9q>89XnrtW;t>y_!4TrFm7)r!2B$`w=aJ8-jK*KVblsrx7'
    'irHETAWT`MWJQ^9An1y`R9Y}{zQR&WR@EwzCB!nxbvl%q0L1VH_@_4<-0p03dBx{brJYJ?xr3pn8fqpbHAZeDn}F48B^dZxlhhR3u2iCc*>e)g'
    '63sZCLaGE-OdzTxLzfIP>dlx~#oA`J+F=VaDTR07qG^g&gkTSGOXn0E4k?+D-yC=ia&d%osW~vFrU(S`&dPz+>mjDTxFORqmHg6|Gr7w}rk1=;'
    'nIzpb%abB8Or(&-$kDMjTqqC&pv(f8-UwwAxHPqtln`AGde5fGO{tycOIvQp37I<%PNZ5210k~lpDu00K8}ikwdYb4R#79Gx+!Y#^pIBy3IR5G'
    'A_}8LQ#C`#k=+nPuGe6#cAFNoEFg`QkVL4SY$m8gCaZufLEcna0wXeV;Q|LKD@_=)$r6<`kO@EnaL_{>W0Bs-(gamW+{G`dyfBdYixj+K2_D|m'
    'Qh^WTRikjRIG7K{Qwc^SZ)C=(&RCsO)`=1{!BoYNzOcF(Hw{VYrgu2gR6u_a6^lf2Tq}`-p8_L{Ulqs}qq|q;HLFuCaMLI_0N1DjG|D35l^777'
    'n=HQ}Bjx$U@@la|+hKsUQ{pEQtO61l<Pw}3C_q+A$`I11JZ<U(vWhg|?WNA?ZLSiYL*g7F)Rem6rnJ_IljO2XKUzp;toc8w1!oYwaLFJ_i;{Dx'
    '7{|#}S&~-`i7q;+427C8h|sA_Y{}Un+)>^P)~6`m>veCeE{aMex~eEpniRpcHe?W@9O%N-I89L%#5NX?WgtYCR6j~<3b82kP|!VS-3^+}ci;}q'
    'nuG(+uq;gukSX{Nt}@8hwaURa<~bOSnrsnyo>=OwO2(S1ZiL3-h7<%<byKT)M_z_I@jcHaFoeuiMtPCaQdD3$O5GSGOOUuE2TNaRN+4%|3scG!'
    '4y4i$d!j_0mxKOmH$%z--?)2m1!VvMzi*@jM@%*608R<8eMV*B8AMj9V#Y|>mXVc(Djj7tArS#Goe$7yh(Gy_iDw+w%`yKw@PAWMW{V^hs%)sb'
    'OvWH%l}rgkWx7dgkZIDH;>uOHagsQ)|A)A@QLpPpjsPFY_($Vu_%(or%nU^`B;}uZ`mH*WWoLqO<=yqUnM`6zqMX|V&|L*|S7{;2q?Up128MIJ'
    'PcE^~uV->=fI_|qHSsIw&I5s#kA;2-ob$#P-L2WcO9A7-Zw82sReTdpT>x1AzpHB0P(9%=G$87{bMpawjMdBlIkL}w)?--24+KUL+l<Ysy@H|w'
    '79*ZO8}(gctoMOc--NVp+*xVt(gnaVWh*j~K_+hkXu*mzhwV8k6+kN+NLQEEI3pen1e68{yE`i!)cc_Q&qmSP+eK{=2rj}55bEd#`+0fc9V>`S'
    'ncz1q;YhLfw|Ua*oH~|}m&t%ByMYs*vb$^B#*a{cHP=_SMFWnSIFs2+2s4APIX+y>cuZ&DcWHG4E&vp1z$0w0$EMy@xaQ+sL;P_yC<*@oqoE1S'
    'EYcGaNvKMPiN$wM1LG-kcBIih9cS;Ng)suldkZ^Z8UVHXY}2pG@@JYg$*fi0X@o<~#gO-7h?=OlBMOcNEQWYN!C4Qx)S2nUM+Z>b$;-7nb-KO2'
    'oZx?l(zQ~Kt#xMtfE8;8<T8Ymz&TUTfF%}la0P4SnSAJr6{wa6<h;{^@$OC`-AE*Q%xJ5+Kmn&jdm!JFo5O>t(e1GU{V$JS18q?f7FjYb^GQ)L'
    'z)W`=P~x59diwqI%iHVEXE#2)$pR5Y7}nh=AU9STpw$v=#GI(uAlPp7RjdswTao+&*43fvS#G-r>T;iU^!K4pda`1PohwWF2t%>UKx0}9VxZ8)'
    'R-NiW&m46TKnY}_IJ6bYDo7jC%^u5c#qNYf-jBz@lS~*q>aKSy)f7>;M@HK3WkldAN_4xG=_Kp2a3X)OA4$N;lb`IqAG;>>dau>`P!R05`S7H_'
    'zkE5<e83mcp)P@Dch|8fM#EzVbRSCK3GB1dFFo=qi+wE<*o2g3;`euGtKCT;_2cdFm&dcKgF%Nb;NNI-0YKsm#S&yrKdBjCBI2wlaaHL$HUThm'
    'AEWsRWD+xy%T5zp?itq8?>|H$dCbZq<$FUGSs|{Rg_o&y2%FETF%9_W0@gSIRSL0WA^~XG+Spz8x8&{|<lpF$J+6dfsYrnu{A?PKWHRE70cd&5'
    'Y&e^3EeASEWqr-uuS)xAzH#U6luNyPoUgy1FC}k<*)2y&eQdNq{Q}KZS{$jap^;s|-3hQ{OxUD-&?s+!v^q`2Z`EP%bhYqjh(Csv*nTAsu(1>@'
    '#u<I<vBx|;K#Mt~(ph@MDNWUePR^1D#ofUaR<z!C$u!A*vdxXokyMgv;4vvrb1JKV5;%7J)REXQl09-L@@E6~jO;c;l@tLAurZN(-Txg4|MGmR'
    '^wE;k4d6<TteKm~4vht%(p;Pq+pBwUSOs{IY#zlA>lPgVvr@Tcn?t?0e5dZfTa9$hde%Bx0S1=fgVoWoXcZ7~2%=5EWUT@&yHV5P9B0Y+J8MuE'
    'Z-lT_nZ8386Ak-rf|Z7Z(8r?3Jzxv4aQ2Hb)4B8ZBn9?qNz9qT`+4HLHBPX*DnjYk-NamICETOc=k@o~=3Bsjd^U$qJ&+`3nxr(Lpu`2*)e4xJ'
    'Mx@9{yKSm$0X}zPmjL=^+xfD157u_9*6kBkIfx0$iLBRBu5|fyG{#L8Nl-d4bY$_PkgBpNC?8rEsC&H~<B+=!rf`Ri_@>YQI=kO$HT9OkS)|8B'
    'F-A!sqpk@{Rzc3A+XH|zC1b0f(q_St3$uG9{cVPi{%+UEw_j{Lm^Jno2kw!>Ig`{T%F^QNZ>HUaWID_XfM#52#UaJ8f=k)F73cpI!+Ep!FHh&~'
    'ffUq7F`BVZva!#`egp-f?#PW<0?3V{`U6l_@rEQQnG#<PHE#v*p&j-}7cJSJ6BYQgUta!oHuP5|*NDswOl&2!kZb0Cvq;f!nLg36y)p1ifo^q{'
    '!3K~dfh3VLw{3LyK*l#>0(_8?h66yV4lz-}I#drBJS7I?#)(KO;C%zenyakH_D4|F+z{Q?fW%=z@AS0xW<P#k%j$+NBNe*G47SNRDOCz*rNKA@'
    'Q%my}rc|n3CWMSay&@l=I90uU$`1D(M1A=6O<!NnGDx6+^a^uEq{lRIvKru0i&Re)z5;{dfVs_<3>eBvMaoHiAupPv?elI8@4>b?`}g+rJ6nCu'
    'pEhI)ZKj&i%{|A_lCrNZVpU^W03vxN%~O@ik%3aSNdL$?VE}cdZEv{0)1>WsJe|=>DZ~-jI)TK<?9Qh`HaQ|e(lsZOPy-KB3L{8mf1%NG2KB8h'
    'tl#$7wY!Y%|JTi_zzI-=lhy;L*k>)264y!5aB47lGwY@(-_A+|264bCNcaW{8Vi*swpaXjNGJ30{dQoB2h4=p&{Y=7m`6Pxt8{$&+P$x_Ij$bo'
    'DA{TXX6D;)fc6D2xQ8umN%!gV1k3#IFZS&X16k!2wVMqx=h5c6ss;@JSe#j1skYJmSb_7woVe@B2b{$;1gVp+ZO#ElXI+I5GjB(@?sGq>pFX|W'
    'C;d2kDg{enHIw9hNvY4^Bjr*h;H%PT-z2jjeP$2*PaCA%VnwwJ6gt`ElUDCExc@6^zzs<P1+!$g&qOAW7CcJXOg)d{*<?48pSez?okLgNO*W0s'
    'T79a%SlZoZrMu`LJ#(?n^U2D_9%_p0WQSGov-0kvcNf?{k|t<b<f8}K3>FPw(onyBjsP+{NxpWC$PPoI74w#mA*YuRtBYRw^2b%Fa{%j(RD)j>'
    'QKZ5-RUr#FbI;CC?8OWaS#_%xk-gOWb!F-E$AeK}fyUlvby^!3rY>p26r9BZ=qCy@-Cu^y1p?5(sYPO)t*{>Sl#n4KYPM{3(wy^GYbQS2J0g1c'
    'f!=Myp_+|)bv}TdCEC8lh5AJhGLlwTHFaGfA?d5@eN-$E6~`iIX>dD29plxpfpLBY0y#^B4(zg#N3&C~#je<Xt$AbupybtaZDmrddj-obPchdi'
    'dfUuE2SQA{G4V_4+2hOm`gQK_a8w8LbT4znmZEdDO9`Q-2_}t`&dVxcaG@1dju+~nda-s_WM@8HBwRST|MhR3K*Ab_0SqQ9HnpzF4~(M`kCtg<'
    '_OfPW?|^sW3I%9TiP;hvkVK@69dasiT`*Xdv+E}-^k+bh?xVX_{Gu;+L4ggFQ$~d~fJ6mXAqxXS$_}Jur0&hn?E_MX7s?Pn-dYulLfIvcG=+sJ'
    'eCks+<1a5lxTh{;n}l+OGNi7TCV&#H^hV#`EqiS4x?o;z1p}}X!?arZVDmj-n+wR*;8xWG!eR%4M)}dkKw_i9x8a){9UxI;x6UBmr3<yL-){vI'
    'YmC_bzTjheTSC>1kSm3<0<x=0Z;SrYb(nK~gh`k;hh`e6j=58*OEq39LXN-GznqTKE-5j9?s+#S;HWdCTdWhf`lvA|pVU=6P8L{x$+6Gj(*W9Y'
    'u{hmYre4*R{{Ar^K6k)^k5dmaswbIfp@?;%`#=CF#fP7Dt>dADI!kQ0N0A2rs;;n}BETw{Z|j>u^{cL+e-KIS;Cd@h{EVlu)c7%DigDpEt(j5h'
    'Cl(=ju=So7sDcVxFHPylx%EWl&HMjHhx^yJmtP*Iy@83pe)&nkSAY5Dq5S>JQ{#KV$Lv@I>Kf8&VEIbNI-HNOT4`981!hhP0Za|TSDep4**Onh'
    ')$T?wf2aceEsQ^Y<o_$2ufy2i{&B+BrVoiWOBIONXPIvL)wR)Au+M@*zxWWKNWZy)s=6pad<11^NN{1RBPZ8rIE<&q&nF^7j)ODQ5Jx?<RKUwn'
    ')tifRpeE8{u+53ggtQBkRVj(L5Wy-+W3#%Oz;5pUzkBA_&wG-OdPW`aU+pQH^L-^x8T8O4fp10ZryQ(<JJtro(t=b#f6if4UgvK(ORWF!rQ)rR'
    'pVKTH=qbSLet|`mUNS9Ao3Gwe$_4u;lwP9)796XlL`{vH>7cXK#YAY~pLamH6$2nd7GHw0ZB&pk;{fFi2~}-z$d)f*;Kf-=X_{LZ9p|ESY$5<d'
    't8Q1}E^;uUzs9~P5ty5bD~XRjD=sQ3Oot!0ugkLyI91vKp@J=ZpKG}<d>_t$YpIN|eTtN8Y~jv5LXw2R_iulAUXQRq27LtQKpecZ><d}PVrhl$'
    's|l#xx9$@#VaNU(yN91^6d`T}f$UT8X)NIwpa6JXBZL0MOxLBzvF74(vN{@|JJruXMu7iv5js=2ov)hvhp$p@eSFka?W=Qy1GuLOwaEiGYhS^4'
    'Bv3QdX$pQNU8M`)`tl1TN<vEPY1-_hfBIti>&HJk%qYL+)F-baiJ#_|u(28kf{n)$AVMa>i6{_}EfoM8Yy`E@1<SR&$hu|_N}oQx9K+>XSSQtC'
    'uZUyom^O~Uc=doh)US!X<}oJ<adJ^h1IugKjaogJ_gL(I-@aSqt2p=fP=3<+UplB}vmVbzUQr5!QeN2fBZq=o2kO^28q{XXaT>@5=SyhjmwJp9'
    '`z#Gxh~KZRtBIE;R|m%Ib#mT=SXL_bPZyA@LR6q96C|vWa#=2i?yy$bfv2j{UYaA6lRBVad$QF!t@qHedH?cB-`*cSUe3lXycG~1_A4$BB``cJ'
    'LoI@h#9^kgSooKxlb`q=5*w+VIRF<eMYhvG;qo2$hd0B+kL+5&YGb3V@zQYMtVKnek-Y;f^qTMs>WY-rq$>9#%_utETvmxmcSgdo-6g!^h2rcQ'
    'Wn{ifbY$i-3bJ^o_b7mTm?g4wB8-&R;D9o_<ibjet`orbLF=yD%3r0D@`vd9c{I6szHWMYOhh*I*)p>VUII=;IcrbNP>)}%#&KTYfzFBSwUF&>'
    '8;x9b!}aG9y#f8(`*)KN-t6<L_{_xAR|7n29fvDQrROca7+|{tAR!b*N*Mv{cX3!3HMS_WBT(Dabz&NHRjd1V+mmk*ogFMIlOwEwM(8n6D8KiT'
    '{FI<+VQ~VsWidbLiIh;bXdB&7i*11;NZ9VdtGdYk2<4|A3B3$HNM|Iw1`?bV;gq;?v$}E!aZyazf%i)=7b{khU1k&CfSgg|vc0>L+;KqWfxU5`'
    '&U#xa<;5MRb3>(u93NW@sb49#iC|n)J&f)Yd^rxb5wwBj6D&3k)mG?5{9W6iZwE+Iotjvu0jmPgd1FJSugDorYn>60aB>>2rA{v8P@#+2nstJL'
    '?1l$@_W9~f`GXk}b&@D+9ofHaK3NN`(t)h@5{4jYI=1zsnPlZDo&y+2oCrAr67RsS53yVv)Hh|ozcgLp<eeNkVN2ClhgxI5j!SEj-_oVfL<Ad>'
    '|Me-;ABAs#HC14Ir2jB?gRJC}m&f<LQ|=U7ne%M$<DGNo0}f)O&DGGEnPtFO0+#FGE>!<Vz6qY~kKX#IaXZRd)5USo(BMJO1R7ARP^(tKzH4#v'
    '`miGt0?0c*dB-U_#WclLL#4P9D%{{sRU4qUyNkDbOpgEOUOd4e*h?$LK#v|&sItI*JE7wMWA8Mbz^2To&>S4s5qQncMnK^-N!%Ir)a38in13pL'
    '7>;wLv-h?7oM0;jtV}}nanMI{1lI@hWyfL;tud^afUH7~oVVEe`(bhK)SG`aO#H<p<s|J7=d#5J%H=vxew<U0(bv&K?0_jL<bYkNAl3qO1xlck'
    'eH5j*kal3?G6eb81oER%tj-8(57Vp!4y_I#`-$Tfd8_A9#$zipyQLEc%_3)-@J*6k;p^}&F(PqZ9M_L0%n(J!5d-W+Y7paV15O>dv*qFw;YMs4'
    'ieD?+D12%ubkwz+h!A&!bW*Qhty`mkrGpFm!wUsS*g~5o1LtlzOO&K{iI>TXmfMt^7z$EK!w|aXHT*EP`hel$kUqcsa!WVQMi>;769>LhHPwNH'
    'pH|?#MKyYAt^z<@QKWI|k~GDC6@?B&V9jj<($zKj2R(hcXK5Ub(`0Exl6<<Fw(4+V4mFU@>;9hR0Do==7c*3`UJ>)!JsVMB|LToLxx5Du%yTgw'
    'nzMr1PY-yzdM*IC0Dpx#a=C~r9J56V^fEFPrYY>$K{)}h01xNRblbbD3|#-X4zS<G&t%Xi5!oDk2OEhq1qqvx?FenUz^mxtLl3|nIbmt&#;eAb'
    '%QZQ3CBxO-ZCkjiPvkF&@9psy5R~=y^7(87q7*N;=z?6c>vZ!PppVmsm4FH-eI6XW^5Se0e9{Gtp=obv#TM<3q7vVuvj6^~T(LvqP~UE_lu~DJ'
    'oTzma&*-&9e3dddBnob#e@Ys>lFw_<L=>xtB<ClpaL6fbZG_X+r>nobJefW};Q!&rjmdArK9p2JI7aeV5v)ZwruW#f0;|-~2k#FT-xCpf8mYBb'
    'Vhy9{l5Kjw8pv}_Y1~*Yibz-ju>@NNuq|GZ9sqlR=X*&&7;R7$WD0l-FW-X7Y_t_6`q+wG`OBB<_08tvIBml8E`j++K|UA~LK9lL9z!hyX>JLE'
    'n7|PrC!}7(3Wa)sf&;2T(z4abxcWxw%|3lO)oUr;T3arAb%h<7s7j`>P;G}QC#X=}0sAp0w$W&Mh18zGzJ~DPeutFW<pk0`Ki&)q#TF?$Ou$#`'
    'ZFCo$^;l^;Uc1oBx)wp14!$~i*<iyldmyM)bLxKAz2PojrKcNh^{Xddv*+YcgmoHDlUqo9oP;nF;Mwjcn7$9}OPNcisRDA)+*(%N&3E|AH{Qp~'
    '$Ds)E@`l27+K28An|onfU=PW8C95Z|$}@r9GlQ_DVjk0BN#@~;AW5Th!@~A%{;ER!{o)#b@@9u-?QuDCu*tJ~d1Ug4j2CN0nky8~Z$MJD(+767'
    '<_hu$NU_w`Mx?(Dsb=>wD|;|Dtq3{Y=RWlH?eVj|{f^4@`Ss~EBk~>6_=+rk0Hu3G>%wGTI>2Dayq@o~<0vMj5-OJ4C-Ta~`PDx2J$qFDQyv1a'
    'gR=y$`uu)+^3-+B1CUmvQ0_Hq6feC7PCWxi1@ooJ1Snn{X^-Svvnz5*)C`ol9oB4D?@zw&otqE?XauYhPS#+%y|P7}X<fm)NLp~hCTAxAMM7x+'
    '$)owGm2Rm?Y`137U;bG1{T=Mqaf=k>rJ^+*PkYWIfR-$ZK(xiSM4?_StsTX*R;ObPp|dE<bjfA!)0^6LUB<8ac&0NDt1p$03aY78it-jK1!c_u'
    '<45YUlBt0|`!$vCEy4z$2AGu>eslZKaA7C=$CuaBTP8rG3_4FYLp}lZOj(PL{4Fsc%M17w<=7D@^^CG>i6aDMuEuASU5?0CxhH?$Z}@xjhv}Vy'
    '0E!R3Bny7J1C<I?AFHrZWZ$B%;+7!QVZ&8y$ws-m94ILq@SA;?-mjW6{cN9K-hTh-o4Cczuv#$j)OB{#DnK6_&nhS~J{uDsU@Zl`QJ_;DieKeI'
    'foS9WOQ!1ScIYx)ykI(i(bKE<Zn>oC;*3}Wk5Ac?OH%*~6R(t-6F%EfbhsH%U=7WjA8Z)c3JjTd(^GP>i~P3-pofo_uTPS1XVd9)D+4)3KuJeP'
    'lvF|KkzfmdGEi!f1~UaSk=${{yiHd;&Lh%bSzdM5czV_C^dG@}dB$HHKcCY_-aI2KM+vC_^M<GkYr$)0PQXy&0-O;?EU|Y82_$heAtAo$U0rk6'
    'fOm~j_jwmNub`>jGYTM)xE?A#t}mAB@EwW4A_sb{G{3qB`=zoE^*);c!mhQ;4)j~P_#Dkg4v?N&atCc06*Ps3TGT=@aaEhk0~xJhi4^q||CyBo'
    'N*>2vBlh4#f&jxTb=TK<)j|5&LGzQ2VP}RQ+6$k8k(-Mz34|V-2&ruxOr#YQqcN~>E)oV()_AewpYI{aXihe}tfZ@Mp}&P=$3y-@e|dX*`PJsv'
    '-#%Nc*fkx!X4qmB*bqLU9<Oi+7y&fv-Z_pt)Es@bidyEU8<j1hca^R9BQfX~<h!9~))I~$nLwl?xB#%4A_wHXc3-J#KQkEdXk#J8wZg_?4ZUPq'
    'M91ZU0(X{V(OtS;x4WE-gaX_lFVxOqYj}<!x-|ld(Og7E32eioAft9+5f%8!=mcE`(nq+%;KP3gH1$Kz9=Q25=od(C#df2^Zk!(3c>!F95)UtK'
    '*H?4RS_7@Z!SnQUHB_**vv~rs$oDu+zdYKnC&l&997;CmkwzhgoLUA!a!oUGwUEK54){YrVYPYC`#!qSd#B3IH{GYJM9{y*^#1s7J9EFIOuKVP'
    '-#(_Wn6@20`BEF9vF{kbbW#8deBI@Sj2)Ks?iP?>**$xtc#V40<MZRkBMADx0afWlyA_*oQosZXSy9V|dgB`1A{I85Yf^$`8b4c-ezb~k#<%1M'
    '>;NVhcbAt)DgGYZ{JdAS0_HSeX@{aUk&SW08;*RSAqj;FI7U>sV%;GrOqqUwr%=46)6r?i&81x()3g0{lCFE<bIl;!Y#!+uh(HtA1DLt?T32z^'
    'gUybJ)ob`-7E6oTGoz>2cHZLQGH>zk;k>@tFQ?|)9O1PxCMN?YZ+zzDSf&620X1}vGpA98bUwh*LLJN_w+RH4cL|O>_ws3qr>9fhKS4?hl5!s{'
    'Wj})1#AcI}Kxku1n%(f!$S7+FtyOTw>ZB2^qil=qoCB_!%Kl40&%d7rlny=~1?Ptrf}<f5TV}^%XnquYc;sjLKVY$DUSNbl%nbatgkUM`0=m2%'
    'zdt_ns`cIUZJhKl3aA4$t!m7GblJ~_UnLY%qg7%Xx&ktVryBzOoGBF`_#xnc6}GDG-r#fa_n3EKSJe&0IDG!j?CrT|8PKN#!R4Ge`ww8$+3F%m'
    'eH;~Nu7LNI24;b>w2(%2tt}yh%g@vku=g84_^BnIABIoI!E-4S;UqAj(!0kmny%y`W!mY~w@_TEF6<_?4VSqVYnz*^$Q-U3ssA&qpY>@yyuQ4n'
    '_@3P!FJ$U4nZ|+P(xQOjDoBXjo`Nh-(w=?t8Pt7Q3#((hq6o~uQH1S0Pjq)s3Z3@+@pxvTMcc1D8!GGcAqPQfK&CGo0#r9kbql;>n0K<II+t<S'
    'Jm*~iFlGu{5tMo-P4a>CetwP8=w;vNZPuKdBLY`oHC&lsx^n`%a!h%G>?uW16#Fn_L(u}e$UUd6*kozfr;XQ>g#zMhex_Cv;Oi*16s1>I=j*NY'
    '*0IZ^U>yUyov}_1^|dF%I=0khCzhO@yGuL|Ume76XeTW3)_~2Ct>fTzxPbI&FMXlv&e6Q{vF54c@ZlRpLP<kh1SjM?!fpsYUgf#^XKY`dsdjpx'
    'r!$CwY71<kZB3HXs1pgIAD*gFa}v}c5gn~oY7!zk0fJgd6ij4my2rPXJB4-Qc4Y~BU|kPE9XJRAvBTDPff_anLnq%Ue5XK-C}7-+eEBF9m1{<~'
    'lU$ITuf1J4e{b>)=e^~W5KCM(JlJ4Z8og0R67(<N(+6O}=1{SbKzau~v@%2XVd$<5gYL^!JqBzQ`$LfA+b<vA`K5jsmhdEd0K89-4~k8+RjdYp'
    'sY>~HAVARj4r~~^D{S(aq4rLs5j+AZ;LTyDCK<|g0~23kJlZEt=j;tQg3mH2Myn)8X)ADgugMDcU9BC-T%$|CyhVg+2<mGE>*7s7+cpOKlCKTv'
    '{rBgOpC3McelvUcRewKqpYsKXX$E|LX7vo#K2e5&EPAIpNy7f>E-nW`Qk30G&fzRJ$)G8Fz<cppeGln}&5Rw1qVs()j|g0}bmSt6yolT$mxf4T'
    'DUpLsx(eO}FmE7~5r8q<&V7P_<mv|0$8`+2eB5Y3l@g+JJWb?S>cY%JJFWE=NV6USW}$#A)BQ!TRp~Rk@RP_Z6|D2lita*<KYe>U+kn7AMvX62'
    'TG~M|Lx`v7T}K$54&umz!*O!4c3%}Ia3FoenQ82@?NCpbsetnJ{ik<ISlh$T_Vj9RCl}7kCO&AEB9pUYsJ_{GEDHqyLBC?D$aYl%W#vpW-{(PB'
    '($QFqnzp&Z$n~l_+OJU^V>-u|2lPFOiwGheO1YpSl`S6~X%BR{#?(8}U!>Zxs=DQyaD3RhMM_gm{5!VwD_pu-UqiB|qm%5c-&8p6fNC6YhFnAD'
    'UOBT*tn;Z8o3c7X9z@=+4d5|zbQs?W3K?{7q%EmjAJpUXd>m&h)hhPi0>vG5fW`-wE>8Ka*ha**q%&+nP8^=nnn?B2;hce_1S!_bwx!Bn{3`Wr'
    'Civmq{`<>~0l!iUTNFLjWq~8c%*EGLc}H7ajFi2fX==r?R#!aIVC1A}_89@{?n1iNJ1p0YW5wBs8Ux6d46o(e;8BjKPklE0pNRFsNpUR?vXTZo'
    '(wAScF2E++jI`9tRI2|H(Tyh%HJ>IDIvw$5LOO7iH5NA5x*QN%;b<bVdEpZ<Sa9%(PgScmO}cYOs+SGm|0SZQM`Xd%dv=blAWetmk<qbdjJgTU'
    '0n#>GG;O501ah6L9N+V_q|u7q%7=Od#@K<St7!iJit730;r&a$!L&NEcmgSOD4^7~01H8VZ7QQ3O{YLnzTv!~M%6M$p53QbQqjT(#;urtlM5wk'
    '3I3k!x04ra5O+?MWBX<5AJ-B<hApH>3)@~%Z)dB(MUI|+e$@#?D2;ZoDfiuwnD^~IhH8%p>v24O95#PT=_m9zhD;-`Y{qaIc`$K4T?=p0ChxC7'
    'gCn^xZ|JdGC6+i)z4|DTuR&A5Fn2|)+}qah&7NMsEpLqQlrWL*NB6wcXltN6qgIz74&{4MN=Fv*M~}!VU@+OC0H92R*$7*vw{)f4!`0>MJ;&MP'
    'bCZq_%SRPpDB7EN)G{!aL+CIJ*9NcOoa+WqIm19f8?LO*+V1@*TNEr8-*P<KuwNhF?a%FbR^0%osInzmRO%D}M-DBvi4&IrL3j%+RP~I_d~2S4'
    'gFWHImX6rto;_cs;>%lruT$Oj+w0rI_<p^82DGRYKj?mPG}KOWz`JP`r&<#`Nl@5BP|(R7IBL;XOREBCvH3^0PhocvV_|~{3;5X+-vZKYbxt$m'
    'a~E(F>Ra^F@`{Di_=T;iCS4~d@~KX_sMcJlHt*Z+Q90E6Sl;T>uloD@Lx22Wi_>{FNp7_xGc1&YTys=q>(-n>e5SEYQ)7e1tQ9G>7cjOoIrkU<'
    'INNT7DW!a$5dZc(XY(;8RB84uAh6I!%q^73I}wz`wxXl0((1#E_67zFgKd16z*O5!6~~J#4Q|v0l=;W;@(C&Q=g0BQ<M(#hh>4@&yn~d?);r%;'
    'Le}s-<Lq?=nd*#Tfi_THcvQp59XWvsvQ=@{6)Kn0pZ7mZ5kc*4-H98oz=gw)3nh=r1D&F!AbZry0O14ob0N`UBqR9_Bp!#Y3|IqbT4by8e$_kL'
    'UxNEO3UnhbRQBY})tTsxuxyRl`oivX^mO!eRGPeg(Ajew#zsBtS0huGhI+BJAHBS-U~#G+-wfLN>HLx*W)!<xJCbFl_HS;;puM<b7B7KvQ}U{U'
    '0)jNG%Fd$>cxs_`&3)`Wz^<}O`QD@REh5TwefzBwN*<*suvZOBsp&camIFoA)TI?NpW(sZR$pS;EPJ2*oTbwrhT;?3S*NS)i~n4>&yUY%W*u~M'
    '_jN5DRa1$rv#q5X`=``G%pBB30v)9epcq$N=Zh}0il%KBh!lT^aBkmoHDQYpRWm9`Pb@R^)!A016mni^Hg>vD8Z=cz<4V>n)Sl#s5dPYBOfuf('
    'Rk!Q-*RT3^(ghMCl0|YAYZ6ui(MbVZ0O6WgOdUCbxe{A1MC5lIK}~5!kXCAX*-nY3xO;T%Ah5?Xr&y{H_@T9@!QK{&zE9;QY&pA5^`H_;?w~!B'
    '43~<pR;>WPbX0@wi&Lt13F-ONW3H`*WF30%HUt%nZh1Iq8B~Y8I@3igl4C#f;Okk{Sy0!s7dn}3^}_GaQ}E^NIIU=ui*!e4kw-~Vkg)+gxhfSS'
    'c+_a(PDLc7ierr2XosVHrq*qXtja}I7*Lo>5*&0o+i{u3N#rHogNrTfG2}jrbhqk}+1w%P0X+_!wH(3Xh=LchL>?{Z+KF$K>!NvmdA+eVSQSU-'
    '?lo5oiho_6#`}6&o1?_y=RaaMd`GL%fB|z1iMH5nU}0R`4Qs`#Zd4h~+snu4@HeWz5d`4$^nTo8bIfA!!;5-1Df`S>@8PBk7N{U$zNn9EpHkUD'
    '+2Ahpz(PDf(&!^<Tg^Z+y7wI`qjM5_gF}}J9A5fpr3qFJ@OG4e(=`euPqPB%cHL?8#k-_})8iBIbH@ppDI%Kz3OJ97#F=8Y83L7;?&u8CnH<1L'
    'K&~UlXh6cSWM#KX-p9*hI%wp{tJso>>ay6FfloqjnS}TdoL1*Jb7vI=NkZl0W#Ad3wLk-T;!E=;y<vsjT>`qrF=9dv7VETamBZZx=N>S$AvX+L'
    'IiS>1!<4ct6X{|`tR)t$H7s!SyXlz}E-$QKXmow65q9<lY-%W$LdN7$JX(poBtKGV9HjvWcO&UCtY`LMgCJT0syJi!afj{7<p_K|KAr5oGQrtL'
    'r%rQ4#RQ!gl*^s?C+cf&U=Co5EnvqX|Ai)qgVSBHAbXUu_1bOj9`T&vA0+VJq{M`MHB+7o=w>!oMmgTkeaQf=X_%Ct1uVEz&cvax0{W?rv~AG4'
    'p4^^VLnum8p$xyHYtvv<j+ZQ})c^o@+&EFS-^Z%pN=SdS*eZ**t`0zBlf!+;_lV~YqJEmok;f*u-KwP;Hdv;;Daakw(2+PewvmPxSJ>#46IetV'
    '@en(1g~|(l+rSpDLk~w~I-%x60ZJ(e22kK@aOw(DcPi{>Qx}=lgk#1Md<BV@1hzpTF@Pq}b+@zWk!#ZU_;viOZ+oe!#>Xv(k1qIh9!vsNhUF7v'
    'CrdQoRi8(q7PI97uopqak;w;}{pVI0F<b)}5Cm^Aw<$WBOF6JPVT?w13V;JaavRt*R-=IXfH8pi#TW__Gmg>%^!E7fFm|H!p<Z((IZk6ZvJ{(r'
    'I3PDoY4@IR{tJDxGmt>(C^Ks`a)n7}A_)%PS&-+P--(Qbbj{7kIG=@ClZ{q>N{pFp2|b9ha0rWIrDh{r1*u%-6Fr~kMpsR>_Y8jRcH1fZ)7J&('
    'pWcBhXS2fh3e<Yu0%}uVR}dsuB}eNTg8Ej7r75uqd^V)zFi`9FLll<)m|t&1!F#kGs!N0I_kr`WoXSQ9?X`CGIMJYQ__#Ba(3~7&A({{$&V$6='
    'b!hpAJ2P*6{DDuWk@wZu_s&Ob^e)IcWzE(poJrS(1=zSc9|r@lGC){QP1*wD-#IX}YYYmGq<W@kQbNw&VV^YAx*kWNlxd-A!JPQa+J;+){lW}<'
    'K*2Y3?5S*S<g)i){D&#a%}<X+<Z7>4X{eCu1Q4O*3IK^wr(Wp3<pwRt1z>kIlxR?v#GdTcHiV0fVt2tGc7~~dlHwur4x&*2X`>3C$$>Eh%VVf<'
    '*!#1WWGEXB=ut9<MA_#ylk8A@tygu<{~6P*fl8&X@`|egg_JpK$P{Y<8K%WvPrhPR0xCHve|D&IZmdRT=Eh8@CcDlyNtdfq{bFzL_Jc={>ai*c'
    'x{)$qa6%RUXv#S0l_pg!1~=(>G*InPHiRv2(K--IH4x$5C5Fec*UM))_|HGb0v_J%{mawYV0jD4(=JD~S(Vs%x!!?>0q(umq6^j1X)d%X0U`@M'
    'Ai1&aloC5(S8FbNG5B8^b|;S@$j+$zLqR0R0M!qD0m$<L3b`%~pqELN9Xbkx9#`b$#zdOoSVY}*X48Z0TBV8GZF^aLn%Y_)b~tzp?=8>6sBbx('
    'q1q5F#TbyeD{>^5e@>~d9G#2%?i%od3q8MYhNG3D+NL8er==uLJ$Y*~fSz0Peaz)$;jH1{4JwS0Yg$I!jon{+Z=-y%RLk?Hr_<RXN>4eUNRnV0'
    '7G)fz<?=!*pbqoU(vU0o{MPFseeTpxOSB!0O}7&^{wn>9Zvj!K<xoR=TY7edOClL(km$4Xh-MZbWUyj<eym0g;ycQ%;Ty0i=OLJ$;&Ax38BxwQ'
    'op=CK?OGL@&#z~b$=F-oBZm(T>ULk~;x=h2IOb%&s93WWQMT|sNhe#BLuepz$m~J45`*`;%vg_a?;oc^4NgUZgqkN-%TX~Fi?SlSMOyMmf-xrW'
    'e&-7i-Z<VG6-+v5%=l{OJrXXfzyA@~+3gH)YS-gV2JD`~MtodLCf&Mos7r=*inVbzpjN}WPO;V;4&n4U>rQ@O!kr>|cz!u;k}RGYRL$dnn9jls'
    '2*y?^6;$lFEJT{`SQSphYfZiz@`;X8kXLqXgst7H_2mT3R=^-&g$}e0lvJs%1vZV(4dyzrNX<D#ok|m#F3W6LaqL<(Hb=T`DsSc5%=gVq<)Od)'
    'b_O3PPE)b}fXGwC;l~Gdh1Itw=jg+vGmN$pGB{|+B;IhnSgN6b<ZiE`@+I%2FRx!k8Mfq!S}h0JBS>w4shQ)DJxmu?;-ezfk&p^(0tGFqTjnVl'
    'sSi=O-3(2RQQGDD5<lzv$!4p7I+US9!J1ZtCnprTzI31=0HNA^O$f(GG1y<$DCpFrdOL>VcRf4xDy!nZMD*p*r1|4FP9Aq^<{;rjB}K;WleWE;'
    '*}3Mh##8{KC^01i`9+CUAi|1-p26W075R3LAy@0FK0ZIZKAoZmD(9mn`7{8zY2TYCm}(0G+8zP+28t6nE!J)(oHo=jteII|w9JjW{paPHKK^fn'
    'X<J)^QH!dh&IoUxQ>_5lwT|gXU)}222f@jz!J)$Xphn0C&>Ci|J7u(*<eDHp^Y8CcbSBtFYDVD!jYVaxNNP5)Cgc^s2}@}^#EI2VC+%&5TaDsX'
    'y;R*j8@kJeei+10KmY!ERzeTAT4!^~ac2t<04SP73izEQ@f6GNODC{GOVop;8;1y(5N{dXCO5NH<np#X{q5|6%kog?t4&|&;<Z$8ri}yLS)-%k'
    'rWb&@IK_F^R7q5k?p|l}&HW#BA?k0pyAT64AGA26T^jPN5@-h3Y2bP^8m3&cf+v;S1@ZB{Cj%lx19mH_de3RwW5;Pf)g{j=-ceD-31Uhs6#QOM'
    'kR3$gss}*jZ~;<*P#ATA3rXXHyhh!+GDM#)jKqF>eR-xplRo`uswK%nj`Ge*3DUF;>C?I7y#o;sx*`+y>})>D&C_03Ma$v7p|W-w9{DoO^}mh{'
    'yuQ7BemU!;8nx7pO&1SsL3WT=ux)DhdMf-I`}9ofOcU9lvd_0s0lOh9E`Zfd3nIS5U$|iWqxaKU)b3}Ly6_W^ZHgcva2gY6x-7q}^#-<(Q(FqU'
    '<g^Csu|(%biOKEEti;QOvAn<9_!}F~r#S$deOt&29Y;t<A3&m|#emJ1yIa*oR9Znb0tw6rXGI^7JX~2icJI~s%JjgVR~kQ`@b4f>Z2=q|ZP~}<'
    'L&ahNT!RPooC0!r90iaXw5ZS<#PgD_c8dRutz~?2;lglkITj#g<HS=5b_N6z*2>EY>&-E{UMSZ=I0_~~*OuaEi%Dwgb__Rc_FR#F`p$=N?c*ng'
    'a+28aS<(=I$f<M`(`1xinu!uO-TT%AWmkX_3N07{2i|gR3QDRI%eEz@SFFc*2#pzwTIuF9Z>6(<WCAGUc{q$oml0+J^XZ`3-h#(sP+<yN&@AlK'
    '0zB<O{?Q=LKRs`5q=iFV6C9;bm%>2FCcbuUd5||lQjYknj*=Uaz~vSY8&)Ict<<wmmu6vmdwY5NQQl%@PXRywlv<|Z-wG=pJ`fz+ozy|4i%AZ='
    '3m9~*<F!t6eE~4r&V~SFE?*ecnvZAVLa6P^&PYP$+)Oxs6?)`d`=kUOz#5MGLX1S7sG0%iqb{6f&z6uK-3*t8^7%|E-674!vTO;p8^Ha|Ndq?1'
    '<^uAr;-JuQwKy7=NCN^$t&wZ<goUlL%T-OeM<k#0^I0t2fGvrT1wZKGf&4qjq!V5A@|vTeMuK$a3JBv;uQ_p~6@>@TG>jOweIIfyQ?s}J^7Qz_'
    '5$eGXG#S0Dg1n_J0tWlgSW{YF9;g{a2<K|Km{(G3t+Ro0W{^{@yC;$mE+wJID8;)y{QVFGu%+|ZPcYGx9jayZnn>C*FK}GsC?jI>5jlenq%Ai`'
    'h4|rz_^qaOlskm@@%H%X<@x2a;fo(enG@3)4q2|!IbjR-0~lZxfNs{#;cC@T@D13r)_D^WC)+;RC?9vKwK3!?7xL-llODYFeA$xXL<ztpy+}NO'
    'XK6tb0&oWMHx;BJf*d8BIdGwx>Iy(W0tn$_-A+$miAFgDczUDYW}5+&tsIHFSZRxc(Jd6p@}O9uOl1I_T1t+o07tM5F4-7_*E^MoP~4T9@bk<3'
    '^7L|Mm(8xw)~ZntgSR^PgUZJK0*Y;@PlFnvJsNU}Hmq8v<ATFR14f$LtE)?^)SG?Q$LDz_;99csvJl5%29#gE!66}9Q*Craq@UhMMqdSdQ;v~h'
    'N+9D2B+wCSY!|0pN>0B#&&TIaKS~O{&8Bpp$1>&NJDt6e(1S}#fu6O5&;0~(nfW4Anh3UKm8KYBm)&bC=~4>x|03nh!~KFHEOGU$W!8z4Qbq^f'
    '3#stsF_cx=&u?=U162*PSISHRP3=sjL%2p0N<UscKaPDvEH4q`pq;N|1#0MJ9F|~%cR4n&!#bs`*odjcQ<^MPHz}K;d2M^d(^U#BU&HuGpC0wy'
    'PH(T+y^qfTwp|8at-80gdS4^@h8l&RzGmOuQk<aJfLg(w1|{Wq*RvD$@K>?!KjJz5aYFd85Rq9MxNkT&olL$nwi=hQLLq+-OC`4yK|6%K@-fh4'
    'PXe{#cB$b)M~uJ5^U$$KXG0cl&R9BDq*mheg|FF<%9ce*W)m3^89olW>J70bdBt7|B^h=5I!xj2dj$3V`|~)v+(s%0qSK~F`K-v=&IuCVH0;S%'
    '@n7eG6)K5`P!PwX6i@uIFD}iobNar@IqO?Y<K_8-cfv2v4`*&*j=g0_50pq>s7^ki#sa$Hck)=(^U6d6z|btoXDb1OWB5XiMv(k@H<Q3WWBTp4'
    'Z(km2YA4vPMasiPDzrL}pFafy7V1u>Rt`PBH7&Xvc2f>DR4J3A0>YVeq>3GYxUALeTVP-9MZcPTpG2i?Q0Rc_z@e`;=5P{8Y)#V`bWt^6Anj^O'
    'Gh(hHwxpRwb}Q_NMo8(>pgvw6K8<lJD6pV_>I!fzVC^9DfN(R*at`OnM2b&1a9FL0-$D}1xwle4uN*4a)@~;Ft3=?(&u;(^Kg}<1H>~D`!p!U+'
    '!$wP>dVR|36_lu@8r7s=qxsR5Bg8<23J68e2TUk5x81d6`E-rG!x6@|QxgDuv<a{<7po|;Sv@|iDrZZ(Yf~uDQ)g;1hZ?jZ;t~tkUkzK$Itf?F'
    '^|0Sr=TEOMPfriOg76)RA}{Mq3n4nhX5g`Hxh1XT3RK$p(p6WRsJ6VS2e=^9jd{*C1S}p04I9<X9jL2U)!T3T?dQi&KeLzn<>||j%g58p_}9s4'
    'iq$XeKt13PV0^>pW{03NXVV;;Ph&)eSO!qhXc19-8Q`~`E`xb<1KV=AG`RN<EZ*<8VgkHYpM3NYlp<yhq)!WnB}7*&(P|w)HpJQK&k{;Rl8+oK'
    '27dKzZ1p~`l5+bN)W_pz`zrW#0+5neq#Z9?gx#z%I)~Mp0aNIj)(v|TRjO(YG&AxJ@F%zK6X<>2HQ&oc?InM%)9+ZR&$rfT9(O=e+{D^Qsurjr'
    '3-BOjIXL8r0S1LF3A7uyWJwURU^fDNY{e`P?lqH~T%WzPT&+MoAQNna@i7|!A2u2`9(K|pJCxSS5=sGB0t^Irh#EsIIB&Ki7br&QidQg3aMEQT'
    'ImDT9Ho{1~u|rX;#)=|@V?*~-{M?<3#g#Q`(GswuKn4MVz(Qi$qJ)1mg!9jyYfn}cL>gIoD8M#<*daMlw?-K&8im0Fjm%~%EtZu*7-k^!5!to*'
    'q^qt{zm=W$FR!mJZ)fuJvl3+vx1yKWLb5nhaBZaOq)9@><eZlcs)P51v?O_A4V<;*tJ^73T=q!uKjZo7_3N)s_Sit88itQKJvPp0E>yPp&c?$$'
    'z`5y@609B<c_HdCT-dPVYMpC$<y~EnOY_%X%;35I*s^d25OPkL7)OgVopH3@JJOlb-pgta%L=Mx_8HiSw_VY70Hu~tmd0*S&*y7mv$wbBlN;1_'
    'cn$I5Z46$+0jCA<0npYMLEKUYjGp-4h&79i(?qhy6dbfh3!|OF%RdbSZhU%8RJ;zHl##$;f3cA~$Ic628GtgoN{=M1YZd%>W1kU;!8O-|uNw#*'
    '|KWSL&W5BL5p-nQN}ILiW#WHQ3aE|XkctgM%^l9*T58~Mk&ntb(QOkeHjZ16wNu#nho|d))%VAr^!4%N6ilfVIEiEDc}OjJr4fB!$1|!0EKCA|'
    'Y#B&W6L<lrX7)HFA|6a<lZi_W|8Nu1%@ifs_+;9PA<t%^UmAXd3B}I=BZ7jh&Mx^GjS{*w1vXNFI2VtyX}ddjRGWX6g}vi9=h?F<0H4E_0acNa'
    'S8S`KrM#&@bzFYLGQcwzGE!6JtY3FUQEYg>TlYTGm(7fPyO{ae#=jo^#w`XYy>J|^BI{7vm91<jjnUllDEq)#sOQAPE`;R)6<J`WA}cAafOlOw'
    ';o1w_KXs>xejnDGonbGaF7fpLX()}e`ATaA-U&Js{tOa><LwO~rbLs4oSeo&Uf}R_^E=%@x$4&GFLMenulDsq<Lv3TIt@fIR0M1sCrM!QrEthJ'
    ')V|83Utf*vPHe#zZ6oQBlz=&<k9~W>RkOl>iR+sw@a+MNH0f#ATWtcwl|6@p5lRFgqsfHFex$FqUlj-r;H5T)bUkvfjx+7DZ5pfhxT^laUaW@A'
    'Hia)$W%xmZ*^#7i2!C15mT?YIXgRzT8$DY&KckDriymol+*4cde+IUm^wXIcYjy)U5P1(-G1unH)Z!|**JPiJ1S_e#2u{tRG^!>7H2}X6hNre!'
    'tQy;07VGi(@l3jxxC=mY4WkgvK>7gLkuc|CUsD6eOqEW3MlHbzxDy8sRw>4QblHk?UVBr$ZWuTv&YTtRrnjEU1oCa5`|P4`BUolTpqm<Lnd3$W'
    '6s41pPI181y4!RuUHh2zFY3BYd$s3@uzo-508Dea*{V1&G|H4KMX_BMFPu9wi(xwCblx78=6qZeB{{H9R$SPLca)1pn!Y*>Ztv0PQ6Eugke_XV'
    'JTH8iQuCe~sS_2rvS-9wY|=%5cFf!210^kh+U9OB`)fwc{*ZatiwIC^eHibCI{w#3?~t%K`OK$*P^iyS)Mk(py%jgXegN#iL~+!2^ekTY3i4gJ'
    'f>+YW0q=)O?@bX4U_9RN0R#b_YKw_<6u2*Dx2&TAC#4KY@QIRj^d$in2V4>zq~vl>cZ2WYEl})_zZc)ALpkP{0#%7q0<J)|;Ik?#5a(==z$@+1'
    '@iQyz&ISe^HHlY_xo_>mu7ZDm>W=)K$fgH~0MC;cQHQ#K;;~&m^Zl0!ILiU{0dDAFZ=i9`il0NE!qKplcRSOC%N13={?#8X+A&Axh21WVtxTFI'
    '*ojT2yyfv(8mKNRE?A&joF?|OTeQjoAhvU$u2(gYd=Kpphpl%$fj`^RS(A?iA7lkxbk{Oa5#ZP%i??v%C&+F>#`IVxJ(NoGLg?U&wXw;k-%%U;'
    'KNqckSwBi9InPeJkSu)f$&q898YBCfXf)+P@4K6sJ>FTVIt4`NsGpnt)SWPUl&ef~zeji2yx)#%jnYO^+VVN;zNHlX5CgQ0EN2KWlsuLK^8(Vx'
    'nmTY`1c#(+1qkI%=I^p`um2g??cJC(BJGU{YE_mXv8yw%uS&sb8V}N<h+>744PA7Fm$d{~(Gf_Mw6h_s{_411{#FhNjpkW2*NCBez(Jo|Zl=Ig'
    '$>?R*kl=FyvZ@|fv7*W&h1MJ=mBe?V(;*gjpU{4M()fx;q4`mYTdk)>0|dGQ1gQ?-eQ7?doYRUHrM205!(z_{PMPBlTycKp;^+?JI{xYe%xj|o'
    'A4T49w6JESvcDS{UqMU~^?pL@sN#*zC=U6MgBQyy8^7?qyVU5yodn)+itPPtz|x%SYl0s0I-EYT8BiV^Bw;%SRo`drNPjqd5!E60WSMt}WBP)1'
    '+)?9vm8Z?0<xD^HY&ahE$g-<((g+yYdlZj+?~35aIh-?znoz@>QZB4}tQBxQGGvmhJ0U)*)m<FU4aKI_z~GB-<^y=J0nLLV_XYaG234PDr1`ew'
    '#Ssgn3YxakYKX6Gv8~-`Pl#9VyK}?b>QR4u8C^mIsghzkq<WP^>+?28r5Zrd$|-XW!y(_T$br{Z(yDRWOmoR@`$gX#_4#9q0Fk<TWrOB1amdY9'
    'DPipSxoepKN=jkGy4<2M!ia3h2PaOYgrH$}^HSwf+w-rwOrZ6@ojvq%l*kIT!OEN=*JF-)j@GG`6NiC4`e+T=!H~;6Z>TAAmP9(D+&%XDJJ_oJ'
    'aar;4a4uqtvp^$yq-x}81Jf|0$5mG<74?dh#8yPoqY-t-E-QS=1V8B<0d|si7Z&yU(2lPk8QbGoazMr!Rg8l4;PY<K)*a-kObwunRSunGqdaFG'
    ';%2HiMO0}?O`P=6w<=BLTEU$4@<xZN!>swtLe3*0AcrSEfcH?CKoWV?d6cSYWRxI~E`xC4NKp?GoCOJeja1?`N2IGPe!pc2z}aWOqf>7i&FnrY'
    '-dDlOROhJh>=u&v0C_Z}H42r`Ctb^CTe_Nh`smHNl5M9a+%-clN4nUe@!<w-H3v9f%qab|6&s-?0Y^d+Ee(f>cd!6*!LO0=8xF^b^R(6F4b3Ro'
    'UnM;C_2um+eL8%WU)~<{?bB)7ci`kQm5rQo%rtSH94FkT8c;=%#yI;gvt!{TA}_AeK#e<*DssKs>eXKx*OzzWE8Mr>PTFlEC4YL1Tm<m=7-;3t'
    'EkpsC8jJqtHB|t-Y@Be8yrZT}O~@Vuc{hKaudPv!pPpad&Za2HS%Kz4&o*lv$>Wd|Hfyj2JQ*D&o%!Zal%+UY630?v0&S`_@9vXYyMrJ0lm7c8'
    'b%PNB)<~_<1CH`GR%oG?aEKaWFgI$okxexLvUX4;YO87qz9G0RhDdj)x1aU*{>=#c?8-LL0AV@s1D9j5K#3#vJ*@@cASoW!nSn#4#!HIKxe=Pd'
    'ES<78?J9B&oA_3bw)if4a8m;TYQ;HlhFo~VMR%^2>Y)Y_8*gfBEbK#d6sj|jrojr-b{7x6w2NJJ-|Xq(<@Id77bhCkwDo}s!wD-oqm;%87xuO<'
    'ay%5Bp}~9!jw2(;A9>Jio{tMVR^%>L*3<V}`z#j^oSlOqWr=#E0c<Oyo~BDnnB(e1sYMP<Ii(s&I<Pd_9l+g`O?T==FV$aD_Wq81;v`TM42Y-G'
    'lP?2ptBXD&4a`P)Bfx+Hj)C7yz}YHvS@XPawWYhhmT=X5>-UO!DgngK2+0(u^Mzx1lz0Z2qcM$yWccWTOcl_RkDF{r>Iujd7}X0k(l#0GDg*BS'
    'ga;?L;RO{RxptK5dbDMJU|XqE4+7YSjBY~hroO-vQk)<U^b9)WdW-BvjM`=PNdGe;d(e-EC)2aTlY|1nEwGbYQ(8VZPy$_R5pGf$x?2ODMr@4}'
    '1*7C_?YN*zW$Zetd<^%3ayONmYW5D>Z~^Fm>vd#qa6i-}PYVq;7tIhBMWJ~MH29pcBHbrA0K8;-Wn8ZkiT@+AFCQ-tucy9Ny;E<_*TPv2upIG;'
    'Q*5cR!4_zZcXfh@+fd4cIPExCeXPWG_U%Ymy~<AQTR@M`FJC^+BLd)Ab7aj$659uwB8wvd6e@D_b)xt#ewHu`MN*lvQHkhCjf4&ovTJB9*Deu0'
    '{F}}5klOT;=`#&BBs5K96j+IBsIY*je7gYFaZ)W3bT|LC5&jT$0NL1aqswfJPc1S|j+D<arcmY>bs&|qTe{a#R?lH56;D7eu!`UiNFN|m)r}#J'
    'i1o0_*j+&S>-*ilJF`CUV*mB*TE7P5goPbg>TscP?xWxkVKG;tDs*j1yRwN~f~-!f9jPAKo{zlh!zQ}at`a`^OLV`Q{_C5g$&HIIq_&*2I3JdU'
    '1X@(5qX~NDSZvm+Ew(ZfvY$s0JFqQEcH~JjD#UHDxOe^@X`YjPpT3+Lp=Wh80-j(K;22=!Gug3>An!6<ZZ?#+D=twXb{6(3g3Z(Db@`HZJ@Gk|'
    'JJqjg_;-96r?8`p0df@F$gK?XAbktLrRj5M6WL>ASA9bkVgI>$8-1pTs+<Q9SV_C$9OT-J&s2il_|6=uk7qAvogF*Yz%i&nk&b3=@C@N~2_+Y!'
    'vTdw}46xxEFh}6LeF6wh0pGR3he)i~-m^TjRq+v^_umNZyZG;^h?3L{NFNJz;=mCXwTrP>NcsYXTY8+<eC*MA1)ogWA4*^}iH~LLL6I-JY5t3n'
    '@yjnK`J+jQxzts>aN?AXX+4gX4P+Mu4a-UCaykdHdB?H-Rpo?78HhgAoqB$i>n|xD-ygm_A11<FwYRsk(x#pIT@mcJ2i`4ZKu2UprAvAfviq8A'
    '#<B$;4i{K&fL?jT*fR=Uyyx56AFSAZV^8A`^5fBZ)Ow#`q_SGpP=G{Mi#<e~CTF?MxbO#r^+9;~#EA(`Lb0WDH*s^F9^m(*_TOLZ25RIL8P#mq'
    '*q7%D-TUYOg2PPC6&mz}p2o_vpa!_<CY{)FUKxuA;<>}YE!4Yg#=o%@IZ+n@6sO1H<c^#!)IMKs>WnEJwra_+l6+sBV)!=o09eS3ytdwM#S-rl'
    '-8aGG$A90@fk0l+P&>^;1~}Q!RwCWi+K2#>6}YO8HrB|<lhKGJHO%Ev*HExQtG5Pg*VeR~90nYcPA9FSLV=Yu1Jg>qb$m4gv$`u?2a-mR4oV&R'
    '%*)w94`{x$>jAt@+x2TaZ~E)QJ3xni`k~>Ud0P2dP%K7kpj>=OsH^HINms<VQJd8M0i*=@0K#&>{9)FN^O$!gQTgJBo)7!{iqPJEf9PM<Is^Oo'
    '&?~gkUFYOC{=<1=-#iuv@M?t$5EVTAy%QDT5uzvk)QISV?AjZ{W!D~m*5^<5%+)%x#q&t)D@F0S2c!p2lb(eA7?CbV6JL4?u2#etXHKA-3$<a)'
    'o=W#X`I}2BpYIjYnTNdLho8L9KB8n<jzjQ2Ko|fa0K_u!@xuZgCGB*zMDWo>f|JD=D9x_v#k<QLF24QI2|BFZs4>t@8C{sM_tZHa(H(#Vn@!oY'
    '$bMm8EjY2rdH)`$57^e&p~E8n`j`%P8=SMZGbckV0BTi69-Mhn+DCvGk=zxT8)s{Pf}x6mk*#c_;0HkSo-!smK&`tDr3R*RbzpBV=dblSk$S{c'
    '0-xUmTX30=Y;>kKZLG->CmLf?{3bS{$HoKV&oK>}2k&mOYq|@LKjeOXJ;j9@sioi-5JjNWNSrfeT?#y?hjG`6G|HL8hqabf8%pq~hGgh%?c|t>'
    '_m?l#`>*=?&A9A@F?Znri1g%2X-EpTqPaQ>^|IKp>DkT$?v<*;a#H4=9=yOAMqBiqu4B9yUHowg@Nhbb>1VSL!N0;X0~eXYMOtek+tD%{xgrn%'
    'PAk<TRTNGOS2jmaRk!t&tCz17(BRLeuV;#Fd?F1Mpj@f5w;E~^Thmhn;L9jtE`&L7*uaMK>Z3#f+OORYtW@5<L5jOmNFbCTU}ts_p1oDnx1T{m'
    'ry*0W2|w?ZaG>yuK`9_{nTFhew#g?2ML~`kvy7b-otJ!vz+Uz3<B_2KZ1Uc7Y8jM1&Ft5tO3uKh8(qEkMMEm#u=@h4Sely#dj8<N@lx0alWiTo'
    'OfU6+L}qWFzkJ*vBnA759;dz_n>%)!BJs@>y%VI+n1S7wVM&d%J8aNceC=O_KZl{yZOt|Bv1actZy#sNRbmFNLz!Ax-Jt2=0FX}u%$_y|nz3M@'
    'U%q!EW(=sqF;b}<-(@xKl19w&Zb98TV|*p)DO(D&XW;VH1+3mEvRvGn$R-s#4r}S@xCb&7N^O;|5{kdKolL*O)!LK&_BdWXz3J=Ek3*mSczQW&'
    '!Hu)1N9}D3K#_076MPX7QcihQa7b|C=B4M3Qdg_6IE2w_&4$>w-n??X58A)%M$!+o10f?8ECmgdqv!Pucx9C~TTvuJTDfIvTe)6bXmNm9=4xx|'
    '?m-s+vdhoe`{Vj}m@mJcB#&s6s2jOO+CE31S#XntlE(4V*ap_kQeK>3m)1Zqv0BtAR{R)d-+6FGN!J_jokMs|@#TQ(TUIGUKpv6xv!av%R&=V|'
    '>IB|sjyAm%8l9-qV>Fdjc@MlL#+Q2ye|~&EwE(b&L^mj~SCI{tbQ7Q>=dJV!Orbt;KA1hzx!{k0cR`N%{FUcuA#4v*_V<bCwy)r%R{U{2!~0rl'
    '#-!p>0h<pEOeC=d5)dPy5I11_48lA)OHtVTJ=5;_Jb8D0WbdE#<IF*M^}^c;@j9@*I=j1P?yZ;Vz)ghnI;dlS_J#^wfQ=7%b`9(}#(JflQbKjt'
    'XNk}AxR*aO1V{-C9lXgqZCDRcYDKfD2q;Mbgp**`;o}3`4$E1Dj(i+*S5&O9(;drK)vW&bK^?D$aoRJ4Ai@ZoBQ47hP~jl}Pd+E*qj?0MTo!#j'
    'JkTiDsUVWss&$I37Qe%SF<uzWcmbR^K@;q~rLe8z>uPU2@ww58VMP(+xLB%+w4o6Pt+z~1@9BXqW{GLqRyV&~J?MNm0nmryM`O;FkB>o}JA~yG'
    'd<2^?V)tRk0b}G+0Owt-?BsHQ8Pp_6)FCJ>w6c?pl*=*uv;Fq*W7OXG@Xe{Dj5^vo^%BZ-B6a1AG>;=iCCpI+`*w@*d0A-6fsieSwPRYPUJjAI'
    'O<|s(zCmu0+0a#oPo`s5D+vq5wc{j@2T*S#q>upPTZsx@0?<U6shPK)Q0a1_{|wIf@N%LmtqX}+keCxqZhZjNDtbuz4o6{mT2@rfm*rTS@U_d9'
    'aK<n97_nt{I{Mddz>lAA*WFAh^@A)$PM|Lk4gd-qA`e;!4iae~5*7`96D`ZBQ`D#mD8-iL-9@`|mm`YzQ#sbG3;YQyF+c`$@j$f3AxlAYpoz-?'
    'M>MvZq!l$qz(XB93mu_IAm`RaI$Vxok2?!csq?_1x>=}-V}&D(NCpn+B3g6iqafFNj%;T~Ke-Vmj^6~l5YQ_v0UUGdejD$xP&ar~!>LIPs5SAP'
    'qB<l!PbFCm1)!n;-g<VtqVA$x@y;3*dCeTT!hFx#PHA7h`(9sOZ^Q(y*UK0^vIidj5#gAB_R;5(Y|Oc`O`ly_WmaJF;&?^VjQWAJ1K%u^_m^k$'
    '*Dvos-w4WpRE^%>&I=o|hPPH}(2!lsI6z2o><P66cJFG!4n(>Sn%XFAgSUGIHLZX7^cBJW^iC6~kB6hN@DQvd(l}%+Pc|e<?&vEGrD7SiIB0+u'
    '`U33hxzLiMP6OI72R4tqPY<>PRBtkD>#-KYRgIH>jq^>P=ga5Ef7{m=g|mlZmvm&{p48VepDU+?gGE!o!-;eN$kAsozHPOVTdnwqWR9a@&3ERx'
    'G35)9`_ucW+Zx~Ukp=PuP^TXcMVSr}M?RD?Lj>$f8lDFZ;PdT(K3>33YmJ?BEs^8(-MhzY&%=+zI4jOhoHOR>HO(YcA*!HVE>%hs(L5B1QM5+j'
    '3#)UXV3S@^H`y)O8l7MNQ1{mo_CQ<hvp)Ge*q)VRW=RH1Vc(?0;ajqbqeLqkRBzyoc37m3C2P7C>4<~Fw7X^U=`QzyuWv8^W#jao+ApzKvA=T2'
    ';2>Ct7L2nsDW^cw9-AE7<Nyj?eE4|hIFnaai2@kBiL8m-W!cWTNe>(hLiH+GE{CJjf&QG(M;J`RW4%zV*=*M-iCe(XRsgmFv=ZdYo!TQS#odCs'
    'u@Qlo904w&Mdg(svSS>pNz1Ozz?X1E3>G`Ru|Q#Z36yv98t|{!3HjoORPGnq4eybbdKh4kK|FRCrz1n92c4gQ8sPpoYjh|8A<JU{Jth@#8Uaa='
    '9D%HMQUllD<i3JY{+KK;;Y5Q22Wt~Y3iZp;4qCBAQHU+zv}I50K)@?{9jZ~!jJIxLvu`Cm?n0K&`;pnI<*^YRKs=KC9Jx_yZ<d84RX8&=Lnvuc'
    '>s&!SX(a{j#b?-2w^BtuT>sGeHKH5mYl-tn5hloDoSe~t63rnRHaXF4e1N)eW-?EF+?a$<T)vG(Lzb~~c*S{*cMR*{@#H1|8*3sEKdCsJ<D}7P'
    'LxFZKEM!T)ChrRaRi+!hrRMVW(oiTeg_^gE?E0&KZ;_n^MG-TkZ>1%`BF^WvSyMWf6!OT?jf!6(B&0>q>Uvx+F7`ZCwjOS0sr~gq9gSQk>vVKF'
    'mKgn_%g+GI%o+boO&f39$otcfM!%9{1NO>_Y{63RVFxu@jdzOZ#=)w&dhw%900|RX0OpLNgajUWONwMt{r^MUx30%=BgsC|u5iCR!ufq3pg#ba'
    'kb6y$+$QPEeBZwNi6~OCXBO3zMqNC!JGOYSr_hzii~uqtq*pfmb2|Yux<QwnhT!wruilgmAG)sHAZ*AIlY-u(0zCm2b#K7<XG#)+>otYcIA5(4'
    '(#}+p1!q>92hD0^dDU;zM+M>Tstn$eom1-8(ZwTP#G^$f0P5vgK&ayIJ_og!V;V+W;HV`n>UzjtS1KR&gN}Ev#9<i5ptcr#^7P!09AX0t!3)Cb'
    'dbX;YH&tT=&O{=`gR*Evwh({~`+?#m|JopVWmSU0<m%`Y0qTa*EHp?py3GO&jjUzT%G8u9yN7tM;_yLK3xwQrSc*>_f6k`uGJe@>y90r0DYjNx'
    '*J_7}ApA(*D9W}rqe5?DEC%1V6@Z>PR2|tQA%WsOmZiwi@vrJhpHFh0Zv(G;Rjc>bEyzg5KH@ke^#YOPu-kIj+SY(}Ic7b@`q=TaX6np%x%ggK'
    '^}ZM5KVoG7@7!NkY%AQ%lvl3RC+w==65e0SH8~XM7K0s}=1o}VBpn+yS9V3YgcL$i_eCU@d^}U;+MfRW@@BvO-ut5#;?})dd7MjIJ0q1|YQmco'
    '&G>eRD8-R(KKMPErRTmGV@tOyCp%x=hrGX+`+a*@tE0u#R0T!2IkuAfntW^-kWo+sV(Xp*^DR597&}ZeNb_Xp?yRfB(scY?<<l=ObKdRkGlvhm'
    'B%Z069K1mQO6-0BYj_KK0@^jq;5FB9F^mMjn2VpxJ3e`{nwbnoNe8{2g>v+6K3u+Vqah4=5Zqs`bWVn-C7Loj(z@eeJqj{%qyh={&?p2)MxBr^'
    'Y0wn%$T?XVDBt{Yr4e3AK6HWSr_*ok?!4DEnL6UD`;z8}hRq5Aj4Xhn6hUN0@Dr>v%HVO0ckK0YN}I)uKZCohg7B!h<hKRgJ$!b1r%>D+p%>dM'
    'hmp-_lXJV&3#l<;gRy<JpsJ0TIyf1?hl<Dc>r|`PrJ>Z}r2px;^*cSibHX-cPWnPC=y5^z2Apy}_)JDQSja3E?G<T|lIYz|iWq>aAwxE^H1h=J'
    'J)Wn$EpGq9l1I_ab2?Jknd`xi6$vAPYDM~$TiaU!_YcMn4p5GylZr__IR|jHq$#ExUca6Zj89A)JGtFapRdf&eMLl8_6>y`@md<#S8{k!A%I!t'
    'Vd|)p@3IRP9B5z?+A0#`@x9}(W5|1{oqBwL7Szp%MJGT~h{m@XsUsasr|mLQ;iN}LZIeRv5&X<pq`}pFrL)J+WV{Vl@8fvz<2r{0UI>+Ub>2CA'
    'L#pzzRj}ADbTl_<cxNW911NBVw-H*9)wT*H9=@z`*S_rCD@!tEGl&U8x*DbeY?Em?u<8ar^f^7=prwRgKveosXyw)o*vpQ7?C}F>*hwdC`_!a@'
    '^*R^W)9q3Q@vWiFIGtykE64T+)>t|vL3+ho1Hh$Nsk7D6*4=)j^Y`D5s-O0H@aX9X_=8lgO8?(#M(^Oqy*6KYKU8@jNbjY_w*FF(1RF<az+b?|'
    'mLt^m15i7{ZV&q<>A_7m0TEOu04t$epd3iemOk{uhe==Q77%_z@XP$8c0?l*p{@k|j&Ygm=e4jo_C`~$#gEp(Hk_MmdGx>u0YPR<1F*HGSsK~I'
    '9*X9Y7<hDLDl0sWP(9?8yc!uVFA0Y029Y$K*`&#1bD`vjENrS_ldb1Wk4d!vqtBAriW{5?!m+b+Vf7NCJ=TwTo&gZ9xXHH$)Qtejp1u;j8}QxW'
    'BAc5sPc~;_K-mtcS1pnNT}3+EGknKyxRpe)JBCz{J=}jj{dU&hmnUHrw9bog5Y^ah5l<1XWnf(+gaMpZ0+>-N7MKltBV!RI<enpY(R8eu&JMF@'
    'n7_;4*Rul(QWRv}>0|An-r`V1LY)Y^==+?xmgzUmG;e#!v~0xxVT%Fgfl#b;NdWtf_B#10NV0Etg2a<db;hJF>hkQ)fu`0d)n(d$`w$WrQ(EoA'
    'diuOL%kgfo44}`tRDClCy{Olhgq+Z>t3!g%r3&5(W|xNBrtoX0iC5)_ee(tNl{SW+BIiy(2`h3pUK;O5G_8O^JxBmzJUz>Kncupn=K>Nn!iwf%'
    'sz|{cXP(_RMGm0&s-bU7VcAq5aHMt5`(0Gc)wM1)j*iI%Si)Y{v+F4H+y<F*ZY@kpL4?Gk0@QeK2c@E%$(%8gl3NA_MRqR8)UtAE0q4Fgle_Zt'
    'Hb_B94>}FDMNc3F6xGmKeRHTUa|jhHqU+cT!<bgfR$L4RKx$uX4(zo<jzE6Xv11*Xfh-L8CW-|10%(i{1_m!<2M+JP)JEVG$v%TQ$C0N&jtee@'
    '&?y(Yly&3*<o~CU%J)<(2mS}FV`mX|LPdNb%huQ@L3N7OweETmUl50=HgnE`^=gr1IW)Cmqo6;Bw+?(;)V9piMduZ@M-56f3VP7veKi}V$;NX;'
    '+xKLXf&=BL&d=Vl>wq{*iHW|mwJF_O7a$=Vg?l(^^i832$#%M-m{(Lq0nLhNY?cQIp^c_<R=!6|_w<S_XdLyi`xFJdn_|~c+LW9>ppUjuG4LOH'
    '*{MMW^#`Rt7&ivknF9&6U^J{h3WSFMG!=xkvf55Cabs)}3Xw+d2LSA9VMl3x^Khp<f71Ky0PEHFlYiVh7zQM8LTRW{B$qaj@dD=wBm_GfVO$>('
    'J8c5-Az9c0ju(+L5u<UebYPAW(|cRk>)&=UP#XjL#!eB?xB?=Fhx8Wj3Pp-~fKgIX_WN4EJq57<X9bmOngc+1S~2c;m^Li1>g#g$uK$Xk0?641'
    'rILW>K-W-`a>9OL2@A-L&hw;XASylW-a`dEM@S+pWqDa!KIZM5`oqJ{Ap+1FQW_5tSej;_xe;<8*mg0zR?*2mm4YC2pb`_z9N@cQvtj?$vAVBC'
    'H8?itzPeKHDM?qRx;jGdrR7eivC>44@&*NOfrN^P@DMyY%7^$0qQqGVwlB8Tf=c~}UBtuttqJ1(S^s$6Q?^Bpo*h`Jhtg?kDHKaJQaFl@^kML0'
    '{*uXb1Mw&Uz=1r!PnLdt%R9`E2*Q(IE_?Hq!MOyqXIlU-@-#&RfGP=K+lD;0oH3{t)ev*i45}>(84|g}?^@8T#m)l2|Dc`4yLDixy)jupS-gsr'
    'c-@Qu69to)wF7|1$d?vMYJF>n)zTV(6~8OQQUPJSTw3vm9i_AjqVKm)&kx(5pWazq$)<#)s7_lq9~o>w^$qm3>kdKK{B*;D8v!h)GAR!j06~0b'
    'e|=mLcT(J=7svX$I}KsF(A?Ok;b&?s&c;Ds9c<_jGeb{lv&>CkR~~3eIO1n0^ku?oYNZ^eF!#UTR&Pij*6g&%n$&6!E=#I(-e3!Ju0loZY<ac<'
    'ygO+?MzirSRcc0-#CKzNyslX}E*>S<^S_t&D%8GH!4P=aTHqyz?bRFALfJApgbo0Jxs%2mmQ7Q!H=3mk^+8LV;%U>d3d5rYbnt^`{qmX&KjE)q'
    '&)j(MlDP(lozw)#VuJpH{Q@6(ydeZ2vO@=~>c*Pnz{@oHX@PiobFsS51U)`vXAP`keT?i92lpaRypX?rruBsK9AaxaOfBXCEbDT>lBoJR3)pi*'
    '?9mj#tVYdI4*by(cjfRaxttMUz!i9Ab~GTv(RVfVGHCA}YUeBpazaO09)<IXVEjmmVnCP|jVbvsm9l?!9Q##I-knl7JzP%K6_UrjP7a{+;-VVr'
    '0d~+O;6c=?nH*|Zjn#1u2fr-CCQ8oLik*k7F?&?R-?CoW4=TkEFVBxJ&wJ^wCKG&XlqXlvE0EwA=B8I3cw-A3*tw(vq!f_yX?oQrXHNk~mWRx2'
    'AUnK;^mU4F|LVDHQ?Z`1G_CSNASV|pmc~)i+J;z4%H=u`s&vlejU~7k5b?xooU5tk!)(d^u8GQ*bv3ICXE<uH!URq+@<v5o6yOTp?|3}}kTj;N'
    'zS6iNrQW=aJg4bM>$*B!OUDb?zP+z`zPsDIg*Jojl|6uOAjEPl0TtF3(Q1(0w+<t4d+Ww9(GT{yfkOjkcnU_P<=y%R%!0iw?46DkfC~%I6o)Z`'
    'gR;_6R}^OuN-;pWq0B(#!!Ff@;f60{E+e#p#rwHx{&ndiqVYQdvZb0phuMCUQ)TZj&oxFx=8vQ25y49$U~R_#$0{Qls&xi@5Fy2!SLLjvqg~$L'
    '86!nmMgh*<4`jXVh9=L9qSaK8JX~=Oq}k^U$8+?z;x}C(t40yDOPj`Gjy+1drcRy*sK|8Ppy<|t?;^qB$zur}JIElBlnRcLa_MTY^XgPHG$_(M'
    'fU#K#M2G4=GRg0X*&?+WfYre}gc(yGppz!F4APND8ZIOJEMA^RykXryBODwsDe}-QtlA1fI{J?E<^IBUoY&qxnsDFi-JUnCrw9ySId&HS1sIkA'
    '{~yA}Qi@>rYzKfXsq~;@{OE=E3!e5APU}a`@BQ<!1{liy)wE%K14|45g37g3dhJ%c%A_%h&}tLFs#zdo4u~aQuqZgD<CSWjuB`^cj<z{?3&;C6'
    'f#tmuQ7|eh!hm`hye9+x(Wh-%!7zE_7+J+)iq=+<q|`?xzjnUn9FP}R4fVd1gKd_s9UVH8238UP^Fy3AAVD~gWzYMA*j1ee$>h<s(TPeoV{m|y'
    ')aC?to~zR}{2Pxwh}zB!FQ=zxdHc?0CbDPvS_9aa*Wiuy29PmkpR5TH*-}WAmGIDv5xld&R<?QYwlcJ$cGtP=>xmz-&D!PjP7LC~Y6><4q(Q;U'
    'G26wQlfnVG?oDC69spdXa@aBhk_b&JW7h7Mw!8@H^bu=%?YY>2_0e?&Q9%{f&^T2;BzLe+yqDOJAB4_r-hrc73bWUUS7z{&ybXA?)eJ<;hpy`7'
    '{yZLJ&j+hfIIpx0Z0KlzGV@H4&9DKvpjsfqfpDy-#mxoa!;;5v-C^l&#+6AR{w@C41?eC7UH15FSLOPJ{bestB$`*Q@BoWI`MpzHf-RkvN*qiK'
    'G`ydsFi8$jQDh7-Fo0f~!~!QeDhn`P7dg?S<JIWia{dc#9$(FtcI^+8p*DF6g&Z<a)p$zZz@clv$~icNU5A3;t*Mr{3pq4+nwHy@G8jcX4}Fd2'
    'dpQqQv;U$v&Z%Fh&vfMt+5}z(IvL5*3VReb%Q?dn#!1^>hnyndm<`prO9E={GK~d47!;OWnsFSPP<}rPwfCN$&tG1RAFnL083mm0&Ipi6aWw)K'
    'w;HLInhh#7jlg>x8?FpwG2k)N+u>BE=*nZ-x?=Kiosi4U=R~#2flX9Un`MYJ>N-}HG6z#&0TpXgf-AXfem1EjH(Nr&1a4jyYJ!u40P(}qr}_A>'
    'cV7uMMSrAFthS!<yk;5$u;GRpdJm<zX_lujbBs2*o*OUxkUW7D$9gRXoB7gC<Q?m8gA~`Rr{JxxGqH24+c5_4VHj-67U>Pp2@FiIGERs2nfy?s'
    '4HuSP73o-mY{K}5m&?#kj}Lc$;ydh*dT*Jiwx)sa+X1Xw75JCUK!~*&C8yGYq5y!QRN&uQ0KK$#=~0JIrKZ((qM+=@Ec0K!-Wf7LN$$Xc1uH3='
    'stDjmiFkJ<BVCL<0YE@b)8uHhM~KF#&U9sCSjwx6Uyd3=daYsv`Oghx^YYr-_IxG*y{1){6QFMbw4#oAGZe=Tv|B=mB3OaBz_e={oCD#}BA}$5'
    '!ITLW6X!}?z#4Px66fXN%e5sL=b~#>HeE0jFe@_$(|eAzD59i*^)^oqH~Tgk&9<d<AQSv^fL{yzc$j<TTkA2=)&-SIzdYViHR103v`5qEhc&!U'
    'HPV?Fb=ad|nL&=y+1wE+A{&XYfJ7RHhHcDsn~|fM(Nae{95qJvFIJE5q!URA)j$)Xt26;Lj0&&;-dTaXuna(w?9`7`tFt*~>XWF8NTg*Hamx`U'
    '^8qH7_YbeGs;5o&r#(HLKi{9e?ZX|QMJ4JQ%&0Bc*u&<lI!dm^hBmRUOhiIyR4Z|m$_&<L6v-Yq*<S2YBJ!B-+0AuyElYy8U_4^6PtcWu7vhm4'
    'fJi{ls94HdXOCAH*mvYm^QdOzL3}zWwJXbc#-nc4zxm&Od3^qJZ$8tr70)D>klB#ky=36wW(5?~(@`a6vEtI?<)&ICp28bKmIU~+c_rR)P|fBx'
    'x?48BI^G#|<Jcxu6MzNag@A(S2Hz{teCE^@;7MspOJ?>rM0WFKo<D|_=kj6qw6DuJ$@%i>?T=d*M|(W)ydf0eX?jk^7|9&&aXhXvbeO_>3BVvA'
    'Dpoz%B}%oiV@}Eq7&4mFWH}&r<J7km-A{kKu4jiPbI!tOG#PPJV@J|+)c_+=yU2MzGvcW#JB+Y|(OSk5kl`rqW_?{8_=a={dcU@ak{i`SQZwL)'
    '0V%1rGNcz2R^tPKgf`)j2EA?q-hxYADykNC7k$1KMb_Q+X!qLF3os2>Yb+y#gu>Y!KwiNI&v7@}TY*8DL_S#9O5^5+4<@1o)XVs#AJkzE*Dufd'
    '$0wfMp5|TKIWwdBnzuY0s|fTwu!yM5Ih`8tVl2mDg8-9z!)~=?CXYW$4PcPohSd~yJgU|1Yc9TjIBh<L4}b3YGDc8bfSZm4%ei=Dtxg9oAb^HN'
    'X-P;;s&qT>ifrvn1|Jt1C0D;}pgs)9eO=Sz<^0>L?$z4`!UIptKJP7Xk~W1L!PU2b)Rs$~?3$#*$ebOh4ciPT0r{MYS>X5Ln|7YOtbA+iA%_uT'
    'zu<IaS`+)gB50ODy{Fho*y$G+=`mKL8R{!r`muFdY(V7`8M=8O@wSve^~ZI&zm7ER+!5r^eZdZq)*Mg&y(;o_$+SxXfG&tdfS8O*V^arM8O0fn'
    '&dQ*!#)9tUgMC2B7${D2MRNi>2ne6L!U_gnrbF$aon)h~>!wiS=ys|h2cn$l!gL6(MpLM|ydgMh4D%5?=*@t8-4rvEWB-VW3BLoxX~v-FM>Em-'
    '=0Sasu_xKgpoxR`F(wHrAgbM}S$Y^8(>$`jUGSWv6fH&i1o<3;gD!xUU@$JDfLg^4A6tp6;MhE`?42&pKs3Xz$|hT}j+r?)KRhjWNiPYXu7aZ#'
    'NB;q@wSiy))fY8J*?HhVrG?5EvE_)`StoF3m;<15td7A=`k<auq+>8p{fy1C1I|q3ggHghIyJ;hihyQ7GnM`&jdK)fRFi8>YSPSE!Me30&y_aY'
    'TEpO|BcIjcc5k6T2~r1O1{+9&BHXqCG58!kwtceT(cm#jwf07@5D$8dgg)rc6>?j(y9BU@4_w&2T$4{H>r9D_+@;ZW!S}i~%bAM8)Z744aDn!+'
    '&E{Z{^o$2UYC{}8iRo9$07sp;{?qFI{>9#~v$9KgfE84sfCFN$NJ-Om1f}ho!M%O;h(6zP>J7Ysel)&tWPYxi0qZdWI~!76wz#C`!BIG5orIPP'
    'ol||)a&AJZ6mdC(fC%MWUq!(MxlU=&DwyXz%eKG6F5kaJ8Vc*>%*&)t|NC1zC|m(SeVrpI;dt@QGMP~44q$<9)D<|~vxTX6FpepCHq>X$)!U_A'
    'DMdDqq!rs=cea$OI;OjvdCaIt5(a38qL@O(ieU}PSQ7=fLt=MPXXXTI2`H;^c^sFI8VG(}%$ru#z7PthBTz(brHN+&!UhnHllmZ3L`$_8ytfJ@'
    'J`{|pPAXLhC^-Hh<9sqJOGSGq?E`n%&Kmb-b{dB{Hh*_yJ*zsfx>%hIYq3RWj48RtnoZ7==f(qRA}3M}F{v-%#8FyRuM6VKIemU<7uoC6;Spz`'
    '$Ewl*jeZcIb=K0|QfMbu^FX5O$R$XB(y)UH-FA|=1U$0h6G!Poa=yOFr}M5)i8a^_VB^JCO~GaF0s5E)0s8`nc4q7$)s&}UOJ<)`cmk#MoVG2i'
    'S?zepQQY5)&|2+bIAKA-gAU~wc1~>NrY2PzRCFrdacFx`rZkm6_}6W4*gjyftBxGJ+k>vn@#`QF%k)gsGxpz&maFh+)OXK8C_|;QUJy#LOiPsj'
    'FT+iMz-s!|nj+q;u${$j3m<(r`l&z3*B{Szch}qabtgXv1*(QOmLnc0w-k=G5(CYCV{ERZF1AgJJjiQ@=mTJs2_tV)fU9|6@Gtw}3x50hO4?9|'
    '$|MFAm9qt64E9$7Y622;z_dPkwxA~BKhPE=#tzugjnaaPL#NA|T6@IN@YA#GHuRZUu#sw~1R3r4W-b*3J+f}~m|Mc80%@c(aGlzm0gz2T0OJA{'
    'f&j1DA%5s&`0m^(jd=h0le2%9ve|v&p$VZP*iNI=PO;mfW4cJu#K|k52?hvW>jA;@#D@<w2k7d7Q`vz&mtE)i)=E6PkMFR(jUSWNhgppaLGj6B'
    'P02=tl-lYnXKkjTPXgY9B5B}HVCX<*rmY;n>`#B-ycgy((<e~$a{AK7Wk*XWBY{zq0jU<BI^y4x<jJTst`$eq=oyE>&c=T#g&P15Hn&P05?87%'
    ';m3{;uP@+q`h5Q~fSR`}?VbL29StOHo9%Zo&?q?;aB9S}1uT;!73_{aJ+2^6qXbP_+w@}Rz&<fcVtTtJTz&QeOof^}c@GN8q8T4_HW3xR54M7L'
    '@Jxpn!*MRB2IZz9fbyggO_scNOTOu|?}vU4bTzW{+NB3v0Pt;i>|uFxgD5Z=kUq^~e8mDhun9muQmLoBf-Hbm&i0m(gNMIjdSqG=KPs<13P8tn'
    '5;N!S$@oQ-POTh~uqJnG<!%P{txemgpsHOJwja}DdiR6he|Am}MH-NF15it&S<v7Ppp@LCFkVuSk-%}CG-)O2se>~R@|n)FtGoXY?NKUnSk3bD'
    '2XjR>6y%rf3N{7AKWdkwW@a<IYXXu7%tAp(Z4|tbZtS47M+d?mOF7v?nACi@{0`W#ZQ`}+DyMW~kEpnPvS$Xcp;HiM0xt}3D;i99+N@?%dmouI'
    'EXcT)&Je4?00iAJ;Px+v%e5;#r&<B<N^u<<z<_KZXc9Jx$wWbUbCa%)8o)HPz^f}sV^a_myL2~4upMvK+0Xaw^5^MZKeuPyvp93Ux0Dm=b40ZO'
    'qvKd_a*YQqy|)%a73-&=en<nPEv+_dH$zo$;MGyYQB4j1ys{m*>RQ9IwmJjCZu%lA(~?+APhg65Hc(v701QDY<|HsGA7$oh1iANZ+3Iwd$n?Lg'
    'NH2D;940E00=$ASwIS4?4^9|_0*)JbR?=ls4_Gu43G&p>#yB|dC9+&r3Fd=V<@R0{wuup5kvRhJ!hz+KoCA+XRbg)-Z_FI6w85{s9jNOr`I!^2'
    'SWRM<_t3|`t1n>m^M2197CfRB_LcNdOW&fWTk7mbq&-xpQGjgHp!`0T07NOsCrP5W$cJI6t{*;TNiUo^+;IT&nt0F^doaA2?FEbjFxa)AWKDqj'
    '-8g#T%<8R6M4iJSH5#xej-f2=Q;tu8o5d(+YaUK-+3@QRcOue^WV55_75|bPWj39Ux#;V8QZRu{EYf01gMDqS7}F{3lBs#Ml$Ns}xy+aESS3}D'
    'v(`otcke*~=o?61IxQ7(7(?lbG@c_$vQ-i9kTPA)3JwN>wiM7i%CqQit9*I4oONe7g+i$V$r-R4@6gOn8zUn$=A*Z2bG;(Dc)UR>R$$ZBXk}9M'
    'VYRS&e44!ews;jlSv<!!q#RwhCKj+X0Fh2FX3loRjB0H>LHp#$3HRIx_<dO+a*vpdhqh1jm-F58`5rIp_jj9<OV2d}%{D(lOL-$2Q4J_=+ri{G'
    '$AY9edJ91F1%RpnPnJ@q%~4ITORW)md~-58%pXr|Np8<Cm%Wa8!-h~3#+#ls^&1syOT}>9mtulQCc}o%74T7d>tZUhW=D_$e67qBKB8_v{K?ai'
    'hldMi>}kxm*KX}BUX9u0U-00S0~;|@s~fv5C&|@xU>6?rfXe4u`>X@VL)qx-unkvu%N}DD_HzGl0UA*`eQA%&NEATDks^I@u;&J`uSq&1&*o9n'
    'sFfuRRM$uBN6UutfLGbP#o}h%@i9ur!{PIrgbD?h_Z+oH>WVi(0n{;s!Vex?2E<Fc<?#<KnMbUn(#0)izDksd#2%&+hAgdQ4+_Lz+S4zu!-mTP'
    '=@Q$-Dh!?$QPn}+Ik1pBjWGavdGk|wzy)qbU)6zc7!c0p*d()>(ksax)g|-ygfZIvN&mnX<N5Tf?j2zik>N}1c~meK(0lEZnv}7CPzGm%f>y?4'
    'k=;#+g5T@5bc$SZf^;>~Y>#S||NG*;xlZ3dJ@2E)3dCQ4Fb2<7UG6re3CPtFJ4&iY7>1r`Z(I}!vl{l(g7kBHRmEbLhL8vAjo@!jbjE#e@Py8s'
    'Hi1@kp=r401SlEJq>A*Qp`v3;OpR$)_H(iyC|M3}H=nX?D>CEa(TjVV(Ej@MRhNeDT}>%d9NISec~V`4jU+A(d<obekUWeHw#H6CiDWq?BT9>j'
    'ZOPW$Q7Uft52DB;_p;6*bTg>J-qV62>uetgOR8@mBC*M$0u8qU>{zU~oI;#=*qQ)`5xn^BN^|c6a3bncd%1glrzHDPT$+Q^YnwQw`lMO-XXgM8'
    'iFGYlO^Y0qh*BKSK&un{2n24`v2JrZp2OTfJnyn<4!9<~lA74YG^$NV>0;|NKGsc)IF1?(@L-Z*#@2<_@%~Ent}UC;Y_`YO<ww0tMYexCDcyNp'
    '9r@1!7Y>hnfE*q{H5;w8oubespA!qS$%D;Xou^nld)H)40PI@bbfkJn#=n30qL=enw?ju4KqYMrHcZ8*8w5!`7yx;ctC=RnL|nBXli-j#Id(mf'
    '^i8%?R_!t+xPy*8-j?yHjq&p3<!+ZuW;`O$U{Y}dw9)6GeoWh@f-4rDYl9^#4?sNCzNhJPMs{%{t&O%m3pnZ|@!Oi@zYse2@7qpOKZA)IBr>83'
    'AzxTykqWHojvBT+{RP*gb}u%TNp#CKT|3R}1<7*dTy@mJ@7Kkh?>T+_czKYQv3E{78yKSvKDRi49gw@F4-OPs1}K(**qgD=>DlLGYO9vELZg9?'
    'rL;Uzj0eB7^TTu7skk#YlK{wD1egewcZF1Ir001FTxZFCMj9j`hiLDX);1LH9X>d}NDYUH5Pe<DW)<7+PERj8VvYm+Lww7~bIt|4CSHXAtb_M@'
    '@zn>J4MZ6EVl$^Ge4%P)sp6v4RkyS=N6mtKU6lSojh-{`?Gr%!o=PB)PiJ>l0F%!#vsI?iucj6+Bj6qJhPNe@-sBr9@wAT4P@YniX<c;>Uf1U#'
    'r@QfGPeG&$5I9H`?@(M~BYp*hMJ-a~24iWdF^#F@U82@+2EY>ptVVE>l_i`%YDny_4DlT=%7uZ9;6gFB)dD0Dr>zC-$y91b1IHI@6?|%IU{8(x'
    '(yehK4r^P97&v>>dDFLDwd>Z2vI?R|!9qMlN47Aai5<HuFs;{6ykl=fs$w>cd{AI(LvVQp<o38&O?Lj^!h2C9@IAAd5wdtZo;I(e&pJy6h`>%E'
    'DxzOf1h|-{1{7qPPLEn$cHL1iBo|pqMIW5Nzsc90z9&(j4D|u@jpA!I8JE#}v<NJ5nI1}(cXu5bKF|&`qLfj`H$<MyAbrTPv)F&s3cqKES+iMq'
    'X|WDWH<^yj(|GH}tS`un-l7Jumn|~sr!Z*3k&s1@Aj?Y5%a$5GYK7OL)1^(cpmy#ckVP<$+#{_PIn#*r$aw=P89y(sY+W?K?kEF8lA63~hBU{#'
    'JN-k)3u(xjP7QE}IRNVdLW;&Vsn|I?kOltgT4uB%pV*LtEmM5qOnOVCVP+*K2fk%LMCs()x=xP|cjvKlie_ST?RAWCWHq&V3%rXAs1l1uVB(vt'
    'o5tH$Buz@SBewupDcj(~YIYW|{*bn6eO}+tfsK#U46u?)o<kFORkhTP&(=`BkzlF006MbeNK3!pF~M1Ys}{&Fg<_97<h<zel%?@w1#j?z@<~+C'
    'XakT!!qXdk{w?q~q-WpF=sY|F(iP`bJzHs3ns+`NBhUEs`uof4H#e1f!N8)K1DF#fmJM++40_X~L<1KkSkswP(~MtBB36vcVDHP(7ITz=+QZvF'
    '(c9B`eEn+YOa;XhxhW~dWN5({TlTS9+TRAHFq}!HnAv=Cln=s&N`_|NrpTD9_ACJ0!(6d<_6b4@jNAu=g#~b__}FKyv?lM9O`$Qe(Kv=4I%vAm'
    'a-GBS&QAx-=T~>5H61m0@Gq%sv&FqT|EgEKBZtYZ63~qj*#=lPa?w0ujVBJJt97OI;7YQ&VpGw^xS}jono($92^kzxZ_fK0C>xN{){s`41UlYw'
    'swHL)7rCGn=_)Jbey|2k4bWwRSjSgqD$SdhMB<n$!iW7;w8U;+=L+z%O~3)xA$hUus)L31VWx-!b}N8r%y8Wx1hZ@ED5YWDN|NswO{Q1*&Lz0E'
    '045{aQUJc%D3eM*q0fYbYulhYlN5{B)ImsPVhux!6DOsJ=9T_02>Bsp@xNd6Wm!|$n7%dGr_nf(>jR~3WzwIc@)<?32h?m$`Hfmdo^~sycN&4O'
    'ls$k|4^z|rT|SBa_sg!Tua}&upf{UQkQ(|t48TfHOwzO(3a`-kHu?}#pZEt{3})u(fuZ#SRWZ1d?VaqL6(o=|akCb-i52>N;VF}GX%oqy97)Lv'
    '=q!!sdA9C^zwh%z46Zt4>oG}qdmBq0t)tXb%XSqPCD4%rpvth%B%ok7Hj3hpSOBP~PLI@uJ!BPNnf}B}AI^NxP~meMzb;`fk`JgA)m%$Vkt6%Q'
    'bY!ALk#=qsY=!0~3V3uH@cdpWC*hEDk)`8u4DZO0+E%i6urY0MCTgPeBA74sx2r^vg$)1*9QH6&6N>Vd4%>o5`ejZZfGk%|<OeBxKVHuFYiz=a'
    'XZq|0Ff5?PQ8UVQrJNtCp(ak#5fI}$O6g}x;AF3DNT~)mb14zyZo7t;dlc@Sizxtde9FLliGp6Xj*9>9iVv4Ze5i85J05G<HG?E-X;SNa>>Pp8'
    'umXsPrSRUi-b7>n=i385+~5CkdAWZ+f8m_pWgGVU@_^;=f_LjH+wGfk+CrLYfG0xF*~L0L00oE6%?KI%y?WnJ9x`>JCYXx1WK<KGc*GU%z`Lrq'
    'dHh#hOFa6X7%7l<id1W6^b<D4!09rSV<$Wg8m+jH3reAD!5eH2)OL>#KGIQrg_!_-Z}aTfw9hYlW3k3_@PRqL_&^m6&>|<bDpEawsdmahI(*n*'
    'Rb}hDEa2o)hcRX;UKiijdi>7k-VE!m*(3N|Oi0L_fTofNRU^W=p-LyR1~J=k`7qJ29oW!Ln?z!OtB1I>M+k_lpL}@g))LWudYy@X+_zjFMvO@o'
    'WObH^RnYaOqFfEIyU9lL%=Rc$W3d2wAxQN$(u=f(k?r~dB7Ns}-G6Mo|84K2H+w`h2XPVB5%{3?6gxek8nR4K4uVPnT<zSSX3_?6QmQKE<iqMt'
    '--Y)ki$x#lzHmRz`t(1aK0n?4$J1qe`|$BK|GPG{<{^77eJYSZKssbE76)eNS(t!>5#3Y-?E_n|v}ic=J*h=7Qyr^QTld}t_xlg~&1zxIyQY}|'
    'kDV)R@3upV28>yGD&YVJ?DP%LCe?{`CHzHPugsHd@o#z6SR3+BKk^gZ>X&C;-k~;vX{6i+XFmtE2vOp_NrvL)!*)(tg%Ghndl*`)ILh!xq@2}3'
    'OEBrD<KgMC-LFmPv+~WRE*%xT56YeZe~s6(IvON`zdCxMl_81FBWx(@ri`v}B_8BzeXqQ{|G4~@EukTW<SSLVivh&&UWB8k8L6za?1r5v^}vfE'
    'gl1e73tHIgq-;UMo?Ae}RLV~W1H~QJBF@&DOzTNwQ6{YOnyX7SbM~&#&k(hmRT|JF43{FW=jx=Sn>RVEhEU6UO0M4*3hdzAIph#SNe(|`sH4_#'
    '&y?3@&nZ4B0CJN&?}IzfI*{c;N13q>j69gJs2wT2*P-<O6Ti)cJgwo~KrySyHKm>O4EO=|eV1YeP}2m31GJ#<EOkdJD1>F}W&$XeWLFLm7B&2*'
    '1NgN-pLVyM_eLjp@B}#~wXf7(b~W+V7LlBQ@@$gCd5++800M$Mv%6|UY|}*^_B=TJ*{8m5m&?QN%fb;HSczlTMxm5lWb2lgg8IhRHEg;tih_L#'
    '=x5fL5aV&;XV!EJFulqX@9JmlRpmD%#9zFMgkqwwsB1pc&yaIMpo`H}b{S`xNnO}1y^(&SibkZ7X*%T<1Mu2PiO^Q}vq<PkpPqo2|Nev1*w?hq'
    ')^f}qQA;x~3k(NTD)hZ>bUJCR*_r7O#a1Chxhl(JG1Dk2gdDi?k;8J$F^pdNF0f-y?VM~IK^5nGOmV2z1#r~bhN{R}cdr!$(oyiTdEgJQ0Ojea'
    'E5-kG)AXKp$(2Vg>_}*U9Gz6fw8@)O)xt4WKKn?;;~^;lO{CO)a@Et;&UzD97mU{4SQ7E-^Mi6BIE)4A0OQtx&CmyD(m^DZ&1JQKp*dzowqqid'
    'BHu^kOZL%uS*p9{1HxY)ukC=2XQvXDq5R|!;8D;s3b7CaW*TaxHWeEKHP!ac(e7BOI-0bCG|#0Rf~z-R1Sfub=UPguSEN1wKepo_%s~=I02ByR'
    'IR=|7ng_2sMjDlUAt`}X&SbGvzY<mgR4+gK%+GqKU-WrT+te2%aysTAjE91D<>)p)34pF-9B8D$b8{Gq*XyokL53BSEBc#_m4dD>H+G1{*PpKF'
    '-H{Cd(fPJrl_7H$s>M;{mjZ<V*d6tW!mJ+uKrI{}Mfd}5ailA(IvZ@c@f{6H@OiK0grX6ssoAHx)vSi?%I0>v`MD}Ksua>qE7HIyim|?fUr<Xj'
    'XWQ#i%Cp@31w0{)_F7j#djk+6$NZ`DUAs2$wN!gxj|?_%4HcCD*~KONml!DGO(EE#u#a%n7j)xmX5PaBQ$C${ge`5fMR|c?3b?Wf0XY<GQni6h'
    '&^FmtpA1;$z#fl)@>v|c8HpXbthB<?&z}9vOZ%d~KU{wO^zeB4RWG|{!DCjG#RjmGwb8AKmKe06DoFf2jXsS6-%d`J**-$EfM`xtdvvz0Zd$SJ'
    '>kl!cFOSy#6S72sdV1cz{hz-)KCVyvJ+|l8<7XU2ZDuHJ$^_?U7$u9UJ8G~>O^nP5BuAT-2_PrQtn9;6us3?<Pp|4u>jy`M1eQcnXNzJ8)kn_f'
    '*@j^05*x(?Q3)pd!z%sI4ZaA8^S%QsU47gohH$GVCu{nAX?J^1uilpm$c3%w4QMEeDIT~gPXh-is}0@-AWBn%iJFtob^;b7^~CApY98fX;o$Fo'
    '_}@VIwnOp9ohjEGiZ-Xo(zzC3Ok$>wCL24DOsoZ)HnCW?O$FZ==zV~|q9})D%f!1qIp6>6qR;Johd-aL1<!B+d2o0YR8pX?=5iWdh#XQd39)aJ'
    'Llo6INfEnA0G+gv1M0+{Txqjad;2AvpRTS#N@e$oYlR+}(nL^7XQQPUZObXSqcwDF@EBv@1e#Bi=4+Hei6yN%3e4?S(H{BF>@A`YWCF<m8&6u$'
    'Rvw8q!1#f1>hwa02HRM306=PWR&47M4TXhllvY+_#p!16;>|o{@8M~N41A0<<TuX@r4h3C0P|pq+#IO5owuoYa=;1RahtAwIB*-}e6zY^j0XhL'
    'e7M_v0kvrfJl#t8;IIo?vOt-@o4o>Z_EBn?9I{YScwkMMfcVUo=_J(HSs@=EZoG!^a6iwV_m%*bnB4?;r(jM88L1TX1(@Jcw%$z72?TaR8yk|a'
    '`)M@9)QH#<EB7hy$}v3sm$vB#?8&hYk2FIEjUL;e7`yG1h^^EHOMb-Ur}7S0jRJxK#_<xh1~+I(Q&#iM92q+xyUxqQ{cd7%c_J5qz5qU}aC!w1'
    'RcVS_Ih5#Qb)mKi(i8olxt>i_wnZj$M6702o8#@*aMAll7P(#oeP-}X1N#+$(opkUq#Oz@K=8n=<dR&NsN(qGh2l1-eg>4lh8WZ?k05SW=e}Ls'
    '?3TFEQMNh*hQl`5rBRx(;9jkmich*qcYf=hwLl@3{ld*{8l?Q?m2gOj`DVH4Nq3`XCVaSx$My{FUIL)qV7qL?Gol(K+FEOfMZndx1Nt>$ZoCRs'
    'iWLb6?}~bT|AYS}wX=SDe2^)6+X0m+{oAR55k^O54QV@l>#|wan!&pg4&ecJ%+wt=i~~YiL5rKGQw0Bbc-h~;-kAw13DPw!YcU6j-7KIufY(EF'
    'bL{;O4xkeRf!ZIbCBz5sUD-*n2%c2y?HBQQ$3B9cB$SGEQFw;}a-XIp5$mX0Ko`KM<P6BGco(UFHuo+R>WbLpWIw~L?$`1WS?J68{<Iwq?Z1v%'
    'Xf)WmDK4`H^jVCQFs-$@`k4?+mG$D)U!~{4-U-kJBfus=-YaHb4k!x0J)C9#b#S<z8v2y#V+Ta{_^>T#v1`aY6>AGUN0d5Fc;bgsI5Lc#pS@X9'
    '`}Z2l{=IGS_~+?7cULj+oVL_4TOyvG^miB}okI-3b*H%}u+uDnemxhCUx40c&5>=QE3o9>JEHjaO53KUvcC|6(iK6~L$cYp8L%ex!ZrkCt%kJa'
    '3Q7TxlIxoWA&&qk-W~@^IQGgiqz+ifmj<kH-lq?qXnV6r>$O^Zff_Xf9&|d;2%7+m9L)y<_PfLO_u^cp=1ng6r_#z4%A^Aj`0uFICyH7AvA2@s'
    'N^>Dw><c0%;5#$$oSRTtOU2Q4hwbe%(SHn7iGF>Nx)rIe`@>O+j#2Iz`#l)o3@ykAczz%ZyamgV`QU_-^9gCpmryx;$=(;bjcy)_CQlxLl+`tM'
    'zVRvW>$C5i0_QyI91{^$6xD)P!>xLh1Q}c9Mh31Ez{56xLa%IxdTo2q>6UHC!IhhyDe2|(#1`aDmthBiu>A=SNaK%*0O~WvQrQ<e@dH83%&6TW'
    'mTWT<?}n;SosPpuX=UmDT<y&e2sZRCjYDBZQFh`HGHsaFoI2713fdw4SVI>e!58#THFi?}+@_uZU9pg~x^TVSfkwZUg(ebdqt<*yDNeK@Fq3|$'
    'E>@JZf~YdaY?W-Jk!CU+^2~iy$MMyb0V2Tmt)Be(<?(JeT7aLYw>2O(s?sadP|r<K9HKE;4I3p|?uKbL78DDEU;qQ+kv!Xb7j@ZrbF)MDSJTOp'
    '03ki^=@kJmAd^V5MwE8axi{&xyOt^&PaJkKl^{Fz1GQtgbBC1-rkRUvIWHxf>&=$%j(lY+*nTOtMoNS4bwzMEwo9W#(Pg)}!Y*|HacL$Sscx9)'
    '3vw$RW9(XT^pF{?9oa*KchZbco(5e4Faf%W2R4b@L2b`qoIrWGlKCgB(1jMji(DedR=ZLed4JY)@5*)Y!JpW1{Iuuv+wh-W6Md#*))76btpUDf'
    'kc2|tJA?9NX^_ohAtW5{sV@?yE_-&JtN-qG@z*Q**3VS<d-ab!J+|{@&t=S^3OPqV6q=$SO1kMn-Q-e)r3xSj?}4T?^fLh2XA4#Vg^SJir97xH'
    ';dWPMmuJ!6&U@4F(HTT^;=mPMd$DyB@?)87sX^^R_ScFk6$)Mtn2l0Tq)w2}1k@etSmhpPZ`16(X-MMFNc%fa-(sX)8+I$=qi}i>j|47La~_Bv'
    '+dw-JT-G;$g~k*2;Aa%jOJIJ#(vi%i-twb=mC`-m&xbvApz4h+c(_(3M-u@C!bE0at3(<G3{yb~N~Ug32C1>2G}s$a*>)ILRV%#fcl-XfeZ1&n'
    '8^5ZYe%JHoU!I@d6;9+xpD{j;P%=<wDs<|_D)7MCc#}Ll&6yk>dHe+KXS<MR_sXX0xd@}^4p_&Y!msvb956$pj_*LK;k<YTS4L&_%__d**mvKF'
    'iFgC$8V$0#s*%;CzQvxp%_exA0>9GrfuvF#Vh4|uTQE)=FailoZ9NetClie?iQ>}O$#^@8${QW~Z_Lu!!iO78Yn<=z5Hj*t@H}y1Y)2ux6-SAI'
    'P59LmJU8kr3KS&}HJ8ylepW_orr^Eg?lT+S1^Wlp%&+bPvDts<tf=)TJ>Q+i<Ku?n@a1;t@%rT7)^g1veXzC0a*_uHHiZN@$HCO(Yq!24Gs$co'
    '@U8~2l?T_^TEkA9PV2zZSKaUS$Joni5#J7~|9)h>JX+z@ryn_i1RI}+<S-*hVssr9Y)Ho^ov=fyh||q38JnCpg_c8Dd6kWvbwB&~G`jnC`?Mr2'
    ')y6ghqGup0I)rtb)DMrLa~5G{uwWT2*rp9dnYIT=u(tXxN^8><X$jfi<hdWNO^8k42Iah<4P7X>oeHJlX-h-c)~g>hY4*%ZK|+?mAVM^;lgqCp'
    'OhF9nO`iKzndJl|x@(`9fC>h}A#3F|jL3+&Cq<B}O*B;l1Wg)D=Bojau--|lliqzP;M=Pv1b5p>^uPVWw~mUPKI0A`j|ed|&2sFd%-#*|(gLiH'
    ')EcN=s-|>n>hSp0KB=$6TeRhvXGk}B5AFH+a_%qBd)XgI4QbN`4gilBJSr90IIoywn+%{~$#7Eu@$jm^&ZNvxWtc$W=H*MU;Wm4An_Jkl&A&&Q'
    'I7*+HHp;GcPHNRy+eGOC@C=!%b*WL<H%ODj>!?Z*&!`z2$N-ow%@VEo*@b-3=l1Duq91$DF4pr}8(<l1(TwS{`pg9k4LT(^wbj}|QwQ4ORTOET'
    '1>68!*smB2yL@l^7M_2(Jp8`V_<i#Y2T)ePz!29Kv~Y~p8{SIJN{ibO=o<s<Y19qhf$DrmH3PuI&K?}IT%}Tfn~b#iJnoI!(tlwF1bge7c?g)8'
    'MJ}@32&40Hk&!xqQlyf}5O1SowuMZPPP@A8{;}Jcs|Dc8`5#wioZ0z7q2rWE;W@7vh%d36X2IrpSr4EFXvDJ=9-VYb`dI9BQs7nl4T@k)H!1^r'
    '=d?*&^JZ)J>l_zRYSmaynhVonwSGjv&C!bnHhfR|Zw>(+tC3@?i;ujfTf()kx#v4@TViuo0z)y5=ErQSX+^1n%)Zg0IY;$~DB?9WAzN&4E8Atf'
    'c00;akR+!YCsF+F^VSHpWWss2+Q{8g<&{L^&4Y2<X<{m&nShTU_@@Pp4#hVSS;WX*Ix$O&UwZ>6v3MUe`n~AW6JE*Nq3H57w}pcJ3TRQWp4N@c'
    '!xk;xP#FS6j~;%*rwr&9rlZ-QWrqTQp7KOv#mH>=>9Y3Li}qJf+Va_1vuJH2J{ES%q2%%y5xA6AkSG-afJngv^5FPSiZg&Z>zFA~?qD@|^7AC%'
    '&D7`hZ###$PLVBZ?LkNtL!eh3RnwxWj<;MC^aM7de@dhiyGv65k|?jQ(=21iyPvLN8~5D?H}`CcD>VS|apS3wae@n~J=HmAjg>fGBEoATfIE(-'
    '`;1B*d&*`G+ng+?cy9!w?_cijTE7x3rF%-5l?34L5#cR&u~H->%3bW!5?VwpSvY7Sifn|;q-_)+jpb#XrAqN_Q_tU40sMgNI{+YLPxV4P0*2=g'
    'G+{uVj05-+wJH%>5A{s6)d95u@u3D&+a}ap6X<?j85RRT{B-p5DDkiL{I2cSyFX7~&VL{az1`{U06TKZ&atHKTmZr`l6bEwW`t2``e$R$%s`Vz'
    'M;)nGJXn4r<Z#Qq{O-~8_wI;q%h}%=oM&p3a-CF5_INbQDVv<jCh9+H&Vh<_D0-u3!&w8QR)iSm-*u%Y0hHoyc0dYV_Qb1!U)tlo_trEiBJ1PB'
    'bv#x_q;LRYM3dcSy-1^7Zz;fAkmCWV5o8m4$+O*Yajs}=zLACsN6A?Uc0j3<4p{1^uj+k^T~TgFQ9}{aF5V$GNybW)me|DuP<P&v0Z$k3pSVI5'
    '<~=PBdbY{`{PTG2m6H>^_0g0zbHMU}m{_Dm6rpS#(*34t6cE=gj|oH+wSroPn2?PCR96lH^%$eezkI)TL?k(h&Dt8gv1~)BnubuQupxJbUO`*R'
    '=#Flss)|Gk%9uOyzl{d`W!dlg-Iqx3Matd7=g;T+&tLz(cZx+NHKnk*;=OpG9;TTM!Hm*XGBp;Z_fQz-&O}5?^KDC{fO)H%7<QVv+pOU6@-WYL'
    'dO5v*y|Y<oC-x7iun%;4rxNiZ;;a+#97dUluIWtosk9+I_kg62{m5C>xVi)eS2nkO1*1LgJN*SpJcLg9K||5LPR7zY=~pUwhU5dP>j<1!iR{`y'
    '_>_v8U_dg1thyhZjkjIElm4ccT{sbG!RWH*@Sf|%Cp&ZuV&<ySq+u5TD|cI^us<J#*bI#s2s8>MN>|f+>9z~F%H!J{&DvQ?i=e>Ov%!^TD5eW|'
    'OxGEEv3M&=|EdlS#shME@)GGqvqGTZ-SdYZego(GU$0%rP>xfY9VTak!rQ4zFw;Q5WZ(qtof$R;GuTKl@V<bsm8udBoTuich|>>&XaPF=r$!Wh'
    'WBbbvca$*kx$~B;M2~O*7RxEOn59|}OQh985k+uFr3+BXG@B_Y8T>QT`<2KL4+3t66aFR`etGx}M0oH1rQ!qaK+8e1fPp0-;TW>#l{G$h<44&{'
    'fB<m19~R#&eFRQQ1Gb@BFD<6>&tF60qi?V7j&twP5%NWvW(0N34mHcy5v_XO5P`{q*gI+yfad~9BiryiG*)WHMJs)&q*cE63i9eF|B5kB+Y;^`'
    '+U{qMaG0SLJg}>&arCN~tcs7<8LkZ3-t`n_V#fi`<9z5HeAs(xKlUZ-t;cBVqLAT7&-!I8xKOCT2T-icGgC9R0uK~R3n&0ILaoi>MNw>N0zRQA'
    '8#jAT-GJe&W{6#=NBNX}Tgs=cYx?SKldE7dKB{G3G;lL^Z)?;wlML3M1C3I^@<YOQ5XDkku~Z8P4v8wPE{dH$%+~y`g~|D{9o_wT()(TUhTg3K'
    'z^+KHp-v$0gmQ=K40z-cA&5mHi-<>Zx8Oh~m;}6|=UEA>n@bR-oDN?c|9HA4##p8Y6a-ZW*hw=WO(!0-8wch#TBk50c&4kB;w3r90@jAtD!}P2'
    '$y|k_b?#%<wJ(XT+1nOMa6M?tMu!JS1<{~d2!2Fe^uQi|+KHqI!00*Bi5ytXG|??}t3o||RgZf4vTLk~fTAEWvIQ+)rclF^ook%J!}c-QW`iAV'
    ';7@ct>;rEw#pdjwUHkI(Du2*>B2C<XS+!BVboOd!;thtaQVlzLGMi%96dwqjL$JjF<<rO96*ngD{%c=N@sw&mWM!{^Jn7}Kck(~8dcFmV0_DJ8'
    'P;A}w)(2^hGIW*Rn%HG|#;p+=P&y*(8)S}v!nNvnsOk8e$bZ+K(Y$AWhM%ow&`CjM9W=dm(@ngW=t0IP3J>HXqK-;?NFG?M3!%9cqxU`^v!e6E'
    'mv(<%kH(N1Hl;zg*=(ffzKd<shQmf5Md?lkQs#YZc!54ql{ryOmm02RrK6IuK@Wc~50AX)-Y>!3Yh+htLUPvDwOWaD*x@P4=aJx%y{X*LJ(U}*'
    'q&7)%GPs1`=ViGr7d|BMT?ZFBJ;yZ0FpdMdj;9&Cw_%s@#z%oX9B57tIcJgBwM|P6%^0X_wRBao$JeQ+$2Rn|-=A3$c7C7P762w+N-wztkfN;v'
    'M1~?x3!p8y25$sw8TehZse}@qyS%)I${&cCzP4WMEpJHfIW$WRH){MuF$OuFey=URxp&vnjAvU|r_PI;_=(_Es$1MXMEe1#>96HojmZiYwRw19'
    '9d=S9GKQXLJ<NH{f^QapLW!1|7lD_)!)3g~fjwCKN+pGJY#)-`-+6D2T>(R*ciR}MBRveXd>tGsrwjv<Ov%H$6nJ@20k9OS3S)ZFrGtjA$L}`0'
    'wU^6gJomi;8Z>Z<L<H0~a0)5AZoyz;EHv(oRF7e&9H=6QXG$`HQBN()lu}$V`efqq3w<g36`GQ4(IwZFS4owUx5|E2SBgvzgiA3TBvbk)qAIXm'
    '8o1E_f@lc9^^12Gefl{u(Wf@`e81D_fl|zAg>3-33*ZkB1|q2&p{r&Dd?1nqhez>h#`amYx5_?VVO#40Bh?>k%zb;h?MUnY;?Mu*+SI(Iu6NVO'
    'h$zy@C~C{Ifb7zLqI8c3O9+xW135fD<T~@lm{z{&&KDEnjjz0Rq}M!E?EuN<;0&ZmmTu_=>`yv<mqN?U(or)}{TVx>phi;htT+#_w?YR<*ZCOi'
    'w(qip$Cg<Wg@b?s5a$^oG6Q7^DYpr}-3`cs0%J#-wHDVn8bdoQUH~sIGEBbN8+bj8yV6gOw;<HD7`C(N$5S{F%aDj7qO*&$6`xHIDl<^i75F82'
    'b|WJCf#6oE9oF9N`L9VRIz@sshEu<TQ!X)9%e#0&7VKE*sM&3k<Ibi4jHOL*&e4rXYOSm5(t5jjn0fcmp36?`gA*s~In7TEmpEqyN-T6>c9xSC'
    'oSdrJ3LYFJX0$YgeJNaP>tTf-ONlo+`F~Z0-nsM=1q*I$89|-pG*b3VD^xl1h;m2kP&PT#DrL5wT%^T(;ki^&vphV%;nDf_ncK9}9-xtGD1hQk'
    ')#SmgS;0sQ?GGpZ0+2Mqq`PafY)t9c&EzC1g>Jp8Q?qRB0V28Amx0aLJpu^ujwAYEYFBJrAKA{3uvx1EUJ5`+z5q1@aQO&wg`FEyTRMg2cctck'
    ';2ofLg`NExId6(mX=trA633^UM2d}XccPp6MgoHZ2aHHEGWxJFC5PV+bLm!+e>&BCzO=_*wr}&g<@uq%%pLZih>i+Y2lC)~S*<*Q#eS_h7M|`o'
    'Ky6^`BKR>YGe}^lh@w&^f!#_^<cFp%_>3DW;2%DT&ZkdLC|Y0I&hvuQU~f_DE(L1J3J|iIXhPK)*~;pGEfpBd@luo~EI>9?aOe|7AuTO)(@#9{'
    'Hy@<^J9XrcWV>)4Cxke8xkWo%K;Z!f;4?l)p`2ht#TqX1n0w%7nJ3}oXv=FAbAv~|e|m2Bd;RdehA}cJKgZ^PaH&kJI<pJpo7o|WuoDCLrKozW'
    'k)z6Gle{ZqHQ0jSDL)w+-wOY=rX5b&sIvgJBT>32v$3{-7J3>HmZ}k`J;!bk;5vkMNIsg>M;;~kmESG9ZU6lzf8B<CZ}iv|<;gh(mTGiVX-^gz'
    '4yb(gG*kCAwHAS3e4|~Kg3beN38(HyULGg?m|?~Cuv^p7Uyu7AU+(^M?}-H=nIdmyYpYR>n$ZBQ>;UsdR<_XrrZmdO@^(#-@bE3GRp|n`G~Wis'
    'eD^}*pP%__QrkXc{`d34!{<Bw_WiBt{e+PDPo&7TGqr99Tw=rIi9yv??TB$I@(ajCFOiVfc;-WoO$%rR<Q^59v@O*=d@$bJehn}8S5`rPDU=Z4'
    'DGN~BS~7Kd!<*Vz3efN5Sa`3#G~U$K9IS~=OlTymekD5`gO4{|#Q9<Kh?CPb59Bm+cea!~80ZLooh|ajAgfs5TZ&QiNx^v_9kzlY-AGQ+wCDhS'
    '(oz7_gqtqp>G`EUUCr|#)5wZJth6#F=ti>*aMAUWdZ!42mr{H|a*UNCp{;wo8KL)RcVWf-kbctvi@4UlV+PG<Iky`#C}40sGM^@Es;2^l0x=*-'
    'jzELKO>Q{?<oHy3seouJ?Vfj;)4#Qdubu#>YpV&GIHA?W_nrp`;gn_yphNblup?kr02%<z+9^ptCmY<naS=tL=9TM9w9y=~n%yBC-~o!gnM@Z;'
    '7qGLDfK#59_mC2(b{&-6CE}%<W~~hNsOCb;G1Hgb<>tp!1iy0)uTD0PXWbL3Fv7|pnS)T(77$wEWIrQ4sFTW|OANNQ=^2?!d_(3cH5FkyGl=8z'
    'RH3>9mcm~1oym|<B#Y8{-KMS<d$;+SL%^n?emZzq9i)<==&e`K;LvRkCWJ&+lGUJc{V}(^?_9#wM%7e`7=Y3k*h0mI;e`4fn@<A7R6M7#g(9cG'
    'N3Hn=%7R}8?(^93vbtg{HM&D$YQL`n2)O};)-rhpg3X~FWU*muPr4$ZK=|1MUKk3a)Bu##DFP&eT3*TPs$aMsvxp1f>R!KOon>@@HelyKu|VLZ'
    'hzE==3HgmEGE@Z$6gJLBpbR^01^?<mfO+Ypoy_f#$PW8OMkUe3(_ou|5ZE9Jwiq_6Ez~t^-wwp&0kyrs`$4`9+K|Q)O)>MTLB8I$a5oAR?{FMy'
    '2@CiZ`*Ix5E>kcz@vtE`><^24q>j#8sLz68j5LhWN3VIn#j;YoMnbc<fBe(=%j2F$68ILFj89H|9YA??69L562CpO<!+zrII4zFSCqB}PEme-h'
    'M<T+NVMg%&_G|dZ>8W8Ad(DTYur~_Sta))EyY3oLZRyeDsnvOW@Uuij7oEZI4w9Y5p(eITfr6EkX8u8G)9+ixHNQ6JEdXq0qqC*#ID4`=?wGJ8'
    '5JMdxL}>pV2M^|Irz$oTD8~p=0dH3#@WzyzE<*o!ygQHc^WC4P?F8WRGIj^SO1CjJ+G<rpGbpunrU<Ase8t8FQ*FvFdo7#@unuc9fpV4XrIZ!F'
    '@b5Zj|KM^y>pd?YsRpl4Pwn&T$-vG5wMnQ7Blb)`2V^G?W<4qGWoI6qL)j%&;ZadBokul8#ssA;w7gt3coi3psNE0udl0IGD5zrC(%J)Zl0hj`'
    'JAq&kPClZjCxnccm{RbYzHUJpj;(^F*Jf6iW47El-`?*uc+L>mOM;>;;1U!~kJ#8H$Sl|(U`<vsDs}6TLNv2VFiGsz2+ETYw-T5Nw~eN+F?4s{'
    'YqYPK(m<Jt97Cdt854}j0CQM+%VwkWLUm3h6<&<BIf9N9sK0<;U&(^TAM`~1j#WIj*JkhCWl&qqP{GB6Neq`pMdjEG(+XR%*eWuTFB8O1QBO=l'
    'Em7p)1`3;eSjpsClW&`D0m4sxSFb}19b3|rVRP5ntLB)HZN(}AJ+eU<pKBp3<ght#2?Z_z&=Zv(M_$(M+wKGwZKm22hgHAkr=7|X2r)QHh|(ZP'
    'R1Ld$6OdpB59oxBu~{^s;t`KT&7IfA#?gmq<@JDn?zXGB;MccAJI{J2d*<iV803eII<BZ3Gt0EObtG90#VOcz2F^-guBtksE1*l>qC|?$xf)h='
    'wd7;g@%VhXwie2;vC&WJ%o!Vgm_m`u$&!flH7Lnb#RRI9Q*%W-2ozAeH(BW5xH`@W_O`(4_wz+hJOydbS2nu~#*dm=);38G;*#QOV2{z@D5axi'
    'HWq-NgxIMj3XFx4!pk9(#)>HA=#MyF1H9UEMx?~(r~`<7n`=S@#SB87yo?Aa>#RdK%@#WtI+cb3&j}3>ee$vBN?a}*e`9FH))3A(ZN41K`fX?>'
    'XgcsX7*PWICS*b;R*&$(yJ>8y;5d^~&qREYJSCKAr*yNDC(pK&8=sxG%hNV<{EP+dBwxq7w^W#3b27+WT5_5`w^sYC38juI*<RYL1(XWt(2NA+'
    'GTHPSmJJrA+FPGI|3fbiuU-V#PM?jTcL+6{jB@NeG}JO5kb*<4Yz>WU#HMOI*43%*VA$AYo1L6_SdE;SRBn61cCBMX8vW!wr<f4ZsMDhj59G(K'
    '8?gl>Mo?J_Cj}W=D0pTRz{^_v_E^pAzHcx4qwnHP!RO6Y{8|M8t?)>qG!4%agj~d%h7TIy0c7hqLRTZAPw;i4=e#qZyu2&08QE<GaFdA#aSpwI'
    '`ErGDaBOb6g49(5-U5P*$LmPE6pfj-+8C)^UXYSOF)SM-qgeW#g#f0t^1gQUr?1y3)xdsfz348_knT&|#_fYgE%yK$zD~pyRkX7SUQq+xbe@*K'
    'SkXpkL!|yT0C8U(b5g_o-+%P&!@qXxTT~`koT*yrV=*(2n+8y!RPO3ow~;t?wd21gmS}5gsZY9WAt%YQQ@9+Rk+y@KE2X<6p~fy!PZs6&#^QjX'
    'ID~0|ceSRGBJEz9^&o%^1caXfR&HhH<>^2E9p#YHeRtQ}_;pVo+=rN)+wLn|$fIi<Ioc-3LLflFZJLb68I};`4Tqo%BJm>I20K4)9Z%19S9UZ$'
    '=->n1L8DS8ibkNFGzE||K7DW0We@H}yI>$&a6y{tu~%|BBxGI53snCTJNhl5Uh9VFR6U5j+orNf0fs~^gg!%;Y?_-C?=hyJY$)|^5nYJ3Nl-;U'
    ';FbK%kLjZQ?{0|ycpb8NxIFFlMa<2_u7mkk!XLqhhd2j20wE&6V=Fay9TI7yGO5W~QUuec^F{7?=_GA_x-H_{b6;zaFey;`wFW<nPy`$pbQ{b{'
    'Mu96RS34-Zx2dh>g4|E1-4u{dlbLnp?3zNnot!-K=$mTKYf^o)&AKu^SUeq<=>;@+UzfT=f}IoeV0si@N5z(CY%`8Wx3vJndfv8J>!gLTGBV5c'
    'h<4!9S3#vcSvNoX@PRUIPP0yBcZBIiX9iYsVML-+>}9JXRdS&Sw(UL9p03WyJ$uW3u5o{BXL>cLU1v@Qoay|)zVsH^#Zsxn6#;9eK@d%uwlP#G'
    'gHnc7XB~oflSAgk%gPBa-Ov=`T1l=6&?l2wdLUw27*$SLp&%p0kJM(gyq>3qo881Pk)DvBhEGiVX6?mvyT00&`*CWI=bdRdy!Xk+9-QPGeo&bd'
    'G2=$>(&;sh%?e;TXmaGF4%)@}j{H=Ll%<uu3Abws{;HyJeSB*4>`a<d4A`@`y@IGkWEli?;t$Ng&j**+1WCyXibZj>jsqM8kXxBR-ma<k+n>H^'
    'f?U-mLWKoiQ7nbLzO^BlT46}{B%IWMMUG{snL*wypaXz>vkZ3kX%4cQ1&g<frZ+tIOS_l5y}1PBNT-qFuwedu8=VN$Y;(Nr4Y|1%eb*sr;BQe8'
    '?OlRLS*vtnj|+@itTR8onV(<o2_mjC{$?X@caouOxei}=ksC~))(MX-a-$%Cn3<7D0j`yb*4Wq+RV^dmE(w9V)uUhZ>E&)$*%?p<U}5rqkR8o!'
    'Y>Xe(8Cf$T*YJ)3f7gP91j-p)#XE@};D{&VD?ufDyG`&b>XqKHSMju=ja_k;;*3&tWor#pwvgvCQa2H6Fn-{Z_Z(i8;W$#~g$5E@C3aI|&r2CY'
    'Ao80un!jss(Gv(j(+CcU2X1a~DreHmn<5c0(q`9#)ps}OR0B|rCkB0KEW&mr?rh6XYeN0a<>m48_8RubhzCejV|w43<2|?xFA7m1T<C@ezLS0z'
    '&Czy{qZp{$yg>=J^peWzXxpVyZfR8W>T|SH%~JF}$^_f6ZFAi-M4LW<Mge)IQJtYkpzA&SKlZ-0%Z}qX@*kP?ethIZvb0NE5@E`F_RE~Csx0za'
    'gBk2RNI}1TBYFT3lB(|jp1ymIbk?@;B4~AWXJtlIWk$FJPE4;|r`<*xXRT0#f40%#^FF}M?a+6C^hdpZv+GiZSDI-wLU*Pxv9bLqJeN~8XVf$z'
    '(-8ni1zV;E^C%M8Ler^+nU`vkt{k!pwjZy4I_b~5Jq~#D!2f1aTa4Y(V3MBZx{*#C*sIv!^mhr>&yMh)ZN|oiWH8HIR!aZXr2`*+C-w7X4nk$N'
    'lk|a**v^6?l8nK&aGWjW6=x|(so=*7_UkFhDBwSSsW55m3sLjDmWL--yZgP%ey@15u1{mBT{v?=ZUod%L!;q8;89lMsT{`zd&;&_n5(HyXIA;r'
    'e_m(LM^E@yN^T$h`SYu2#@YmCjudYkjX9i;v(h}!H%nW4c2I(qIq$%OJ6o|3V}<iCTdTkdg_h6rwfK~E$laan-1;LWT5Ef+3H20}De(kX$>ixe'
    'S?CdJ$E*=Gcs4*hz_xC)4mV-|ejBTpH-~=+iY4znKSy+@ZK<(u*fb;`wo0;=RRD)cn+{)!n2`;sB2e}KcbS3K>S28X=ReN~^ivkVV=I0AczwpC'
    'pfyGM?<rCAI|2t$WIJRKF0RVe83sU7@jVK~GQbj|Ypw!mL`2)FU(#0ddEL;TvXXNu|8At}R165O@6#HGs)hVHupnD0S3=JkEiTXl516QesfnA&'
    '|HV}N>9QHxAHE%)e%{MVpy}IH%{1a@15KHHq<f1PnkZ=MNtveu^z^h75-NaN5KL@wztV`!K3`}Bz|}i*gNh_F=p&eVKt#0^zz3`*p6ldn)FGzW'
    'z)(=r0RQn<n-&v!^>50$D?M6!d??+W=4n@IauRS--3G5)tMv{*Vm6?*?4wv@qk(c<3orB3ZGAAV<vOruP$~TCc*1?A)z_yig6`M-M;%w^q6&{&'
    'rwt9sm3_f^(8twB%01ZFLYp-^l<kmOkC`JtbK0&YQ#zJI%}b%`czBsO@8QZR49%P`sVLn&&jHZZP3X1K9?1g~S?d6nVl^!dgQZ~?O`cnW->o_>'
    'Tr3y(9#cRzubx><<j5y#UlPDO@4QSvky@}zTF16WH4ld4sw!Y~3H6b|h|PksN0r&O94fyc06EV-9os@mtTT-)g`wk+rVJ-0H}eQkXOjnS4Xn}('
    'K|-1+?${0pOV`87W7K}$9r>qK!Tsjk&}eEPE)NRj(q1Pf#8k~)Oapli-cAm+&I&f_gSJ7{Tx@m_)nM%EgP7wJ?^8qW&QUNzscvKCwX*{TK($vY'
    '!KJP&f7<&5p(qZ~6~)j<54)!4?wY0L$13UOeDn$)r0rS4Bn76hy#JsF^Nd0vW$bDMibuV}9>!j&xmDCgH~N&&kd}=tg_HHVKp-i{bQu70HEw^t'
    '`C0D9-eDvbf&4TB#apDZ87W)sY};`_=rd1X+h9Gim(#+7C70|rML+yfaJi&(kWBa1*M0TocDoFD&i0iqx`6{f*vT_1yIC97(~!A*bVBC5b+fZm'
    'H(69buWW1YZg6(mtbEU^H<vOW-HR*4kMWMyy4p<-cx>s8$}cG6sDza8c6~Uy1vo4h>x#fGb6|_e%EmB7d!UmQqw8w3Lf&l*3x@L6!l4FAZu0na'
    'RGu4c?zA+VF36|&E%<lo)mIsra+U?90#wkfh0f}6zN62C5e+-3=4f;0YzY<vE@UT)L8AN|Qc?m&6gp!XjR8xkKp*&JFD_!Ki~Rti8jkx0@0_UL'
    '{r2iz72lIW<kfcp(wGH|l&0lUwRzfShoY6vhA1F81I&WVQJHSgFVJ*9S{QT1Gm5!{71y>ZUSw_3>gN}QvhAouQY+&|Ij7vQ(W3`kOvbo^-7I5L'
    'FgoM&nAq4pgYepPm{VObaT_n=FSdILgl$}G=}ptHfQbkZRBn)SPKHN(qToFfai<td9$Is%Ddr9YTw-ozagv;!^T)XPJYB`@T|N)qaiT&<8ZD)a'
    'lv^!Rt?j57KIF|+K6=0@+Rg{8kTXtzxSH!+cQp8%4{=pGGw$lNHsP@bkX_?QpEaly8w&A(Mz8=Q(o1m)f;Te|V$X5g(QIg2ZiMi1C$#qwFTeb9'
    'pQ}bYf#2waV1_jOvV(|r<k@XtH#ke@5_;%VUVn}VTuX9f#Wo6XuQX$_GY2l=L*PHZ7N9-X44Dsrs!!E`_V{j)qlT+&%?n`~+QtS;F-1qS)iUVv'
    'IKg1ks)M1{BiAA~Z?8@_KY<zTqCg4@p#ak%6Uf5n>MDviMZ<zo6b2Wjo1;y9wODd$txuB-!bv;QR9Tv3)|dO;u0F^otcGG0^uST0oOUp@u5$%U'
    'J7rfu=4dM8T1fcwoJ~l+&WbdZd)Hc8TV9NY9FBEM+uz`#bE2Hl&D^>mU0X!O!?ShJ2?5eDv<Z)~5W4r~$i$tY78<R){34(L;O#~9W7#9KHeYSD'
    ';Hu7puz@2X*n6h@lLv@%6=2B#IH9l&8ZVU|wcFMLs~~p#s?8*YX!5cCd*>Rsr=Kn2eaq~r6J>+;Ag0(|i`Xg%u%Qj~CrJeA4slRTv^)H+Qm=U~'
    'uZ0fsY~LTqK!(xTIB@~G04Rqfgt7(LjqDoZ<Rwb>F45T}cp*Fl`t!me_oR7s7Mo2u*!A!cKb(Fbg#hQ&`@O3oK+)!qZ9>AMwdoM)X_LvpPOLU%'
    '3Zl|;J9;WcI$p3OK;6;B;ghxX2=#&VVKX^AbH_rDE8k)<o|6pC(3pj{E($mY)X_5Ml{&zN6*}=@o!$$`Y3qoun&Z;DIvrzvJKo;N>r=myo15E*'
    'bHeJ>ze;#SpKPO{oo6F(;b5hAqwu=9dkNldqxptCs>nwnjcIZ-R+hulHpXKE>XTQa8TgbpD^z<6V-t$s>RJwBYEP8cU?IqZR$c}0n|4RY0z+KN'
    'ZPoCMVt8;_=({FlY;y}VkO^#v86rHdq{?=w9Mh$C4t6w3TC220OkrS2nW`>kl%{TZp8R5*ar5|E?@s-T{i)E-T2N`K6gaYqpTc%bg9_{h1td#G'
    '#*7t&2U`XIi?<CZ+k36387eCpYW-2q{o%#$FTfbvh!mn3ZS+9j)d9>MZ1wDQ11F`iY;vgD1HeMI&fC_50GV4W>vrvE4qSnCWzPgX+d!Q?@i|h6'
    '9uJO4kAk2X&X~=@F-I&faw0=1p&9@(asdLARTc`yyLb#Yqi413nFb<zX2U-0tt*?tR*5a~7SElii4&$R)Fz7FQhlC11TLFgOX#c%tKO>WT|V}u'
    'U;QXQac+l4-1n!xrwP{`XbS~#d?}@uZUdWm9CBq7%PR<_2$_&NB-8|KVt|Fo%SEvM))zase&(#_YAH_#^Y0Jnw<ejLQszAHZ?a7PfNu&aZ(8a#'
    '73^4_sGu4gZEMUx1*Vs^kD#E1`X?5Qbafo*!a)YsKgk_H+{50*u1vgfp8kRXXi7kGzW8Z49|FEZWg>u1BvDR;Y&LL3f$Bs;Dz&c$kkZSYx8J|`'
    'e!F*)?Ev^CWB)}SntIusf9O0{vyXWb`~VxvK6M?=VuJ*vP5Qu5>`K+H4o~yTbA7-2C|}|?S6sp7v$f|pKQ_-g%b2&-$s}B+E)JbAz|u^mqXOVs'
    '8WgIVSn%k+aHt;XDK63v3a&q58Tb#V?hYCD7O#CKmwic8F6ekrwZS6);1dKplI?=X=XDgN`6)n&q(hh_eI<Mh{!@>B`FQ-Rr^EQY7TW~)3F6l~'
    'UZo1YKYMp<<)}i4RMnm?f{JB}4U4j15&l-H6(EOJma-(K9KVXIKbHg`{hEE|?P((Q^m=DsU=bnIP1M5)LuAmfl#-ixa+KW0FF=1YaGM5JkD%-L'
    'lFiTDb6xS95@I?qk-i)J-{F*f!~h01Ga%3OzEYsACA@rZD|T%GE1(k_1@JoxF)G9ucGT);#llt|?&&jylFvQ<FUIE|*71eh?cChPDgvTul3Egd'
    'U#2k?=poSzy^X5OUmS8Ppbgu#dePp7DG?NBnpI86z$!<rg*qktgLl)-r}=izsoR$b*lcEXs0K;83A9$Cr?QVVr8$w2rqN0NXz+Ta(z(*yt5MN)'
    '#nNKE<QMrMXt38*Jn(5yi3egT0ro5Cekk-=vvWkr>^M`@ZywYOVBywe1IFxXiFqYPWP=a(k}JA>e0cM?H{!rPO2AZcR5vJbq?!Q2qi|xT#sDDR'
    '95QeL=`q<+l#f@_m3o5+zSZb?wds<pK?)mt=k^)TITdBF))XlTXC2~cW)Vwc3DAe`o2AbOrE+?R*ik@HJO%gFt|m=he#CliE&WIBf7-iUqzE|@'
    '7Y7CgxKSQ)@@jhcRzc<ryN$aHRP=~+kPF?AgMx^dR6L62N+i+dc+utX4qpHK=I&~f)7WdPaZRyvD60;XH5DJhv(W)N1nHU9@x}O>Q|X+XVBeW('
    '0e}*`6{uL3cS5lob9DLCMR2+~?FrLnq%x2?px(wiw#Jigpji*N(opB!Fx}7@I3YVnC&<p3;|DbAO87RbVM>3|&42DoC|<L1!KGAvG~G1`0PooU'
    'Gl7M0?y_>y%%)Zv$B)^gZ)--mG;xNuXicd#+9T2Xm0&q<?@xRBIAfrCm_C3(XwBe#$;AZMEPw#EI6zq|zEb=Hh0z%Hq*4Dh$71SO^}D-h%;BG7'
    'Tgmw;-3hF#A!))(=8zfrtmtN4I%G7%IhNYfOx-yu(5p95Eaw{oy~UQ!X!x)fxu|~MPVib^-|iV3N5EpC_jIkZq~_dg4rI1WHZ&>#KRY&uIy-DG'
    'Y?(#YcDar&$X|Iix5$$Se`su->n#Otb_kvn9hGVUCX*WEAFp%zkWC!gY^Go>sFuscyT}`vYCv3OLIqs4rsR??6Iyrr=8Ek$JG-2H*NE$Z;%8$='
    'l9f6gUcIMYyGKzhG-HlHx@Hubcx~dBSX)4_)s_4b_RnQjvQ4$?*i(#?EnSt4ABT#m7LhVC4aTtBi=$u}c#F+}qjqIl$@IO@QBYQN>Cb_$5-#^q'
    '+QXgh$qP(H^1>zutjA92hC))n_Us~0NWq233@KQ_s*u$Xj?M6xA|c>xTeWWm>ry!O<o`~7zB;R^O^H;t)MTE1f>Swzd^Ld!EB8w5$j$_faexcU'
    'R0+Jf=milMBXtK>qdG38V^<*$x4gdi@%DB{s3p`n8hB#Um<k<U8ePYD!43A(qCpXlV7fW6c>=f+Xuqg9dady&anaY9KeMracM*8|SNF=E{=HiZ'
    '$|1Di=yBy({470seb&5bYZNfWY$mNWD{6<goIpZh0rxGLhm2u$C3*2ZfnUjP-!%zSqJ@->N#~0JlIWdrBl}4MyXr?TmO6f3*zYkw(nE5oQ-YwZ'
    'R#cKRmtN70{B-(Zzcqez9zLL?1Qa{!D1v^Z7T6)-5poI$TZ;kgLLPJA$;e#7<1uV6)8<M}gpR!aa;xfheQz`pWY++vqlS3)k&6q_0__5X_loLK'
    '+>EIgYGS3<+4Uh*eDOf?-f3Gxm3ond{N>DSvm3nZL&^nyG{eT3H_oW^IS58d<LoSzdEK-rZDJ@nc_6Mzb+8rqwXWEM@AB{8oNlgf_XMUkaa!F*'
    'I)ObR_-x7Qu(>I&olN1Tso;Xy;SZ}t$QkDATuMe6UEa5X+#h^<e0_@=f(&+jMUS)bc)eRp*sEEQgaHmw)~CcMC?7zFp+(=Q#O+y1R6&CKbdJ-+'
    'RSn&(C<c&L!X575mX=@EaP|6j><P&YZze@`TFOZ*=*Acd2r}>ya&@glB<X-8<|R$4P?hQCL`FjzGdZt%D}pu0F67}}ZmwQGg04R?6J*vs_2)`)'
    'vI#O9D@Q-Gt9gL^Vw(dlA|;p3$w_>T?oFMc4$k;Ys=^yk<K={sJEl55{Kz`;d}9Kg!$#0z)x^@qj7(DiAQBBW3|)QzkpZ!ghrrfd%)xk2VC%ud'
    '@Q15IJ6mnJP|kYal(%=?pHfGO?1=l>C-N_<Bx*ch?i`wC@DXumX)cwDk9`<LS_?&xs&kapK)nM;zR+5pq+xdUdmoj`!p&(2v(h*c(5oAelte#6'
    'BL+X!Ni(cMU+G*TPq~l+LTz=d?#}b27Q#!o)BVG#?{<cn0stIa0Xpf(P^gD$t&dUzEsIK#)HDp|Ywc{FFQAi==2EDW!>whu?1dKd_y$BvuO9AX'
    '|M;;|yHIJ}z(dDS)F@!4qy4Ds15^Y>-L>Ikq`qye0X&7Kt0jz7Wv<jSN_=rPzJGhuf4sZBIsIkZ?_X>gZ0B$Y&*ByADuXX(!y`CtG1)kHs@>Um'
    'gfEn4@p%}Uu7e)4p(G$j1Dq|6wj*b-FI644(aN3v7!+m42_w{vkcCHZWzw|S4*>%!*guN((2T5R>QB(JW%NMn4dm^%v|B9)*ORAIe8Ru-u+LSb'
    '(SQXUm!{NJXU+pvo~@o>FOF{Jg`&gw1VF_NRGx}tf!e)(cDJI7)ZjaIC2#cZ>fLG{_Eg@KC%qx0NWsH|??Ri9n0Z{SF`Gdj$mkWA#UtvcDZzw1'
    'd?`dOUi?ZR;ea!hzu6ogmh?{OhqGq4dpALrgVPRa%B36w+e~UGsbrow)+netAX~G7{2G9=*fK~7{zjaxTMaI)j6(d2nf%ju+&%=Z$et7n!^0G8'
    '<QZmeG)b61#rQm$=diq+qp~nSnP~GMp1{AOHeXg^Zw>_Jkf-Jkd#2%OWVKXDj>ZJ%u?-OKl4_(ANR;GUnF^xrr<MkYC0=Rx1vHZ5>IQ_y#r4qs'
    'bNv3y3Y<k18=;5L=mAi~7WM^C8&XBm4Zf93;uqL86S|w(wpcu;%RqJe3gNmKjtQ32Pp79)-)mB(!3sbGyZSq4sRn271EU`yACiD_1KSGQyC;=0'
    '!Lk%JoT^YrOv^LGg981RGTk5k^2;ayfAie3Qkf(dNRk5;8(#vslZYwg1V~~dGARJMlhlRS75vHw+km0~fz4I(D!Xm|A1>>Pnh$#t#3?vo9nS_y'
    '7DZhnb(2SqHdC`3n95VrF&b1pGg>MU_=M_|6Uleot!}Z~F1@;KU4OaV>mUM@3Vy^OSWKqQ4n82g*-SB(la|N;5hb3TQKq_Sv&qE(9w%Qo1HKwG'
    'tDrF#D<dCO#(RCe*AbW<jhF-(86`6I#(V7KM@ZeK$e!r{`hv7zqlRhKjb;m0He77FI!g_u=8G+<-|2>gKLx#j_HI_Bo$VZXPFmR_ry1UH@c_0p'
    's`MCk6wOU)OE_aXM~k+&-27Um|FRJOzTfT%98=+_4m=~xtbNZ_3wS=rSOKOCjRKK18_zWWvw^P|Y&74yAk|1)9e~jW<`Q?=?}R!x=k^^=Y>})D'
    '015|S!F?@#giuelP+qQ!ChfpqgW%Y7c7nxON^`vy?MV5n6PK5B;>GPLZ&c0?#e>-bHBK-UZgrp=3l3AMM+Fwp7;(LukS3^ZPTqin*~*{T5mv%V'
    '5C+LV9dtAF=1!+R^dI+Rn=>8)T+vc6JJvZtR;)OeM(P0aAS+aV;VE#ioiV1<B1^b9dYihsif}JK_u3t{zIt<eed=#XgLg+0IIUb=N>u%uwe*>3'
    'c$9MJpt!WQuzAYB_X!S5h~n7*os;FLV+&)!SYmYUQbX~dupsb|=EfL6XoJdP*&6mg*%GD%rH?OI<*05<3h!Z?H(*bESoU$3w(|0&Ov8m1w8^S$'
    '0tR=t*H_zr;m(OG;5~2wVyTbRs$gSx;Am}>DWz8~P^kd`H7Q`(NXTrJZYcsN@T$eF+6;~J7hM)e@8b{Kt>mt9GB7|!6{)m_h}BlXc1qC++q*4x'
    '7XxvcsiqB=l(3ufRx~uo*)3H{a=PT2p0PW{vtq}DGK&$(sgzteRBM7rK-~m$9)o>UQjo9|0M!<nJ_4n>ihpCJ<-=>)m#*l|-R(T_NdKdvvn}K5'
    'PFYNMdqkqN?)@mnP1?;?&PgKcW$JMcB=0!DMU$&^djmM{$a^TG3f_?bN>_7NE?-1}<+Z%oQ<gQR8PszhJr%_9oIY>B2FnQ}Y!))ichN_l{LL2V'
    'H|7dGC&J44-es4}B@5^7TH*w4!>bQeZb(4M!AFUfD=T<Z@M^HWSO*6W&73uPl{8&Tl-^H%b;zHCIZ*3kQ~Nz%{IkueHsazmQ*NzlQ<50u<LDao'
    '0+eo0o4J*C*D8PvYHN&UQYdlBt8Kl6UdgtY`|I0>cSUFCo3z>~cg4c8`2y#}NgxDL3D(hVdiYc_=$zK7G3aQm4_~{XtZrJ(NYr94@kw^WDsKvW'
    'NL<O&;HFLFsF^w&Zd*#&1v80w3LhY%ki#tdsnpnJd@Xb3UdKMAV+4%fgW|nfPho?E$EyQD)RPAo28{+D?A?QpP3nJui-BDNwTSI(Qq_VD;l0Hl'
    'tnMU2KIF;Y-*N4EmQ>LBn7u(piZyT}P;Oi){O)Q>K$N4iX^jqVoMdtqTfNqjV!@7CX{H_X+$Tg92h;B#+tnHQ=)~!H@QkiL^GLOWOM!UQso2M}'
    'IH4tC;B2MOK4~^4>nNL0kz|D1>h9sWv4_u)9>3G4zf6UITl|$>UX^K_E^MEpWpu2G*QKLW4+>vn;k=eZWL5AeM41PaY7KSW3ce6MO;%TQrT#Zn'
    'u>U%8t{xOMdUrC84G(yeb+n>L_clpWz**1!G82qurKfQzKu^UY&2G1I*;eOiX(dYwnS7vRIy2ACw#}xr+9q~w;0XecgMsHl9DLQ2Qe()GvwCdq'
    '8ONBzU<rihNGz?aa9>K!9P;d2zTBNzLJ?xSEWHZ>vE$5*7I`$Mvav<54$7#cQb9fNoL-Sk+O$$9+y5npmFQ6MFNzjg1ibf(y_Ip^IR>aFIm9ri'
    '5OO`Me?lsc7B3!{G*bY>AiKEKHG3MEAh{~lqQPTJTfTS~Uu^erulJk3>e)v}9W8mLP@5}k$^zq|ePpG1M#!MvpxyLq16x1>RpaBdeH)lT^p~Y('
    'y3Nn^F#h)>Z8~D8|Majv5N%_a*a`*4rLvnTZVe`t#~s0Bu+(g->m5KDc{C^p_*AT$*SyPP9GAn(9am6i=jn&@U!Wljj7n4>gi`6_G1TeE5ex=N'
    '2LFHphS(FJ-3DKzOhilv8|{;(8mlY8`eKuYcbbytMcPuKGFWDNc=oZ=a}+TiEoIMo<S0763at-#(~KyKrV&MJ9>r6mcy-gCU;NJ5g7{?4bN;>a'
    '2nGxcySC%MmWe#F5p<%^Ak2A(W=n}+G9v)Wc~s(VDF$E>S!(Li<s-gSl$1~W`t%nd%%^Psa3?qS%tLzjblty`fW+0Ut1>pW@r_9H*UYY^6hflb'
    'oDUxSzBXhZ1dkmON<cLp6rk!Wzvo*1n_q$+XYX(I&-tC%hIlYkFBq8yB!M}TzA8P@=e*J>4-Za@UZrP7nW1#5xW(DBb4!bumwUBcpW2<=y*+PD'
    '1*jEd*bMA7)T97XXhW}TMhH9yZx&!C(;&|~wvpiPR4hP2@BtlWS<Z4EXYqIcWJ>+yTeuVao@c>eyE6kG*b;#6krzjo8A~z)tMK?J3KTu3q28tM'
    's0uA>;#kXyU*Ye^3z!U@HQEBIk!q~lb|kZW$(33vEhrE6I~{H<55f^i1*s<`ibLjrsyoussRw|_z9jGg7=8q4e7wE6e!JI{LrwVx2Iy^-&9Bh9'
    'kwJ5Tfg5Tu@=XEg0r%x$%1p>ZYF*-RI*RmyqfqDLbEvJJe$X4g(b`|}mtTC1$6ta2-bh(&OUL$<gc1b?UXbh=Boz=i)d3FcOabv4GQ6k30mzF('
    'Q47&5RX9ISLGkaNmB0G>!|VR$bkFm{R}YVGPUAo2js|CYNQXDU*ol3%-l@}p_nwC)${yPo6!qQ|2dcu+Y%p6OI=ZSNRCqnNR%MSzuI1mP-`(zY'
    '2?~P24^nUTJz-A*xM3$s3;@p9p)|v;W;TEts)N(2*4IG!T1(g0m2G)7;lQW=mw)-<x9`q)ezVPJe9al<oz-N`6X;Tam=*7v+Q1|ENCQ73jU(g;'
    '_Aq<&I!DIenA!1f4R#Fm<s|QsYx#PEZI5^Q$4%Y*-R18NeDlnUsa0wz0Ocp_;0%U10m5d%jsY1Pp;M}$k{tMHJk4QM`;Zh?{S!3Ry9|Kl;q;%E'
    'NvQ@vGf5K<S@2xo>NHl2Nc#izVMsQ-BdAt*>|mq}p0V(lx7v~O>blC;WRKh(|MT0c`^PtLK+JZI3`Y|uwl|>q2x2Q6;T3FqGY17CP!>{j#o?tp'
    '<G>Vh>L}12z^)-BuLk2jZ|wc~s5*D(CcK5l`$zW2O9k-;n(!Qsvz~T<RNV=YCTFP8mMzdgtehriqS83*s!QNAJ0PC>1o|L58@k7d_gE6(TcP5&'
    'B9D&YYvp7aaAq#Ogb~?lQw!%Zz~4cDr}{ReE!%8zI`ZXzd4I2akyC3&7K)>X+5>2=_een;ObCV!VxFE!_(elV38&vQ(yBANX6j%OU!5BIBVYWl'
    'Z|`WX@QwCsxjQ$n;HwX|qn0I}nG+0QM^ly=FTBRd@jf$sY9VHuB1&JuUqfO=B~PH2t6{N}KTpW!nWem9JA6O)HGrR8jm$TZ6bvsN^$qFB&e(zr'
    'f_IA&9_i->C>5KhK{&8Tjxo`Wcgbar%tZg|&E0R#m@X<Ra?@63^Z=*BV=LqRQo9DbIYi(lq(+31jUbVr_*8xM$We}_q;{<GkWaZDx{vqg@ADX='
    'lwi6TkN<J_5M6>rthmP06QeV#X`OJoI}kRIor&zk5u{vJLwaofIWmHXGC+>6zdqa8W5y=o9qDB}5I@+c!23eqAVawgYlDgOdvVk^j)i9yRLnz&'
    'P04O06CBEsAK{hWo#dKSf2SODXH#fEd;@%|Mi~vno}CIBLo;>;tSSVG00p7yeMXrW+YmT45*A^U>*uxjJ-2|juN!ahe#@JUosZzy{f*=~Z4Mwj'
    'KnuwIwsmXVMBew)huD~T2I5P3H4|p0mIKd9SKYomTRIZy{c3Xo)GyD-1!Gf|9JALz+o@?O=$A><LMekSFIXUQQ`KQz?$`&Ni5C0~I2_1nK{0bJ'
    'NA70)YQHk$<rW*I`#Axtl@C4?*xOMfPX%}gybW7)gu=Oc6F}z_oKxwl4RF3X8FioMiMjv$|Ni*;KhCrqg$y{|YZ5$fpmdQbR5A-juqUik7uooV'
    'jSX5#g3J&KBx`ieEGsW+<lJ%)s`zIhJO74%;n(Mf26M7hjYg%S&a@2<WD?tXqMV@y#||Jmkdjgl9$VTZ1H4MfTKvf{Sr;cPv@ty>{=bu(ar^qW'
    'U^RP3>lqo{8X9t-97n`^L`gsd8=4{Oq^>b=1Zo*@j;91HX-^9S^#T9lYR)+yg(dz~Zzx#%dydjQ@JsA;sF}QJs^b$NtyC<gM(VjvI`1mhL#;9O'
    'Gc{r%8s<qy?g6zK*@fkeaJD61Ab|G2!(^JNU^Fq9)>@*Og)_1ACSYp?@Y_)GZld&#i8M<UYB9lp<`}iB+hB{=anN^=yZ+-B-#nQCk)!?BIdJC('
    'uYARN+b1Au<gp7P0^pOXYB^WXAF>Hb?B*P1u;7~_(pW!Iax2}`VlJ?dfBBbxmJ^x3TGnsmde69~h-;}%R14raV{EUe1U^q%hF6Qi(F1Lc06^FU'
    '?Hmn0OW^@PGif!o8Ekd&0&Do{*Vni5VAIZir|+N1$eujzrC*^+4PM`MY%Aw)KzvZY@mFfLzRlSKU~A-s&q|EYCu`jW*70`<>_aB__0PL9u@h<6'
    's7{~@%UM7vh`2FA=M+S<$#{G}?E7SmwiH8&p{M4v<;lSUjZkjt0&9Dv4-flC$zzlBFGyUI&Z?XUFq^b?bnK}EnE=1blxt`)jvytB9<f(4C)B~Z'
    'mp0ew|LG^%8_z@a@Em?Ly(o50ay<Iu3>&@`7G$O$$pDPei~;$gdOC$x;{Yp@Tk0Ly&lKj~U)t8eb9;CH#eXoV-M#(wyWj71HXx9E<N<ytyocru'
    'iwT1#`@NZhWmpd8`C37ksyJ~TFu1a#IIY_{%TWaC!%F^1a`cDwe06&M!vI4D>ZT*16=kMN;3qcGc(PVczclQT=x0MoosNpKwy95nama?5MTT>x'
    '9{pv$e|{mkjZLB3=bQ;6=_pOzxFIFgq%H=@jbjqpInCzgQ_oBRAx^TC-;apKeDFg4{qg$x`K9o3jL(|!&?q}2gZ6H6w|@B2N#I*5W7al%cReH?'
    'Zc!mKYiN0O>_fl#c<@qQJzSq(hKf^E5D;EKNnree+73)vyqV4QEH)KD0uPuR*(Du91{pU#k-Ll3r8u5F`h$FVe}DSn=C$5DoL@|(pb1j31nPDp'
    'ZVJB3q;LQzz^GF;W6)vQUL&P)%qwc<kx=Eq=+dY)r9)5Rww~5Mp7iH)n3<#KKx9tA7_r$ag%;cBog<F5PHbrbdiHcev6VgboF&57F<<~3Wm(SH'
    'l_MAOo#JWV=s35E?lW6Hk%UaKSOopXXxZJJhBAdxE%eQ7DX8~>DVLMehL?GWvXvdP{Y*8Z{Y8Aaxz6gtslU?y^N7E%y9ujd&l)6IO|?vCT`O&B'
    'bCY9&4t&{ns?20R5oX}Y)q*S<W58$3O3nxg`>6Aj-|cXCE!Y5-<3A0EK5;n4Rtdq6R()&IDQ8rs*$^U9SU^RIy&Ad}r?OyNNLvS;M}MO?_xkm%'
    '&Uycp-0!?}?p+nmW1-#|EFf2B@p=Pr2*m<IP}hBo)zuh4^#guUy@0?|Y<wxmkH2jXT*3acBjrROT;w3U<xPz!#HqB@L>hION1QYi8?@@SBFL*2'
    'K?Tl>HO;1dRjQpo==2BJ>CN@y52u@Z{GRQKU;li%*R5{<?#=z4<que5?i^M0t=dU*(onS3)C;X-fj1Obik=B=M1U#lLKplC^j)IfS6O9MV-8=@'
    'SClUKH+}f=cI+-i6KF{0(3R#M*@>gExPjd~rc?Gx4+{9Tz%CH{v7oeD?EH+XPSdZ&QoKJZvOZeNM|I+v6^S3S@IWZL+KR+t8m%d`J3nIpCjpet'
    'R1p_n$MkbfUWFth9rb;<u}V-!-4}ecp!fCSnbid4ynt(t0#-67mePY*HdohzpG`#e0w~l7@E%DdgVN*+XoLdft7(+tjyw9jJN<co=MRt_wJGqx'
    '#3W$8GyuYVN(oT6NHZxl2@Ny@a0K9-n9GAXT6{D1i><O&4fT>c|A~8?+4=$7pyGKOHax%wGBLlybk&r>s=>htkI%xrGVmoj67jHcP-X%Ze>szV'
    'fn@gdX+9~LMQ=w`4Rt732LcC)pa5ohHj-2;04yG?*33YmDbS>-JR+lhkRUzGrD#UD<cgmCNOri85^DE|oI9X#IeRT)bQFpMECgpvCAirq5;D>^'
    '(Bvo##F(NRb$PZ@4?>IEZ}&Hip(S%M6^|BJTa2K{gC=F<;O1MOJg?IME^cP&!0o030VM6TVVAtXh!m4N=FxwDcRL>Y!=8|J66s(QNm>rH3kb;v'
    ')Dj;9@j4){QC1f*_*a6#Hkqg+Cesv2R%%$*ABJxE!>@jl>qpt=N;EA7R<aGKBqyMfgyI26W&6-Tagsvp-s5zvAbE0}5&tSe2mDZ0g(Ks$9kdgk'
    '2;YY#?75qul;ekDU7P~ys;W`~Xe$_<qJpF+Spor5kA+88A)r=z9+rA-iw@5aqwz<sV25)@hq5e@*G`=cNS*>w*7oQr8->`<AV5J1Z=9*|PTRSL'
    'tm=lL)5A(u)VZUG(Ve{A->)phJ#&sd@OYM8*aq*}0+-vgX^&-3>Jw|6%?GVCpi3TkDN?O^rD>7N@daU9#ixN=U{&ubo*4Ni_84==W^jtv4fJPG'
    '=BTUGwP)c;s3gkOPE<K3)Q4Nj*;tz{xT0t4!RU>0ai*azr(`ndAF3I?$lxSJr?^1C3Q-T*BpAUz8ZA;wv~yx%rTu`aePnmuSJ!g?{llIDnN(7y'
    'HV2AWDgYpY0)g0x0Y58^u?j_~L8Y|R)+l8yqayecN`=Ig_!ELM97z9OAt%Y6b4D&Rwbr^?Lmx$7ngxOY_SP`j$A&EvD5VebL|;@-@D65Wf*o1n'
    's{IAwdDLO;zw7<OS8xA_yt?!BZ2aQkaRngC9<ZAIIXMF<%}!>!O|%D;<+@pZf!--LhR5G;&X$!+nK$*Ivi6-mJl@@$e|$U2=F{h*>6)XTWmcu%'
    'aa7^`1;^?EgIceH_BfUQR62?tPixxhIMbV>dUtNWygteO-jN^0N^-N!Q;p6=3!uP$_(ZvIoik~|I_No$lIBabWOD$jbO12Et}Zc;x~us8wVZB#'
    '*ZxcU!+vGpMLv*qs5G}q)cm72?M$^6<jKh%b608QH38=UsHwc!n$X+_1<tONb5pkFNba_G>(!000Wj+THW;%rtFYUmrZ9k*Yg5Bc=2D!QUD)Rp'
    'LhxC9ud?c8cI?AAupI9l^YxzdsHje*gpq-P8eO}`42Ifqu^^pO0K?Um@Nw9q1`5ZS`)0w>R-MZx9TYOZK3(r*_W^*G#|U`Xj@=r9!}s?M5FfZA'
    'A?x5T4yycWo^?hb&5rl)JO>kq+g5WEN41%5_4VCV>F@TmZ*l^H$8%XaIR;9{RUdgi3|w5NQg3mZ*xhJALmIp^9tE)dAW^h?g`2HC@cG~4gB{Sk'
    'UF-ya6vx?!-fAfY2y}$hOjD2nXH+BpQS1~bLybk03kh+aGujn^3rD{G@6SCw`(Qimf&rbyWooG4HAfd+_rlXUXQ|{%5uDaCGCrVCO{QS4xDr-g'
    'VZI#2S+=KtcXhb;{DYpE0Gd@M#opsUOtWJd36b`Uy*8)AR?x)=$TsJ|PC)Tur#>y~-0Jz=QN6Lhy#7C)@lPMLkxo^r@F<0&Km-Dma@QJk<RNVo'
    'p@J{srdP)s&zdQp&dR)u{)3?9E4`kdumey-X~YJG-grtfs7?YlV`^8h0A5LBF<|S0GA4~)y1kj22iQH5zOJNTKhsC+T#M|D-km*n$OSKTbAeBS'
    'KmvlXu5Jy~VTQ>bec%B?1ll>pIVx30v!g$ak7g`wz^prH1AlY7H(MaRv&@dbf%TLT<&&1MBU=D32|UWBrZ7SsNMG=GazIk*5#i>ft}n~YL74c}'
    'CaCxCw|9FwyAc4qVdENhi?kBv++w>I(LfK<LPKSa^||Sr%!-#^A`KlPkAPRMel?k+p8f8$=Z%v*$Cy9`O;%old(Kk=CwQuoj427L88HcmkpVfg'
    'a%d(Xw`xqGuI^t$w1>69zBzNq5CfiCcoWS2LtYrS;y}d*QVvD$JU#??xuqAR;ET{Dhu)*3Cs6g(4n?&qhducYZ3U-}VFE@b`avKcBEO`Pz+dsl'
    ')5vsICw73?O&NE980DymGCdRfcI6Hi4r5Mpub>(pQ&z+Q-gHL26hQTsYsnVR8F*GuOh@@w4|L-L=!l6U<T_}<y*fMQdDbyc{nfr1!7SAUfJa0`'
    'ptL`Y&8soALi1P!B^MIjaSvM`Bo-#vrpZ{&^krGemSGo!gOI?}4D^|&A2W_LiCVG|&QXGevPVcjPYJz7irAMVd5LmP34{aDExh|?@4Z?1x)py^'
    '3HosLxF<A-$3)hqS*{COa|h{*3A?n<DaB30UR1h;0%#U2s?pO?5&%uh&9BC{Lp-V__5D5LP9~?W3pU>fw8I4zrSOaCrUS^gP&7&69dPiPy|4H%'
    'o4-7p2c!6=vK;n2s>AU=C|vXAb}uPfTbpO!pQjjHn2j4V%w!7)Hrol1ArVs1Hn0s))m%I!2&V%`D09^aOk{afQ^ZMrpz6^%o;@-mIGd>8dsM2n'
    '6zw1jahma#5#|NCvltF$P);Qw0U6-OJQ4VPr5<F9ISP2aU%+0-N8og%!_jFs>eW+9ErQ2pcK##}KI==N`ev;_$2`slI74U{<ypw$lHDIwkKRA|'
    'S=0y~onl3Hj}yh817OJF<bcka7NpavVpzkGL+N=lmYuwJn_}sQva~Ne>eavclm7hb?d#Wy(CJSB<aZ*7dS4Sb)}};kMRoZGr~v*1G|!7d3hSeA'
    '(Z~ff%GN|4jxa$nh-r1_7RymbOyB8v+&f`vZnA4EQ*6)((v1#NQb#WL+d&6jzata$<QIs2ZsyM6PX!@aO3hNS<e*}8x<Bn+u~`=S!6~X%EM2e6'
    '>{ROYf>5DdZyrcNJz{Ie5Hk&>lpQFHD-R60C{)<y2PK_fKmO$}dx}9NyMnz4TEV*1&1{GYAoJXTbbF;geKnEC@AzqqOdmBcK1Z5T>Z;Gj97U2|'
    '{k(@D!HBd04r+~~51I}%Z-Iq#7rbkW#4{~m3UDs47YkWI81&p{H7V+{ckD1~$F33i?ak@o><D&Aph3Ec-YShNfrRmZvsUE&SsEovhe^Ko7MjGA'
    'r=dFW%*dG4$!esCLtzI_4?dG8kuG908Bjfo1Fg6C8NqFB_cbc`2z4QHV-I%#Cj=~@Wf7%18cK6nibDH%5Hfppd;Qa{C6V<#F?n|Zo3<X%B?c5H'
    'WDsg*0{9r3z#*MGM7u+3!BVKbhVs;UTyc-1`lfz+^ON4~wo3xmZeuct5(%{3aelfIY5c7-8p9!eu+co(!4eFQ@ibFk3q@E~MlVP^_Q3ajw@*{k'
    '&=h9v;30jW(gXJbHiF>=Un!(-K{N_aJb1FLbmgEk?M!1fP0Rk~SdN;t{=MG4eta@&J&Vbx;g}M*Q${8?joK}xCNPu6gBhfJR{?c+KAGykdq>P?'
    'uth?$RF(#(#+HKt@#)O?RiL)O?2$<YLE`BZ2pGnWx@Oa)+Oy3?nw??z#PW+Bf<Fngsmyr=Y`n|<s0Ezl=7+~?xjXj_TC*0k12C&0*}H+l4-O?7'
    'w4wA;dq8+r5Hn&1++?Vau~)1cpnlA`I@8YeC@RY{)i1|?-JWh9_8Fn8uF<WOIw4WnS&#!GsEutx2`OCY3G5IudD;fr3%-JfFVstsm41s2hwX$n'
    'Px=1-eg5BQ`S0aMum9&Jm$-8Tg=aLSpg37N$1_LG%r^hRGlxu}&|y1!=WTYThT=jMXNVG7>}{!684l}o`LL#cl3}=S^rutT|GbkoyTc@icOw8n'
    '1*I(uB5Q;k5NESFq@#`VqGXlfDAfUA&5J{qIv{DGGRGp>W`C3@$A{1HdqIF*>luKALTv#`?U0eO!8@)s(3wGPavp@;(B)I3OT4!7eU1i#FUShU'
    'RA7wdL3lh$Q}G=%daiSjYF|KdAYz$Qdw6CUg#Lra%$kw^!5<A33v_eS+RVMdb_D;NZq2K@myWu^{oC#SlzE>{<aW4m!hgN;=GKQ9lB3D0Yd(b*'
    'gB9f@850?xNeiQPFUC?vsM(OjJG*3O_9%sd^>WUY6JC1eO^=^xsn~>7(OK4{6k?%eUPP6owcgn3NdeIql*H^Ab<?iA=In9Yhmh)*vbZOW=VG4|'
    'Ph6u3sr!Phpx%O9b`vWAiU86&P@dpt%+xO^oDY(O^o1<Do~$gF{~*sT3^8QcvkG>qz$Bd0!0teL8m7)3z&u)KdIn~xUO_Nop$+P^Vv>}lCv#5m'
    '=tyJ9m#32d{O0K+Mm4TWuC19co#U805E=MVB|y&BX4=w9Mn*OqqEU7r0Ei`1R=EJ*D%VRd>D&e~VBdLj%Wgo4Zir!@il^6tcmY4DM968hAlXxR'
    '-c}CDA_z5bN^@zr>o53~&fgbHoggF;^bd6+mn=rH&6@{iBN#xTVR8;>R9+qzwyX$e$WdEqZ?<x{IO@cEwV3bZ=1=bX%~5j)E@ug9cn%Ue@Nxq0'
    'qN-%1t#hc|QfIPD#D|ed9!7`u4g&0RsJvrdiUl71b^aFlpW8y5DpeGdWKlGu)&h_-?UZV%U)u3)dc#V<$0BVsdFhpr>DXykOHX;Jx?tReS9G?c'
    '&VnokR6$^bG$`sbL+>2apMwK7N^5{teFl&V^n#g1*gz7Crws6_r<z^Y7hBO2(Lc8$3a}2OXot0UDSjg0fPiRTQh@i7qm`Z|bl96<Z8p*B8f31x'
    'hN>$|$zl|P3$N&0)R{;RN+3-erBu!~q6n2_N5Qs<iZmfJIqN+U5BQE?$p+=fYnh-2tGr;2I!gaaZpOK<(Z%Bd*)9bHkedWH#ZV$6P_@V-)Oh|p'
    'c{*b=C{9-7^a3`3Jkh#a4VI%^nd4?}ehW%=20&vw0Kb@w#}B*+PPHrNo<RCh8wV&f!iqj2&Pb;UMwYD!Z%DK}5L?Zl*?83B-;C|R<yZF)r`P+#'
    'H@zXpxS)k{prFRR0LVCi%V`a;f1I;&;uLT!Nz4hTIUu$yf*w2#m1cF~c+@9AInlj(SW5(UN7be_ck_d$r4)>sEzx{JrhwWvH)iTsPRrEmPqd;n'
    'DLedb@!aXC1Fxt3$_}=@-nE{kQyV=hkUVx_&YlOTPB;QDb!6n(Hu6AujKdj}b36N_ZWJm<TbWRJ8lE3SALWj>1KUN|GoxEwTVgq=DOg}fEhW}y'
    'Y2p?^i;-d~kQC~kb3Q5lxYe<FFJ?S#_Nyt(8gn{QB5{YB_(59%3+Pc$sS(Pxf?DJ;no85?1Fdd>Oz<&UN|D7bArV=r9CKWqv#02ex`2Fhed@co'
    '35nZsdW`QB#9fScJpY-t3_)RQyj;z_HK18g^<iR4CNPpZ9ac+SsUjYA%J`k0xgr=TpyXyFphFP}XH;(;_^YLDa;ZS7ieLw{>!{ITP@Xjh)HC7X'
    '=~8|G2+JSXo`3e-sz@$#Qs+~W1x20S)MQ%$VX$ce=Z%=@_ew2wq<Jj?zX3Wjgy%KsB_(NIJzPIisgT<((l>S=s+9xsG@v~bz!TKK*{Wa;9vc#n'
    'CztKYk`{j;vQv8vW;ISjlhmW+K(B9`TtB1Frls3Z0K_sCD%iC~t16*ANs9oeY<|c<L9ON*hSVDUBnij<z<ya?5QJ#lp;9_}^Qn$3Y^-j{#OA17'
    'gi`B-2h_RymfJRW(6pm;IbR0;iZKz$xpCg9D(}yhj`c4~|Jy*^deYwUo`Q3QUZV+0)&@9}BPv+4W|Nu&7C^x&86PQ7P+!GaWhdpj&-!KW{oC)?'
    'R*E4>&3$Md4N$BhstZ+=BF9>R@|6wj9BL{wJTGokO^Q4j{IqE$8*beJJM{JGzJGD<ih^ygymzkPy}fi*SF?{>KXs2SHfuA|iRz#o6<(<CY}*4+'
    '7{-d`rP9m+lw^DEfAC9*r!~CZ9r5%t0)fp4P)qN`fMiXZr|ib??BSS&V2f7Ky-#8=YC{FB0l81Ysu9Tt_j2(2?e|~&`tkbu_ott}{eG`62D_4N'
    'Ycb(Tos?}uCQT^#>J>FJw`!yS1M?;?IOIbIrmlr;3~AMP>f!;Nr+D&D4w)GZTVJRjhN#CEnA4Bo5(1(plp-+Mj11BME)5u8rB#(<FC>+R%gS!n'
    '`a|CRJN$d*^+!uTAo133x9fQ0IrnUN^u~i>=N&mmNz`xcz{tb)%yW@Ug@dpf*#iYTU^EyH|Bd4HzdqgU+Cx~NMkf=6KS`V70Jti(c$^zCq0($E'
    '_FMamX(cxlB+yy$ldktjrYq~p91e<{4L$CQX?MDyG`vn;sRePB=0(u}$wa|^FqE?>0E}i(WwQiPD^BNZ-UI8jRo@`M@yk;D*FWA~>;0eJF4GHA'
    'bgY$%k)g4(OKjn$G--~4><#J^IABhLIZ$wL?(UMC%3h>cSJq|L#+RKB{1553?X=Uu!(;xzsc=yv^biNeYo-F08?Ds*rnJkP$T?C&;3yTZGZehp'
    'm%g$Md3i)XJ@31pcIP#G2mNnkav{xb*k|O}AcbN-Glw9QSDCF9AK7Zk`J_TOL;%COy5djufM&IKkN(N|5ZpdhiDqPrhxWiy00tlzyhNTLd4PU~'
    '7n+64sTAoY*(}~yo^14GSL_iIP4D9Si*LXEk3C6f4wiuQQvj=vI&%evi5zU#!HH{52J_egufLe#X*l2zWI+lrm|dQGA94ur{>eZ23uJbKYs_kA'
    'MFFLfEyBKaidy=fBKzEj2Rm{lgL{BeQK1{j2bn+Sl~1BQU<>^61-{w)1QqYQ`-1Sdt|~xqnl5DU3B?w+nVoGMaPE=D4_TUtJg!IH2iyTXj;qo4'
    '@<I&xhwqj+G2qE!HnXzHJG7cS3O$Y3g}Q1vQaf};8&gH3DB_wDB9aBn0<_34LHf(~>Thlz?-lPDv9WH7^0qqe%aDel`&_k6>7xPmMi`EF>>IE^'
    'LRf$uNy#II?sBf!2XjIE%K_(%A3gNPzuw`_%7anQe7I?vi0uP#5Dec(W9g<eX@#zA51)v5n!)9f0MG!xdRUvxn?u*|$$JzTCUm0p1VV%l(@?L7'
    'y6DxFNsoCU)!Pgee{EtTfRmVk`e29}%5rL{T<UY2*Q8CL7)Ao^k|u&a&6JAHu85tkagNb5r}ZSzt4=$LvStnsq0HGPOZg~wgofS6i`lyDo(-_p'
    '20)pY0X_-}r~;^4X6Vfz(#*7|R<q%Q(QYzQUyxhLQsyTD`LBRZsQ%@r*?;)%<F)+ZyB+FkJ0LH%H?sbuUXe;t_9&MgK{HywuBXpLi-jJP*eJj~'
    '#sIs~XT$bO-3j99KY3Shz!Mp1luYqfUnB;6izdlVuv)h3gD$eW%V|kMdEE*AJ*cJw-YDfVTna0A$74U?GqgaGCyhwQi$^(_u>vShvCbvl<>0mA'
    'JtU3*0U`KkrwkADT!L$AR&q-*<RcdG_3bCF0YG4M0Q>-mGY9rAxNA%$QIw#e44Y8`XAH0+kQQ-lZw8963S<FPx{{#Dh{I#nKnL6h{oUQekGI>P'
    '**8j8+;?r+dkp}6g0W^DR6&W@w>hXs=|Id(4t1N^v1Ph(^t=^M*9@)H&Q6JQMT_;sms-HR`r|k2hD=6>DDS_qdzDw;Gos9C#UF9(GbO514q71c'
    'v>2NJH3O_hz{#>-azlU23DkEg*ROsW?$2)T4&SInKGXuDqWBHoE2=W}KNI$-lsGKMyFqP*sdKyfOqIuk(#`4)*5>;1dt|)nFTUrE$XBQFZaDF*'
    'A9U%0NJm<sZgv5YbiqXx*Q~*3h!z+wddkuyd>S5S;xkdy)P>&j(l5|PTaH=Br*nXiC*Ct#DlHY=MxQy@7^WJ_p^A_YTGr1B=G$ro9Yzf>!4DlW'
    'N0#Lv!XZn*f6l0OdBAEsp;7Q)G2PY_c+%<`n|VzJ<{PPNHGTlgY88My60rt=l<l(lsDh>)^59QI7{A)No5HKx4wzgA8IV-{paHZ}DLz{8c`T?<'
    'Z2?fJ24L+pCVj0;u^rjOE=PwBuzBO7fA!7RyU*RD0oJSre37?lcwc<eyhj!ktXXuJD4+mX45+<E-b5A6SV^m~tPTN_^Dk76{a3x^{O$5IYK8_x'
    'sUc-xv?564HnB||_`qx?m<1q#_YKoi#HHXR7nb0L8a#|&SpwDrn9Tb}{$i_4?{&r3c``U24BI(V84(d6+p)0P0=90EWjN2l$+E7fuL$?nz;1eD'
    '|1{{@s$a<F1I+Y3Ea8*0j36tHj5x1In8~8JmW&cmf%ECtTSw*eT&N%cxuW(qPv;5>Gv<n7J1%q5@L?Uldc4-VFK>Rh*1h{=Bo@n?GradcfH5;K'
    'xjSAm)qhNHg73gV;#s<ab8?t~XnJyP6gy#MO%d~P3;50L>s$Px0<!HapctQWOw`jcJPb56x@FfURi(Tuxd3^h_!<hUbsEiM5xGU797bMQ&YNoM'
    'aVz-l_G#+xj6;Q5*CiUjf~gjP&OQWDWW_nHrwvHqt%GFkDOzm(0XXMU4-yl&n5@3R0iAz;w*t45KLhYt=PTdFFDM!wM=)TElo`Xm(AoG;T}^nk'
    'v(h?>ZKbGWJZW4i*qH+?Sw1}fA0CCfc^@2S9@a}66Uei=8ND~1QVc@b3niZ%HuM6D%c0{=Y&>R{j@-q6bv09egqhce1#BDm%RirXwm`BH*#fJx'
    'G*^Uu2b{165F1pOw;?+}Vp00Vb?UmuMz3}stOAOza!`o}Xi0o{{B3M>?y7kqsi#&rp<|`x+1bJshQm$}hcx3tV$*m4|KhD-OSY3OTB^Alt1hlA'
    'K#mx<{OjkxK7*HIaLDb>^){P?NUtBsuF(qv$<pe~Ns*lt8==z?U_={RC%?f~?y}kdwH&yBR~>-==7+r*7=(<}?vWW=945u$Orq>XK^?P{B13Wl'
    'n^#ohVu9R@W9KVA!8WY0czBx+EXBWh3*z$TT5fc&b1_sRpP`L9Y1K>=;@R;nVxxl$aFS0ZQI68KL7fa2wkwU6s2jH$;B)p!IK&ymO-to3I=(&a'
    'Ed$IoiBV)uilitt0!?xWan$KJN;M~+Xn+C2RmK>Nn%h*^F9@Z%64I>o&>VDq`r$^$SGRj326(Fq08PP^4#1-gFyGb;dTt`S7e+U=DN%&ac_2r{'
    'Kt;3EqjH#F1xiglunK(tTHnfR*;6kA5vO&gk<H=;C@pybkRXBvjSM<D1qztTek2YeVZQ)l^obGyw!g|jFTCL0eq_@w{@<;S&rz&el`+9)hjDb!'
    'vj%V|jgctSms85A*V!mGWfHam`yF}q=FIBMnPZ|ae%&8EHK!jw>A1mm50D%|^}6pZ*C5Fo$?S?Up(P4AZV489If6NW^?+tp5!AcsWAICjj|1#d'
    'K3>Zw6M{4yb5G&930*)dUA8?v4NPhj76cOIJf4Ycu=1f}Q9U#Rw1IW5uO7>xOW6*w`j3x$b12CDEt%~K%+O+i-F353m(@8s2}Oyut5RYEivf6='
    '4ouYa8Z8^HmVo-;ukjnbzt@|lcRzf>*=H{Z9N-I-EEB!+rH*Yl(g&}KUjs>i)%b*5#o<kCc&xvx4TDVwY1Q_~IqUx)UhAc7zkV8b+Pf_qy^n!1'
    'Mi-<;%?3}~f->3!|G`Ed2pAx^D86L6sCkS1jJ!3ahLw_ob4PSw|C8;JkAy;={UkHb+&O_h{RE`2RCO(&X2p4SGt(Db0iA4)b$~)wUr<i)wI&ZA'
    '>q^Sd$0Lpb{#mQZyHRx_!q53keE^zM&fCiX^eAaY5sz4-9$95==g+0M8J>Cr3f88M-CdF5ysOTxn*V=hKDVH!>$X3iNd_DZsmURnis@94Pm)x+'
    'jdMzOJnkD6oMbd0;I6$kApMv%8LIskU7gq?>0M+M&qi3FX=rgeI(TUsu_s_&8sivLmO(!V?3iL)=Dk*K)#bp`wUpRe1N6BfQ|16R_|Nx6SYiI~'
    'zjsoY>A~Zj8hnESa?4rA!ddyA2kPWzTsww0gH42n>e~ifs)l45mOPfCcedC|rLbqCDsI>4Ez-Tbjp_=uA2-W_hKfiBCOflsKTGE!C>U=-I%~cK'
    'DRa!#g+&erN7X<2_xQyO^`k%C(-38M3CAF6sjhY(n{^|Bol`rRYA%g)=Yfs5!&~JxDL^v04vRgNmL8%nJ8ypVM()-_OYDv{)c@cSvd$1_mj=X@'
    'tFI$<{66|TXw64I^Ij9xSzK<}fbWjgpnrLx=-1P;a$IaJs%%5tB-eQ?OTRs<AvFgif_H9(?n;CHOX>j=iw|KYvEEVH$m!}h%^hK8@ykQM`{lmd'
    'TS3O3we}&#YHC7fj#Sm#5_$r}u)djy;GZ7Zvkb$QF^;{_upH5{^3yq@fBB;od@@^VrQ6~8=ChaH*o%!2;RH=|*}0~pT2(M84s)=}N3doY6#vg7'
    'l~o}K7x)&>85yT)P_o8gvvLQBWn*gLis;DE@Kb6bRi3nEtG;V6EvR*tok-f+>WDL&1IC*^TF5_@k@>Uh`bV{CUN7t)->muBb^XIyHm_H<HS<2>'
    '+^)%G&CPoN+71jX^gecoHy|qBi?lMF)7rq`m0>8T_cj0m1V>S@mEyMd2iOz*UG_U?%v%~VUdC=gF)bj3TDnIT@Ph1?9pIwrhDLf>QEZ(@nlO}7'
    'Hf^Fz+0`NddeWhvg>>YOtaqF>Ar+D+i`|3AYhFqyZBnHaa0WQ5#t8(LDMt`det2O%8hCyMb+7cVU(1}-BNy`J>vlRPP!tgF6B$a`y;1?Rl67^q'
    'hF>^rm?{}71V%REXRZ-7DLN$T=!f$Z>Ru_!`Dl(0s9(Owen}P3kx<=C;E9dnsvVRAn7u?ACJRty(-0F-aWPt46_0(9>84q833;ibP!ABX*ue&S'
    '=O-Z)w!lWmVUTJ%2uVe%P2lJqkHc>9+Lbrzw&Ck!qjA~+yzD?>>Pm>C9?|6Z_ltP5*Sb(pF$5f{k8YEv7)mGsZ+rsijAm^UHUTE-$&{)<K79;{'
    '_!5Ezt!@qJ^?CdX&Tb4}W)p{sF%d?bw565{uq8D&;&vQAr-B_)AR=IEIEkL_=-|OcRz(ryFvjB-v1>YI402EOh#pXGF9>_Qec@Sr&cmQ^piIos'
    'I({Zux1eD|>6s|j*uFe4iNRlBH#~1hxtWa6G!57z3Jt%iAN(XzW06{shEvF{1N9fq)RWQxS!!(fI;D=4>+P|5_M?@Y_eG?T3&6cn<%)iRN!v`6'
    '^%V;~2^3;z!CeJp#Z!GXCfjL>V`s*;YAbE2l)?qp@|;mpM7H(n=Ro>FMWc*m*9h=fO$HG&T@Xu+rYp!xZiL7rmV5y7$Nz<;i^c_l>x@4bsC!pb'
    'KBpGQk3zj+crX`K#O|bMr*N4nWim`pK*<QTrULLnfyg$Omc*9;Q@e(bKK0HqCyqgRUgAi{lY37miVhNqJu|bU66y75*h2<VmUzXJYOJd(D<6`-'
    'M2^~Z?P(DhH6<ik4heRUCTw3<eb_X<B4@R76a&6!I>psmt`k@!O@s@$*s5#zh}(j{uNAwx{2{YLviOczp+1u(Rj0DV@Fm$PT04akXW@W#+&Vmc'
    '0sxU5vI%|FD3<aC5Y#7GSXp*vXW*d}tcwBvYlLGsg$hrobp-@!OrHqQsicM9PC%_t>6=iN1jD3!EHuBl*SnwO;q;R}{qfU}XP2eD+hGJuscx2T'
    '(fGj5TdV<WiCQ-uaK!>R*4i72IEx2aJ2=1Bu<R$pijV(Df3=qXJU#sQ`@6@R{WiY@z+uuv#P?VtweWyx4V^Gi`vmxW+Kzw=U0X-023i4M>7MFb'
    '1=wYAcW5txch84h<|!jnQUT5?txZtykaAU^l2)>~T#$@oWW>4AyCBKB)(MVnUTG(T#Doj%A4+hYUCA~+8xv3JJeWA@+^D^)7@J%Whf+UK{>B&?'
    '2`ndLT$v*%LJoul;A(aCnJz)5KFM!nIz0DaX>o<JfFP}<R)kKUqgdB@4hG6dD9=0*W2R}44jT6Q99JWsAeZJ6NaejC@BU)UW>B7@%^k>3Q<<Q!'
    '+d3MvtuNg))9FN&BGLnZOq<}vI<TXfTj}Z~du)>aTUp_q5c9H?z~Xb^5vvEd!n=04h2m|u)z2^h-Ul+7lo;ub8zxwmXa7)M?fB#_fF3_`M%n{k'
    '9PaF4a$XPQR)O3j*1|qytI3U#4Bd%6Y7M{^l3eTR>4-|Z=epuo)?R|<p7XyOCbD+;R8jmT&M+$|7qYIQ=12$R60ncb@Hu)Gp?<I_g^4VBT^`}v'
    'd;#q8oW&Mz2P;h;fY+@BU@w9!rXE?jZnAxxgbK*JD4hXpqtQ>15^|20bt~nj@X{8P-`x$pL*09K9=dirFJg)+606V_Ow|#%$5BG0L`_s4Cjtp$'
    'V<TySm4xU(8#RE9fJ(05yMH-U`I{&I;>*`#5BUxsLvXIeN?o$pGWYJJ<X1ppXwLRggR~Bu!efSBJjE!z9UAJYIowkA@$dzFdK)7MEntR#irX@L'
    'MS%_j(Iag(lWVhe*hI1YIGkn7C5qD<+d5SQ+6u}aa7p^N%X$C%9Wa98tZCYrdaA9kOWy=94wx!|gmW6fQOGQD3?xv5rHCMJA*u!kDHeA;N6^~8'
    'UBvcJuD-s#mc8gI;zdVAs%$}Y21Cb7R5{)k)B#9ngi25oRCa<T#>#zT94~i(|5uiO$whlaL%=yrwn8GC*rP=G;n}Q7gKpP$dN(`R6dMJ2!kj52'
    '9_gBwS{G+2?q1h(ALTGc{$A?o)#)`wpigrB{*QKAWay(aO9ebnlx0Im@uUQsBBM}<$Ve@hh!PG6k551iKV$$ZP#|O3r2%=){^;d=@&*T5qI^S4'
    '$#?t!hEaIF(3|qQ8pPdp6CEh~sD&e&X`-l8KWYFF`qfMwK=0*0&edsGv#`10JEzu_R~my|58M2Oj7$%PfHdU=^_mAh0F9!Ke)3}=A)p>gT~7MG'
    'aLn;(J!=(R4<a`?O@zYWDR9Nno9azdYXAsM^|b@E$LNUyC8t?<H5E2`F;-)Bw%DWK-?!g?_{G($hdY{O?D^LUCttgiL3?$OJYVrUTCW*u$#~a8'
    'v{YOS*ye+`W~kRZZ?vgTzp^$n#UHnd&k**(Dm2f8$Ll3mWFnrr7309^)QCk8Q6)G{Htg3XeiV9ZXPTb%;V&~NzW5rCZ?K@-ao#%K#ylM7)V#$v'
    '89Xm(oo76#Z6N_(8G5tKf(q$PG#SuKy9YI&vkt$Sm$KMZ;rR9JYmnMOv%=B=@bD=*$LN3zOYos_qi5G6P!w=r&b$ek*y6@|x|w22BLT0F>?KZg'
    'QMrHhaHo5%L5Ouw&}dSmL<_(w5Q%dr<5cKsNekXSibr)D8}GI!k5C(?MdYQnSOp;21-`@8zv=7SySKaWHZ`QFq(FUc0wp0%Qd8RCHV&7!46sOb'
    'c)%>>=iw3E1h7TmbzNDP<Zy{p^`pw0t2@1xKVRKrkL-SvGJr^;OkVjlR5?)91d_)w_~~2$5X{KEmI|@>I*#*uz%dr(94TvYn+CMyFR+|vS28=@'
    'o&lm-vpr~>ybRX_N>Mm2nXI97AN56AMoQ!3qXyE?3}J>UzmoFG<pL{tbNcD_;oCjC3WE&*5JIqI+62D?`pCI+7xbC|vFNVmv%wcYq$N~IUVdb-'
    'K`|>$&1mB#KF1q+;9iY+zjuSvYGO6VN11?2o|}M5;N8^%$e3&~z=fN?Pp2{&_IgR$%Jj9`RRyW$A|>Qcn@0Xsdwo0hL`PzxAbxd#9w>~{#K|5P'
    'TTN^nHxE7g*)s?&GJ$~l_Ey<wjh`i~HV8%OFZM<D7t-m^8Ql!CN#^lWB#ISD(h*vnvO)Mxlt=B2vql4GjF%>gKR0!?km8cPz(V@%_2cWC{n&~D'
    '(d4LeVKWE&TQz1HFu!4Pj$pLT6uf%mE0jYpk^kG?w|BQ~WBbnE`4k+t>q;6+GD(pX<r&SXVk@an{W_L2ZO3tW`2cNBBvB(uNj#~)`~7VIe2Q{h'
    'H_cjSU2C<mL;@Swz<vYR=nV`NM$CZRcgVEbO9@Tmk3(epl=Sqz(Wji^U}*Rn3}i!_#BEy5>BBD6KtB|CV3uj4^ETZJZ_rLS(}D!6yAa6g_xgVb'
    'CW|{~6+3I7w1rI|jBGH*U|=(YslCuMdKz6)>SDSKUx(4v)0uB;aD6FfZD_%Rg5%q>U{|ubY_D*w{tz}<uIKD{+vNa#7QkN$G=qf}WgF^KVy$36'
    'rzh!xNE2Qx-LGa)@>EytQO^1v{1$k`aHQ(4R{L^GHbo?8L;M!*a6Q)NE3`J9|F^-_J@|n-{_nxdRxN`z`<Q*h1EKLH8k6q5<r*-cv~Be{hQ2%w'
    '+mv|UHhsGFkXgR#h0u0^23m1-gKnv@=+rHEA?Wu^%z|z9EF<XkEv+9?xYVmV+-d#Zv}~fs^_it@J5+m=*G{F2cn7}KX9G4c!3F3p{0J8s1I`7!'
    '3j^A4V1&LsP;Jw+L)WI;E9-oVFBa|ppNs6v7KgRpvwF1XXj1$IZjcVG4S{4b%L+Xcu13(OBNP3=*LwN@18+f57$MyP12U^S-E8Y$%<uj!sc2aM'
    'H^*b(N7|~=3rtTlVfJZsIwBwg);EJN2*Muh3{%x;O@r>MvB2@*0GEAvk5$}_6}@MaMkQVRstx$-1`GzemnnoMhVD{BrnzAsW{1t!k!-Uc8ZIr@'
    '514HkjCQhv>Wsjmy~M2U%3#g<{ZgkM0dWa8t>C|2vt*m~XIr|aQTjoX875`?_TWzRfe#(|w^VTIz%^*qo6=bF<(C1|Eka2TjIdXC%kCFA`@03B'
    '$6q(3g`ua?RXlJb4A@0FZLgw7A&d^%mFS0RsM9T*t~&5_gEFZaKAoPR2vF_OK=lv9Dt-L9RkD6)8XgpaOF6XhZ$D5~4Lyd1>kn+hq-=(iC(rMD'
    '5M(;tqVDy<Gb-H&9oCAlUhfMe!`Xrt5Q+TrCG*p(M%lLO=K|Y+k7Sqw7_~4$7+r=EYJH;z{9ssV1IPx~>gkmBpl3p2Gtc#4&|9nzo6D~~u=54N'
    'aa-^@i0A)W-?%IGdg%rc+j^0XiP`WNI4cB|L5>Y<O*bIi;6K@+r@PRa{hnvTvhr-tqP+`NpU%CgFe&}I)it~S1y-ZBAa%^#WHtHs+Fi0o;^(@$'
    '>-oTp=ECd+z8Ao$QhBy#kG3<wpwd+s$97=$V4%S_2`M#eVB_VWE+YaaT>ry}F*<lXxg5RR5-OrZ+mPm}5AOGRbe00f3S9tb(AFSK81$`f4J_5r'
    'AXZ$h4|+A}7F?L1VclL*n8bge81!Yf?&CL?+YTa7c7{IfLn42evOPjX(3MBfM?GJ4nbuP&x1Vo^HY_=~k<ha}pdYw&+i%@Mi9P83@fbb4p5-%3'
    '^?3?K@Qfb7UK{jvIv@fQZ2+r?-$678+`tPo)l>~zgQ46D+<|4$<-T?4sm#IW_M0a0dg-(Ky?8$V2$y6P&DfqVFXVn71=N3kvEE}-HUqObv;*5&'
    'y7L<rIID+q6v8`%d#nXL+MUdg?J!C+8c@eAw@2A{C|fGsdt$=SCRPKT!ny`K0jE0cYkzM0#v`!_7n;TYaa<qs<vwurCTs%3W^TV{Yd&qp@?k)0'
    'bfX!AT}Rs<;6T#Wa!RwKLesJViA8+?Rpy$SRh!lRb9>GniNy@%%M%kXwx+x68-t!}GCFYKGe|Nh)c|H3{6`JGIy^_{RgLe|gu&XQh`8$ZY{L$0'
    'X4a)YrpV2c(3r25kw-n{IDB^eZJ*BD({^6Z2HAUt?b$Zna5eDVfs5YJ=~fpukO_@BP(4^UD(wWa0;a2w#<iD4KUH|G=gE4ph?grCWKJ2Qwb1PT'
    ')>;Dq;tE2y533E9wd;nV0c+p$@E>de1I}^iSp&~<?I3_OQ{g4kp;y`^=YoN)x`^<TZ=l&kq1&bZ<b`fqPUwS;2cc%`OgH*;VH@LDMS^ld)28EI'
    'A>A<U!j*xAMiL6bmJYsCD(V4UHCvl?X7I=0P`;S<FiIYKFRccvrO#B0_6Wf}^TM94^#f+X3Z?58DSNqU>y#8646dmIOLtKKCICDPx{9}UPsf+T'
    'QFx?fa@%`JXZG^n+n4UV(1XEg!a=Y@LpMze8j>=G`MU4YN(5!6F)4k)pl`w;HR<daEwk8|CUBr%7p?3+`H;^ev3tBrqz9f)7OCFX=uU7=vp{&g'
    'exLd97(5Cs^-17qsz#S!3_RFwm`RgSTvqGXX0Nfn>dd`-HI3bsW^Iw*HeG)QdEBR4j^O4|hy*x#YCzW$`n1?ZylIj>XX%5$>iGi`Mk<{X)N7Ak'
    '_Me<B_h|@z?@qUzq_cIKF3vOHO2AU3JN;}^hb82ab2Nao(I|3DmtWHsslL^tC33j4feoVd)H>u~%6?Vk&Rzc<Tb%&<mhHHv??at>DhQCSxT3Tr'
    'wr#;>=u={EsApz{f#*{Gr~&2L)iq;48L9P<uJx>2I9H#1KH_O8@-JKOSO?wtbdEq*2QcPBN;;vz9tw4r0$Z3tO75ovTgV!^n>_46T|;S@lt#o?'
    '{n{j0Pd+R4Gz>?}rF%CzKiX!UGc|N!{lSs4VcMxZANIE$_I0ht0(i9^gRU?r5)5aH&J?SxkNIWZz|}D2b;*YRXezBf*sdPHWvXZQe6u%z^8_X6'
    '_H76g%FYC<Fz^PLI$wuu33~8S0(eW@^3pw3WwvR!bxT0%ldrFNB6<(YP?xS%gKMi}mgrc%&G1nzNEMxVv}6P>?9mDL(1ai2L7UhvW#)j{*>{b)'
    'Rj%IC6P!H}!|g&8T3dy|;t!Z-q2H=S7xcizg6@Oi0`;frVK~rC%W&!T!#+Ls{60U30l8CmR#8*cuZ!L1q-bCDT=?>g_1(bGZ1^%RS)5RI%ya1u'
    'A_mi%+{(TMt4!<ry&fDl7bcn-_<r3jX+3C_uZ!cuVia25)NF$d!X916?uA;<4o!Gso*hzXt!MNHJ;Q_l3}+OMCI#qyI&0Ubqs&(ArO8iU$ax})'
    '56e-eg#mPe<(oeEEVN*lhL}_rUa=iQNBTVf1yp6v;%Nx`lx!bLrRS;vkgQw(*S??}-R?<TFgAazD`C@i5=%8b9dt^k3Urr}!uBb=;psrw^q3!7'
    'K1ml8fnlzzJulSh4o}<AjoRD5q_K^Euj%!U@6LlLa97N60}+FEr%n-7RJ+>dn+Nai_tuMKwPfyWZyI@f{?wh{?CIKGH20_LfbD&7lbMr5GuX@a'
    '7ORHVqTy5chNat-?b0;^I$>{HpkolGPe{Z1)PX3_f;FldT<WNF$FNs7m2@vn1<iOF-9&_%?@p)ipx-%O9Nam@Gyk-oq)#5IOM6K|*hXAGSvHNh'
    '5C2pn$Bthz5!gDI%kK4ETN}WSP7`7jMsr&`m`yq=5(J?c*pxra3m{tQO3x7d=h<`znPE|iMmnHj_KgrGff?YxP^X#Sc?g|6I$=E6o%|%J&CA{J'
    'oRXkl+GqzS$)Ok#2zBnW&FNppQ+k?KikD6_U#wS!sKpkRp%4yd82YkDha=F;D()`ouYT{b9__OVRGW^OLoU&2Er>LYZkL9mX0d*NDbis|4Fbx;'
    'Lbuw(Xd1wS@oH}k-j;*m3t>e6bfbBfV^zPM!M3cBGG5u%0P@!h0^6XC0C2&4+Pg&A6KVg9rCMRnux!(Y6Bba|nGE8({fWA|<-(OOh*KwC2oTAG'
    '*g?{UtVj7n!JHu}ppd80&^<FS`nKBZn_wu7PU_G>1K;)yI?1DZbR#BIQO$NgUDy9`<iH;8gLsA3!1sf`4bO`8XkSLCs+9eCK(|4whNfGAPUu4L'
    'hDHFx2<j#@VOXd=%(cbpwlQwWlvBYA{1~rSk4MZ~N7JBy+HATVD-9N`rt5}o@xxMP(1&~F+B$9ZfTu<K$}MKux@*#PEo!e9=zbWOUO%AVhoRNc'
    'qPFGi(HBN5BgY33WRRp#pN8h@n(iBPS|QZ@(9`KKgU1Y?vSz|aVk|J2<+1?``Tz{-X%HTsQI`p@r}n-Eew-q=;^ooKQ`<Hn<8{jQ?!jqhw&q%9'
    'pVEZ6m?eX*%hky5Fnzei>cFRC(tW0TzHZl%qpg1}a`d(=Z!_8#7}9;A5c5#{kcX7p%!khl15Yz`+JVZvzzjnx3<iOZM;c8YU%Rh6(ns+R{p*vX'
    'w=D~V;X)z|3|g{+%MM4;FkIa>ExJ1rZlde^kR!0c;CS?HN>XCdEl+(twA8TfD79f3rt$U2aSL7sZHozdy1`snEPCiu7AV*$y4DW|Dhoo((ji}5'
    'N_5zViUUF#c(QNP1r%1@9yeRl%-^lj(p4jW=DuUawIj!m=V5fyy@klH4Pju8=u9v4k`A9j(}<6Ze#q#s2c`M%8<dFGg`Z&=bVYLL8HQ~PV8Zr1'
    'qb@W4z#RPUS>jW2UyDbrN$q>S%XAeo4+dmUgZT)(#4z}fgoAKk!(lhTLNF*p*9H~?C}hL5`s69W>ZnUOVpI%jE}wjd)~4YzP^+;2#Y3gn)wQms'
    'OOu$Upjgi68eOrXwlk+ZF;nIf_T(fy#{AskeXv>LFQdf@9+&U>w`?!DOO|oG`VF3l?lXA8J=F@hC?!GT_WJ?63D`82<@apY@Qg4FEO-KxgB7NK'
    'V6(uYOpKvXYo7Jn)%l(r{-DrOzPpONXd12Vln+an?ub_A@k)`s4HWNAS=};a5zXiLl^OENW#P;r5KqLIUZN=b==!S;K+jNESu7bw(Pv2#FO}62'
    '-BrS99wZ#<@$o-SlqC~fq6E>BC)vAGJQl)J<wfN1<uAKRX_-~NnBJvb7LZe*ogAo`)3%btC=n%Td1uZ(t}@*5oX`hGL3d}Xi<(KRJBNK-06y=H'
    '<GD#h!@V=|S8I1#7_Gd5;;!M|(vqR3Dogi+5_>X55zuKX<W=&=$%0Mk&OUmk4|IYR&m*``7(TkpRY|XbM}L<yk%zy~;L!zen7PqBQ5Yv2)3$UY'
    'gyGvaL!EiL?wh7bhH)Q?+=AqXw4}Y|zH6vH=qxZ`l)2#FKE~nx#h?CneZaLryxbEsApa0I$05ADFVu#L50oeWi%;b*U4)vw6VEl;5B?`b{Tc(X'
    'PFC&`&Hw5D&(F@z&MP;XcKm6afG;E+99`_WOaC@{&lKo^C<HqZ+bJQ5rd-J7@<mBj>)@^nXfMKLJX0JeT(8zk<~T|;qez?L&gb%S+~kFPbn7O!'
    '&_U_XB@=Z?U*K_C@f<&2yto`)ydGapl%dos4d<VyKWJJ(!P=pWu?~)z`|j?7P1i@^*q@H4(?&b1Gg<W<N7q#9pa@)A;_K1rBcP2>C(gi~PGg^}'
    'nWKs6jK{CuPp4yQ=WIF|IcVzs#fyN2K<D3Uh}35MuC2@<)!iG`+UNa<LM}Svcl%N=C=g^(!jyl|RTSgpGF~>*8ZQJe=)5MGbrQmoVQ}Z^0Ct5&'
    '6a~7s>|+q!uw>OtE0Y|gfL4=6RzkSNNLH%1_=^wG>XwhWDBXpzJa0CZUZbVBi6V4&-ftoZkCH(`Nhr~ZEt^w!<^}G)EZHUf(9~4jQ2wIOpH^G('
    '8jV&Cfp@06UXU<ua!{=jigoy;A?M}_8Yvr+WIbIGecZoDdp2`d{w?)v$-1OVc?Xw`UjQVUGo|~N|N7;=(N+jbo64LhztW#cp?h<8#sCyg6Gkk7'
    '>fbf5etDZTuXkH-o2bnjxl*rNSK4(xpK|hfbzvsR9=4Un;hJ(ONad3vqwr4gcy6OrfJ1ez2+TFQk+xMFAyGsQO%NqMEU~2{B{^9HO@Uvs4#Vi9'
    'L`{rL8LwYt9U>Qr_6|_pc{>NHJKvTDDxD*NrGQ38jOd_t-Gy37h?JU*y@s-zHV)+9%!MHU@d50mL6bJwD9wEe>}tP**G&ndgJBlPX8PQTmJ&(A'
    'SE2}6bT)^za}o#ZDLXoBR~sXX3jle)xhba5t!eW(Sw((c$faeCr2B%yJdH7t3D3eHtPpoa``Xqp9*N0UF0o)+nM_8Ua1sSXnB2|0c$xrE(E{mm'
    'cw_~pkzvu2f@W{PfiSOp+W10-rLgh*TX&9vH;P-=&u)ryX=1oRNcc70zHMZ5m_6JnJ>!#;EsUh2#Y?9)mYg4aC4_m8faFlQx^CnDjPK|@L8UqA'
    'CLs0|uhG0*u33S%Bw|BeK6?u3NWZ(JmsgU8{+i7qN>`;vhAfzTJ)Lr@X)brlb-=Ro>%TPK!iVJGE2NkfEwEmSD6v5q)_OjR11Lonboj=`cuH`y'
    'ifmek!X|TV!{ZTEcZjF>%<L*)unIEuOjW^&w*r@A3P+<Ur~mBY(wUsSxi}bu*|k;~Bi<Y)l`57yJ`;kZF!+t;cUQkC*SmPJSdxjrl=YnY640X6'
    '67CeZ$epj0*4wM*+hDh)d{iR)?X}_;Bu*3TMOP^+DDZaPYRHMJ9kZE`K3Bc#;>Z<7D1V<zrnhfZm5<#-3>Yaq1wMTe1*}#Uq}^`RccRE-)D+}a'
    '4~)?mC%(JDyc9rEQ*fXd%@$j@6ixt`_YwQB1$H!FHD_1)bxRozNk%lY27r(s2dvp@0aXpn(Z=&7`Et2vDYRUaGL^O#?%gzY1JW}zP>&>z0!_^z'
    'tkH?EarKG1DB;!g@}AE(O*av8+U_owNV@=ITvx?`^5>zH7iHR$GN?;V={#tDYP^G<Z|p0LjH3yHsJTU?fH>F`)>oC!w$-c_ORMLDoWH}k2?%mQ'
    '5j`VAOhV1Z1jXENMQXcPinC}|<~|>CE=^OEbEe1y2*zHH3n!NW(O@bZp!9s$nox`}Mm1NC^s6E`pfMTz!&+8IHJPyJ%X+)f=u#F`-m|2Y=?ST|'
    'c_?Mc<6$H^n<<f!VL@Rt&>Gx}L<+|c>NNB~Te;DfRsiJup{!dTh|neUQJt8dMkRFlfDrtvMq1g)EiZHm=6{2j_AKQc+0WcigV{2R5-#Z^-sq$2'
    'qmLznB*&}PD9c}q-c+c{5!O!Qx+R+MU^j0xkquaF3#H-wYy;_-@gei-1^_BAWXl9BaTjAe$qo8YBh?yZctpr<f%~)J&sM~9qX;;q98j8qLf{62'
    'zaa5io&ZUBpBf97E;9lDkf{L>+vj*cB?#iZ>C~wynK;}mbdD|@AcLL~|3g1GCF0K;OV9h{6$DcU%%6hdgKhN+1Av+(5#W|&M1|@0fp9vC_~H5}'
    'A}1X`J^%i`%5rJcE2oX@(dW1>Ww0XCWtFRJ*L}zCl4eVAU;5(-On8um=`!w1*h&hNph9VBh~RH%H0?oA@@0;E!PQELl9JrAoq_y8(f+C_T}F@w'
    'xki5$R?1LmcK^~MS121m?hVPx-Xkj)2EAAv0v$bR!+fCNJLA4=JN0L29v3bcgpBGY=Ep0Tj|==>c+#IEdmGJ)R^NJPkBGefjn~?6Sr_ir{=R<A'
    'r^w@9%hqKcIxV<D<mjaS4;L70zLEk11Y9&h$v=p=%RzJ4gux<A1$0rb7{nZ1O1VURimnMo{ArbkgTMdSR_+Rwp|?MBBTS5Fy_qdvUVY@K-T}$e'
    'c%G6OgieNzEVyBqk;vH?y}Q%^l#?jHdeOfa#md-2P*V`H>zrTi_;-)b07dGj?=b@z3>MrJhc?G3qoJ!i3L5~qk1tB2%86*^@}e^jr34htc}cq<'
    '(-TRB1@BIn2JtkYzJb%C6`O(83NaJ*fkcNV!WP9@p-LER6sVbL-Y1l3)BB2j>3VERF4NNeKr#{r`g}z({zlnH;id_CB}Ku0J&=gVGk~<sB!U$5'
    'kgaaw2<cLUoK%d?j2FPtCMejnT7+HWdv_U3qMKP91xMl7ID(96P<ME@xRKb@bsdCm3`B*A2rJf=1l6!oqbQ%YTkcKD4*8@tF@^K>%wx;eW>NY;'
    'iM7RKOXpp!Emrj9lLs96>W5JK&iQ9KqN<t4@y&(!&SRX7dT+A&DY?Z<NStWVY~@~D2q_lzbg<oePNVQiAb#Jfdc_4hh2z9`0nCF6TUS5QvmxoZ'
    '_8>dUjq%XdYT1;7O-^~^ZL9fq{$D~#REi%Sjb5LgO)ifPoa3X@aqF#C>(o|1Vxo43ing^FCUnyu*fx@K;)jY}RFA6<adC`j|6$VD2B>7`qw&ph'
    'DI;^0m=Pv_nobD?O^dRoiQ<K~+Vv*Xf?&#BzEo(#=n=B1fo2>U2Rtb>MivzNu<=5p7XsaVMZa%7O)?d{KWe~<=hM4Jk!huum<n&_=+J>~<b(sE'
    'Ey}+rO?h&~p_2p`zq>bV)#T5>vD}ds?#F7iOq?y2OCF(AzIluhBY=6o9az0lleraXABSlRCG)NTxZb!E1CZN|`4Tfcp9K>(V^T31`71eg6}Y+0'
    'U5sOvysH92wf<GbbpEV|^^I~)G0|J5q>h3z<L<YzisQCiW#<9j=``)F;>AHkvDTYu?70+0p6B{sLQS`UCtV&-zFE49TZJ#-ri=8Dbll8^owkaV'
    '2z0u{kk9W{x6~ZbWp~48D}<_RoVR@Vw|Z~+H25_O@W0WId(eH8bHAUNFFBKub(lE32@Z}&&V-rNDwdPaoXk+mo6gFRwUvNp{q-_IiRqni^74|I'
    'N1IV|$(hWJmK?2$(2$gf0tG?$u!mSTPj#-M1f9Bsa(UNbbTA(py|B1Q3n!ErAG7rO^bJaa;7Cd(#Mmo0q5}|0kgZ{fg$|<2EZj)DX%tG5vka1M'
    'bxnd=y4tt7)$KL@{5R^!-`~y~+)`dCE(-DsThhj5PbGycVzO<hN#dA*eFWFqd`&SmVm{o0|EQeuH$L43%=lU(-`qmtjrxIN`}lv?Qa9J`sIn-{'
    '3ExsVlH$pgY41x6{;%l!i94mF7_=%bSI$Mrf(l(N@5-i5f@wofD7GuX8uq?v7xW(c=(EMDurN%Eu#CP`oSN$TapP$I9`m2~M$BOr_*M8)JBPG^'
    'IPccqhl)y5!ftqOlPVl4Q$P)hm}31H^j#XLUH*OpFqPU>1pNP_Xf1NpvnfJj;EFlXRJGTIhVmG7I)9kEeE=@ZpZlRWy?D2qH<=gXofYk)MytBw'
    'D<@=Mv#z@V<BZjC6F-U?t*$gebH|JK(n;yDWV)wb323FUx7VomMM+3YqHdh5r>h9^G8=<wJiqC17%Mu6R5i*3{|(cTsd0#;M@mbvfWJ5S`NT5z'
    's;f`3a4FKgQK-7KeWmSRccUZ#->;g&-hMZaKg<jBm%6*viOlT8Fr<)<;DQupYip~rxL|FLx(F<#FEGriBAwLaN<8f7`{Ik_*gbG;i!YQ27O4!d'
    '>{4TUCj^MpUrU?W7lD!$Dcd=xVgj5EMa@!Nf!xY~WT-~8L#r*YUQ;S8gV=OV>*%U9tKVp1#qj0iFzcMGkdU=X_dKi=xe)wtk5hk)D5aLLy*Qro'
    'Y2uN%*<y896(lvoBKT;-->GmJ|C<%Ki`X=f=6||>QF26^BtpAXy`+#x5Dcq=$c?5^Ve<I?&_=7GJZJ7a3TbD`=GL66PYvD@G%x>4Dw#0NT$GX$'
    'mpyIW7XmC!0RbmfcG0|Ea})dW66xLuxp-H|bkr1k6XXlZ9#jhX7HvNPGBft54<JZ)<)nL=(>@f;z2me{x{8zzU5%NSE&SIH<i=u_C|~GNR!_`n'
    '9xxu2iA#98Wy`;?DVBmCOFraF8d=#bTI;?Nv&98QqqvRM_q%c9dZUMhBD|YA7fOYvVBjlB5!VGgvI0(#JOwH<FS=RB>qI#?e^cCQR}!U}e^qHJ'
    'otc{TmO`VkN9JxejT@cTY9VE|-vgisfxOGH52Kk!g88Pi4KGh@q;F?l59Xz48fF2ioIC1y{9z{9-%!e*)ru6#j^=Adeik`b6ea*&&DII+S4>X='
    '$o<!}&qsEF@1mPd-_JU*V3TgKVJW{pO9ERgh18bVAW9(d<B}9kfy3+7Zjo>)dc{qkyi%*h4Kt0*zl#l6<g6+%&u^+yTg+jXeWc)O^D4EU4EtIJ'
    '$u^qlPXC2*Y-(yNb6rrhU|?6ImDk`}H&m*S$g&gR75D)a;~^!fk~-PcnzHvYrj!|kS@<C(%pu=mkp<0Y)x5%<g^rcITCWf<TTsYMNNT>p_AV2t'
    'h3Zo7wH3aM*j92DD@krz3#yZ)Qfjt0-*9-FV%E(zmv~Q>wG~NU*dittX*dhhH{!kjz;xg%Sz^WJ&qL+z`oWzHTA|q#bh=@OrlLgdg$Sf9o9~>I'
    '#^f|831t-iEkas2y#ymy797Mwx`S8ZrHX>JIJJ_EbJJ`KJ$-6Ozg)&OL@Y!Qs*}HxxXWek7@}(F#KON%@sUDwGemJ;rqMZf1s!ia52masjR9In'
    'Sb}b+k@p#b<hzPxtrp)IEY7v_h(&$r9CW$-7TVQ}o?VXm&&%Fx!EL3;F71Qk`<0bt{2|#VB28znt$fU<NRqACDfuB0RFp0w@euwyuC)jzp9lU$'
    'F0MutnZ3F#Dlf4>D5{q{#4V=Av>t?P_YnY!LlwCN6J^RnvJsKpv4D1Pb}=r>vs`y4az$4TU=Sq>9#~sG9={tln|0(#xcc7ZP{oY$UKX2r<Unk+'
    'RKc(SQf_QXMb>&rC$NSElS4|ltf7)2A<GMoG(}qOHx%`T!)!3FMCpohmmo3~m1!N>m0=ln1rtbzcL@&~OSaC3UQ&O`-;Fd7P3aM7`wrZ_)zZ!0'
    '3$w+Y-=!4`K3ZGhd%=LP3*XR7&_!;~$=IqeaioAqLlU3(Y!qCYB3;~<P`#RokuJ~X3=&(o**V<n;&^ghOel~I&r<wLy{GeQe4wh*b5pKoK3U|7'
    '<)Wf}o3R1jlOC$If{PINx)PVUQrb!^WVak~vL*{lbMd{UXvtZ09C0$nahi>PC3IY%8?0xGq?r$j+*2AalVP*bCev`g(Q1_f#5w)v(DP>keK8SE'
    'CWP~n*^!c5K>x2h`u6<UIRE!?K3_nZbP_tL<#&o_&AXg3{($p;y4utGmGggn&3Hcl_f<ZfPDgQTPTglYryWTXU0&wJ?(VMQ{f6@2eW!GEy=^JG'
    '_*YYv@4ge7Z$%#D%Aut}5+(6M?(KaDq;h_7^pDZySn2#Yk2`$rVo34F`Axj&(DG~umBxiUs5S~IQu<<-)E(Tl7|x-fYNbfkv>qx>LCaM=<;T(F'
    'BoF&>OLy5NFaUUPGWuccOn#nRj!&G^(aBhuTwaX-=gsI?*%@cY*>_r0JVTL$DE{%5E{{Y!4#S^LPtQjOKPZdUE!n+`IG%E-8Da`8`<BvveFieU'
    'RlfaHEHwV<a(r<*I(81n=i}4E@#(?Qcp~;cB<Q&8KR_+ma`l+LpPi2{E&)S%b#{C>zEF0~PEUW5TYh$EHj30nfdb0%ySJUdwiMwsrM3H@DXl%='
    'BvtHL$RMcX9z=&KxbNL)N|F6$_%Vpyqa#4C`Rb>`qkqT~4bI`w1qO$|E<C#DeclRN?(1z9CP{g7aST8hPzT?f!kc?~<$OPye6LKtA8A8Th`yFf'
    'jej~gesef>UR|7>IHTiZ<<-&Yp+m$ldOdbV7q2G@QQkyZ&*L!US2B0XRPt5(OGJ4-x;*&aIXF8x8J!*i+Bs<oP}lh2&E@Fj@%Ucpbi8QpF7NO~'
    '>^|sxxQ*b+Ehx)DXQ9D*MzxEx{~jM)7RP^2m?Ozvp~*dwkFP`NAv)r*R8G{F-#vVfC8ctu7mzYU+{z&n>px7gzmny2;AA^6+m@~D4*Kn$DP`d_'
    'g0nk1Ck|9<x%2qw<;Ca%Y~`d<;oX!)LF7hgIG1#tZ@45&%N(4byF}>gg%ZRpnK!ar6}%jmbK?HVgfcyRpwR4l+KU2{e!hm?ddK%#uWm_nt`{$M'
    'MFdnH{Z5;i$9$EGR)lY(g-9vk&fMt@{t+?u%oX?Ul(UOVMeFLluHGeh7i<mK(JE20oGLV4f!tFp7ZPZIlD`58zK|)x8Qe(`y;sPlh5PYa<a39#'
    '!i2F>6XYk5L{0W?;`nCDGGl1z@4b9;bbQ$L-2b3G@>6(j66&fmi~MCQ$C2R5gWJuWQNTCz{K&jflE99~c*=8TuP84C4YV9Y(IJvaBIU`_Pm~>@'
    'MSxU^CQgZ;oFB(8N0Z6;<mK_t&c*n6^wapznH-%QVtkd;vrFYORj8=cQE6{qjV?iH<BJPG*tw*nA2e;thzEYN#X)!{f{i)x@D^#?+D=I|?Q)-)'
    'x&CbvUZ#@xE^X!@%=kZ)&_y;b$+5Zf?WfX+J1>t;M;AW}2_Q59<>mbL6X4yK23#nFto^iI{<}P$T)beE4o1hL3+LqM;Nt8Du-Ew5nOq!9?$h^#'
    '*u!rL>&+6pMb62W7o*e3tFwy}C2#!vf-6`Bh{`D!xCx!8aBr%4F&=G}DA3)Gk`535Df0<|bObfaNIiuX`5s;2%Y*PDKb~@C0&lvI0V~=SMu+qH'
    'K8pnIJoTHnlZy-iZ8SxB8F^G1S0ZZJxNZnAFpM}Sl(;8|NyKoGNB8GR*yssg371U>1y0n??KU8NempuDD?4xJJIc;q>v5z}se67e8K(m33Xnd1'
    '5lGj7bpHfMd`{)2)aGlpnk@Ze48W^pJbNhyZb=)yl9eNO9dJX7NQHMp(5`bdpD-Z^UyY6@<H9S^U?wLz;!R5Zd@Gr<rF#+zmn<YI@>a4p{G9-D'
    'LYf`Qa_5(CKb87?uMCy9Z$Ir6DsA-o^5V_7@V+1)ph@^n7RB}X(WJRdos^6I5|b-uX~H%Uai<WrLdkif)tvw*!sp_Qy*nknd`DEcQsOYvR6HO@'
    '>vp6?Jh3Ygd`BCfBcE{-|7OGLz01z&73Gc?`eGkD)OST#E|ZcBL{a7FR5?DHTuzj&z*31A^77fRH-GtcuSNPt?EKkFT?=0FXyS}sPR@?sz+c$m'
    '^K+lSFWVIby^<9yEQK{6p8L0N?;o7#B6-$<q#5EeAwNFKcc1-x9rNSroa=<VTBRE=cvh@Zn^l3a1LoXOL|=gw|5EK|rB3_NRbV$9ofpc8N*Swd'
    'j&PABD>A&Up#n2}X0|GzDw8L{bmWg*0d{g}@$ILAfFWI$_w^pO`^ESaHunL1Hx7rOq@qiLsgYpK9rE=?muDAT|LrJgPQ_e`)z;$vfULr4Ixk;8'
    '0`ORikg5B5z57i26%4R#a7qJ7JSELkZd(W|rR%qz41o&r`UK&(nQe0Zp4=>%RI=;81Im(3-H$9N0oxL#NkwWJt=Q?zWS+jA-&|2P+aUm=Hi?hV'
    '58xLcxF_P8v0%~oKODb&^IF--I<t@7WIyiTN*5$$OKwGNwp6T=5xN@$N;q|I5+Oogj{Z45?(%Gt1&E)=A)LpZ&`nkyx=pUso5$6qY7~K+$EnIG'
    'NVKXY&gbW1d~)^=c=d12kB<%jXp&D=YOd!>SDfFg(XcYc^mD3eD}%HvEa7C8CPgfy6O3okDtAL039a12jwm{-9q4B`JmiH^Jb~K;^-;X)sBHAK'
    'WCO@hKw7E3@YWef<@bFa|4d0xDNiJRh6|CC#`RajI1H7_Y;Dd@PQaaxvKO%Td-L^lS_;Gcwjd0JIn=hQ>}qXYqZr4oi28Bwg499MU42|p1JpwG'
    'Db?0g9^4au2!L|*s(F6-J$XpyXJ^M0*E#!<qLvq9BBeuTbbchpa5BCul_X6=*7n|%-MIc;$6Z9k@Vg3R1%TpP2Y<#VlLP4c%ke25?3NxV6w-Qx'
    'Pc7OMrXM%<Qh`+4o8&f{$$r3eYCf_SE&B0M=UDJZGd814p9G4JoMlj`o&F5O$SKMJk0hN=j?PXez=YZo>_uk~f5@XtrLf@no0nkIV))si)x~89'
    'f3N`I=u|}8AYsP`Fa|cjNP<OI>qqv{qY6x9n+Inn=SL`aIDU0>dUQ!>)wb!JpG}T_at>rXaj2`RtqzU}RyO%WSTwo6DX>p8CMt;TIUvJV;|oX5'
    'ne=<8WC{A~@u{$w3b22o*ws#}6c<mqO}cM;qaet;Fq#KPaWWBmv(u<&4XZx7GEq&>Z1<F1{Hq!1`V4S%Qd!#+0|-$IzH?5;XQwq05<#~P+0Cb~'
    'b1|Jx)~n;uYq%1ST(q<@beFUD{f>*D;bJ|UGdK~_sC~HMSLd4b2Q=QHOJUO<e51TRILKpOR7$ftbhgJ`4I{>Qh*V|}bR1rDb+=$`#apkG1G-Kn'
    'i?_->^dgI{5a)`!qOBTGEvZ~2HcqE&#xFriWn<kN9z-S{AEdJJo5j+-nYmykpW!DxN#neKqf>O4>f65qWd2mZ`g^HlvI=Oge$8W`TJtoTbFj4w'
    'Iz^^VAM(KJLrXlLtHcS_wh49m)6Z5C=~G$7xP$)pZykTJj^<%}Un;j{Nn>l3>ViV<vVDbR580hm+vX{=CHH2%+_uUg0i*>>i=&@c_@8UOpz5`!'
    's(M?fdOIa&nJN2Hm`uX?qkuqim#o+<J<*!b<x{kIrmO7am1KXF4-uW96=sy+OZmp(hcWqa$!1FwbEhBNJ6O|hH0>4z{P~-s(^qHC<jwgx%thE3'
    '=f|T<9@mxOj@=IF?yU+W51%xDjOIc7A^BDQosf0<>%q^je>^(<^#pZ(J#?2JqWQ1K(R}^!S4in)90k9=S~B)>a@hKIhpViw(bliIt`N-%9ro=f'
    '(d0kI7i2};EAor;8v5=Mnonx8URN`AGJ}c2Q|IRpzh{#)&H;*?fIyo+jxJ6isXss4Z+FtR3+Udz-O0!x%T^i9(>W%Jc;1Ti8~6XLAp8AWH=6Ic'
    'A!#-zd+-I)fIqzdN7K!|UfKU+s{P*c{-3Xu|NCqqGafDZa%>zb?mRmO$X9{Ig%6bZi&ll@3NU4VqSG*eGCBMq&-`7Ny*o_-o~+smo!?GwDSF=~'
    'E`Raj5cWD`2-Gm8(Xe%`Zy2Vj>i$6O>pe?nOdVL7HZb}|-wiY~?Af}_n1^zvP1lO`HLurWX3wxpS8KdDJLXAR8;w$$j`*fa!lAiyB)ZKLAIy^A'
    'UANoasT_}4&z)rL$%Xso6)(oKI<%D?iS<rf)K0I-xNa-{hhQk41<|sSBqVD+JrniB*5yu9_Sxov{)^M`kH2pq^0LP<-KTJH$@ksfEP1eGU(!pO'
    'zZ8^_?r2qVJ)#(0wQK*CXe6bWG!nD(^g;U4Jxj+m?-~uDJp90yE*u(Jv4)oB(+=LXQ#U~#e2`0nr5xmxfr6h%OUUH{B$2edOV|4CZ=8^i5<W;U'
    '<(pKoO|?cz0_!o)pGPmcA|#ZwhC{JKzEUxLPy2WY#%X#4H$C1fE;ZrZ<%?nzG=j9}>-ox*5~Wi{YTh#K0Vp0if5+~Il{UIjuz?DS5#FB~ba#n!'
    'advjuXg9>l{`fTgeR>Ad`oAZQdyWKEk%!eq-^yn>Fq(PVi2?$sR>`vPo)RGp32}*v!|+Iq41c$gVWFAlPH11kofF?CcNmqA{CjJEGsi;RXy#)k'
    '2G(QgaHdHYO6dVatt+5#J>)$rAI4>?rd;PA4j(y^TV^?PXbDJLdC46UZ~=|`DbXn9v`MF#2p3&yXG4z}xoQ^M2lekYvb%iX<}_OEX0=nLLXFn-'
    'gY;e)i8g8odY6ZQvNx@6lsK-J#MAYPHMf<Knev(#v=oE<*_QDkgGc1Jt{h<_q8Uo?*(hJ97dccJmSWwl{n}h2^q%;&+JRFcA>POGk_}Ead@t5K'
    'mIpMzXlu!af93t5f2yb1s$D4$B5!@$=w&bYPLz!y%4o-(7t}uQH~Yx^HIGV`k5>>xDL>gfSyL(im$D&>#SY3A1rL)8QHLysAu&aMJx2PN?0kAt'
    '98eBkfa&^E=FS~9e;+O5xi~V3R=T3a+=iu0rD%?ydq{WvIVmDbk#!4ALU8lZNLz1pK|L5F=#pi#Kvkl==t0J*P*WH`=w3Ol^hDWH8r|G$Zagr='
    'G7V{_i2_NNYoXE#e94c}h#N6@UOv%}^P|(rRYUr`S)Qu&{Mu%Y#%lM)&+J$SaV}5WFdT}eHn~Q$l<onlFikyp(MdWMO^X~*wT<fn3b1^dR~vFg'
    '+)xYw*Xc-Or*XY8di;7U0kV#L`N_PB66YvZW``d!<jLMzQoon+sK<>Pv)vE3k$>B4_`b|FUwOQ{%X4m=ijzqPdk4F_`4~!2>1lXrE_1}Yex9%b'
    'bPi0ml0MJZ7AIZ#OdgWNuk?16i#L<YH|NgxXD4IYH!qlCud#%@Sud!22cwJEXB+Q*NS_$5tBF94k6s=ek4|4Z=cCK-c?BVf3*GKIoZKC!MYaz*'
    'xz`XUn`*#~PcV}l3`QQEfwzBfcKYh*b^5F_TWwLpA(71GVfx_w=gaTUPKR(Vy18`92IHzh-ECYCW4>T@3nNjS%D$?iEVWwJdd<ys@^wLm3N%~o'
    'LQC>Mx)|nRwS_%7{KcDr#G_nT<ZSD-6-u}=WkW9L<S{WyyFmf!LT;E$x`~3k)xZhj4|6){*9;rCtJNae-`f+1BD&OMhtFKRypes{3!u#QK8XhJ'
    '<--nz&>C$azZ=D8HH~-u+gThm)wpk(tqs{j{GJsBgWqv3r}av@UgNGq)59OZ8KQx}_ZeE6!IYw>&@JwIy6R+NzR~8m5}Bv5Dk>+;R8*nG%AVd?'
    'iyTFTaAxD0=4*)m%N$M}6d1M}g;pA}(_7lB@9iUcLB&WWK;g})B%hOH=~ufa<>0W`&04bZ0c5waTieS_nAW2%g_|r@${)lbD_VD8bycOYsmVy-'
    'UXTqVD{o2ihbB=qb<CW_l4Yg^$>~OxyIiP2<^)U7`m)}R$d6#Q1z_q$p3jcovy+B7+`z?fLvy{O4eQ+Df$Uw+1G<oeFyKd9sOLNz_nf`w*$qfm'
    '^A@s<c0%C{OUg|u-c-Gb^YG#{(dRgk)&l8K_Xfkf_PT(v$F~Y3akwh3kL+PGx;OvX`y^VqPr32IB(fr*Z=(RFlh*GFc#X{5mv%!fW+<DP6=FUi'
    '6eVtJvEfvp*pz6u^SRlRKvFkD%;=B+>@`bWYwU`?we~pUC3}A4h-+^7zukq*l}gHbD8Ll8cPdON1S0Pt+G_gnONw~%iYAI@4z(;r#t<)&-iCrQ'
    '?X%;<!eQFsFb`pYqR*ERtTY}~kc3t~U!HZl7VHDoQ`VK*mU*WOxDYVmY@CaD9i6@4gC~c*l7fd$Ta#xoe1IkQH~Wod+B9r0va~9d&P$**W_P=E'
    '-Bc!Dc{fS~(*3Z6{VJxG2Tv+lFsS@K{oBry9MSn`j;$0M+0o^~T$T8xkqMe$d*!N`EMB)ZAW176Q$k5mDUFD;o~PrzFEVR9uRT0}o<Gl@=g;%!'
    '`SbjF{ycx4KhK}%&-3T`^Za@KJb#`)&!6Yd^XK{V{CWO7f4=<Z{{aSJ);j<&Gyn'
)

BROWSER_WORKER = "// Read-only proof from the actual initialized CORE instance in its dedicated Worker.\n// This helper never supplies, replaces, or overrides self/navigator.\nfunction verifyActualCoreDispatch(module, stdout, expected = 'auto') {\n  if (!['auto', 'splat', 'loadsplat'].includes(expected)) throw new Error('Invalid expected dispatcher');\n  if (typeof DedicatedWorkerGlobalScope === 'undefined' || !(self instanceof DedicatedWorkerGlobalScope)) {\n    throw new Error('Dispatch proof must run inside the actual dedicated CORE Worker');\n  }\n  const hc = self.navigator.hardwareConcurrency;\n  if (!Number.isInteger(hc) || hc < 1) throw new Error('Worker hardwareConcurrency unavailable');\n  if (self.crossOriginIsolated !== true || typeof SharedArrayBuffer === 'undefined' ||\n      !(module.HEAPU8.buffer instanceof SharedArrayBuffer)) throw new Error('Shared isolated CORE required');\n  if (!Array.isArray(stdout) || typeof module._probe_xnn_f32_dispatch !== 'function') {\n    throw new Error('Actual CORE dispatcher export/stdout capture missing');\n  }\n  const begin = stdout.length;\n  const code = module._probe_xnn_f32_dispatch();\n  const prefix = 'XNN_DISPATCH ';\n  const lines = stdout.slice(begin).filter(line => typeof line === 'string' && line.startsWith(prefix));\n  if (lines.length !== 1) throw new Error('Expected exactly one actual CORE dispatch record');\n  const record = JSON.parse(lines[0].slice(prefix.length));\n  const keys = ['is_x86', 'navigatorHardwareConcurrency', 'mr', 'nr', 'loadsplatPointers', 'splatPointers', 'checkedPointers', 'relaxedSimd'];\n  if (Object.keys(record).sort().join(',') !== keys.sort().join(',') ||\n      keys.some(key => !Number.isInteger(record[key]))) throw new Error('Unexpected dispatcher record schema');\n  if (record.is_x86 !== 1 || record.relaxedSimd !== 0 || record.mr !== 4 || record.nr !== 8 ||\n      record.checkedPointers !== 12 || record.navigatorHardwareConcurrency !== hc) {\n    throw new Error('Actual CORE architecture/precision/tile/context mismatch');\n  }\n  const actual = code === 0 ? 'splat' : code === 1 ? 'loadsplat' : null;\n  if (actual === null || record[actual + 'Pointers'] !== 12 ||\n      record.loadsplatPointers + record.splatPointers !== 12) throw new Error('Inconsistent actual CORE pointer family');\n  const required = expected === 'auto' ? (hc > 4 ? 'loadsplat' : 'splat') : expected;\n  if (actual !== required) throw new Error('Actual CORE dispatcher does not match manifest/heuristic');\n  return {\n    schema_version: 1, source: 'actual_CORE_module', context: 'dedicated_worker',\n    expected_dispatch: expected, actual_dispatch: actual,\n    worker_hardware_concurrency: hc, probe_hardware_concurrency: record.navigatorHardwareConcurrency,\n    is_x86: true, relaxed_simd: false, mr: 4, nr: 8,\n    splat_pointers: record.splatPointers, loadsplat_pointers: record.loadsplatPointers,\n    checked_pointers: 12, probe_return_code: code,\n    cross_origin_isolated: true, shared_memory: true, verified: true\n  };\n}\n\n// Return bounded failed evidence rather than discarding it. No arbitrary stdout\n// or error payloads survive this wrapper; the caller checkpoints then asserts.\nfunction captureActualCoreDispatch(module, stdout, expected = 'auto') {\n  const start = Array.isArray(stdout) ? stdout.length : 0;\n  try { return verifyActualCoreDispatch(module, stdout, expected); }\n  catch (error) {\n    let hc = null;\n    let context = 'not_dedicated_worker';\n    try {\n      if (typeof DedicatedWorkerGlobalScope !== 'undefined' && self instanceof DedicatedWorkerGlobalScope) context = 'dedicated_worker';\n      if (Number.isInteger(self.navigator.hardwareConcurrency)) hc = self.navigator.hardwareConcurrency;\n    } catch (_) {}\n    const records = [];\n    const prefix = 'XNN_DISPATCH ';\n    for (const line of (Array.isArray(stdout) ? stdout.slice(start) : [])) {\n      if (records.length === 2) break;\n      if (typeof line !== 'string' || !line.startsWith(prefix) || line.length > 1024) continue;\n      try {\n        const value = JSON.parse(line.slice(prefix.length));\n        const numeric = {};\n        for (const key of ['is_x86','navigatorHardwareConcurrency','mr','nr','loadsplatPointers','splatPointers','checkedPointers','relaxedSimd']) {\n          if (Number.isInteger(value[key]) && Math.abs(value[key]) <= 2147483647) numeric[key] = value[key];\n        }\n        records.push(numeric);\n      } catch (_) {}\n    }\n    const allowed = new Set([\n      'Invalid expected dispatcher','Dispatch proof must run inside the actual dedicated CORE Worker',\n      'Worker hardwareConcurrency unavailable','Shared isolated CORE required',\n      'Actual CORE dispatcher export/stdout capture missing','Expected exactly one actual CORE dispatch record',\n      'Unexpected dispatcher record schema','Actual CORE architecture/precision/tile/context mismatch',\n      'Inconsistent actual CORE pointer family','Actual CORE dispatcher does not match manifest/heuristic'\n    ]);\n    return {\n      schema_version: 1, source: 'actual_CORE_module', context,\n      expected_dispatch: ['auto','splat','loadsplat'].includes(expected) ? expected : 'invalid',\n      worker_hardware_concurrency: hc, numeric_records: records, verified: false,\n      error_type: ['Error','TypeError','RangeError','SyntaxError','RuntimeError'].includes(error?.name) ? error.name : 'Error',\n      error_message: allowed.has(error?.message) ? error.message : 'Unexpected dispatcher exception'\n    };\n  }\n}\n" + BROWSER_WORKER

# Final Gist integration. The embedded recipe is source, never a binary/model.
GIST_REVISION = 'eb7d86b1478ec50ce3fbc2ef11b7b8bf90f2d373'
GIST_SOURCE_SHA256 = '43e8177bbeb29ef9ce2ab98cf0af811eff52d6c204470623c0aeaed1dc57623d'
ACCEPTED_SOURCE_COMMIT = '4d68c154eaaa802bf2928d335257677d31f4f560'
ACCEPTED_SOURCE_SHA256 = '564d62fb7eeaffdd7adb79f9470eb63caed4d3689a07d1450afc553440e0b5dd'
CORE_SOURCE_SHA256 = '1b02f8d4743c70be2f501e51f47b4c248fa1160a7677c041ddf1e753353f7081'
SDK_SOURCE_SHA256 = '26c3ef13fc8285a209fde5ac3e6eec17e2a24e59d6af76f92f54e6bf62998a72'
MODEL_SHA256 = '51425e43e7ad5aa33af06464b77f86c64959ab9317353e8f549c1b7747150fc9'
BROWSER_OFF_FLAG = '--js-flags=--no-wasm-revectorize'
BROWSER_AUTOMATION_FLAG = '--enable-automation'
BUILD_TIMEOUT_S = 1800
ACTIVE_CACHE: Path | None = None


def require(ok: bool, message: str) -> None:
    if not ok:
        raise RuntimeError(message)


def terminate_tree(process: Any) -> None:
    """Terminate only this owned Popen process group and its descendants."""
    import signal
    if os.name != 'posix' and process.poll() is not None:
        return
    if os.name == 'posix':
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=10)
    if os.name == 'posix':
        # The session leader can exit before a child holding the pipe does.
        # Its isolated group remains ours until all descendants are gone.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def checked_run(command: list[str], *, cwd: Path | None = None,
                env: dict[str, str] | None = None, timeout: int = BUILD_TIMEOUT_S) -> str:
    """Bound every build command; retain diagnostics privately, never print argv."""
    import queue
    import threading
    command = [str(part) for part in command]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=os.name == 'posix')
    messages: queue.Queue[bytes | None] = queue.Queue()
    def read_output() -> None:
        try:
            while chunk := process.stdout.read(65536):
                messages.put(chunk)
        finally:
            messages.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output = bytearray()
    label = Path(command[0]).name
    last_report = started
    log_root = (ACTIVE_CACHE / 'private-logs') if ACTIVE_CACHE else Path(tempfile.gettempdir())
    log_root.mkdir(parents=True, exist_ok=True)
    descriptor, logfile = tempfile.mkstemp(prefix='voicevox-command-', suffix='.log', dir=log_root)
    try:
        with os.fdopen(descriptor, 'wb') as log:
            finished = False
            while not finished:
                if time.monotonic() - started > timeout:
                    raise RuntimeError(f'{label}: exceeded {timeout}s; private diagnostic log retained')
                try:
                    chunk = messages.get(timeout=0.2)
                    if chunk is None:
                        finished = True
                    else:
                        log.write(chunk)
                        output.extend(chunk)
                        if len(output) > 64 * 1024 * 1024:
                            raise RuntimeError(f'{label}: command output exceeded 64MiB; private diagnostic log retained')
                except queue.Empty:
                    pass
                if time.monotonic() - last_report >= PROGRESS_INTERVAL_S:
                    progress(f'{label}: running {time.monotonic() - started:.0f}s')
                    last_report = time.monotonic()
        code = process.wait(timeout=10)
        if code:
            raise RuntimeError(f'{label}: exit status {code}; private diagnostic log retained at {logfile}')
        return bytes(output).decode('utf-8', errors='replace').strip()
    finally:
        terminate_tree(process)
        reader.join(timeout=5)
        process.stdout.close()


_original_cached_download = cached_download


def cached_download(url: str, root: Path, filename: str, *, expected_sha256: str | None = None) -> Path:
    """Same pinned release path as the accepted run, with bounded 5xx retries.

    Transient server/rate-limit failures retry the exact same public HTTPS URL.
    Every accepted download must pass its pinned digest; there is no mirror or
    mutable latest-release substitution.
    """
    from urllib.error import HTTPError, URLError
    if f'/archive/{CORE_COMMIT}.tar.gz' in url:
        expected_sha256 = expected_sha256 or CORE_SOURCE_SHA256
    if f'/archive/{EMSDK_COMMIT}.tar.gz' in url:
        expected_sha256 = expected_sha256 or SDK_SOURCE_SHA256
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _original_cached_download(url, root, filename, expected_sha256=expected_sha256)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f'{filename}: HTTP {error.code}; no download accepted') from None
            last_error = error
        except (URLError, TimeoutError) as error:
            last_error = error
        if attempt < 2:
            progress(f'{filename}: transient download failure; retry {attempt + 2}/3')
            time.sleep(2 ** attempt)
    raise RuntimeError(f'{filename}: official pinned download failed after 3 attempts ({type(last_error).__name__}); no replacement asset accepted') from None


def unpack_recipe(root: Path) -> Path:
    """Materialize only the digest-bound embedded source files, never execute."""
    import gzip
    import tarfile
    archive = base64.b85decode(RECIPE_PAYLOAD)
    require(hashlib.sha256(archive).hexdigest() == RECIPE_PAYLOAD_SHA256, 'Embedded recipe checksum mismatch')
    target = root / ('accepted-source-' + RECIPE_PAYLOAD_SHA256[:16])
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(archive))) as stream:
        members = stream.getmembers()
        require(len(members) == len(RECIPE_MANIFEST), 'Embedded recipe file count mismatch')
        require(len({row.name for row in members}) == len(members), 'Duplicate embedded recipe path')
        for row in members:
            require(row.isfile() and row.name in RECIPE_MANIFEST and not Path(row.name).is_absolute()
                    and '..' not in Path(row.name).parts, 'Unsafe embedded recipe member')
            data = stream.extractfile(row).read()
            require(hashlib.sha256(data).hexdigest() == RECIPE_MANIFEST[row.name], 'Embedded recipe file hash mismatch')
            path = target / row.name
            require(not path.is_symlink() and all(not p.is_symlink() for p in path.parents if p != root.parent), 'Symlink embedded source path')
            if path.exists():
                require(path.is_file() and path.read_bytes() == data, 'Cached recipe source changed')
            else:
                atomic_write(path, data)
    return target


def load_recipe_module(path: Path, name: str) -> Any:
    import importlib.util
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


@contextlib.contextmanager
def cache_lock(root: Path) -> Iterator[None]:
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.final-gist.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another final Gist benchmark owns this cache; wait or choose --cache-dir') from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class TreeMemorySampler:
    """Sample USS for the selected tree. Shared pages are deliberately excluded."""
    def __init__(self, pid: int):
        import psutil
        import threading
        self.psutil = psutil
        self.root = psutil.Process(pid)
        self.stop = threading.Event()
        self.samples = 0
        self.peak = 0
        self.incomplete = False
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _sample(self) -> None:
        try:
            require(self.root.is_running(), 'Memory root exited')
            processes = [self.root, *self.root.children(recursive=True)]
            value = 0
            for process in processes:
                try:
                    value += process.memory_full_info().uss
                except (self.psutil.NoSuchProcess, self.psutil.AccessDenied, AttributeError):
                    self.incomplete = True
            self.peak = max(self.peak, value)
            self.samples += 1
        except Exception:
            self.incomplete = True

    def _loop(self) -> None:
        while not self.stop.wait(0.2):
            self._sample()

    def __enter__(self) -> 'TreeMemorySampler':
        self._sample()
        self.thread.start()
        return self

    def __exit__(self, *unused: Any) -> None:
        self.stop.set()
        self.thread.join(timeout=5)
        require(not self.thread.is_alive(), 'Memory sampler failed to stop')
        self._sample()

    def record(self) -> dict[str, Any]:
        return {'sampled_peak_tree_uss_bytes': self.peak, 'samples': self.samples,
                'incomplete': self.incomplete, 'sample_interval_ms': 200,
                'scope': 'target process tree; request/response plus one-second pre/post CPU padding; shared pages excluded'}


def browser_launch_options(*, headed: bool, browser_path: Path | None = None) -> dict[str, Any]:
    # CDP Browser.getBrowserCommandLine requires explicit automation opt-in.
    # One shared factory covers every browser arm and untimed profile/control.
    options = {'headless': not headed, 'args': [BROWSER_OFF_FLAG, BROWSER_AUTOMATION_FLAG]}
    if browser_path:
        options['executable_path'] = str(browser_path)
    return options


def verify_browser_off(browser: Any, requested: dict[str, Any]) -> dict[str, Any]:
    require(requested.get('args') == [BROWSER_OFF_FLAG, BROWSER_AUTOMATION_FLAG] and
            not requested.get('ignore_default_args'), 'Browser launch must retain explicit V8 OFF and --enable-automation')
    session = browser.new_browser_cdp_session()
    try:
        actual = session.send('Browser.getBrowserCommandLine')['arguments']
        version = session.send('Browser.getVersion')
    finally:
        session.detach()
    js = [value for value in actual if value.startswith('--js-flags=')]
    require(js == [BROWSER_OFF_FLAG], 'Actual V8 flags are not the explicit revectorization-OFF configuration')
    require(BROWSER_AUTOMATION_FLAG in actual, 'CDP did not confirm actual --enable-automation')
    require(not any('hardwareConcurrency' in value or 'force-device-scale-factor' in value for value in actual), 'Unexpected browser host override')
    return {'product': version['product'], 'js_version': version['jsVersion'],
            'actual_js_flags': js, 'revectorize': False, 'actual_enable_automation': True,
            'all_arguments_recorded': False, 'flag_evidence': 'Browser.getBrowserCommandLine; only the relevant allowlisted flag is published'}


def finish_owned_tree(root: Any, observed: list[Any]) -> None:
    """After graceful close, reap residual owned processes before the next mode."""
    import psutil
    identities = {process for process in [root, *observed] if process is not None}
    alive = [process for process in identities if process.is_running() and process.status() != psutil.STATUS_ZOMBIE]
    _, alive = psutil.wait_procs(alive, timeout=10)
    for process in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.terminate()
    _, alive = psutil.wait_procs(alive, timeout=5)
    for process in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            process.kill()
    _, alive = psutil.wait_procs(alive, timeout=5)
    require(not [p for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE], 'Owned process survived cleanup; refusing another mode')


def final_modes(threads: int, baseline_only: bool = False) -> list[Mode]:
    modes = [Mode('native', f'Native CPU ×{threads}', threads, 'CPU EP; dynamic shape; per-session pools'),
             Mode('browser', 'Browser single-thread ×1', 1, 'CPU EP; dynamic shape; unshared SIMD WASM; V8 revectorize OFF')]
    if not baseline_only:
        modes += [Mode('browser_mt', f'Browser multithread ×{threads}', threads,
                       'original CPU EP; dynamic shape; global ORT intra=N/inter=1; spin default; V8 revectorize OFF'),
                  Mode('browser_optimized', f'Browser optimized configuration ×{threads}', threads,
                       'vocoder XNNPACK; query-fixed vocoder shape; ORT global1/inter1/spin off; XNN N; forced-loadsplat757 + in-placeLeaky514; V8 revectorize OFF',
                       fixed_shape=True, spin_off=True, execution_provider='XNNPACK')]
    return modes


def strict_pair(reference: tuple[bytes, bytes], value: tuple[bytes, bytes]) -> dict[str, Any]:
    check = waveform_comparison(*reference, *value)
    require(check['finite'] and check['pcm_exact'] and check['fp32_exact'], 'Matched pristine-XNN/accepted recipe FP32 or PCM differs; no accepted timings will be reported')
    return check


def collect_output(runner: Any) -> tuple[bytes, bytes]:
    runner.synthesize(save=True)
    return runner.wav.read_bytes(), runner.raw_wave()


def run_isolated_schedule(modes: list[Mode], seed: int, open_runner: Any,
                          log: list[dict[str, Any]], references: dict[str, tuple[bytes, bytes]], checkpoint: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    trials: list[dict[str, Any]] = []
    checks = []
    indexed = {mode.key: mode for mode in modes}
    try:
        for block in make_schedule(modes, seed):
            mode = indexed[block.mode]
            progress(f'{mode.label}: block {block.number}/{BLOCKS_PER_MODE}')
            with open_runner(mode) as (runner, pid, state):
                warm = collect_output(runner)
                if mode.key == 'browser_optimized':
                    checks.append({'block': block.number, 'stage': 'before', **strict_pair(references['pristine_xnn'], warm)})
                log.append({'event': 'block_warmup_excluded', 'mode': mode.key, 'block': block.number, 'audio_s': runner.duration})
                for repetition in range(1, TRIALS_PER_BLOCK + 1):
                    with TreeMemorySampler(pid) as memory:
                        elapsed, duration, cpu = ProcessCpuSampler(pid, mode.label).measure(runner.synthesize)
                    require(math.isfinite(elapsed) and elapsed > 0 and math.isfinite(duration) and duration > 0, 'Invalid synthesis measurement')
                    row = asdict(Trial(len(trials) + 1, mode.key, block.number, repetition, mode.threads,
                                       elapsed, duration, elapsed / duration, **asdict(cpu)))
                    row['memory'] = memory.record()
                    if isinstance(runner, BrowserRunner):
                        state = runner.page.evaluate("() => request({command:'resource-state'})")
                        row['memory'].update(wasm_linear_memory_bytes=state['wasm_bytes'], running_pthreads=state['pthreads'])
                        require(state['pthreads'] == mode.threads - 1 if mode.execution_provider == 'XNNPACK' else state['pthreads'] >= max(mode.threads - 1, 0), 'Inference pool ownership changed')
                    trials.append(row)
                    if checkpoint:
                        checkpoint(trials, checks, "incomplete", None)
                    progress(f'Trial {len(trials)}/{len(modes) * 15}: {mode.label}; synthesis {elapsed:.3f}s; CPU {cpu.cpu_percent:.1f}%')
                if mode.key == 'browser_optimized':
                    checks.append({'block': block.number, 'stage': 'after', **strict_pair(references['pristine_xnn'], collect_output(runner))})
            log.append({'event': 'block_completed_and_process_tree_closed', 'mode': mode.key, 'block': block.number, 'samples': TRIALS_PER_BLOCK})
    except BaseException as error:
        if checkpoint:
            checkpoint(trials, checks, "failed-incomplete", type(error).__name__)
        raise
    return trials, checks


def sanitize_build_result(value: dict[str, Any]) -> dict[str, Any]:
    # The accepted helper's public proof is already an explicit scalar schema.
    return value['proof']


def final_run_benchmark(args: argparse.Namespace) -> None:
    global ACTIVE_CACHE
    from playwright.sync_api import sync_playwright
    import psutil
    require(sys.platform.startswith('linux') and platform.machine().lower() in ('x86_64', 'amd64'),
            'The accepted recipe is pinned to Linux x86_64 (including x86_64 WSL). Use that platform; no download or measurement was started.')
    require(not args.experiments and not args.backend_experiments, 'Optimization exploration is closed; run without experiment flags for the four confirmed modes')
    require(1 <= args.threads <= 1024, '--threads must be between 1 and 1024')
    require(args.baseline_only or args.style_id == 302, 'The accepted configuration targets streaming Style302; use --style-id 302')
    require(not args.threaded_ort_url and not args.threaded_ort_archive and not args.xnnpack_ort_url and not args.xnnpack_ort_archive,
            'The final comparison uses digest-pinned runtime releases; external runtime overrides are not accepted')
    root = args.cache_dir.expanduser().resolve()
    ACTIVE_CACHE = root
    with cache_lock(root):
        query = json.loads(args.audio_query.read_text('utf-8')) if args.audio_query else prepared_query(args.target_seconds)
        require(isinstance(query, dict) and type(query.get('outputSamplingRate')) is int and query['outputSamplingRate'] > 0,
                'AudioQuery requires a positive outputSamplingRate')
        query_bytes = json.dumps(query, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        modes = final_modes(args.threads, args.baseline_only)
        progress('Preparing pinned CORE, toolchains, and the confirmed browser recipe')
        env = ensure_toolchains(root)
        core_archive = cached_download(f'https://github.com/yamachu/voicevox_core/archive/{CORE_COMMIT}.tar.gz', root,
                                       f'core-{CORE_COMMIT}.tar.gz', expected_sha256=CORE_SOURCE_SHA256)
        core_tree = unpack_cached(core_archive, root / ('gist-source-' + CORE_COMMIT))
        source = core_tree / ('voicevox_core-' + CORE_COMMIT)
        native_name, _ = native_platform()
        runtime_specs = {'native': f'{BASE_RELEASE}/onnxruntime-{native_name}-{ORT_VERSION}.tgz',
                         'browser': f'{BASE_RELEASE}/onnxruntime-wasm-static-{ORT_VERSION}.tgz'}
        if not args.baseline_only:
            runtime_specs['browser_mt'] = THREADED_ORT_URL
        runtime_archives, runtimes, binaries = {}, {}, {}
        for key, url in runtime_specs.items():
            filename = url.rsplit('/', 1)[1]
            require(filename in ORT_ARCHIVE_SHA256, 'Unpinned runtime asset')
            archive = cached_download(url, root, filename, expected_sha256=ORT_ARCHIVE_SHA256[filename])
            extracted = unpack_cached(archive, root / (key + '-runtime-' + sha256_file(archive)[:16]))
            if key == 'browser_mt':
                verify_threaded_runtime(extracted)
            runtimes[key] = find_one(extracted, f'libonnxruntime.so.{ORT_VERSION}' if key == 'native' else 'libonnxruntime_webassembly.a')
            runtime_archives[key] = sha256_file(archive)
            binaries[key] = build_runner(source, runtimes[key], root, env,
                                          threaded=None if key == 'native' else key == 'browser_mt',
                                          threads=args.threads if key != 'browser' else 1)
        accepted = None
        if not args.baseline_only:
            bundle = unpack_recipe(root)
            helper = load_recipe_module(bundle / 'accepted_builder.py', '_gist_accepted_builder')
            accepted = helper.prepare(bundle, root, source, env, args.threads, host=sys.modules[__name__])
            binaries['pristine_xnn'] = Path(accepted['R']['binary'])
            binaries['browser_optimized'] = Path(accepted['T']['binary'])
        model = root / ('sample-' + CORE_COMMIT + '.vvm')
        if not model.exists():
            prepare_model(source, model)
        require(sha256_file(model) == MODEL_SHA256, 'Pinned model package changed')
        environment = environment_info()
        environment['rust'] = checked_run([str(root / 'cargo/bin/rustc'), f'+{RUST_VERSION}', '--version'], env=env)
        environment['emscripten'] = EMSDK_VERSION
        environment.update({'script_sha256': sha256_file(Path(__file__)), 'gist_revision': GIST_REVISION,
                            'original_gist_sha256': GIST_SOURCE_SHA256, 'accepted_recipe_source_sha256': ACCEPTED_SOURCE_SHA256,
                            'accepted_recipe_source_commit': ACCEPTED_SOURCE_COMMIT, 'embedded_recipe_sha256': RECIPE_PAYLOAD_SHA256,
                            'sample_vvm_sha256': MODEL_SHA256, 'audio_query_sha256': hashlib.sha256(query_bytes).hexdigest(),
                            'runtime_archive_sha256': runtime_archives, 'psutil': psutil.__version__,
                            'precision': 'FP32; strict SIMD; fno-fast-math; ffp-contract=off; no relaxed SIMD/quantization',
                            'cpu_metric': 'target tree user+system seconds / synchronized request-response observation seconds; synthesis latency has its own internal timer',
                            'cpu_padding_seconds': CPU_PADDING_S, 'cpu_sample_interval_ms': CPU_SAMPLE_INTERVAL_S * 1000,
                            'cpu_browser_scope': 'one mode/process tree alive per block; Python and Playwright Node excluded',
                            'memory_metric': 'sampled tree USS peak, shared pages excluded; not total RAM or a hard peak; per-trial plus pre/post padding',
                            'wasm_memory_metric': 'linear memory byteLength after request; allocated high-water capacity, not live tensor bytes',
                            'browser_headless': not args.headed, 'browser_instances': {},
                            'actual_actions': actions_provenance(),
                            'core_variants': {mode.key: asdict(mode) for mode in modes},
                            'block_order': [block.mode for block in make_schedule(modes, args.seed)],
                            'build_artifacts': {key: {'binary_sha256': sha256_file(path),
                                **({'wasm_sha256': sha256_file(path.with_suffix('.wasm'))} if key != 'native' else {})}
                                                for key, path in binaries.items()}})
        if accepted:
            environment['accepted_build_proof'] = sanitize_build_result(accepted)
        previous_browser_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(root / 'playwright')
        try:
            if not args.browser_path:
                checked_run([sys.executable, '-m', 'playwright', 'install', 'chromium'], env=dict(os.environ))
            with tempfile.TemporaryDirectory(prefix='voicevox-final-') as temporary:
                work = Path(temporary)
                shutil.copyfile(model, work / 'sample.vvm')
                atomic_write(work / 'query.json', query_bytes)
                atomic_write(work / 'index.html', BROWSER_PAGE.encode())
                atomic_write(work / 'worker.js', BROWSER_WORKER.encode())
                for key, binary in binaries.items():
                    if key == 'native':
                        continue
                    (work / key).mkdir()
                    for path in binary.parent.glob('voicevox_benchmark.*'):
                        if path.suffix in ('.js', '.wasm'):
                            shutil.copyfile(path, work / key / path.name)
                with serve_assets(work) as url, sync_playwright() as playwright:
                    options = browser_launch_options(headed=args.headed, browser_path=args.browser_path)
                    active = {'mode': None}
                    @contextlib.contextmanager
                    def open_runner(mode: Mode, *, profile: bool = False) -> Iterator[Any]:
                        require(active['mode'] is None, 'Another benchmark process tree is still alive')
                        active['mode'] = mode.key
                        runner = browser = root_process = None
                        observed = []
                        try:
                            if mode.key == 'native':
                                runner = NativeRunner(binaries['native'], runtimes['native'], model, work / 'query.json',
                                                      mode.threads, args.style_id, work / 'native.wav')
                                pid = runner.process.pid
                                state = {}
                            else:
                                browser = playwright.chromium.launch(**options)
                                pid = chromium_process_id(browser)
                                root_process = psutil.Process(pid)
                                state = verify_browser_off(browser, options)
                                runner = BrowserRunner(browser, url, f'/{mode.key}/voicevox_benchmark.js', mode.threads,
                                                       mode.key != 'browser', args.style_id, work / (mode.key + '.wav'),
                                                       fixed_shape=mode.fixed_shape, spin_off=mode.spin_off,
                                                       xnn_threads=mode.threads if mode.execution_provider == 'XNNPACK' else 0, profile=profile)
                                state.update(runner.page.evaluate('() => ({hardware_concurrency:navigator.hardwareConcurrency,cross_origin_isolated:crossOriginIsolated})'))
                                require(state['cross_origin_isolated'], 'Browser lacks cross-origin isolation')
                                if mode.execution_provider == 'XNNPACK':
                                    dispatch = runner.page.evaluate("data => request(data)", {'command': 'dispatch', 'expected': 'auto' if mode.key == 'pristine_xnn' else 'loadsplat'})
                                    require(dispatch['verified'] and dispatch['worker_hardware_concurrency'] == state['hardware_concurrency'], 'Actual worker dispatcher differs from page context')
                                    state['dispatch'] = dispatch
                                state.update({'inference_threads': mode.threads, 'fixed_length': runner.fixed_length,
                                              'fixed_matches': runner.fixed_matches, 'xnn_threads': runner.xnn_threads,
                                              'xnn_sessions': runner.xnn_sessions, 'spin_off': runner.spin_off,
                                              'pthreads_created': runner.pthreads_created})
                                environment['browser_instances'].setdefault(mode.key, []).append(state)
                            root_process = root_process or psutil.Process(pid)
                            yield runner, pid, state
                        finally:
                            if root_process:
                                with contextlib.suppress(psutil.NoSuchProcess):
                                    observed = root_process.children(recursive=True)
                            if runner:
                                with contextlib.suppress(Exception):
                                    runner.close()
                            if browser:
                                with contextlib.suppress(Exception):
                                    browser.close()
                            finish_owned_tree(root_process, observed)
                            active['mode'] = None
                    log = []
                    references: dict[str, tuple[bytes, bytes]] = {}
                    repeat_checks = {}
                    pristine_mode = Mode('pristine_xnn', 'Untimed matched pristine XNN control', args.threads,
                                         'untimed correctness only', fixed_shape=True, spin_off=True, execution_provider='XNNPACK')
                    verification_modes = ([pristine_mode] if accepted else []) + modes
                    for mode in verification_modes:
                        progress(f'Untimed output and repeatability checks: {mode.label}')
                        with open_runner(mode) as (runner, _, state):
                            references[mode.key] = collect_output(runner)
                            repeated = collect_output(runner)
                            repeat_checks[mode.key] = waveform_comparison(*references[mode.key], *repeated)
                            if mode.key in ('pristine_xnn', 'browser_optimized'):
                                strict_pair(references[mode.key], repeated)
                            if mode.key == 'browser_optimized':
                                strict_pair(references['pristine_xnn'], references[mode.key])
                        log.append({'event': 'untimed_output_check_closed', 'mode': mode.key})
                    if accepted:
                        for mode in (pristine_mode, modes[-1]):
                            with open_runner(mode, profile=True) as (runner, _, _):
                                runner.synthesize(save=True)
                                profile = runner.finish_profile()
                                require(profile['verified'], 'Untimed XNNPACK provider activation not confirmed')
                                environment.setdefault('untimed_provider_profiles', {})[mode.key] = profile
                    reference_key = 'browser_mt' if accepted else 'browser'
                    output_checks = {'reference_mode': reference_key,
                                     'reference_deterministic': repeat_checks[reference_key]['pcm_exact'] and repeat_checks[reference_key]['fp32_exact'],
                                     'reference_repeat': repeat_checks[reference_key],
                                     'modes': {mode.key: waveform_comparison(*references[reference_key], *references[mode.key]) for mode in modes},
                                     'interpretation': 'Cross-backend errors are descriptive. Strict accepted-recipe exactness uses the separate same-provider/same-settings pristine XNN reference.'}
                    matched = None
                    if accepted:
                        profiles = environment['untimed_provider_profiles']
                        require(profiles['pristine_xnn']['provider_assignment_sha256'] == profiles['browser_optimized']['provider_assignment_sha256'],
                                'Matched R/T provider assignments differ')
                        matched = {'reference': 'pristine_xnn', 'candidate': 'browser_optimized',
                                   'scope': 'same query/model/provider/N/fixed shape/spin/V8 OFF; only accepted native recipe differs',
                                   'same_run_exact': strict_pair(references['pristine_xnn'], references['browser_optimized']),
                                   'reference_repeat': repeat_checks['pristine_xnn'], 'candidate_repeat': repeat_checks['browser_optimized']}
                    progress(f'Starting {len(modes) * 15} measured trials with isolated per-block process lifetimes')
                    def checkpoint(rows, checks, status, exception_class):
                        partial = {'schema': 'voicevox-final-gist-partial-v1', 'evidence_kind': 'incomplete-actual-measurements',
                                   'status': status, 'complete': False, 'expected_trials': len(modes) * 15,
                                   'completed_trials': len(rows), 'trials': rows, 'matched_block_checks': checks,
                                   'failure_exception_class': exception_class, 'script_sha256': sha256_file(Path(__file__)),
                                   'seed': args.seed, 'style_id': args.style_id, 'modes': [asdict(mode) for mode in modes],
                                   'environment': environment, 'log': log,
                                   'warning': 'Incomplete checkpoint only. No completed comparison or improvement claim.'}
                        atomic_write(args.output.with_suffix('.partial.json'), json.dumps(partial, ensure_ascii=False, indent=2, allow_nan=False).encode())
                    trials, block_checks = run_isolated_schedule(modes, args.seed, open_runner, log, references, checkpoint)
                    if matched:
                        matched['per_timed_block_checks'] = block_checks
                    audio_s = wav_duration(references[reference_key][0])
                    result = {'schema_version': SCHEMA_VERSION, 'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'evidence_kind': 'actual-measurement', 'modes': [asdict(mode) for mode in modes], 'trials': trials,
                              'environment': environment, 'audio_s': audio_s, 'style_id': args.style_id, 'seed': args.seed,
                              'log': log, 'output_checks': output_checks, 'matched_recipe_checks': matched,
                              'schedule_method': 'seeded independent shuffle in each round',
                              'schedule': [asdict(block) for block in make_schedule(modes, args.seed)],
                              'notes': ['最適化モードはXNNPACK・固定shape・pool設定・2か所のカーネル変更を含む構成全体との比較。元のCPU EPブラウザMTとの差を514/757だけの効果とは解釈しない。',
                                        '同一provider・同一設定のpristine XNNと最適化版のPCM/FP32完全一致を測定外で検証。第5の速度測定モードは追加していない。',
                                        'navigator.hardwareConcurrencyを上書きしない。pristine XNNは実際の値が4を超えると元からloadsplatを選ぶ。',
                                        '別の研究実行の27.5%改善はこのレポートの測定値ではない。',
                                        'USSは共有メモリを除くサンプル値。WASM容量は確保済み線形メモリの高水位で、生存テンソル量やピークRAMの保証ではない。',
                                        '全モードV8 revectorize OFF。各ブロックの初期化・ウォームアップ・正当性チェック・プロセス終了は測定外。',
                                        '可視化は性能と数値比較に限定。スペクトログラムは生成せず、音声・テンソル・モデルはレポートに含めない。']}
                    save_measurements_and_report(result, args.output)
                    args.output.with_suffix('.partial.json').unlink(missing_ok=True)
                    progress(f'Report complete: {args.output.name}; raw scalar JSON: {args.output.with_suffix(".json").name}')
        finally:
            if previous_browser_path is None:
                os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
            else:
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = previous_browser_path


def actions_provenance() -> dict[str, Any]:
    """Report genuine current run metadata; never invent a campaign identity."""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return {'github_actions': False}
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    sha = os.environ.get('GITHUB_SHA', '')
    ref = os.environ.get('GITHUB_REF', '')
    run = os.environ.get('GITHUB_RUN_ID', '')
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '')
    require(re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) is not None and
            re.fullmatch('[0-9a-f]{40}', sha) is not None and run.isdecimal() and attempt.isdecimal(), 'Invalid Actions provenance')
    return {'github_actions': True, 'repository': repository, 'commit': sha, 'ref': ref,
            'run_id': run, 'attempt': int(attempt), 'run_url': f'https://github.com/{repository}/actions/runs/{run}'}


_original_render_report = render_report


def render_report(result: dict[str, Any], target: Path) -> None:
    _original_render_report(result, target)
    if result.get('evidence_kind') not in ('actual-measurement', 'synthetic-test-only'):
        return
    kind = result['evidence_kind']
    label = '実測結果' if kind == 'actual-measurement' else '合成テスト専用・実測ではない'
    config = '<h2>構成の違い</h2><p>最適化構成と元のCPU EPブラウザMTの速度差は、下記すべての設定差を含みます。</p><ul>'
    for mode in result['modes']:
        config += f'<li><b>{html.escape(mode["label"])}</b>: {html.escape(mode["backend"])}</li>'
    config += '</ul>'
    memory_rows = []
    for mode in result['modes']:
        rows = [row for row in result['trials'] if row['mode'] == mode['key']]
        memories = [row['memory'] for row in rows if 'memory' in row]
        if memories:
            complete = [row for row in memories if not row['incomplete']]
            observed = max((row['sampled_peak_tree_uss_bytes'] for row in complete), default=None)
            wasm = max((row.get('wasm_linear_memory_bytes', 0) for row in memories), default=0)
            memory_rows.append('<tr>' + ''.join(f'<td>{html.escape(str(value))}</td>' for value in [mode['label'],
                f'{observed/2**20:.1f}' if observed is not None else '不完全',
                f'{wasm/2**20:.1f}' if wasm else '該当なし', f'{len(complete)}/{len(memories)}']) + '</tr>')
    memory = ('<h2>メモリ観測</h2><table><tr><th>モード</th><th>観測USS最大MiB</th><th>WASM容量最大MiB</th><th>完全な試行</th></tr>' + ''.join(memory_rows) +
              '</table><p>USSは対象process treeの非共有ページのみを200msごとに採取。短いピークを取り逃がすことがあります。WASMは各試行後の確保容量で、共有ページを含みます。両者を足し合わせません。</p>') if memory_rows else ''
    matched = result.get('matched_recipe_checks')
    proof = ('<details><summary>accepted recipe の同条件PCM/FP32完全一致</summary><p>pristine XNNを用いた測定外の対照。元のCPU EPブラウザMTとは別です。測定に使った最適化runnerの各ブロック前後も確認。</p><pre>' + html.escape(json.dumps(matched, ensure_ascii=False, indent=2)) + '</pre></details>') if matched else ''
    document = target.read_text('utf-8')
    document = document.replace('<h1>VOICEVOX CORE benchmark</h1>', f'<h1>VOICEVOX CORE benchmark</h1><p><b>{label}</b></p>' + config, 1)
    document = document.replace('<h2>測定条件</h2>', memory + proof + '<h2>測定条件</h2>', 1)
    atomic_write(target, document.encode())


run_benchmark = final_run_benchmark


def final_plan(args: argparse.Namespace) -> dict[str, Any]:
    return {'schema': 'voicevox-final-gist-plan-v1', 'execution_performed': False,
            'supported_platform': 'Linux x86_64, including x86_64 WSL',
            'modes': [asdict(mode) for mode in final_modes(args.threads, args.baseline_only)],
            'threads': args.threads, 'blocks_per_mode': 3, 'trials_per_block': 5,
            'timed_trials': len(final_modes(args.threads, args.baseline_only)) * 15,
            'seed': args.seed, 'style_id': args.style_id, 'builtin_target_seconds': args.target_seconds,
            'output': 'standalone HTML and raw scalar JSON', 'v8_revectorize': False,
            'correctness': 'untimed matched pristine XNN versus accepted configuration, same N/settings/query/model; exact PCM and pre-PCM FP32 before timing and each optimized block',
            'comparison_scope': 'original CPU-provider MT versus complete optimized configuration; not an isolated 514/757 speedup',
            'recipe_source_sha256': ACCEPTED_SOURCE_SHA256, 'embedded_source_sha256': RECIPE_PAYLOAD_SHA256,
            'embedded_files': len(RECIPE_MANIFEST), 'build_command_timeout_seconds': BUILD_TIMEOUT_S,
            'process_lifecycle': 'one isolated native/browser process tree per block; cleanup before next block',
            'first_run': 'downloads digest-pinned sources/runtime/model and official pinned toolchains/Chromium; compiles native/ST/MT and accepted/pristine-XNN correctness modules; may take tens of minutes and several GB',
            'public_output_excludes': ['binaries', 'models', 'audio', 'tensors', 'process argv', 'credentials'],
            'source_claim_limit': 'plan and self-test are not evidence of actual synthesis or performance'}


_original_validate_results = validate_results


def validate_results(result: dict[str, Any]) -> None:
    _original_validate_results(result)
    if result.get('evidence_kind') not in ('actual-measurement', 'synthetic-test-only'):
        return
    keys = {mode['key'] for mode in result['modes']}
    require(keys in ({'native', 'browser'}, {'native', 'browser', 'browser_mt', 'browser_optimized'}), 'Final mode set changed')
    if 'browser_optimized' in keys:
        checks = result.get('matched_recipe_checks')
        require(isinstance(checks, dict) and checks['reference'] == 'pristine_xnn' and checks['candidate'] == 'browser_optimized', 'Missing matched accepted recipe proof')
        for check in [checks['same_run_exact'], checks['reference_repeat'], checks['candidate_repeat'], *checks['per_timed_block_checks']]:
            require(check['finite'] is True and check['pcm_exact'] is True and check['fp32_exact'] is True, 'Accepted exactness evidence failed')
        require([(check['block'], check['stage']) for check in checks['per_timed_block_checks']] ==
                [(block, stage) for block in range(1, 4) for stage in ('before', 'after')], 'Incomplete accepted block output checks')
    for row in result['trials']:
        memory = row.get('memory')
        require(isinstance(memory, dict) and type(memory['incomplete']) is bool and type(memory['samples']) is int and memory['samples'] > 0,
                'Invalid memory coverage')
        require(type(memory['sampled_peak_tree_uss_bytes']) is int and memory['sampled_peak_tree_uss_bytes'] >= 0, 'Invalid memory sample')
    for instances in result['environment']['browser_instances'].values():
        for instance in instances:
            require(instance['actual_js_flags'] == [BROWSER_OFF_FLAG] and instance['revectorize'] is False and
                    instance['actual_enable_automation'] is True and
                    type(instance['hardware_concurrency']) is int and instance['hardware_concurrency'] > 0, 'Invalid actual browser setting evidence')


_original_self_test = self_test


def self_test() -> None:
    """Entire standalone-source check; fabricated signals are never run evidence."""
    global progress
    saved_progress = progress
    try:
        progress = lambda message: None
        with contextlib.redirect_stdout(io.StringIO()):
            _original_self_test()
        with tempfile.TemporaryDirectory(prefix='voicevox-source-self-test-') as temporary:
            bundle = unpack_recipe(Path(temporary))
            helper = load_recipe_module(bundle / 'accepted_builder.py', '_gist_self_test_builder')
            binding = helper.verify_binding(bundle)
            require(binding['selected_source_file_count'] == 15, 'Wrong accepted recipe source count')
            with helper.selected_modules(bundle) as (_, base, *_):
                helper.verify_common_gist(base, sys.modules[__name__])
            modes = final_modes(2)
            trace = (CpuInterval(-1, 0, 0, 0, 1, False, 'pre'),
                     CpuInterval(0, 0.1, 0.05, 50, 1, False, 'action'),
                     CpuInterval(0.1, 1.1, 0, 0, 1, False, 'post'))
            measured = CpuMeasurement(0.05, 0.1, 0.5, 50, 4, 1, False, trace)
            rows = run_schedule(modes, 1729, lambda mode: (0.1, 10.0, measured), [])
            require(len(rows) == 60 and all(sum(row.mode == mode.key for row in rows) == 15 for mode in modes),
                    'Four-mode synthetic schedule failed')
            import struct
            output = io.BytesIO()
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(struct.pack('<hh', 0, 1))
            reference = output.getvalue(), struct.pack('<ff', 0.0, 0.1)
            strict_pair(reference, reference)
            try:
                strict_pair(reference, (reference[0], struct.pack('<ff', 0.0, 0.2)))
            except RuntimeError:
                pass
            else:
                raise RuntimeError('Changed FP32 synthetic output was accepted')
    finally:
        progress = saved_progress
    print('Self-test passed: original report/CPU tests, 16 embedded source hashes, canonical helper identity, four-mode schedule, and strict-pair rejection. Synthetic tests only; no downloads, build, browser, or model execution.')


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Interrupted. A completed report was not written.")
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(f"Error: {error}") from error